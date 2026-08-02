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
"""

from __future__ import annotations

import logging
import socket
import threading
import time
from typing import Callable, Iterable, Optional

from . import protocol

logger = logging.getLogger("hapbeat")

DEFAULT_PORT = 7700
DEFAULT_BROADCAST = "255.255.255.255"

PongCallback = Callable[[dict, str], None]


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
        self._sock: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._bound_well_known = False
        self._pong_callbacks: list[PongCallback] = []

    # ── Lifecycle ───────────────────────────────────────────────────
    def open(self) -> None:
        if self._sock is not None:
            return
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

    # ── Send ────────────────────────────────────────────────────────
    def send(self, packet: bytes, addr: Optional[str] = None) -> bool:
        """Send a prebuilt packet. ``addr=None`` -> broadcast."""
        sock = self._sock
        if sock is None:
            logger.error("send before open()")
            return False
        dst = self.broadcast_addr if not addr else addr
        try:
            sock.sendto(packet, (dst, self.port))
            return True
        except OSError as exc:
            logger.warning("UDP send to %s:%d failed: %s", dst, self.port, exc)
            return False

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
                continue
            ip = addr[0]
            for cb in list(self._pong_callbacks):
                try:
                    cb(pong, ip)
                except Exception:  # noqa: BLE001 — never let a listener kill the loop
                    logger.exception("pong listener raised")
