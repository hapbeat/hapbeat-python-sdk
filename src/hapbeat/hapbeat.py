"""High-level Hapbeat client — the level-1 "fire" surface.

This is the *trigger* side of the SDK: anything (a research script, a game
loop, an OSC handler, a UI callback) calls :meth:`Hapbeat.play` / ``stop`` to
drive the device. The *tuning* side (default gains per event) is kept
orthogonal in :class:`hapbeat.eventmap.EventMap` and linked only by event id —
mirroring the Unity SDK's Trigger / EventMap split.

Typical use::

    import hapbeat

    hb = hapbeat.connect(app_name="MyExperiment")
    hb.play("impact.hit", gain=0.3)
    hb.stop("impact.hit")
    hb.close()

or as a context manager::

    with hapbeat.connect() as hb:
        hb.play("impact.hit")
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Optional

from . import protocol
from .client import DEFAULT_BROADCAST, DEFAULT_PORT, UdpClient
from .eventmap import EventMap


@dataclass
class Device:
    """A Hapbeat discovered on the LAN (from its PONG reply)."""

    ip: str
    name: str = ""
    address: str = ""
    firmware_version: str = ""
    last_seen: float = field(default_factory=time.monotonic)


def _now_us() -> int:
    return int(time.time() * 1_000_000)


class Hapbeat:
    """A connection to Hapbeat devices over Wi-Fi UDP broadcast."""

    def __init__(
        self,
        *,
        port: int = DEFAULT_PORT,
        broadcast_addr: str = DEFAULT_BROADCAST,
        app_name: str = "",
        device_name: str = "",
        group: int = 0,
        default_target: str = "",
        event_map: Optional[EventMap] = None,
        keepalive: bool = True,
        keepalive_interval: float = 5.0,
        bind_port: Optional[int] = None,
    ) -> None:
        self._client = UdpClient(port=port, broadcast_addr=broadcast_addr,
                                 bind_port=bind_port)
        self.app_name = app_name[: protocol.MAX_APP_NAME_LEN]
        self.device_name = device_name
        self.group = group
        self.default_target = default_target
        self.event_map = event_map
        self._seq = 0
        self._seq_lock = threading.Lock()
        self._devices: dict[str, Device] = {}
        self._devices_lock = threading.Lock()
        self._keepalive = keepalive
        self._keepalive_interval = keepalive_interval
        self._keepalive_stop: Optional[threading.Event] = None
        self._keepalive_thread: Optional[threading.Thread] = None
        self._opened = False

    # ── Lifecycle ───────────────────────────────────────────────────
    def open(self) -> "Hapbeat":
        """Open the transport. Idempotent: ``connect()`` and ``__enter__``
        may both call it, so a second call while open is a no-op."""
        if self._opened:
            return self
        self._opened = True
        self._client.add_pong_listener(self._on_pong)
        self._client.open()
        if self._keepalive and self.app_name:
            self._start_keepalive()
        return self

    def close(self) -> None:
        if not self._opened:
            return
        self._opened = False
        # Tell the device this app is leaving so the OLED clears.
        if self.app_name:
            try:
                self.connect_status(connected=False)
            except Exception:  # noqa: BLE001
                pass
        self._stop_keepalive()
        self._client.remove_pong_listener(self._on_pong)
        self._client.close()

    def __enter__(self) -> "Hapbeat":
        return self.open()

    def __exit__(self, *exc) -> None:
        self.close()

    # ── seq ─────────────────────────────────────────────────────────
    def _next_seq(self) -> int:
        with self._seq_lock:
            self._seq = (self._seq + 1) & 0xFFFF
            return self._seq

    def _resolve_target(self, target: Optional[str]) -> str:
        return self.default_target if target is None else target

    # ── Fire API (level-1) ──────────────────────────────────────────
    def play(
        self,
        event_id: str,
        gain: Optional[float] = None,
        *,
        target: Optional[str] = None,
        target_time_us: int = 0,
    ) -> bool:
        """Play a haptic event by id.

        ``event_id`` must exist in the kit deployed to the device.
        ``gain`` is the absolute wire gain (0..1). When omitted, the bound
        :class:`EventMap` supplies the per-event default (its manifest
        ``intensity``); without an EventMap the default is ``1.0``.
        """
        if gain is None:
            gain = self.event_map.gain_for(event_id) if self.event_map else 1.0
        gain = max(0.0, min(1.0, float(gain)))
        pkt = protocol.build_play(
            self._next_seq(),
            event_id,
            target=self._resolve_target(target),
            target_time_us=target_time_us,
            gain=gain,
        )
        return self._client.send(pkt)

    def stop(self, event_id: str, *, target: Optional[str] = None) -> bool:
        """Stop one event id on matching devices."""
        pkt = protocol.build_stop(
            self._next_seq(), event_id, target=self._resolve_target(target)
        )
        return self._client.send(pkt)

    def stop_all(self, *, target: Optional[str] = None) -> bool:
        """Stop every event on matching devices."""
        pkt = protocol.build_stop_all(
            self._next_seq(), target=self._resolve_target(target)
        )
        return self._client.send(pkt)

    def ping(self) -> bool:
        """Broadcast a PING (keep-alive / discovery probe)."""
        return self._client.send(protocol.build_ping(self._next_seq(), _now_us()))

    def connect_status(self, *, connected: bool = True) -> bool:
        """Announce connection state so the device OLED shows the app name."""
        pkt = protocol.build_connect_status(
            self._next_seq(),
            connected=connected,
            group=self.group,
            app_name=self.app_name,
            device_name=self.device_name,
        )
        return self._client.send(pkt)

    # ── Discovery ───────────────────────────────────────────────────
    def discover(self, timeout: float = 1.0) -> list[Device]:
        """Broadcast a PING and collect devices that reply within ``timeout``."""
        before = time.monotonic()
        self.ping()
        time.sleep(max(0.0, timeout))
        with self._devices_lock:
            return [d for d in self._devices.values() if d.last_seen >= before - 0.05]

    @property
    def devices(self) -> list[Device]:
        with self._devices_lock:
            return list(self._devices.values())

    def _on_pong(self, pong: dict, ip: str) -> None:
        with self._devices_lock:
            dev = self._devices.get(ip) or Device(ip=ip)
            dev.name = pong.get("device_name", dev.name)
            dev.address = pong.get("address", dev.address)
            dev.firmware_version = pong.get("firmware_version", dev.firmware_version)
            dev.last_seen = time.monotonic()
            self._devices[ip] = dev

    # ── Keep-alive thread ───────────────────────────────────────────
    def _start_keepalive(self) -> None:
        self._keepalive_stop = threading.Event()
        self.connect_status(connected=True)

        def loop(stop: threading.Event) -> None:
            while not stop.wait(self._keepalive_interval):
                self.connect_status(connected=True)

        self._keepalive_thread = threading.Thread(
            target=loop, args=(self._keepalive_stop,),
            name="hapbeat-keepalive", daemon=True,
        )
        self._keepalive_thread.start()

    def _stop_keepalive(self) -> None:
        if self._keepalive_stop is not None:
            self._keepalive_stop.set()
        if self._keepalive_thread is not None:
            self._keepalive_thread.join(timeout=1.0)
            self._keepalive_thread = None


def connect(
    *,
    port: int = DEFAULT_PORT,
    broadcast_addr: str = DEFAULT_BROADCAST,
    app_name: str = "",
    device_name: str = "",
    group: int = 0,
    default_target: str = "",
    event_map: Optional[EventMap] = None,
    keepalive: bool = True,
    bind_port: Optional[int] = None,
) -> Hapbeat:
    """Open a connection and return a ready :class:`Hapbeat`.

    Equivalent to ``Hapbeat(...).open()``. Pass ``bind_port=0`` to receive on
    an ephemeral port instead of the well-known ``port``, so the SDK can run
    alongside hapbeat-helper (which owns UDP 7700 for Hapbeat Studio).
    """
    return Hapbeat(
        port=port,
        broadcast_addr=broadcast_addr,
        app_name=app_name,
        device_name=device_name,
        group=group,
        default_target=default_target,
        event_map=event_map,
        keepalive=keepalive,
        bind_port=bind_port,
    ).open()
