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
import secrets
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Union

from . import protocol
from .client import DEFAULT_BROADCAST, DEFAULT_PORT, UdpClient
from .eventmap import EventDef, EventMap
from .stream import (
    LEGACY_STREAM_MODE,
    EndpointStreamMode,
    StreamEndpoint,
    StreamHub,
    StreamPlayback,
)
from .stream_lease import (
    PENDING_PING_MAX_AGE,
    V2_LEASE_REFRESH_INTERVAL,
    EndpointClass,
    EndpointLeaseState,
    PendingPing,
    PongLeaseDecision,
    StreamSessionIdentity,
    evaluate_pong_lease,
)
from .wav import WavPcm, read_wav_pcm16

logger = logging.getLogger("hapbeat")


@dataclass
class Device:
    """A Hapbeat discovered on the LAN (from its PONG reply)."""

    ip: str
    name: str = ""
    address: str = ""
    firmware_version: str = ""
    port: int = DEFAULT_PORT
    last_seen: float = field(default_factory=time.monotonic)


def _now_us() -> int:
    return int(time.time() * 1_000_000)


def _new_client_incarnation() -> int:
    """A fresh nonzero uint64 (stream-session-v2.md client_incarnation)."""
    while True:
        value = secrets.randbits(64)
        if value:
            return value


class Hapbeat:
    """A connection to Hapbeat devices over Wi-Fi UDP.

    Commands go out by unicast to every device that answered a recent PING,
    and by broadcast until one does (see ``unicast`` in :func:`connect`).
    """

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
        unicast: bool = True,
        device_ttl: Optional[float] = None,
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
        # Stream session v2 (contracts stream-session-v2.md), all guarded by
        # _devices_lock. A fresh incarnation per open(); each device IP's
        # class / lease comes only from matched replies to our extended PING.
        self._client_incarnation = 0
        self._pending_pings: dict[int, PendingPing] = {}
        self._last_ping_timestamp_us = 0
        self._stream_states: dict[str, EndpointLeaseState] = {}
        self._keepalive = keepalive
        self._keepalive_interval = keepalive_interval
        self._keepalive_stop: Optional[threading.Event] = None
        self._keepalive_thread: Optional[threading.Thread] = None
        self._opened = False
        # Send PLAY/STOP/STOP_ALL/STREAM_* straight to devices we have heard a
        # PONG from, instead of broadcasting. See _command_destinations.
        self.unicast = unicast
        # How long a device stays a unicast destination after its last PONG.
        # Default: three keep-alive PINGs' worth (min 5 s), the same window the
        # Unity SDK uses. Too short and every command falls back to broadcast;
        # too long and we keep unicasting at a device that was powered off,
        # never returning to broadcast for the rest of the session.
        self.device_ttl = (
            device_ttl if device_ttl is not None
            else max(5.0, keepalive_interval * 3.0)
        )
        # One deep module owns all logical sources and endpoint wire sessions.
        # STREAM_DATA/END have no target, so every session is exact-unicast to
        # one PONG-reported endpoint; unresolved sources remain Deferred.
        self._stream_hub = StreamHub(
            self,
            self._stream_endpoints,
            send_ahead_sec=stream_send_ahead,
            mode_provider=self._stream_mode,
        )
        self._clip_cache: dict[str, WavPcm] = {}

    # ── Lifecycle ───────────────────────────────────────────────────
    def open(self) -> "Hapbeat":
        """Open the transport. Idempotent: ``connect()`` and ``__enter__``
        may both call it, so a second call while open is a no-op."""
        if self._opened:
            return self
        # A (re)connect is an explicit stream-ownership (re)acquisition: a new
        # incarnation, no outstanding requests of the old one. v2 states of the
        # old incarnation stop being usable; legacy devices keep their class.
        with self._devices_lock:
            self._client_incarnation = _new_client_incarnation()
            self._pending_pings.clear()
        self._stream_hub.open()
        self._opened = True
        self._client.add_pong_listener(self._on_pong)
        # A broadcast route locked on a network no device answers from any
        # more is released after the same window that retires a unicast
        # destination (contracts message-format.md §2.1).
        self._client.route_ttl = self.device_ttl
        self._client.open()
        if self._keepalive:
            self._start_keepalive()
        return self

    def close(self) -> None:
        if not self._opened:
            return
        self._opened = False
        self._stream_hub.close()
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

    # ── Send routing (unicast vs broadcast) ─────────────────────────
    #
    # Wi-Fi broadcast is held back by the AP: whenever any client on the AP is
    # power-saving, group-addressed frames are buffered until the next DTIM
    # beacon (100-300 ms). That shows up as a late haptic on one-shot commands
    # and as periodic dropouts in a clip stream, and the device cannot avoid it
    # (the delay is caused by *other* clients). Unicast frames are not batched,
    # and they get MAC-level ACK/retry as a bonus. Measured and confirmed on
    # the Unity SDK before being ported here.
    def _alive_devices(self) -> list[Device]:
        cutoff = time.monotonic() - self.device_ttl
        with self._devices_lock:
            return [d for d in self._devices.values() if d.last_seen >= cutoff]

    def _matching_ips(self, target: str) -> list[str]:
        """Alive devices whose address accepts ``target``.

        A device that has not reported an address (older firmware, or no PONG
        parsed yet) is kept — fail open. Worst case is one extra unicast
        packet that the device itself filters out; the opposite mistake would
        silently drop a command the device would have accepted.
        """
        return [
            d.ip for d in self._alive_devices()
            if not d.address or protocol.address_matches(target, d.address)
        ]

    def _send_routed(self, packet: bytes, target: str) -> bool:
        """Send one PLAY/STOP/STOP_ALL, unicast when we can.

        Falls back to broadcast when unicast is off, when no device has PONGed
        recently, *and* when every alive device's address mismatches — never
        skipping the send. The device re-applies the same target filter on
        receipt, so a broadcast can never fire a device the target did not
        address; whereas skipping would silently lose the packet whenever our
        cached address is stale (a STOP lost that way leaves a loop running).
        """
        if not self.unicast:
            return self._client.send(packet)
        dests = self._matching_ips(target)
        if not dests:
            return self._client.send(packet)
        return self._client.send_many(packet, dests)

    # ── Fire API (level-1) ──────────────────────────────────────────
    def play(
        self,
        event_id: str,
        gain: Optional[float] = None,
        *,
        target: Optional[str] = None,
        target_time_us: int = 0,
        pan: float = 0.0,
    ) -> Union[StreamPlayback, bool]:
        """Play a haptic event by id. The EventMap decides the mode.

        - **command** event (manifest ``events``) → a PLAY instruction; the
          device plays its installed clip.
        - **clip** event (manifest ``stream_events``) → the SDK streams the
          event's WAV from the kit's ``stream-clips/`` over UDP.

        Either way the caller writes the same one-liner. ``gain`` is the wire
        gain (0..1); when omitted, the bound :class:`EventMap` supplies the
        per-event default (its manifest ``intensity``), else ``1.0``.
        ``pan`` is the left/right balance (-1 left, 0 centre, +1 right).
        ``target_time_us`` applies to command mode only. ``pan`` applies to
        both modes; clip-mode balance is mixed into the endpoint PCM.
        """
        ev = self.event_map.get(event_id) if self.event_map else None
        if gain is None:
            gain = ev.intensity if ev is not None else 1.0
        gain = max(0.0, min(1.0, float(gain)))
        target_eff = self._effective_target(ev, target)

        if ev is not None and ev.streaming:
            return self.play_clip(event_id, gain, target=target_eff, pan=pan)

        pkt = protocol.build_play(
            self._next_seq(),
            event_id,
            target=target_eff,
            target_time_us=target_time_us,
            gain=gain,
            pan=pan,
        )
        return self._send_routed(pkt, target_eff)

    def stop(self, event_id: str, *, target: Optional[str] = None) -> bool:
        """Stop one event id. Clip events stop matching logical sources;
        command events send a STOP."""
        ev = self.event_map.get(event_id) if self.event_map else None
        if ev is not None and ev.streaming:
            self._stream_hub.stop_key(event_id, target=target)
            return True
        target_eff = self._effective_target(ev, target)
        pkt = protocol.build_stop(self._next_seq(), event_id, target=target_eff)
        return self._send_routed(pkt, target_eff)

    def stop_all(self, *, target: Optional[str] = None) -> bool:
        """Stop every event on matching devices (and any active clip stream)."""
        if target is None:
            self._stream_hub.stop_all()
        else:
            self._stream_hub.stop_target(target)
        target_eff = self._resolve_target(target)
        pkt = protocol.build_stop_all(self._next_seq(), target=target_eff)
        return self._send_routed(pkt, target_eff)

    def ping(self) -> bool:
        """Broadcast a PING (keep-alive / discovery probe).

        Goes out on every local subnet until a device answers — this is what
        finds a Hapbeat that the limited broadcast cannot reach on a multi-homed
        host. Safe to duplicate: a device answers each PING independently.

        It is the extended (16-byte) PING of stream-session-v2.md: its matched
        replies classify each device (v2 lease or legacy firmware) for clip
        streaming.
        """
        seq = self._next_seq()
        with self._devices_lock:
            if self._client_incarnation == 0:
                self._client_incarnation = _new_client_incarnation()
            # strictly increasing: class / lease adoption is ordered by the
            # echoed timestamp, and two immediate PINGs may read the same clock
            timestamp_us = max(_now_us(), self._last_ping_timestamp_us + 1)
            self._last_ping_timestamp_us = timestamp_us
            now = time.monotonic()
            self._pending_pings = {
                pending_seq: pending
                for pending_seq, pending in self._pending_pings.items()
                if now - pending.sent_at <= PENDING_PING_MAX_AGE
            }
            self._pending_pings[seq] = PendingPing(
                timestamp_us, self._client_incarnation, now
            )
            incarnation = self._client_incarnation
        return self._client.send_discovery(
            protocol.build_ping(seq, timestamp_us, incarnation))

    def reacquire_stream_ownership(self) -> bool:
        """Retake stream ownership after a device reported this client's lease
        as superseded by another writer (stream-session-v2.md).

        Starts a new client incarnation and re-PINGs: sessions under the old
        incarnation's leases end, and every v2 device issues a fresh lease on
        its matched reply, after which deferred sources resume. Call it once
        the other writer has stopped. Returns the PING send result, or False
        when the connection is not open.
        """
        if not self._opened:
            return False
        with self._devices_lock:
            self._client_incarnation = _new_client_incarnation()
            self._pending_pings.clear()
        self._stream_hub.reconcile()
        return self.ping()

    def connect_status(self, *, connected: bool = True) -> bool:
        """Announce connection state so the device OLED shows the app name."""
        pkt = protocol.build_connect_status(
            self._next_seq(),
            connected=connected,
            group=self.group,
            app_name=self.app_name,
            device_name=self.device_name,
        )
        # Idempotent display state, so it can share the discovery fan-out and
        # clear "app not connected" without waiting for a PONG first.
        return self._client.send_discovery(pkt)

    # ── Clip streaming (level-2) ─────────────────────────────────────
    def play_clip(
        self,
        event_id: str,
        gain: Optional[float] = None,
        *,
        target: Optional[str] = None,
        pan: float = 0.0,
        loop: Optional[bool] = None,
    ) -> Union[StreamPlayback, bool]:
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
        return self._stream_hub.play_pcm(
            wav.data,
            sample_rate=wav.sample_rate,
            channels=wav.channels,
            gain=max(0.0, min(1.0, float(gain))),
            pan=max(-1.0, min(1.0, float(pan))),
            loop=ev.loop if loop is None else loop,
            target=self._effective_target(ev, target),
            key=event_id,
        )

    def play_clip_file(
        self,
        path: Union[str, Path],
        gain: float = 1.0,
        *,
        target: Optional[str] = None,
        pan: float = 0.0,
        loop: bool = False,
    ) -> Union[StreamPlayback, bool]:
        """Stream an arbitrary PCM16 WAV file (not from a manifest)."""
        try:
            wav = read_wav_pcm16(path)
        except (OSError, ValueError) as exc:
            logger.warning("cannot read clip %s: %s", path, exc)
            return False
        return self._stream_hub.play_pcm(
            wav.data,
            sample_rate=wav.sample_rate,
            channels=wav.channels,
            gain=max(0.0, min(1.0, float(gain))),
            pan=max(-1.0, min(1.0, float(pan))),
            loop=loop,
            target=self._resolve_target(target),
        )

    def stream_pcm(
        self,
        pcm: bytes,
        *,
        sample_rate: int = 16000,
        channels: int = 1,
        gain: float = 1.0,
        pan: float = 0.0,
        loop: bool = False,
        target: Optional[str] = None,
    ) -> StreamPlayback:
        """Register an ad-hoc PCM16 source in the shared endpoint mixer.

        The returned handle adjusts or stops only this source. Multiple calls
        may run concurrently, including calls aimed at different targets.
        """
        return self._stream_hub.play_pcm(
            pcm,
            sample_rate=sample_rate,
            channels=max(1, channels),
            gain=max(0.0, min(1.0, float(gain))),
            pan=max(-1.0, min(1.0, float(pan))),
            loop=loop,
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

    # ── EndpointStreamSink (called only by StreamHub) ───────────────
    def _stream_endpoints(self) -> list[StreamEndpoint]:
        return [
            StreamEndpoint(ip=device.ip, address=device.address, port=device.port)
            for device in self._alive_devices()
            if device.address
        ]

    def _stream_mode(self, endpoint: StreamEndpoint) -> Optional[EndpointStreamMode]:
        """Legacy, v2 under its usable lease, or None (unknown class, invalid
        or superseded lease, lease of an old incarnation): sources defer."""
        with self._devices_lock:
            state = self._stream_states.get(endpoint.ip)
            incarnation = self._client_incarnation
        if state is None:
            return None
        if state.endpoint_class is EndpointClass.LEGACY:
            return LEGACY_STREAM_MODE
        lease = state.usable_v2_lease(incarnation)
        return EndpointStreamMode(lease) if lease is not None else None

    # v1 stream: pre-v2 firmware only (exact unicast, END without payload)
    def stream_begin_endpoint(self, endpoint: StreamEndpoint) -> None:
        packet = protocol.build_stream_begin(
            self._next_seq(),
            sample_rate=16000,
            channels=2,
            fmt=protocol.AUDIO_FORMAT_PCM16,
            total_samples=0,
            gain=1.0,
            target=endpoint.address,
        )
        self._client.send(packet, endpoint.ip)

    def stream_data_endpoint(
        self, endpoint: StreamEndpoint, offset: int, data: bytes
    ) -> None:
        packet = protocol.build_stream_data(self._next_seq(), offset, data)
        self._client.send(packet, endpoint.ip)

    def stream_end_endpoint(self, endpoint: StreamEndpoint) -> None:
        self._client.send(protocol.build_stream_end(self._next_seq()), endpoint.ip)

    # stream session v2: envelope on every packet
    def stream_begin_v2(
        self, endpoint: StreamEndpoint, identity: StreamSessionIdentity
    ) -> None:
        packet = protocol.build_stream_begin_v2(
            self._next_seq(),
            identity.lease.boot_id,
            identity.lease.ticket,
            identity.generation,
            sample_rate=16000,
            channels=2,
            fmt=protocol.AUDIO_FORMAT_PCM16,
            total_samples=0,
            gain=1.0,
            target=endpoint.address,
        )
        self._client.send(packet, endpoint.ip)

    def stream_data_v2(
        self, endpoint: StreamEndpoint, identity: StreamSessionIdentity,
        offset: int, data: bytes,
    ) -> None:
        packet = protocol.build_stream_data_v2(
            self._next_seq(),
            identity.lease.boot_id,
            identity.lease.ticket,
            identity.generation,
            offset,
            data,
        )
        self._client.send(packet, endpoint.ip)

    def stream_end_v2(
        self, endpoint: StreamEndpoint, identity: StreamSessionIdentity
    ) -> None:
        packet = protocol.build_stream_end_v2(
            self._next_seq(),
            identity.lease.boot_id,
            identity.lease.ticket,
            identity.generation,
        )
        self._client.send(packet, endpoint.ip)

    # ── Discovery ───────────────────────────────────────────────────
    def discover(self, timeout: float = 1.0, *, stop_early: bool = False) -> list[Device]:
        """Broadcast a PING and collect devices that reply within ``timeout``.

        ``stop_early`` returns as soon as the first device answers instead of
        waiting the whole window. Use it when the point is to *route* the next
        packet rather than to inventory the LAN — a one-shot command needs one
        reply to know which subnet to send on, and on a quiet LAN that arrives
        in a few milliseconds.
        """
        before = time.monotonic()
        self.ping()

        def replied() -> list[Device]:
            with self._devices_lock:
                return [d for d in self._devices.values()
                        if d.last_seen >= before - 0.05]

        deadline = before + max(0.0, timeout)
        if not stop_early:
            time.sleep(max(0.0, timeout))
            return replied()

        while time.monotonic() < deadline:
            found = replied()
            if found:
                return found
            time.sleep(0.01)
        return replied()

    @property
    def devices(self) -> list[Device]:
        with self._devices_lock:
            return list(self._devices.values())

    def _on_pong(self, pong: dict, ip: str) -> None:
        with self._devices_lock:
            previous = self._stream_states.get(ip)
            decision, state = evaluate_pong_lease(
                pong, self._pending_pings, self._client_incarnation, previous
            )
            if decision is PongLeaseDecision.DISCARD:
                # a reply for a retired request / old incarnation: must not roll
                # the device's address or liveness back either
                return
            if decision is PongLeaseDecision.ACCEPT:
                self._stream_states[ip] = state
            dev = self._devices.get(ip) or Device(ip=ip)
            dev.name = pong.get("device_name", dev.name)
            dev.address = pong.get("address", dev.address)
            dev.firmware_version = pong.get("firmware_version", dev.firmware_version)
            dev.last_seen = time.monotonic()
            self._devices[ip] = dev
        if decision is PongLeaseDecision.ACCEPT:
            self._log_stream_state_change(ip, previous, state)
        self._stream_hub.reconcile()

    @staticmethod
    def _log_stream_state_change(
        ip: str,
        previous: Optional[EndpointLeaseState],
        state: EndpointLeaseState,
    ) -> None:
        if previous is None or previous.endpoint_class is not state.endpoint_class:
            logger.info(
                "stream receiver %s classified as %s", ip,
                "stream session v2" if state.endpoint_class is EndpointClass.V2
                else "legacy (pre-v2 firmware)",
            )
        if state.lease_superseded and not (previous and previous.lease_superseded):
            logger.warning(
                "stream lease at %s was superseded by another writer; streams "
                "to it are deferred. Call reacquire_stream_ownership() after "
                "the other writer has stopped.", ip,
            )

    # ── Keep-alive thread ───────────────────────────────────────────
    def _start_keepalive(self) -> None:
        """Periodic PING (+ CONNECT_STATUS when an app name is set).

        The PING is what keeps unicast working: the device answers PONG *only*
        to a PING (CONNECT_STATUS gets no reply), so without this every device
        would age out of the destination table after ``device_ttl`` and the
        session would silently fall back to broadcast forever. It doubles as
        discovery for devices that join late.
        """
        self._keepalive_stop = threading.Event()
        self.ping()
        if self.app_name:
            self.connect_status(connected=True)

        def loop(stop: threading.Event) -> None:
            while not stop.wait(self._keepalive_wait()):
                self.ping()
                if self.app_name:
                    self.connect_status(connected=True)

        self._keepalive_thread = threading.Thread(
            target=loop, args=(self._keepalive_stop,),
            name="hapbeat-keepalive", daemon=True,
        )
        self._keepalive_thread.start()

    def _keepalive_wait(self) -> float:
        """The keep-alive interval, capped unless every known device is
        legacy: a v2 device lease expires after 15 s without PING, and a
        device not yet classified (or none heard yet) may turn out to be v2."""
        with self._devices_lock:
            states = [self._stream_states.get(ip) for ip in self._devices]
            states.extend(self._stream_states.values())
            all_legacy = bool(states) and all(
                state is not None and state.endpoint_class is EndpointClass.LEGACY
                for state in states
            )
        if all_legacy:
            return self._keepalive_interval
        return min(self._keepalive_interval, V2_LEASE_REFRESH_INTERVAL)

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
    unicast: bool = True,
    device_ttl: Optional[float] = None,
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

    ``unicast=True`` (default) sends PLAY/STOP/STOP_ALL and clip streams
    straight to the devices that answered a PING, instead of broadcasting —
    Wi-Fi APs hold broadcast frames until the next DTIM beacon (100-300 ms),
    which is heard as late haptics and stuttering streams. It falls back to
    broadcast until a device replies. Set ``unicast=False`` to always
    broadcast (e.g. many devices that must fire in lockstep, where one
    broadcast beats N sequential unicasts). ``device_ttl`` is how long a
    device stays a destination after its last PONG (default: 3 keep-alive
    intervals, min 5 s).
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
        unicast=unicast,
        device_ttl=device_ttl,
    ).open()
