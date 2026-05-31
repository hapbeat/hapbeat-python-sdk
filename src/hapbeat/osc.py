"""Generic OSC -> Hapbeat bridge.

Lets any OSC-capable tool (TouchOSC, Max/MSP, TouchDesigner, a DAW, vvvv, ...)
drive Hapbeat without writing code, using the addresses defined in
``hapbeat-contracts/specs/message-format.md`` §6::

    /hapbeat/play      event_id [target] [target_time_us] [gain]
    /hapbeat/stop      event_id [target]
    /hapbeat/stop-all  [target]
    /hapbeat/ping

This is the *transport* form of OSC (speak directly to Hapbeat). App-specific
schemas (VRChat avatar parameters, etc.) are handled by dedicated bridges that
translate the foreign schema into these calls.

Requires the optional dependency::

    pip install "hapbeat[osc]"
"""

from __future__ import annotations

import logging
from typing import Optional

from .hapbeat import Hapbeat

logger = logging.getLogger("hapbeat.osc")

# Layer 1 OSC port (hapbeat-contracts/specs/message-format.md §2).
DEFAULT_OSC_PORT = 7702


def _require_pythonosc():
    try:
        from pythonosc.dispatcher import Dispatcher
        from pythonosc.osc_server import BlockingOSCUDPServer
    except ImportError as exc:  # pragma: no cover - import-time guard
        raise ImportError(
            "OSC support needs python-osc. Install with: pip install \"hapbeat[osc]\""
        ) from exc
    return Dispatcher, BlockingOSCUDPServer


class OscBridge:
    """Listen for ``/hapbeat/*`` OSC messages and relay them to devices."""

    def __init__(
        self,
        hb: Hapbeat,
        listen_host: str = "0.0.0.0",
        listen_port: int = DEFAULT_OSC_PORT,
    ) -> None:
        self.hb = hb
        self.listen_host = listen_host
        self.listen_port = listen_port
        self._server = None

    # ── Handlers (defensive about omitted trailing OSC args) ────────
    def _handle_play(self, _address: str, *args) -> None:
        event_id = str(args[0]) if len(args) >= 1 else ""
        if not event_id:
            return
        target = str(args[1]) if len(args) >= 2 else ""
        target_time = int(args[2]) if len(args) >= 3 else 0
        gain = float(args[3]) if len(args) >= 4 else None
        self.hb.play(event_id, gain, target=target, target_time_us=target_time)

    def _handle_stop(self, _address: str, *args) -> None:
        if not args:
            return
        target = str(args[1]) if len(args) >= 2 else ""
        self.hb.stop(str(args[0]), target=target)

    def _handle_stop_all(self, _address: str, *args) -> None:
        target = str(args[0]) if args else ""
        self.hb.stop_all(target=target)

    def _handle_ping(self, _address: str, *_args) -> None:
        self.hb.ping()

    # ── Lifecycle ───────────────────────────────────────────────────
    def serve_forever(self) -> None:
        """Block and relay OSC until interrupted."""
        Dispatcher, BlockingOSCUDPServer = _require_pythonosc()
        disp = Dispatcher()
        disp.map("/hapbeat/play", self._handle_play)
        disp.map("/hapbeat/stop", self._handle_stop)
        disp.map("/hapbeat/stop-all", self._handle_stop_all)
        disp.map("/hapbeat/ping", self._handle_ping)
        self._server = BlockingOSCUDPServer((self.listen_host, self.listen_port), disp)
        logger.info(
            "OSC bridge listening on %s:%d -> UDP %d",
            self.listen_host, self.listen_port, self.hb._client.port,
        )
        try:
            self._server.serve_forever()
        finally:
            self._server.server_close()
            self._server = None

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()


def run_bridge(
    listen_port: int = DEFAULT_OSC_PORT,
    *,
    udp_port: int = 7700,
    app_name: str = "hapbeat-osc",
) -> None:
    """Open a Hapbeat connection and serve the OSC bridge (blocking)."""
    hb = Hapbeat(port=udp_port, app_name=app_name).open()
    try:
        OscBridge(hb, listen_port=listen_port).serve_forever()
    finally:
        hb.close()
