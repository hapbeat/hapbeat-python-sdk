"""Local IPv4 subnet discovery — the input to subnet-directed broadcast.

Why this exists
---------------
``255.255.255.255`` (limited broadcast) leaves a multi-homed host through the
*single* interface with the lowest routing metric. Installing Hyper-V, WSL2 or
Docker adds a virtual switch that is permanently "connected" — no Ethernet
cable required — and Windows may well rank it above Wi-Fi. When that happens
the packet never reaches the network the Hapbeat is on and the SDK discovers
nothing at all. A subnet-directed address (``192.168.0.255``) is instead
resolved through the directly-connected route for that subnet, so the metric
never enters into it.

Computing that address needs each interface's real netmask, and the standard
library has no portable way to ask for one. So this module calls the platform
API directly through :mod:`ctypes`:

- **Windows** — ``GetAdaptersAddresses`` (iphlpapi), whose per-address
  ``OnLinkPrefixLength`` is the prefix we want.
- **Linux / macOS / BSD** — ``getifaddrs`` (libc), which hands back ``ifa_netmask``
  alongside each address.

Everything here is best-effort by design: any platform this cannot query
returns an empty list, and the caller falls back to the limited broadcast it
has always used. Struct layouts follow the `ifaddr <https://pypi.org/project/ifaddr/>`_
package (MIT), which is the reference implementation for this on both platforms.

The SDK has no runtime dependencies, which is why the layouts are restated here
instead of taking that package as a dependency.
"""

from __future__ import annotations

import ctypes
import logging
import platform
import socket
from typing import List, Tuple

logger = logging.getLogger("hapbeat")

# (address, prefix length), e.g. ("192.168.0.205", 24)
Subnet = Tuple[str, int]

_IS_WINDOWS = platform.system() == "Windows"
_IS_BSD = platform.system() == "Darwin" or "BSD" in platform.system()


# ── sockaddr (BSD keeps a length byte where the others start the family) ──
if _IS_BSD:

    class _sockaddr(ctypes.Structure):
        _fields_ = [
            ("sa_len", ctypes.c_uint8),
            ("sa_family", ctypes.c_uint8),
            ("sa_data", ctypes.c_uint8 * 14),
        ]

    class _sockaddr_in(ctypes.Structure):
        _fields_ = [
            ("sin_len", ctypes.c_uint8),
            ("sin_family", ctypes.c_uint8),
            ("sin_port", ctypes.c_uint16),
            ("sin_addr", ctypes.c_uint8 * 4),
            ("sin_zero", ctypes.c_uint8 * 8),
        ]

else:

    class _sockaddr(ctypes.Structure):  # type: ignore[no-redef]
        _fields_ = [
            ("sa_family", ctypes.c_uint16),
            ("sa_data", ctypes.c_uint8 * 14),
        ]

    class _sockaddr_in(ctypes.Structure):  # type: ignore[no-redef]
        _fields_ = [
            ("sin_family", ctypes.c_uint16),
            ("sin_port", ctypes.c_uint16),
            ("sin_addr", ctypes.c_uint8 * 4),
            ("sin_zero", ctypes.c_uint8 * 8),
        ]


def _ipv4_of(sockaddr_ptr) -> str:
    """Dotted-quad for an ``AF_INET`` sockaddr, or "" for anything else."""
    if not sockaddr_ptr or sockaddr_ptr[0].sa_family != socket.AF_INET:
        return ""
    sin = ctypes.cast(sockaddr_ptr, ctypes.POINTER(_sockaddr_in))
    return ".".join(str(b) for b in sin[0].sin_addr)


def _prefix_of_mask(mask: str) -> int:
    """Prefix length for a dotted-quad mask. -1 if it is not contiguous.

    A non-contiguous mask is not something a modern stack hands out, but
    accepting one would produce a broadcast address that reaches the wrong
    hosts, so it is rejected rather than guessed at.
    """
    try:
        parts = [int(p) for p in mask.split(".")]
    except ValueError:
        return -1
    if len(parts) != 4 or any(p < 0 or p > 255 for p in parts):
        return -1
    value = (parts[0] << 24) | (parts[1] << 16) | (parts[2] << 8) | parts[3]
    if value == 0:
        return -1
    # Contiguous means the complement is all-ones in the low bits.
    inverted = ~value & 0xFFFFFFFF
    if inverted & (inverted + 1):
        return -1
    return 32 - inverted.bit_length()


# ── Windows: GetAdaptersAddresses ────────────────────────────────────
class _SOCKET_ADDRESS(ctypes.Structure):
    _fields_ = [
        ("lpSockaddr", ctypes.POINTER(_sockaddr)),
        ("iSockaddrLength", ctypes.c_int),
    ]


class _IP_ADAPTER_UNICAST_ADDRESS(ctypes.Structure):
    pass


_IP_ADAPTER_UNICAST_ADDRESS._fields_ = [
    ("Length", ctypes.c_ulong),
    ("Flags", ctypes.c_uint32),
    ("Next", ctypes.POINTER(_IP_ADAPTER_UNICAST_ADDRESS)),
    ("Address", _SOCKET_ADDRESS),
    ("PrefixOrigin", ctypes.c_uint),
    ("SuffixOrigin", ctypes.c_uint),
    ("DadState", ctypes.c_uint),
    ("ValidLifetime", ctypes.c_ulong),
    ("PreferredLifetime", ctypes.c_ulong),
    ("LeaseLifetime", ctypes.c_ulong),
    ("OnLinkPrefixLength", ctypes.c_uint8),
]


class _IP_ADAPTER_ADDRESSES(ctypes.Structure):
    pass


_IP_ADAPTER_ADDRESSES._fields_ = [
    ("Length", ctypes.c_ulong),
    ("IfIndex", ctypes.c_uint32),
    ("Next", ctypes.POINTER(_IP_ADAPTER_ADDRESSES)),
    ("AdapterName", ctypes.c_char_p),
    ("FirstUnicastAddress", ctypes.POINTER(_IP_ADAPTER_UNICAST_ADDRESS)),
    ("FirstAnycastAddress", ctypes.c_void_p),
    ("FirstMulticastAddress", ctypes.c_void_p),
    ("FirstDnsServerAddress", ctypes.c_void_p),
    ("DnsSuffix", ctypes.c_wchar_p),
    ("Description", ctypes.c_wchar_p),
    ("FriendlyName", ctypes.c_wchar_p),
]

_ERROR_BUFFER_OVERFLOW = 111


def _windows_subnets() -> List[Subnet]:
    iphlpapi = ctypes.windll.Iphlpapi  # type: ignore[attr-defined]

    size = ctypes.c_ulong(15 * 1024)
    buffer = ctypes.create_string_buffer(size.value)
    retval = _ERROR_BUFFER_OVERFLOW
    # The documented protocol: on overflow the call writes the size it needs.
    # Bounded so a pathological answer cannot spin here.
    for _ in range(5):
        buffer = ctypes.create_string_buffer(size.value)
        retval = iphlpapi.GetAdaptersAddresses(
            ctypes.c_ulong(socket.AF_INET),
            ctypes.c_ulong(0),
            None,
            ctypes.byref(buffer),
            ctypes.byref(size),
        )
        if retval != _ERROR_BUFFER_OVERFLOW:
            break
    if retval != 0:
        raise OSError(f"GetAdaptersAddresses failed ({retval})")

    out: List[Subnet] = []
    adapter = _IP_ADAPTER_ADDRESSES.from_buffer(buffer)
    while True:
        # Adapters with no configured IPv4 address are simply absent from an
        # AF_INET enumeration, so no operational-status filtering is needed
        # here; 127/8 is dropped by the caller and duplicates are merged there.
        address_ptr = adapter.FirstUnicastAddress
        while address_ptr:
            address = address_ptr[0]
            ip = _ipv4_of(address.Address.lpSockaddr)
            prefix = address.OnLinkPrefixLength
            if ip and 0 < prefix <= 32:
                out.append((ip, prefix))
            address_ptr = address.Next
        if not adapter.Next:
            break
        adapter = adapter.Next[0]
    return out


# ── POSIX: getifaddrs ────────────────────────────────────────────────
class _ifaddrs(ctypes.Structure):
    pass


_ifaddrs._fields_ = [
    ("ifa_next", ctypes.POINTER(_ifaddrs)),
    ("ifa_name", ctypes.c_char_p),
    ("ifa_flags", ctypes.c_uint),
    ("ifa_addr", ctypes.POINTER(_sockaddr)),
    ("ifa_netmask", ctypes.POINTER(_sockaddr)),
]


def _posix_subnets() -> List[Subnet]:
    import ctypes.util

    libc_name = ctypes.util.find_library("c")
    if libc_name is None:
        raise OSError("libc not found")
    libc = ctypes.CDLL(libc_name, use_errno=True)

    head = ctypes.POINTER(_ifaddrs)()
    if libc.getifaddrs(ctypes.byref(head)) != 0:
        errno = ctypes.get_errno()
        raise OSError(errno, "getifaddrs failed")

    out: List[Subnet] = []
    try:
        node = head
        while node:
            entry = node[0]
            # getifaddrs also reports link-layer entries (AF_PACKET / AF_LINK)
            # and IPv6; _ipv4_of returns "" for those.
            ip = _ipv4_of(entry.ifa_addr)
            if ip and entry.ifa_netmask:
                # On some systems the netmask sockaddr carries family 0.
                # It sits on an IPv4 address, so read it as one.
                if entry.ifa_netmask[0].sa_family == 0:
                    entry.ifa_netmask[0].sa_family = socket.AF_INET
                mask = _ipv4_of(entry.ifa_netmask)
                prefix = _prefix_of_mask(mask) if mask else -1
                if prefix > 0:
                    out.append((ip, prefix))
            node = entry.ifa_next
    finally:
        libc.freeifaddrs(head)
    return out


def local_ipv4_subnets() -> List[Subnet]:
    """Every local IPv4 address with its prefix length, loopback excluded.

    Returns an empty list on any platform or failure this cannot handle —
    callers treat that as "use the limited broadcast", which is the behaviour
    that predates this module.
    """
    try:
        found = _windows_subnets() if _IS_WINDOWS else _posix_subnets()
    except Exception as exc:  # noqa: BLE001 — never let discovery setup fail a session
        logger.debug("could not enumerate local subnets (%s); "
                     "limited broadcast only", exc)
        return []

    return [(ip, prefix) for ip, prefix in found if not ip.startswith("127.")]
