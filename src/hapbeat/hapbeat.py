"""High-level Hapbeat client — the level-1 "fire" surface.

This is the *trigger* side of the SDK: anything (a research script, a game
loop, an OSC handler, a UI callback) calls :meth:`Hapbeat.play` / ``stop`` to
drive the device. The *tuning* side (default gains per event) is kept
orthogonal in :class:`hapbeat.eventmap.EventMap` and linked only by event id —
mirroring the Unity SDK's Trigger / EventMap split.

Typical use::

    import hapbeat

    hb = hapbeat.connect(app_name="MyExperiment")
    hb.play("sample-kit.sine_100hz", gain=0.3)
    hb.stop("sample-kit.sine_100hz")
    hb.close()

or as a context manager::

    with hapbeat.connect() as hb:
        hb.play("sample-kit.sine_100hz")
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Union

from . import protocol
from .client import DEFAULT_BROADCAST, DEFAULT_PORT, UdpClient
from .clip import ClipStreamer
from .eventmap import EventDef, EventMap
from .wav import WavPcm, read_wav_pcm16

logger = logging.getLogger("hapbeat")


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
        bind_port: int = 0,
        clip_base: Optional[Union[str, Path]] = None,
        stream_send_ahead: float = 0.15,
    ) -> None:
        self._client = UdpClient(port=port, broadcast_addr=broadcast_addr,
                                 bind_port=bind_port)
        self.app_name = app_name[: protocol.MAX_APP_NAME_LEN]
        self.device_name = device_name
        self.group = group
        self.default_target = default_target
        self.event_map = event_map
        # Where clip-mode WAVs live. If unset, they resolve under the bound
        # EventMap's kit_dir (kit_dir/stream-clips/<clip>).
        self.clip_base: Optional[Path] = Path(clip_base) if clip_base else None
        self._seq = 0
        self._seq_lock = threading.Lock()
        self._devices: dict[str, Device] = {}
        self._devices_lock = threading.Lock()
        self._keepalive = keepalive
        self._keepalive_interval = keepalive_interval
        self._keepalive_stop: Optional[threading.Event] = None
        self._keepalive_thread: Optional[threading.Thread] = None
        self._opened = False
        # Clip streaming (level-2). The streamer calls this object's
        # stream_begin/stream_data/stream_end (StreamSink protocol).
        self._clip = ClipStreamer(self, send_ahead_sec=stream_send_ahead)
        self._clip_cache: dict[str, WavPcm] = {}

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
        self._clip.stop()  # end any in-flight clip stream
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

    def _effective_target(self, ev: Optional[EventDef], target: Optional[str]) -> str:
        """Resolve the wire target: explicit call-site arg > the event's own
        target (from the haptic file) > the connection default."""
        if target is not None:
            return target
        if ev is not None and ev.target:
            return ev.target
        return self.default_target

    # ── Fire API (level-1) ──────────────────────────────────────────
    def play(
        self,
        event_id: str,
        gain: Optional[float] = None,
        *,
        target: Optional[str] = None,
        target_time_us: int = 0,
    ) -> bool:
        """Play a haptic event by id. The EventMap decides the mode.

        - **command** event (manifest ``events``) → a PLAY instruction; the
          device plays its installed clip.
        - **clip** event (manifest ``stream_events``) → the SDK streams the
          event's WAV from the kit's ``stream-clips/`` over UDP.

        Either way the caller writes the same one-liner. ``gain`` is the wire
        gain (0..1); when omitted, the bound :class:`EventMap` supplies the
        per-event default (its manifest ``intensity``), else ``1.0``.
        ``target_time_us`` applies to command mode only.
        """
        ev = self.event_map.get(event_id) if self.event_map else None
        if gain is None:
            gain = ev.intensity if ev is not None else 1.0
        gain = max(0.0, min(1.0, float(gain)))
        target_eff = self._effective_target(ev, target)

        if ev is not None and ev.streaming:
            return self.play_clip(event_id, gain, target=target_eff)

        pkt = protocol.build_play(
            self._next_seq(),
            event_id,
            target=target_eff,
            target_time_us=target_time_us,
            gain=gain,
        )
        return self._client.send(pkt)

    def stop(self, event_id: str, *, target: Optional[str] = None) -> bool:
        """Stop one event id. Clip events end the active stream; command
        events send a STOP."""
        ev = self.event_map.get(event_id) if self.event_map else None
        if ev is not None and ev.streaming:
            self._clip.stop()
            return True
        pkt = protocol.build_stop(
            self._next_seq(), event_id, target=self._effective_target(ev, target)
        )
        return self._client.send(pkt)

    def stop_all(self, *, target: Optional[str] = None) -> bool:
        """Stop every event on matching devices (and any active clip stream)."""
        self._clip.stop()
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

    # ── Clip streaming (level-2) ─────────────────────────────────────
    def play_clip(
        self,
        event_id: str,
        gain: Optional[float] = None,
        *,
        target: Optional[str] = None,
    ) -> bool:
        """Stream a clip-mode event's WAV to the device.

        Usually you call :meth:`play`, which routes clip events here. The WAV
        is read once and cached. Returns ``False`` if the clip can't be found.
        """
        ev = self.event_map.get(event_id) if self.event_map else None
        if ev is None or not ev.clip:
            logger.warning("clip event %r has no clip file", event_id)
            return False
        if gain is None:
            gain = ev.intensity
        wav = self._load_clip(event_id, ev)
        if wav is None:
            return False
        self._clip.play(
            wav.data,
            sample_rate=wav.sample_rate,
            channels=wav.channels,
            gain=max(0.0, min(1.0, float(gain))),
            target=self._effective_target(ev, target),
        )
        return True

    def play_clip_file(
        self,
        path: Union[str, Path],
        gain: float = 1.0,
        *,
        target: Optional[str] = None,
    ) -> bool:
        """Stream an arbitrary PCM16 WAV file (not from a manifest)."""
        try:
            wav = read_wav_pcm16(path)
        except (OSError, ValueError) as exc:
            logger.warning("cannot read clip %s: %s", path, exc)
            return False
        self._clip.play(
            wav.data,
            sample_rate=wav.sample_rate,
            channels=wav.channels,
            gain=max(0.0, min(1.0, float(gain))),
            target=self._resolve_target(target),
        )
        return True

    def stream_pcm(
        self,
        pcm: bytes,
        *,
        sample_rate: int = 16000,
        channels: int = 1,
        gain: float = 1.0,
        target: Optional[str] = None,
    ) -> None:
        """Stream an ad-hoc PCM16 buffer (e.g. synthesized cues). ``channels=2``
        with per-channel amplitude conveys L/R balance (PLAY has no pan)."""
        self._clip.play(
            pcm,
            sample_rate=sample_rate,
            channels=max(1, channels),
            gain=max(0.0, min(1.0, float(gain))),
            target=self._resolve_target(target),
        )

    def preload_clips(self) -> None:
        """Decode every clip-mode event's WAV up front so the first play of
        each has no load latency."""
        if self.event_map is None:
            return
        for event_id in self.event_map.ids():
            ev = self.event_map.get(event_id)
            if ev is not None and ev.streaming and ev.clip:
                self._load_clip(event_id, ev)

    def _resolve_clip_path(self, ev: EventDef) -> Optional[Path]:
        if not ev.clip:
            return None
        if self.clip_base is not None:
            return self.clip_base / ev.clip
        if self.event_map is not None and self.event_map.kit_dir is not None:
            return self.event_map.kit_dir / "stream-clips" / ev.clip
        logger.warning(
            "cannot resolve clip %r: no clip_base and EventMap has no kit_dir "
            "(load the EventMap with from_kit/from_manifest path, or pass clip_base)",
            ev.clip,
        )
        return None

    def _load_clip(self, event_id: str, ev: EventDef) -> Optional[WavPcm]:
        cached = self._clip_cache.get(event_id)
        if cached is not None:
            return cached
        path = self._resolve_clip_path(ev)
        if path is None:
            return None
        try:
            wav = read_wav_pcm16(path)
        except (OSError, ValueError) as exc:
            logger.warning("failed to load clip %s: %s", path, exc)
            return None
        self._clip_cache[event_id] = wav
        return wav

    # ── StreamSink (called by ClipStreamer) ─────────────────────────
    def stream_begin(self, *, sample_rate: int, channels: int,
                     total_samples: int, gain: float, target: str) -> None:
        self._client.send(protocol.build_stream_begin(
            self._next_seq(), sample_rate=sample_rate, channels=channels,
            fmt=protocol.AUDIO_FORMAT_PCM16, total_samples=total_samples,
            gain=gain, target=target,
        ))

    def stream_data(self, offset: int, data: bytes) -> None:
        self._client.send(protocol.build_stream_data(self._next_seq(), offset, data))

    def stream_end(self) -> None:
        self._client.send(protocol.build_stream_end(self._next_seq()))

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
    bind_port: int = 0,
    kit: Optional[Union[str, Path]] = None,
    haptics: Optional[Union[str, Path]] = None,
    clip_base: Optional[Union[str, Path]] = None,
    stream_send_ahead: float = 0.15,
) -> Hapbeat:
    """Open a connection and return a ready :class:`Hapbeat`.

    Equivalent to ``Hapbeat(...).open()``. The local receive socket binds an
    ephemeral port by default (``bind_port=0``) so the SDK coexists with
    hapbeat-helper, which owns the well-known UDP 7700 for Hapbeat Studio.
    Pass ``bind_port=port`` only if you need the device's unsolicited
    broadcasts (normally a daemon's job).

    Loading the EventMap (pick one):
      - ``haptics`` — a *haptic file* (overlay that references a kit and adds
        per-event target/gain); ``EventMap.from_file``. Recommended.
      - ``kit`` — a kit folder; ``EventMap.from_kit`` (intensity/clip only,
        no targeting).
    ``clip_base`` overrides where clip WAVs are read (default ``<kit>/stream-clips/``).
    """
    if event_map is None:
        if haptics is not None:
            event_map = EventMap.from_file(haptics)
        elif kit is not None:
            event_map = EventMap.from_kit(kit)
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
        clip_base=clip_base,
        stream_send_ahead=stream_send_ahead,
    ).open()
