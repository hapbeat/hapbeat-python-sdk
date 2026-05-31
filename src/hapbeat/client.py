"""UDP transport for the Hapbeat SDK.

Owns one datagram socket configured for broadcast. Sends command packets to
the LAN broadcast address and runs a background thread that collects PONG
replies so device discovery works.

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
from typing import Callable, Optional

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
    ) -> None:
        self.port = port
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
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        except OSError:
            pass
        try:
            sock.bind(("0.0.0.0", self.port))
            self._bound_well_known = True
        except OSError as exc:
            logger.warning(
                "port %d busy (%s); binding ephemeral port (discovery still "
                "works, async broadcast pushes are not received)",
                self.port,
                exc,
            )
            try:
                sock.bind(("0.0.0.0", 0))
            except OSError:
                pass
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
            except OSError:
                break
            pong = protocol.parse_pong(data)
            if pong is None:
                continue
            ip = addr[0]
            for cb in list(self._pong_callbacks):
                try:
                    cb(pong, ip)
                except Exception:  # noqa: BLE001 — never let a listener kill the loop
                    logger.exception("pong listener raised")
