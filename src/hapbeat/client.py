"""UDP transport for the Hapbeat SDK.

Owns one datagram socket configured for broadcast. Sends packets either to a
known device (unicast) or to the LAN broadcast address, and runs a background
thread that collects PONG replies so device discovery works. Which of the two
a given packet takes is decided one layer up, in :class:`hapbeat.Hapbeat`,
which is the layer that tracks devices.

Design note — port binding:
    The standard transport is Wi-Fi UDP broadcast (see workspace CLAUDE.md).
    To receive PONG replies we bind the well-known port 7700. If that port is
    already taken (e.g. hapbeat-helper is running on the same machine) we fall
    back to an ephemeral bind: sends still work and PING replies still arrive
    at the ephemeral source port; only async broadcast pushes on 7700 are
    missed, which level-1 does not rely on.

Design note — broadcast destinations:
    Discovery fans out across every local subnet (see :class:`BroadcastRoute`),
    while playback goes to exactly one destination. Keeping those two apart is
    not a detail: firmware older than v0.3.0 does not de-duplicate PLAY/STOP by
    sequence number, so a device reachable on two destinations would fire the
    haptic twice. PING and CONNECT_STATUS are idempotent and safe to duplicate.
"""

from __future__ import annotations

import logging
import socket
import threading
import time
from typing import Callable, Iterable, List, Optional

from . import protocol
from .netif import local_ipv4_subnets

logger = logging.getLogger("hapbeat")

DEFAULT_PORT = 7700
DEFAULT_BROADCAST = "255.255.255.255"

PongCallback = Callable[[dict, str], None]
# (command, seq, payload, (ip, port)) for CAPS and ERROR datagrams
PacketCallback = Callable[[int, int, bytes, tuple], None]

# Stand-in "destination" for the case where a whole fan-out failed, so that
# outage gets the same once-per-outage treatment as a single address.
_ALL_DESTINATIONS = "all broadcast destinations"


def _to_uint32(dotted: str) -> int:
    a, b, c, d = (int(p) for p in dotted.split("."))
    return (a << 24) | (b << 16) | (c << 8) | d


def _to_dotted(value: int) -> str:
    return "{}.{}.{}.{}".format(
        (value >> 24) & 0xFF, (value >> 16) & 0xFF, (value >> 8) & 0xFF, value & 0xFF
    )


class BroadcastRoute:
    """One address the SDK can broadcast to.

    ``255.255.255.255`` leaves a multi-homed host through the single interface
    with the lowest metric. On a machine running Hyper-V / WSL2 / Docker that is
    often an always-up virtual switch with no Hapbeat behind it — and no
    Ethernet cable is needed for that to happen, which is why the symptom looks
    nothing like "multi-homed". A subnet-directed address (``192.168.0.255``)
    resolves through the directly-connected route for its subnet instead, so the
    metric never applies. That is also why hapbeat-helper kept working on such a
    host: it finds devices over mDNS and unicasts, which resolves the same way.
    """

    __slots__ = ("addr", "network", "mask", "limited")

    def __init__(self, addr: str, network: int = 0, mask: int = 0,
                 limited: bool = False) -> None:
        self.addr = addr
        self.network = network
        self.mask = mask
        self.limited = limited

    def contains(self, ip: str) -> bool:
        """Whether ``ip`` sits on this route's subnet."""
        if self.limited or not self.mask:
            return False
        try:
            return (_to_uint32(ip) & self.mask) == self.network
        except (ValueError, AttributeError):
            return False

    def __repr__(self) -> str:  # pragma: no cover — debugging aid
        return f"BroadcastRoute({self.addr})"


def enumerate_broadcast_routes() -> List[BroadcastRoute]:
    """One destination per local IPv4 subnet, plus the limited broadcast.

    The address comes from each interface's real mask rather than being assumed
    to end in ``.255``: a /16 broadcasts to ``x.y.255.255`` and a /25 to
    ``x.y.z.127``, and the subnet itself is whatever the router hands out.
    Deduplicated by address — two interfaces on one subnet (a laptop docked over
    Ethernet while Wi-Fi is still up) would otherwise deliver every packet twice.

    The limited broadcast is always kept as a catch-all, so SoftAP setups and
    platforms whose interfaces cannot be enumerated behave exactly as before.
    """
    routes: List[BroadcastRoute] = []
    seen = set()

    for ip, prefix in local_ipv4_subnets():
        try:
            ip_value = _to_uint32(ip)
        except ValueError:
            continue
        mask = (0xFFFFFFFF << (32 - prefix)) & 0xFFFFFFFF
        addr = _to_dotted((ip_value & mask) | (~mask & 0xFFFFFFFF))
        if addr in seen:
            continue
        seen.add(addr)
        routes.append(BroadcastRoute(addr, network=ip_value & mask, mask=mask))

    routes.append(BroadcastRoute(DEFAULT_BROADCAST, limited=True))
    return routes


class UdpClient:
    """Broadcast-capable UDP socket plus a PONG receive loop."""

    def __init__(
        self,
        port: int = DEFAULT_PORT,
        broadcast_addr: str = DEFAULT_BROADCAST,
        bind_port: int = 0,
    ) -> None:
        self.port = port  # destination port (the device listens here)
        # Local receive bind. Default 0 = ephemeral: this leaves the
        # well-known device port (7700) to the single host daemon that owns
        # it (hapbeat-helper, serving Hapbeat Studio) so an SDK script and
        # Studio coexist. PING replies still arrive at the ephemeral source
        # port, so discovery keeps working. Pass ``bind_port=port`` only to
        # also receive the device's unsolicited broadcasts (daemon use).
        self.bind_port = bind_port
        self.broadcast_addr = broadcast_addr
        # An explicit broadcast_addr is the caller naming the destination, so
        # honour it verbatim: no enumeration, no fan-out, no route locking.
        # This doubles as the escape hatch for anyone who does need to pin the
        # destination by hand, which is why there is no separate setting.
        self._auto_routes = broadcast_addr == DEFAULT_BROADCAST
        self._routes: List[BroadcastRoute] = []
        # The route a device actually answered on; None until the first PONG.
        # Written from the receive thread, read from every sending thread —
        # rebinding one attribute, which is atomic under the GIL.
        self._locked_route: Optional[BroadcastRoute] = None
        self._sock: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._bound_well_known = False
        # Destinations currently in a failed state, so each outage is reported
        # once rather than once per packet. See _note_send_failed.
        self._failed_destinations: set[str] = set()
        self._pong_callbacks: list[PongCallback] = []
        self._packet_callbacks: list[PacketCallback] = []

    # ── Lifecycle ───────────────────────────────────────────────────
    def open(self) -> None:
        if self._sock is not None:
            return
        # Enumerated per open() rather than once per process: the host's
        # interfaces change when a laptop is docked, a VPN comes up or Wi-Fi
        # moves to another network.
        self._routes = enumerate_broadcast_routes() if self._auto_routes else []
        self._locked_route = None
        self._failed_destinations = set()
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        # Windows only: a prior sendto() to an unreachable peer queues an ICMP
        # port-unreachable, which Winsock surfaces as ConnectionResetError
        # (WinError 10054) on the *next* recvfrom() — even on this unconnected
        # socket. Once we unicast (a device that is off, rebooting, or being
        # reflashed is unreachable) that becomes routine, and the recv loop
        # below would die on it: the SDK would then go deaf to ALL PONGs until
        # the process restarts. SIO_UDP_CONNRESET=False tells Windows to ignore
        # those resets. No-op elsewhere (the constant is Windows-only).
        # Same fix as hapbeat-helper f06fa04, which shipped before this SDK.
        if hasattr(socket, "SIO_UDP_CONNRESET"):
            try:
                sock.ioctl(socket.SIO_UDP_CONNRESET, False)
            except OSError as exc:
                logger.debug("SIO_UDP_CONNRESET ioctl failed (non-fatal): %s", exc)
        if self.bind_port == self.port:
            # Only when contending for the shared well-known port do we ask to
            # reuse it; an ephemeral bind needs no reuse and must not steal a
            # port another process owns.
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            except OSError:
                pass
        try:
            sock.bind(("0.0.0.0", self.bind_port))
            self._bound_well_known = self.bind_port == self.port
        except OSError as exc:
            logger.warning(
                "port %d busy (%s); binding ephemeral port (discovery still "
                "works, async broadcast pushes are not received)",
                self.bind_port,
                exc,
            )
            try:
                sock.bind(("0.0.0.0", 0))
            except OSError:
                pass
            self._bound_well_known = False
        sock.settimeout(0.2)
        self._sock = sock
        self._running = True
        self._thread = threading.Thread(
            target=self._recv_loop, name="hapbeat-recv", daemon=True
        )
        self._thread.start()

    def close(self) -> None:
        self._running = False
        sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None

    # ── Listeners ───────────────────────────────────────────────────
    def add_pong_listener(self, cb: PongCallback) -> None:
        self._pong_callbacks.append(cb)

    def remove_pong_listener(self, cb: PongCallback) -> None:
        try:
            self._pong_callbacks.remove(cb)
        except ValueError:
            pass

    def add_packet_listener(self, cb: PacketCallback) -> None:
        """Receive CAPS and ERROR datagrams as ``cb(command, seq, payload,
        (ip, port))``; the sender port is needed to accept an ERROR."""
        self._packet_callbacks.append(cb)

    def remove_packet_listener(self, cb: PacketCallback) -> None:
        try:
            self._packet_callbacks.remove(cb)
        except ValueError:
            pass

    # ── Send ────────────────────────────────────────────────────────
    def broadcast_destination(self) -> str:
        """The single address a broadcast currently goes to.

        Once a device has answered this is its subnet's broadcast address;
        before that (and whenever the caller named ``broadcast_addr``) it is the
        limited broadcast, exactly as it has always been.
        """
        locked = self._locked_route
        return locked.addr if locked is not None else self.broadcast_addr

    def _note_send_failed(self, dst: str, exc: OSError) -> None:
        """Report a failed send at most once per destination, per outage.

        A link that fails keeps failing, and stream chunks go out roughly 100
        times a second — logging every one would bury the very lines an operator
        needs. Tracked per destination rather than globally: a host normally has
        an adapter that can never carry a broadcast (Bluetooth PAN, Wi-Fi
        Direct, a virtual switch), and one shared flag would flip to "failed" and
        back to "recovered" on every single fan-out. Cleared when that same
        destination works again, so a later, unrelated outage is still reported.
        """
        if dst in self._failed_destinations:
            return
        self._failed_destinations.add(dst)
        logger.warning(
            "UDP send to %s:%d failed: %s. The socket stays open; further send "
            "errors to this destination are silenced until it recovers.",
            dst, self.port, exc)

    def _note_send_ok(self, dst: str) -> None:
        """Say once that a destination works again.

        Without this an unattended installation's log shows when haptics broke
        but never whether they came back.
        """
        if dst not in self._failed_destinations:
            return
        self._failed_destinations.discard(dst)
        logger.info("UDP sending to %s recovered.", dst)

    def send(self, packet: bytes, addr: Optional[str] = None) -> bool:
        """Send a prebuilt packet. ``addr=None`` -> broadcast.

        One destination only. A failure is reported but never tears anything
        down: a lost datagram says nothing about whether the socket is still
        usable, and closing it here is what turns one Wi-Fi hiccup into a
        session that never recovers.
        """
        sock = self._sock
        if sock is None:
            logger.error("send before open()")
            return False
        dst = self.broadcast_destination() if not addr else addr
        try:
            sock.sendto(packet, (dst, self.port))
            self._note_send_ok(dst)
            return True
        except OSError as exc:
            self._note_send_failed(dst, exc)
            return False

    def send_discovery(self, packet: bytes) -> bool:
        """Send on every candidate broadcast destination.

        Reserved for PING and CONNECT_STATUS. Both are idempotent, so a device
        reachable on two destinations simply receives the message twice with no
        visible effect — whereas duplicating PLAY would fire the haptic twice on
        firmware that predates sequence de-duplication. This fan-out is what
        reaches a device the limited broadcast never gets to, and the PONG it
        provokes is what pins playback to the right subnet.
        """
        sock = self._sock
        if sock is None:
            logger.error("send before open()")
            return False
        # Already pinned to a subnet, or no enumeration to fan out over: one
        # destination, same as any other packet.
        if self._locked_route is not None or not self._routes:
            return self.send(packet)

        sent = False
        last_error: Optional[OSError] = None
        for route in self._routes:
            try:
                sock.sendto(packet, (route.addr, self.port))
                sent = True
                self._note_send_ok(route.addr)
            except OSError as exc:
                # Routine, and not worth a warning on its own: most hosts carry
                # an adapter that can never take a broadcast (Bluetooth PAN,
                # Wi-Fi Direct, an idle virtual switch). Trying anyway and
                # letting the others through is exactly what this fan-out is
                # for. Only total failure below is worth an operator's
                # attention — the difference is "one path is dead" versus
                # "discovery is dead".
                last_error = exc
                logger.debug("discovery send to %s:%d failed: %s",
                             route.addr, self.port, exc)
        if sent:
            # Clear the "nothing got out" state too, so a second, later outage
            # is reported instead of being swallowed by the first one's flag.
            self._note_send_ok(_ALL_DESTINATIONS)
        elif last_error is not None:
            self._note_send_failed(_ALL_DESTINATIONS, last_error)
        return sent

    def _lock_route_for(self, ip: str) -> None:
        """Pin broadcasts to the subnet a device actually replied from.

        First reply wins, and the choice lasts until the socket is reopened.
        With devices on two subnets at once this settles on whichever answered
        first; broadcasts do not cross subnets anyway, so the alternative is not
        reaching both, it is reaching neither reliably.
        """
        if self._locked_route is not None or not self._routes:
            return
        for route in self._routes:
            if route.contains(ip):
                self._locked_route = route
                logger.info("broadcasting to %s (a device answered from %s)",
                            route.addr, ip)
                return

    def send_many(self, packet: bytes, addrs: Iterable[str]) -> bool:
        """Unicast the same packet to several devices.

        Returns True if at least one send succeeded. One unreachable device
        (powered off between its last PONG and now) must not stop the packet
        from reaching the others, so failures are logged and skipped.
        """
        sent = False
        for addr in addrs:
            if self.send(packet, addr):
                sent = True
        return sent

    # ── Recv loop ───────────────────────────────────────────────────
    def _recv_loop(self) -> None:
        while self._running:
            sock = self._sock
            if sock is None:
                break
            try:
                data, addr = sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError as exc:
                if not self._running or self._sock is None:
                    break  # closed by close() — expected exit
                # Still running: residual ICMP-unreachable (should be
                # suppressed by SIO_UDP_CONNRESET above, but stay defensive in
                # case the ioctl was unavailable) or a brief NIC flap. Breaking
                # here is exactly what makes an SDK go permanently deaf to
                # PONGs, so back off briefly and keep listening.
                logger.debug("UDP recv transient error (continuing): %s", exc)
                time.sleep(0.2)
                continue
            pong = protocol.parse_pong(data)
            if pong is None:
                self._dispatch_packet(data, addr)
                continue
            ip = addr[0]
            # A reply proves which subnet the device is really on. Until this
            # happens a broadcast still goes out limited, which on a multi-homed
            # host may be leaving through an interface with no Hapbeat behind it.
            self._lock_route_for(ip)
            for cb in list(self._pong_callbacks):
                try:
                    cb(pong, ip)
                except Exception:  # noqa: BLE001 — never let a listener kill the loop
                    logger.exception("pong listener raised")

    def _dispatch_packet(self, data: bytes, addr: tuple) -> None:
        """Hand a CAPS / ERROR datagram to the packet listeners."""
        parsed = protocol.parse_packet(data)
        if parsed is None or parsed[0] not in (protocol.CMD_CAPS, protocol.CMD_ERROR):
            return
        command, seq, payload = parsed
        for cb in list(self._packet_callbacks):
            try:
                cb(command, seq, payload, addr)
            except Exception:  # noqa: BLE001 — never let a listener kill the loop
                logger.exception("packet listener raised")
