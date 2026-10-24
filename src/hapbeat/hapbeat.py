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

import dataclasses
import enum
import logging
import secrets
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, ClassVar, Optional, Union

from . import protocol
from .client import DEFAULT_BROADCAST, DEFAULT_PORT, UdpClient
from .eventmap import EventDef, EventMap
from .stream import (
    LEGACY_STREAM_MODE,
    DeferredReason,
    EndpointStreamMode,
    StreamEndpoint,
    StreamHub,
    StreamPlayback,
)
from .stream_lease import (
    LEASE_REFRESH_INTERVAL,
    LEASE_RETRY_INTERVAL,
    LEASE_TTL,
    PENDING_PING_MAX_AGE,
    EndpointClass,
    EndpointLeaseState,
    PendingPing,
    PongLeaseDecision,
    StreamSessionIdentity,
    evaluate_pong_lease,
    matched_pending,
)
from .wav import WavPcm, read_wav_pcm16

logger = logging.getLogger("hapbeat")

# The keep-alive thread wakes this often to refresh stream leases and retry
# CAPS_REQ; discovery PING / CONNECT_STATUS still go out every keepalive_interval.
KEEPALIVE_TICK = 0.25
# CAPS_REQ without an answer is sent again after this long, at most this many
# times in total; then the device is taken to report no capabilities
# (contracts message-format.md §0x12).
CAPS_RETRY_INTERVAL = 0.5
CAPS_MAX_ATTEMPTS = 3


@dataclass(frozen=True)
class DeviceCaps:
    """What a device reported in CAPS (contracts message-format.md §0x13).

    ``DeviceCaps.NONE`` (generation 0, nothing else) stands for a device that
    reports no capabilities (0.x firmware): it has no additional features.
    """

    caps_version: int = 0
    generation: int = 0
    firmware_version: str = ""
    stream_gens: tuple[int, ...] = ()
    stream_formats: tuple[str, ...] = ()
    lease_ttl_s: int = 0
    lease_slots: int = 0
    inactivity_timeout_s: int = 0
    haptic_channels: int = 0
    address_max_len: int = 0
    features: tuple[str, ...] = ()

    NONE: ClassVar["DeviceCaps"]

    @classmethod
    def from_parsed(cls, parsed: dict) -> "DeviceCaps":
        """From a :func:`hapbeat.protocol.parse_caps_payload` dict."""
        values = {}
        for item in dataclasses.fields(cls):
            if item.name in parsed:
                value = parsed[item.name]
                values[item.name] = tuple(value) if isinstance(value, list) else value
        return cls(**values)


DeviceCaps.NONE = DeviceCaps()


class LeaseState(enum.Enum):
    """A Stream Gen 2 endpoint's lease, as the public API reports it."""

    VALID = "Valid"
    UNAVAILABLE = "Unavailable"
    SUPERSEDED = "Superseded"
    NONE = "None"  # not a Stream Gen 2 endpoint


@dataclass(frozen=True)
class StreamEndpointInfo:
    """Per-endpoint stream state (contracts sdk-multi-stream.md §3)."""

    address: str
    ip: str
    stream_class: EndpointClass
    lease_state: LeaseState


@dataclass
class Device:
    """A Hapbeat discovered on the LAN (from its PONG reply)."""

    ip: str
    name: str = ""
    address: str = ""
    firmware_version: str = ""
    port: int = DEFAULT_PORT
    last_seen: float = field(default_factory=time.monotonic)
    # Stream Gen 1 / Gen 2 from matched replies to our extended PING
    stream_class: EndpointClass = EndpointClass.UNKNOWN
    # None until the device answered CAPS_REQ (or DeviceCaps.NONE after it
    # did not, i.e. 0.x firmware)
    caps: Optional[DeviceCaps] = None


@dataclass
class _CapsRequest:
    seq: int
    sent_at: float
    attempts: int = 1


def _now_us() -> int:
    return int(time.time() * 1_000_000)


def _new_client_incarnation() -> int:
    """A fresh nonzero uint64 (stream-session.md client_incarnation)."""
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
        # Stream Gen 2 (contracts stream-session.md), all guarded by
        # _devices_lock. A fresh incarnation per open(); each device IP's
        # class / lease comes only from matched replies to our extended PING,
        # which goes by unicast to the endpoints we stream to (or may soon).
        self._client_incarnation = 0
        self._pending_pings: dict[int, PendingPing] = {}
        self._last_ping_timestamp_us = 0
        self._stream_states: dict[str, EndpointLeaseState] = {}
        self._last_lease_ping_at: dict[str, float] = {}
        # IPs whose lease we hold until then (a session / matching source, and
        # one lease TTL after it)
        self._lease_wanted_until: dict[str, float] = {}
        self._ignored_tail_warned: set[str] = set()
        # CAPS (contracts message-format.md §0x12/§0x13): outstanding requests,
        # and the (firmware_version, device_boot_id) each IP's CAPS are for
        self._caps_requests: dict[str, _CapsRequest] = {}
        self._caps_basis: dict[str, tuple[str, int]] = {}
        self._newer_generation_warned = False
        self._error_logged: set[tuple[str, int]] = set()
        self._endpoint_info: dict[str, StreamEndpointInfo] = {}
        self._endpoint_listeners: list[Callable[[StreamEndpointInfo], None]] = []
        self._keepalive = keepalive
        self._keepalive_interval = keepalive_interval
        self._next_keepalive_at = 0.0
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
            endpoint_wanted=self._on_endpoint_wanted,
            on_generation_exhausted=self._on_generation_exhausted,
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
        self._client.add_packet_listener(self._on_packet)
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
        self._client.remove_packet_listener(self._on_packet)
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
        """Broadcast a discovery PING (keep-alive / discovery probe).

        Goes out on every local subnet until a device answers — this is what
        finds a Hapbeat that the limited broadcast cannot reach on a multi-homed
        host. Safe to duplicate: a device answers each PING independently.

        It is the 8-byte PING: its replies keep devices alive and addressed
        but never allocate a stream lease or change a device's stream class.
        Leases are requested by unicast extended PINGs to the devices this
        client streams to (stream-session.md "How SDKs send extended PINGs").
        """
        seq = self._next_seq()
        with self._devices_lock:
            timestamp_us = self._record_ping_locked(seq, extended=False, ip=None,
                                                    now=time.monotonic())
        return self._client.send_discovery(protocol.build_ping(seq, timestamp_us))

    def _record_ping_locked(
        self, seq: int, *, extended: bool, ip: Optional[str], now: float
    ) -> int:
        """Register an outstanding PING and return its timestamp."""
        if self._client_incarnation == 0:
            self._client_incarnation = _new_client_incarnation()
        # strictly increasing: class / lease adoption is ordered by the
        # echoed timestamp, and two immediate PINGs may read the same clock
        timestamp_us = max(_now_us(), self._last_ping_timestamp_us + 1)
        self._last_ping_timestamp_us = timestamp_us
        self._pending_pings = {
            pending_seq: pending
            for pending_seq, pending in self._pending_pings.items()
            if now - pending.sent_at <= PENDING_PING_MAX_AGE
        }
        self._pending_pings[seq] = PendingPing(
            timestamp_us, self._client_incarnation, now, extended=extended, ip=ip
        )
        return timestamp_us

    def _lease_ping(self, ip: str, now: Optional[float] = None) -> bool:
        """Send the 16-byte extended PING (stream lease request) to ``ip`` by
        unicast. Its matched reply classifies the device and carries the
        lease."""
        now = time.monotonic() if now is None else now
        seq = self._next_seq()
        with self._devices_lock:
            timestamp_us = self._record_ping_locked(seq, extended=True, ip=ip, now=now)
            incarnation = self._client_incarnation
            self._last_lease_ping_at[ip] = now
        return self._client.send(protocol.build_ping(seq, timestamp_us, incarnation), ip)

    def _maybe_lease_ping(self, ip: str, now: float) -> bool:
        """Send a lease request to ``ip`` when one is due: every
        LEASE_RETRY_INTERVAL until there is a usable lease (or a Stream Gen 1
        class), then every TTL / 3 (also while superseded: the device re-issues
        a ticket only in reply to an extended PING)."""
        if not self._opened:
            return False
        with self._devices_lock:
            state = self._stream_states.get(ip)
            last = self._last_lease_ping_at.get(ip)
            incarnation = self._client_incarnation
            retry = (
                state is None
                or state.endpoint_class is EndpointClass.UNKNOWN
                or (
                    state.endpoint_class is EndpointClass.GEN2
                    and (
                        state.client_incarnation != incarnation
                        or (
                            not state.lease_superseded
                            and state.usable_gen2_lease(incarnation) is None
                        )
                    )
                )
            )
            interval = LEASE_RETRY_INTERVAL if retry else self._lease_refresh_interval_locked(ip)
        if last is not None and now - last < interval:
            return False
        return self._lease_ping(ip, now)

    def _lease_refresh_interval_locked(self, ip: str) -> float:
        """One third of the device's lease TTL (CAPS), else of the 15 s TTL."""
        device = self._devices.get(ip)
        ttl = device.caps.lease_ttl_s if device is not None and device.caps else 0
        return ttl / 3.0 if ttl > 0 else LEASE_REFRESH_INTERVAL

    def _on_endpoint_wanted(self, endpoint: StreamEndpoint) -> None:
        """StreamHub (under its lock): ``endpoint`` has a session or matches a
        source, so hold its lease now and for one TTL after."""
        if not self._opened:
            return
        now = time.monotonic()
        with self._devices_lock:
            self._lease_wanted_until[endpoint.ip] = now + LEASE_TTL
        self._maybe_lease_ping(endpoint.ip, now)

    def _wanted_lease_ips_locked(self, now: float) -> list[str]:
        self._lease_wanted_until = {
            ip: until for ip, until in self._lease_wanted_until.items() if until > now
        }
        return list(self._lease_wanted_until)

    def _new_incarnation(self) -> list[str]:
        """Start a new client incarnation; returns the IPs whose lease we hold."""
        with self._devices_lock:
            self._client_incarnation = _new_client_incarnation()
            self._pending_pings.clear()
            return self._wanted_lease_ips_locked(time.monotonic())

    def reacquire_stream_ownership(self) -> bool:
        """Retake stream ownership after a device reported this client's lease
        as superseded by another writer (stream-session.md).

        Starts a new client incarnation and sends a lease request to the
        devices this client streams to: sessions under the old incarnation's
        leases end, and every Stream Gen 2 device issues a fresh lease on its
        matched reply, after which deferred sources resume (finite sources from
        their start). Call it once the other writer has stopped. A device also
        hands the lease back on its own (``lease_reissued``) once the other
        application has stopped, so this is only needed to take it back while
        that application is still present. Returns whether a request went out
        (a discovery PING when no device is streamed to), or False when the
        connection is not open.
        """
        if not self._opened:
            return False
        ips = self._new_incarnation()
        self._stream_hub.reconcile()
        self._stream_hub.forget_carried()
        self._notify_endpoint_changes()
        if not ips:
            return self.ping()
        sent = False
        for ip in ips:
            sent = self._lease_ping(ip) or sent
        return sent

    def _on_generation_exhausted(self) -> None:
        """StreamHub (under its lock): a lease ran out of generations; a new
        incarnation gets new tickets (stream-session.md Counters)."""
        for ip in self._new_incarnation():
            self._lease_ping(ip)

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

    def _stream_mode(
        self, endpoint: StreamEndpoint
    ) -> Union[EndpointStreamMode, DeferredReason]:
        """Stream Gen 1, Gen 2 under its usable lease, or why its sources
        defer (class unknown, lease unavailable or of an old incarnation,
        superseded)."""
        with self._devices_lock:
            state = self._stream_states.get(endpoint.ip)
            incarnation = self._client_incarnation
            device = self._devices.get(endpoint.ip)
            mono = (
                device is not None
                and device.caps is not None
                and device.caps.haptic_channels == 1
            )
        if state is None or state.endpoint_class is EndpointClass.UNKNOWN:
            return DeferredReason.UNKNOWN_CLASS
        if state.endpoint_class is EndpointClass.GEN1:
            return EndpointStreamMode(None, mono) if mono else LEGACY_STREAM_MODE
        if state.client_incarnation != incarnation:
            return DeferredReason.LEASE_UNAVAILABLE
        if state.lease_superseded:
            return DeferredReason.SUPERSEDED
        lease = state.usable_gen2_lease(incarnation)
        if lease is None:
            return DeferredReason.LEASE_UNAVAILABLE
        return EndpointStreamMode(lease, mono)

    # Stream Gen 1: firmware before Stream Gen 2 only (exact unicast, END
    # without payload)
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

    # Stream Gen 2: envelope on every packet
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

    # ── Per-endpoint stream state (public) ──────────────────────────
    def stream_endpoints(self) -> list[StreamEndpointInfo]:
        """Each known device's stream class and lease state (contracts
        sdk-multi-stream.md §3). Why a Playback waits is its
        ``deferred_reason``."""
        with self._devices_lock:
            return [self._endpoint_info_locked(ip) for ip in self._devices]

    def add_stream_endpoint_listener(
        self, callback: Callable[[StreamEndpointInfo], None]
    ) -> None:
        """Call ``callback(info)`` whenever a device's stream class or lease
        state changes. It runs on the SDK's receive / keep-alive thread."""
        self._endpoint_listeners.append(callback)

    def remove_stream_endpoint_listener(
        self, callback: Callable[[StreamEndpointInfo], None]
    ) -> None:
        try:
            self._endpoint_listeners.remove(callback)
        except ValueError:
            pass

    def _endpoint_info_locked(self, ip: str) -> StreamEndpointInfo:
        device = self._devices.get(ip)
        state = self._stream_states.get(ip)
        stream_class = state.endpoint_class if state else EndpointClass.UNKNOWN
        if stream_class is not EndpointClass.GEN2:
            lease_state = LeaseState.NONE
        elif state.client_incarnation == self._client_incarnation and state.lease_superseded:
            lease_state = LeaseState.SUPERSEDED
        elif state.usable_gen2_lease(self._client_incarnation) is not None:
            lease_state = LeaseState.VALID
        else:
            lease_state = LeaseState.UNAVAILABLE
        return StreamEndpointInfo(
            address=device.address if device else "",
            ip=ip,
            stream_class=stream_class,
            lease_state=lease_state,
        )

    def _notify_endpoint_changes(self, ip: Optional[str] = None) -> None:
        changed = []
        with self._devices_lock:
            for each in (self._devices if ip is None else [ip]):
                if each not in self._devices:
                    continue
                info = self._endpoint_info_locked(each)
                previous = self._endpoint_info.get(each)
                if previous is None or (
                    previous.stream_class,
                    previous.lease_state,
                ) != (info.stream_class, info.lease_state):
                    changed.append(info)
                self._endpoint_info[each] = info
        for info in changed:
            for callback in list(self._endpoint_listeners):
                try:
                    callback(info)
                except Exception:  # noqa: BLE001 — a listener must not break the SDK
                    logger.exception("stream endpoint listener raised")

    # ── Received PONG / CAPS / ERROR ────────────────────────────────
    def _on_pong(self, pong: dict, ip: str) -> None:
        now = time.monotonic()
        caps_seq: Optional[int] = None
        warn_ignored = False
        with self._devices_lock:
            previous = self._stream_states.get(ip)
            pending = matched_pending(pong, self._pending_pings, ip)
            decision, state = evaluate_pong_lease(
                pong, self._pending_pings, self._client_incarnation, previous, ip
            )
            if decision is PongLeaseDecision.DISCARD:
                # a reply for a retired request / old incarnation: must not roll
                # the device's address or liveness back either
                return
            dev = self._devices.get(ip) or Device(ip=ip)
            if decision is PongLeaseDecision.ACCEPT:
                self._stream_states[ip] = state
                dev.stream_class = state.endpoint_class
            dev.name = pong.get("device_name", dev.name)
            dev.address = pong.get("address", dev.address)
            dev.firmware_version = pong.get("firmware_version", dev.firmware_version)
            dev.last_seen = now
            self._devices[ip] = dev
            if (
                pong.get("lease_tail") == protocol.LEASE_TAIL_IGNORED
                and ip not in self._ignored_tail_warned
            ):
                self._ignored_tail_warned.add(ip)
                warn_ignored = True
            if pending is not None:
                caps_seq = self._caps_due_locked(ip, pong, dev, now)
        if warn_ignored:
            logger.warning(
                "%s answered with a stream lease tail this SDK cannot read; "
                "streams to it stay deferred (is the SDK older than the "
                "firmware?)", ip,
            )
        if caps_seq is not None:
            self._client.send(protocol.build_caps_req(caps_seq), ip)
        if decision is PongLeaseDecision.ACCEPT:
            self._log_stream_state_change(ip, previous, state)
        self._stream_hub.reconcile()
        self._notify_endpoint_changes(ip)

    def _caps_due_locked(
        self, ip: str, pong: dict, dev: Device, now: float
    ) -> Optional[int]:
        """After a matched PONG: the seq of a CAPS_REQ to send now, if this is
        the first matched PONG of ``ip`` or its firmware_version or boot changed
        since its CAPS (message-format.md §0x12)."""
        boot = (
            pong["device_boot_id"]
            if pong.get("lease_tail") == protocol.LEASE_TAIL_VALID else 0
        )
        basis = self._caps_basis.get(ip)
        if basis is not None and basis[0] == dev.firmware_version:
            if boot == 0 or boot == basis[1]:
                return None
            if basis[1] == 0:
                # first boot id seen for the CAPS we have: same device boot
                self._caps_basis[ip] = (basis[0], boot)
                return None
        self._caps_basis[ip] = (dev.firmware_version, boot)
        seq = self._next_seq()
        self._caps_requests[ip] = _CapsRequest(seq=seq, sent_at=now)
        return seq

    def _on_packet(self, command: int, seq: int, payload: bytes, addr) -> None:
        """UdpClient: a CAPS or ERROR datagram from ``addr`` (ip, port)."""
        ip, port = addr[0], addr[1]
        if command == protocol.CMD_CAPS:
            self._on_caps(seq, payload, ip)
        elif command == protocol.CMD_ERROR:
            self._on_error(payload, ip, port)

    def _on_caps(self, seq: int, payload: bytes, ip: str) -> None:
        warn = False
        with self._devices_lock:
            request = self._caps_requests.get(ip)
            if request is None or request.seq != seq or not payload:
                return
            del self._caps_requests[ip]
            caps = DeviceCaps.from_parsed(protocol.parse_caps_payload(payload))
            device = self._devices.get(ip)
            if device is not None:
                device.caps = caps
            if caps.generation > protocol.SDK_MAJOR and not self._newer_generation_warned:
                self._newer_generation_warned = True
                warn = True
        if warn:
            logger.warning(
                "a Hapbeat at %s runs firmware generation %d, newer than this "
                "SDK (%d.x); update hapbeat-python-sdk to use everything it "
                "supports", ip, caps.generation, protocol.SDK_MAJOR,
            )
        # haptic_channels / lease TTL can change how its sessions are mixed
        self._stream_hub.reconcile()

    def _service_caps(self, now: float) -> None:
        """Resend unanswered CAPS_REQs every CAPS_RETRY_INTERVAL; after
        CAPS_MAX_ATTEMPTS the device reports no capabilities (0.x firmware)
        and is not asked again during this boot."""
        resend: list[tuple[str, int]] = []
        gave_up = False
        with self._devices_lock:
            for ip, request in tuple(self._caps_requests.items()):
                if now - request.sent_at < CAPS_RETRY_INTERVAL:
                    continue
                if request.attempts >= CAPS_MAX_ATTEMPTS:
                    del self._caps_requests[ip]
                    device = self._devices.get(ip)
                    if device is not None:
                        device.caps = DeviceCaps.NONE
                    gave_up = True
                    continue
                request.attempts += 1
                request.sent_at = now
                resend.append((ip, request.seq))
        for ip, seq in resend:
            self._client.send(protocol.build_caps_req(seq), ip)
        if gave_up:
            self._stream_hub.reconcile()

    def _on_error(self, payload: bytes, ip: str, port: int) -> None:
        """Handle an ERROR (message-format.md §0xFF, stream-session.md Sender
        requirements 5): only from a known device on the Hapbeat port, and a
        stream error only about a session we still track; per endpoint."""
        error = protocol.parse_error_payload(payload)
        if error is None:
            return
        with self._devices_lock:
            known = ip in self._devices
        if not known or port != self._client.port:
            return
        code = error["error_code"]
        detail = error.get("detail", b"")
        if code in protocol.STREAM_ERROR_CODES and (
            len(detail) != protocol.STREAM_ENVELOPE_SIZE
            or detail not in self._stream_hub.tracked_envelopes(ip)
        ):
            return

        if code == protocol.ERR_UNKNOWN_COMMAND and error.get("ref_command") == protocol.CMD_CAPS_REQ:
            with self._devices_lock:
                self._caps_requests.pop(ip, None)
                device = self._devices.get(ip)
                if device is not None:
                    device.caps = DeviceCaps.NONE
            self._stream_hub.reconcile()
        elif code == protocol.ERR_STREAM_GEN_UNSUPPORTED:
            # the device was updated past Stream Gen 1: classify it again
            if not self._stream_hub.had_gen1_session(ip):
                return
            with self._devices_lock:
                self._stream_states.pop(ip, None)
                device = self._devices.get(ip)
                if device is not None:
                    device.stream_class = EndpointClass.UNKNOWN
            self._stream_hub.reconcile()
            self._lease_ping(ip)
            self._notify_endpoint_changes(ip)
        elif code == protocol.ERR_STREAM_LEASE_INVALID:
            if self._update_lease_state(ip, lease_valid=False):
                self._stream_hub.reconcile()
                self._lease_ping(ip)
                self._notify_endpoint_changes(ip)
        elif code == protocol.ERR_STREAM_SUPERSEDED:
            if self._update_lease_state(ip, lease_valid=False, lease_superseded=True):
                logger.warning(
                    "stream lease at %s was superseded by another writer; streams "
                    "to it are deferred until the device hands it back or "
                    "reacquire_stream_ownership() is called.", ip,
                )
                self._stream_hub.reconcile()
                self._notify_endpoint_changes(ip)
        elif code == protocol.ERR_STREAM_NOT_STARTED:
            self._stream_hub.resend_begin(ip, detail)
        elif code == protocol.ERR_STREAM_TIMED_OUT:
            self._stream_hub.restart_session(ip, detail)
        elif code == protocol.ERR_STREAM_STOPPED:
            self._stream_hub.exclude_sources(ip, detail)
        elif code == protocol.ERR_STREAM_TARGET_MISMATCH:
            # our address for the device is stale: drop it until a PONG
            # reports the current one, then the endpoint resolves again
            self._stream_hub.end_endpoint(ip, detail)
            with self._devices_lock:
                device = self._devices.get(ip)
                if device is not None:
                    device.address = ""
            self._stream_hub.reconcile()
            self._lease_ping(ip)
        else:
            # UNSUPPORTED_VERSION, MALFORMED, STREAM_ENDED (a sender bug),
            # UNKNOWN_COMMAND for anything else, or a code this SDK predates
            self._log_error(ip, error)

    def _update_lease_state(self, ip: str, **changes) -> bool:
        with self._devices_lock:
            state = self._stream_states.get(ip)
            if state is None or state.endpoint_class is not EndpointClass.GEN2:
                return False
            self._stream_states[ip] = dataclasses.replace(state, **changes)
            return True

    def _log_error(self, ip: str, error: dict) -> None:
        key = (ip, error["error_code"])
        with self._devices_lock:
            first = key not in self._error_logged
            self._error_logged.add(key)
        logger.log(
            logging.WARNING if first else logging.DEBUG,
            "device %s reported error 0x%04x: %s", ip, error["error_code"],
            error.get("message", ""),
        )

    @staticmethod
    def _log_stream_state_change(
        ip: str,
        previous: Optional[EndpointLeaseState],
        state: EndpointLeaseState,
    ) -> None:
        if previous is None or previous.endpoint_class is not state.endpoint_class:
            logger.info(
                "stream receiver %s classified as %s", ip,
                "Stream Gen 2" if state.endpoint_class is EndpointClass.GEN2
                else "Stream Gen 1 (firmware before Stream Gen 2)",
            )
        if state.lease_superseded and not (previous and previous.lease_superseded):
            logger.warning(
                "stream lease at %s was superseded by another writer; streams "
                "to it are deferred until the device hands it back or "
                "reacquire_stream_ownership() is called.", ip,
            )

    # ── Keep-alive thread ───────────────────────────────────────────
    def _start_keepalive(self) -> None:
        """Periodic discovery PING (+ CONNECT_STATUS when an app name is set),
        lease refresh and CAPS retries.

        The PING is what keeps unicast working: the device answers PONG *only*
        to a PING (CONNECT_STATUS gets no reply), so without this every device
        would age out of the destination table after ``device_ttl`` and the
        session would silently fall back to broadcast forever. It doubles as
        discovery for devices that join late.

        With ``keepalive=False`` nothing refreshes leases while no stream is
        open: a lease is requested again when the next stream starts.
        """
        self._keepalive_stop = threading.Event()
        self._next_keepalive_at = 0.0
        self._keepalive_tick()

        def loop(stop: threading.Event) -> None:
            while not stop.wait(KEEPALIVE_TICK):
                self._keepalive_tick()

        self._keepalive_thread = threading.Thread(
            target=loop, args=(self._keepalive_stop,),
            name="hapbeat-keepalive", daemon=True,
        )
        self._keepalive_thread.start()

    def _keepalive_tick(self, now: Optional[float] = None) -> None:
        """One keep-alive step: discovery every keepalive_interval, a lease
        request to each device whose lease we hold when it is due (TTL / 3,
        including one TTL after its last session), CAPS retries."""
        now = time.monotonic() if now is None else now
        if now >= self._next_keepalive_at:
            self._next_keepalive_at = now + self._keepalive_interval
            self.ping()
            if self.app_name:
                self.connect_status(connected=True)
        with self._devices_lock:
            ips = self._wanted_lease_ips_locked(now)
        for ip in ips:
            self._maybe_lease_ping(ip, now)
        self._service_caps(now)

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
