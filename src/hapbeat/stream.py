"""Endpoint-scoped multi-stream mixing for WifiUdp.

Logical PCM sources are independent to callers, but Hapbeat devices expose one
wire stream each.  StreamHub therefore resolves targets to exact PONG-reported
device endpoints, mixes every matching source per endpoint, and owns the
BEGIN/DATA/END lifecycle.  No stream packet is ever broadcast.

Each endpoint session is fixed to one wire format when it begins
(contracts stream-session.md): Stream Gen 2 receivers get the device-issued
lease plus a fresh generation on every packet and can restart immediately
after END; older receivers get Stream Gen 1 and keep the 300 ms END -> BEGIN
guard, for that endpoint only.

Every source here is a finite or looping PCM buffer, so an open session never
runs out of input. A live (pushed) source added later must mix silence while it
has no new input instead of pausing the session's DATA (sdk-multi-stream.md
§6.12).
"""

from __future__ import annotations

import enum
import logging
import math
import struct
import sys
import threading
import time
import uuid
from array import array
from dataclasses import dataclass, field
from typing import Callable, Optional, Protocol, Union

from . import protocol
from .stream_lease import LEASE_TTL, StreamLease, StreamSessionIdentity

logger = logging.getLogger("hapbeat")

SAMPLE_RATE = 16_000
CHANNELS = 2
CHUNK_FRAMES = 256
MAX_DATA_BYTES = CHUNK_FRAMES * CHANNELS * 2
DEFAULT_SEND_AHEAD = 0.15
FULL_SCALE = 32767.0
# Mix bus: overlapping sources are summed. A sum whose peak is within
# LIMIT_THRESHOLD (the PCM16 range including -32768, so one full-scale clip
# alone never triggers it) passes unchanged; one that exceeds it is scaled
# down to LIMIT_TARGET (gain
# reduction ramped per sample: attack within the chunk, ~0.3 s release), and a
# soft knee above LIMIT_KNEE replaces the hard clamp for what is left over.
LIMIT_THRESHOLD = 32768.0
LIMIT_TARGET = 0.9 * FULL_SCALE
LIMIT_KNEE = 0.95 * FULL_SCALE
LIMIT_RELEASE_PER_CHUNK = 0.05
# Keep an empty endpoint session alive for one AP beacon interval. A new source
# can then join without END -> BEGIN, which may reorder/flush the previous tail.
SESSION_END_LINGER = 0.31
# Firmware before Stream Gen 2 cannot tell a delayed old END from a new one, so
# after a Stream Gen 1 END the next BEGIN to that endpoint waits this long.
# Stream Gen 2 endpoints order sessions by (lease, generation) and restart
# immediately.
LEGACY_END_TO_BEGIN_GUARD = 0.3
MAX_GENERATION = 0xFFFFFFFF
# A lost Stream Gen 2 BEGIN would silence its whole session, and an equal BEGIN
# is idempotent on the receiver: send it again before each of the first DATA
# blocks and then periodically, at most 500 ms apart (stream-session.md Sender
# requirements 2; the interval leaves room for pump jitter). Stream Gen 1
# firmware restarts its decoder on every BEGIN, so never there.
BEGIN_REPEAT_COUNT = 3
BEGIN_REPEAT_INTERVAL = 0.45
# The receiver times a session out after 5 s without DATA; a sender that could
# not send for this long begins a new generation before sending more
# (Sender requirements 3).
OWN_GAP_RESTART = 3.0
# Re-splice (sdk-multi-stream.md §6.14): a source added while this much of the
# sent-ahead mix is still queued on the device (its BEGIN flush threshold of
# 32 ms plus packets in flight) restarts the session from that point instead of
# joining behind the queue.
RESPLICE_MIN_QUEUED = 0.04


def _clock() -> float:
    return time.perf_counter()


class PlaybackStatus(enum.Enum):
    DEFERRED = "Deferred"
    ACTIVE = "Active"
    STOPPED = "Stopped"


class DeferredReason(enum.Enum):
    NO_RESOLVED_ENDPOINT = "NoResolvedEndpoint"
    UNKNOWN_CLASS = "UnknownClass"          # no matched PONG yet
    LEASE_UNAVAILABLE = "LeaseUnavailable"  # no lease, or it expired
    SUPERSEDED = "Superseded"               # another application played later


# one reason per Playback when matching endpoints differ (sdk-multi-stream.md §3)
_REASON_PRIORITY = (
    DeferredReason.SUPERSEDED,
    DeferredReason.LEASE_UNAVAILABLE,
    DeferredReason.UNKNOWN_CLASS,
)


@dataclass(frozen=True)
class StreamEndpoint:
    ip: str
    address: str
    port: int = 7700


@dataclass(frozen=True)
class EndpointStreamMode:
    """How an endpoint may be streamed now: ``lease=None`` is Stream Gen 1
    (firmware before Stream Gen 2), otherwise Stream Gen 2 under that
    device-issued lease. ``mono`` marks a device with one haptic output
    (CAPS haptic_channels 1): source pan is not applied there."""

    lease: Optional[StreamLease] = None
    mono: bool = False


LEGACY_STREAM_MODE = EndpointStreamMode()

# what a mode provider returns for an endpoint: a mode it may be streamed in,
# or why it may not (None = class unknown)
EndpointModeResult = Union[EndpointStreamMode, DeferredReason, None]


class EndpointStreamSink(Protocol):
    # Stream Gen 1, for endpoints in LEGACY_STREAM_MODE
    def stream_begin_endpoint(self, endpoint: StreamEndpoint) -> None: ...

    def stream_data_endpoint(
        self, endpoint: StreamEndpoint, offset: int, data: bytes
    ) -> None: ...

    def stream_end_endpoint(self, endpoint: StreamEndpoint) -> None: ...

    # Stream Gen 2: every packet carries the session identity
    def stream_begin_v2(
        self, endpoint: StreamEndpoint, identity: StreamSessionIdentity
    ) -> None: ...

    def stream_data_v2(
        self, endpoint: StreamEndpoint, identity: StreamSessionIdentity,
        offset: int, data: bytes,
    ) -> None: ...

    def stream_end_v2(
        self, endpoint: StreamEndpoint, identity: StreamSessionIdentity
    ) -> None: ...


@dataclass
class _Source:
    id: str
    pcm: tuple[int, ...]
    target: str
    gain: float
    pan: float
    loop: bool
    key: Optional[str]
    playback: "StreamPlayback"
    completed_endpoints: set[StreamEndpoint] = field(default_factory=set)
    # matched endpoints whose session cannot begin yet (legacy guard, or a
    # lease being renewed with this source's cursor kept): a finite source that
    # completes elsewhere meanwhile must still play there
    waiting_endpoints: set[StreamEndpoint] = field(default_factory=set)
    # (ip, port) of endpoints whose session STOP_ALL ended (ERROR
    # STREAM_STOPPED): this source never rejoins them (sdk-multi-stream.md
    # §6.13). Kept across expiry, unlike completed_endpoints.
    stopped_endpoints: set[tuple[str, int]] = field(default_factory=set)
    # played to the end on at least one endpoint (finite source)
    played: bool = False

    @property
    def frames(self) -> int:
        return len(self.pcm) // CHANNELS


@dataclass
class _EndpointSession:
    endpoint: StreamEndpoint
    started_at: float = 0.0  # _clock() when its timeline began
    # fixed for the session's whole life; None = Stream Gen 1
    identity: Optional[StreamSessionIdentity] = None
    cursors: dict[str, int] = field(default_factory=dict)
    # frames_sent when each source joined (re-splice: sources that joined after
    # the rebuilt point start over)
    joined: dict[str, int] = field(default_factory=dict)
    offset: int = 0
    frames_sent: int = 0
    empty_since: Optional[float] = None
    # (L, R) channel gains each source was last mixed with: a gain / pan change
    # is ramped across the next chunk instead of stepping at its boundary
    gains: dict[str, tuple[float, float]] = field(default_factory=dict)
    limit: float = 1.0
    mono: bool = False
    data_blocks_sent: int = 0
    last_begin_at: float = 0.0
    last_data_at: Optional[float] = None


class StreamPlayback:
    """Handle for one logical source registered with a :class:`StreamHub`."""

    def __init__(
        self,
        hub: "StreamHub",
        source_id: str,
        *,
        gain: float,
        clip_gain: float = 1.0,
        event_gain: float = 1.0,
        pan: float,
        loop: bool,
    ) -> None:
        self._hub = hub
        self.id = source_id
        self._clip_gain = clip_gain
        self._event_gain = event_gain
        self._status = PlaybackStatus.DEFERRED
        self._deferred_reason: Optional[DeferredReason] = (
            DeferredReason.NO_RESOLVED_ENDPOINT
        )
        self._gain = gain
        self._pan = pan
        self._loop = loop

    @property
    def status(self) -> PlaybackStatus:
        return self._status

    @property
    def deferred_reason(self) -> Optional[DeferredReason]:
        """Why a Deferred Playback is waiting (None unless Deferred)."""
        return self._deferred_reason

    @property
    def gain(self) -> float:
        """The call's gain (0 or more, no upper limit). The source PCM is
        scaled by ``clamp(clip_gain × event_gain × gain, 0, 1)`` (contracts
        kit-format.md "Strength formula"), so a later change never replaces the
        clip's clip_gain or the event's own gain."""
        return self._gain

    @gain.setter
    def gain(self, value: float) -> None:
        self._gain = max(0.0, float(value))
        self._hub._set_source_property(
            self.id, "gain",
            source_gain(self._clip_gain, self._event_gain, self._gain),
        )

    @property
    def clip_gain(self) -> float:
        """Material strength of the source (the kit manifest ``clip_gain``,
        0..1; 1 for a source without a kit)."""
        return self._clip_gain

    @property
    def event_gain(self) -> float:
        """The event's own app-side gain (haptic file ``gain``; 1 for a
        source without one). Fixed for the source's life."""
        return self._event_gain

    @property
    def pan(self) -> float:
        return self._pan

    @pan.setter
    def pan(self, value: float) -> None:
        self._pan = _clamp(float(value), -1.0, 1.0)
        self._hub._set_source_property(self.id, "pan", self._pan)

    @property
    def loop(self) -> bool:
        return self._loop

    @loop.setter
    def loop(self, value: bool) -> None:
        self._loop = bool(value)
        self._hub._set_source_property(self.id, "loop", self._loop)

    def stop(self) -> None:
        """Stop only this source. Repeated calls are harmless."""
        self._hub.stop(self.id)

    def _set_state(
        self,
        status: PlaybackStatus,
        reason: Optional[DeferredReason] = None,
    ) -> None:
        self._status = status
        self._deferred_reason = reason


class StreamHub:
    """Own all logical sources and endpoint wire sessions for one SDK client."""

    def __init__(
        self,
        sink: EndpointStreamSink,
        endpoint_provider: Callable[[], list[StreamEndpoint]],
        *,
        send_ahead_sec: float = DEFAULT_SEND_AHEAD,
        mode_provider: Optional[
            Callable[[StreamEndpoint], EndpointModeResult]
        ] = None,
        endpoint_wanted: Optional[Callable[[StreamEndpoint], None]] = None,
        on_generation_exhausted: Optional[Callable[[], None]] = None,
    ) -> None:
        """``mode_provider`` returns each endpoint's current stream mode, or
        the DeferredReason it may not be streamed for (None = class unknown):
        its sources stay Deferred. Without one every endpoint is streamed in
        LEGACY_STREAM_MODE.

        ``endpoint_wanted`` is called (under the hub lock, on every reconcile)
        for each endpoint that has a session or matches a source's target: the
        endpoints whose stream lease the client should hold.
        ``on_generation_exhausted`` is called once when a lease has no
        generation left; the client then needs a new incarnation (new ticket).
        Neither may call back into the hub."""
        self._sink = sink
        self._endpoint_provider = endpoint_provider
        self._mode_provider = mode_provider or (lambda _endpoint: LEGACY_STREAM_MODE)
        self._endpoint_wanted = endpoint_wanted or (lambda _endpoint: None)
        self._on_generation_exhausted = on_generation_exhausted or (lambda: None)
        self._send_ahead = max(0.0, float(send_ahead_sec))
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._closed = False
        self._sources: dict[str, _Source] = {}
        self._sessions: dict[StreamEndpoint, _EndpointSession] = {}
        # keyed per endpoint (ip, port, address): another device sharing the
        # address or IP is not delayed
        self._last_legacy_end_at: dict[StreamEndpoint, float] = {}
        # last generation handed out per lease; never reused
        self._last_generation: dict[StreamLease, int] = {}
        self._exhausted_leases: set[StreamLease] = set()
        # identity of the session each endpoint ended last (None: Stream Gen 1):
        # an ERROR about it is still ours to handle
        self._last_ended: dict[StreamEndpoint, Optional[StreamSessionIdentity]] = {}
        # cursors of a Stream Gen 2 session that ended because its lease became
        # unavailable: (boot_id, cursors, ended_at). The next session under a
        # lease of the same boot continues them (Sender requirements 8).
        self._carried: dict[StreamEndpoint, tuple[int, dict[str, int], float]] = {}
        self._thread: Optional[threading.Thread] = None

    def play_pcm(
        self,
        pcm: bytes,
        *,
        sample_rate: int = SAMPLE_RATE,
        channels: int = 1,
        gain: float = 1.0,
        clip_gain: float = 1.0,
        event_gain: float = 1.0,
        pan: float = 0.0,
        loop: bool = False,
        target: str = "",
        key: Optional[str] = None,
    ) -> StreamPlayback:
        """Register a finite or looping PCM16 source and return its handle.

        ``gain`` is the call's gain and ``event_gain`` the event's own gain
        (haptic file), both 0 or more with no upper limit (their product is
        the app gain); ``clip_gain`` is the material strength (0..1, default
        1 = no kit). The source PCM is scaled by
        ``clamp(clip_gain × event_gain × gain, 0, 1)``; the playback's ``gain``
        changes only the call's factor.
        """
        normalized = _normalize_pcm16(pcm, sample_rate=sample_rate, channels=channels)
        if not normalized:
            raise ValueError("pcm must contain at least one complete frame")
        source_id = uuid.uuid4().hex
        gain_value = max(0.0, float(gain))
        clip_gain_value = _clamp(float(clip_gain), 0.0, 1.0)
        event_gain_value = max(0.0, float(event_gain))
        pan_value = _clamp(float(pan), -1.0, 1.0)
        loop_value = bool(loop)
        playback = StreamPlayback(
            self,
            source_id,
            gain=gain_value,
            clip_gain=clip_gain_value,
            event_gain=event_gain_value,
            pan=pan_value,
            loop=loop_value,
        )
        source = _Source(
            id=source_id,
            pcm=normalized,
            target=target,
            gain=source_gain(clip_gain_value, event_gain_value, gain_value),
            pan=pan_value,
            loop=loop_value,
            key=key,
            playback=playback,
        )
        with self._lock:
            if self._closed:
                raise RuntimeError("StreamHub is closed")
            self._sources[source_id] = source
            self._reconcile_locked()
            self._resplice_locked(source_id)
            self._ensure_thread_locked()
        self._wake.set()
        return playback

    def stop(self, source_id: str) -> None:
        with self._lock:
            source = self._sources.pop(source_id, None)
            if source is None:
                return
            source.playback._set_state(PlaybackStatus.STOPPED)
            self._remove_source_from_sessions_locked(source_id)
        self._wake.set()

    def stop_key(self, key: str, *, target: Optional[str] = None) -> int:
        with self._lock:
            ids = [
                source.id
                for source in self._sources.values()
                if source.key == key and (target is None or source.target == target)
            ]
        for source_id in ids:
            self.stop(source_id)
        return len(ids)

    def stop_all(self) -> None:
        with self._lock:
            ids = list(self._sources)
        for source_id in ids:
            self.stop(source_id)

    def stop_target(self, target: str) -> int:
        with self._lock:
            ids = [
                source.id for source in self._sources.values()
                if source.target == target
            ]
        for source_id in ids:
            self.stop(source_id)
        return len(ids)

    def open(self) -> None:
        """Allow a client that was closed to be opened and streamed again."""
        with self._lock:
            if not self._closed:
                return
            self._closed = False
            self._thread = None

    def reconcile(self) -> None:
        """Apply the latest PONG/TTL endpoint set without crossing routes."""
        with self._lock:
            if not self._closed:
                self._reconcile_locked()
        self._wake.set()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            for source in self._sources.values():
                source.playback._set_state(PlaybackStatus.STOPPED)
            self._sources.clear()
            for session in list(self._sessions.values()):
                self._end_session_locked(session)
            self._sessions.clear()
            self._carried.clear()
            thread = self._thread
        self._wake.set()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1.0)

    @property
    def active_playbacks(self) -> tuple[StreamPlayback, ...]:
        with self._lock:
            return tuple(source.playback for source in self._sources.values())

    # ── ERROR handling hooks (stream-session.md Sender requirements 5) ──
    def tracked_envelopes(self, ip: str) -> set[bytes]:
        """Envelopes of the sessions an ERROR from ``ip`` may be about: each
        endpoint's current Stream Gen 2 session and the one it ended last."""
        with self._lock:
            out = {
                session.identity.envelope
                for endpoint, session in self._sessions.items()
                if endpoint.ip == ip and session.identity is not None
            }
            out.update(
                identity.envelope
                for endpoint, identity in self._last_ended.items()
                if endpoint.ip == ip and identity is not None
            )
            return out

    def had_gen1_session(self, ip: str) -> bool:
        """Whether ``ip`` has, or just ended, a Stream Gen 1 session."""
        with self._lock:
            return any(
                endpoint.ip == ip and session.identity is None
                for endpoint, session in self._sessions.items()
            ) or any(
                endpoint.ip == ip and identity is None
                for endpoint, identity in self._last_ended.items()
            )

    def resend_begin(self, ip: str, envelope: bytes) -> bool:
        """STREAM_NOT_STARTED: send the current session's BEGIN again now."""
        with self._lock:
            session = self._current_session_locked(ip, envelope)
            if session is None:
                return False
            self._send_begin_locked(session, _clock())
            return True

    def restart_session(self, ip: str, envelope: bytes) -> bool:
        """STREAM_TIMED_OUT: continue the current session's sources under the
        next generation of the same lease. Nothing if none are left."""
        with self._lock:
            session = self._current_session_locked(ip, envelope)
            if session is None or not session.cursors:
                return False
            assert session.identity is not None
            restarted = self._restart_session_locked(session, session.identity.lease)
        self._wake.set()
        return restarted is not None

    def exclude_sources(self, ip: str, envelope: bytes) -> int:
        """STREAM_STOPPED: take the current session's sources off that endpoint
        for good; they keep playing elsewhere (sdk-multi-stream.md §6.13)."""
        with self._lock:
            session = self._current_session_locked(ip, envelope)
            if session is None:
                return 0
            key = (session.endpoint.ip, session.endpoint.port)
            excluded = 0
            for source_id in tuple(session.cursors):
                source = self._sources.get(source_id)
                if source is not None:
                    source.stopped_endpoints.add(key)
                    if not source.loop:
                        source.played = True  # counts as played there
                    excluded += 1
            session.cursors.clear()
            session.joined.clear()
            session.gains.clear()
            session.empty_since = session.empty_since or _clock()
            if not self._closed:
                self._reconcile_locked()
        self._wake.set()
        return excluded

    def end_endpoint(self, ip: str, envelope: bytes) -> bool:
        """STREAM_TARGET_MISMATCH: end the current session (END now). Its
        sources start over when the endpoint resolves again."""
        with self._lock:
            session = self._current_session_locked(ip, envelope)
            if session is None:
                return False
            self._sessions.pop(session.endpoint, None)
            self._end_session_locked(session)
            self._carried.pop(session.endpoint, None)
        self._wake.set()
        return True

    def forget_carried(self) -> None:
        """Drop kept cursors: the next sessions start their sources over (an
        explicit ownership reacquisition is a new start)."""
        with self._lock:
            self._carried.clear()

    def _current_session_locked(
        self, ip: str, envelope: bytes
    ) -> Optional[_EndpointSession]:
        for endpoint, session in self._sessions.items():
            if (
                endpoint.ip == ip
                and session.identity is not None
                and session.identity.envelope == envelope
            ):
                return session
        return None

    # ── internals ───────────────────────────────────────────────────
    def _ensure_thread_locked(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._run, name="hapbeat-stream-hub", daemon=True
        )
        self._thread.start()

    def _run(self) -> None:
        while True:
            with self._lock:
                if self._closed:
                    return
                self._reconcile_locked()
                self._pump_locked()
                if not self._sources and not self._sessions:
                    self._thread = None
                    return
                wait = 0.005 if self._sessions else 0.25
            self._wake.wait(wait)
            self._wake.clear()

    def _reconcile_locked(self) -> None:
        now = _clock()
        cutoff = now - LEGACY_END_TO_BEGIN_GUARD * 2
        self._last_legacy_end_at = {
            key: ended_at for key, ended_at in self._last_legacy_end_at.items()
            if ended_at >= cutoff
        }
        endpoints = {
            endpoint
            for endpoint in self._endpoint_provider()
            if endpoint.ip and endpoint.address
        }
        modes = {endpoint: _mode_of(self._mode_provider(endpoint)) for endpoint in endpoints}

        # Migrate an established session when either its exact device address or
        # its IP still identifies one unambiguous new endpoint. Route-only
        # changes continue DATA with the existing offset: END(old)->BEGIN(new)
        # can reorder across an AP's DTIM buffer and kill the new session.
        # Only under the identity the session began with (same lease, or
        # Stream Gen 1 on both sides); otherwise the old session is forgotten
        # like a vanished endpoint.
        missing = [endpoint for endpoint in self._sessions if endpoint not in endpoints]
        unclaimed = set(endpoints) - set(self._sessions)
        for old_endpoint in missing:
            candidates = [
                endpoint for endpoint in unclaimed
                if endpoint.address == old_endpoint.address
                or endpoint.ip == old_endpoint.ip
            ]
            if len(candidates) != 1:
                continue
            new_endpoint = candidates[0]
            if not _session_current(self._sessions[old_endpoint], modes[new_endpoint]):
                continue
            session = self._sessions.pop(old_endpoint)
            session.endpoint = new_endpoint
            self._sessions[new_endpoint] = session
            unclaimed.remove(new_endpoint)
            for source in self._sources.values():
                if old_endpoint in source.completed_endpoints:
                    source.completed_endpoints.remove(old_endpoint)
                    source.completed_endpoints.add(new_endpoint)

        # A genuinely vanished endpoint is forgotten without END. If it later
        # returns as a new endpoint, finite sources start there from frame zero.
        for endpoint in tuple(self._sessions):
            if endpoint not in endpoints:
                self._sessions.pop(endpoint, None)
                for source in self._sources.values():
                    source.completed_endpoints.discard(endpoint)

        # A matched PONG or an ERROR may have changed an endpoint's class
        # (Gen 1 <-> Gen 2), its lease (device reboot / lease renewal / new
        # incarnation) or made it unusable (invalid / superseded). End the
        # running session NOW, in the format and identity it began with (no
        # linger), before a session in the new mode can start. A new ticket of
        # the same boot continues the sources from their cursors; a lease that
        # became unavailable keeps them for the next lease of that boot. A
        # superseded endpoint is not re-taken.
        for endpoint, session in tuple(self._sessions.items()):
            mode = modes[endpoint]
            if _session_current(session, mode):
                session.mono = mode.mono
                continue
            if (
                session.identity is not None
                and isinstance(mode, EndpointStreamMode)
                and mode.lease is not None
                and mode.lease.boot_id == session.identity.lease.boot_id
            ):
                restarted = self._restart_session_locked(session, mode.lease)
                if restarted is not None:
                    restarted.mono = mode.mono
                continue
            self._sessions.pop(endpoint)
            self._end_session_locked(session)
            if (
                session.identity is not None
                and mode is DeferredReason.LEASE_UNAVAILABLE
                and session.cursors
            ):
                self._carried[endpoint] = (
                    session.identity.lease.boot_id, dict(session.cursors), now
                )
        self._carried = {
            endpoint: carried for endpoint, carried in self._carried.items()
            if endpoint in endpoints and now - carried[2] <= LEASE_TTL
        }

        for session in self._sessions.values():
            for source_id in tuple(session.cursors):
                source = self._sources.get(source_id)
                if source is None or not protocol.address_matches(
                    source.target, session.endpoint.address
                ):
                    session.cursors.pop(source_id, None)
                    session.joined.pop(source_id, None)
            if not session.cursors:
                session.empty_since = session.empty_since or now

        wanted = set(self._sessions)
        finished_loops: list[str] = []
        for source in self._sources.values():
            matching = [
                endpoint for endpoint in endpoints
                if protocol.address_matches(source.target, endpoint.address)
            ]
            candidates = [
                endpoint for endpoint in matching
                if (endpoint.ip, endpoint.port) not in source.stopped_endpoints
            ]
            wanted.update(candidates)
            source.waiting_endpoints.clear()
            reasons: set[DeferredReason] = set()
            guarded = False
            for endpoint in candidates:
                mode = modes[endpoint]
                if not isinstance(mode, EndpointStreamMode):
                    reasons.add(mode)
                    carried = self._carried.get(endpoint)
                    if carried is not None and source.id in carried[1]:
                        source.waiting_endpoints.add(endpoint)
                    continue
                if endpoint in source.completed_endpoints:
                    continue
                session = self._sessions.get(endpoint)
                if session is None:
                    session = self._begin_session_locked(endpoint, mode)
                    if session is None:
                        source.waiting_endpoints.add(endpoint)
                        if mode.lease is None:
                            guarded = True  # Stream Gen 1 guard: Active (§3)
                        else:
                            reasons.add(DeferredReason.LEASE_UNAVAILABLE)
                        continue
                if source.id not in session.cursors:
                    if session.empty_since is not None:
                        session.started_at = (
                            _clock() - session.frames_sent / SAMPLE_RATE
                        )
                        session.empty_since = None
                    session.cursors[source.id] = 0
                    session.joined[source.id] = session.frames_sent
            assigned = any(
                source.id in session.cursors for session in self._sessions.values()
            )
            if assigned or guarded:
                source.playback._set_state(PlaybackStatus.ACTIVE)
            elif matching and not candidates:
                # STOP_ALL took it off every endpoint it matches
                if source.loop:
                    finished_loops.append(source.id)
                else:
                    source.played = True
            elif not candidates:
                source.playback._set_state(
                    PlaybackStatus.DEFERRED, DeferredReason.NO_RESOLVED_ENDPOINT
                )
            else:
                reason = next(
                    (r for r in _REASON_PRIORITY if r in reasons),
                    DeferredReason.LEASE_UNAVAILABLE,
                )
                source.playback._set_state(PlaybackStatus.DEFERRED, reason)
        for source_id in finished_loops:
            source = self._sources.pop(source_id)
            source.playback._set_state(PlaybackStatus.STOPPED)

        for endpoint in wanted:
            self._endpoint_wanted(endpoint)

        # A pending (guarded) endpoint that expired or stopped matching left
        # waiting_endpoints above: a finite source already played elsewhere
        # completes now, not on that endpoint's next pump (§6.10).
        for source_id, source in tuple(self._sources.items()):
            if source.played and not source.loop:
                self._complete_if_unreferenced_locked(source_id)

    def _begin_session_locked(
        self, endpoint: StreamEndpoint, mode: EndpointStreamMode
    ) -> Optional[_EndpointSession]:
        """Open an endpoint session in ``mode``, or None while it must wait
        (Stream Gen 1 guard after a Stream Gen 1 END, or generations
        exhausted)."""
        if mode.lease is None:
            if self._legacy_guard_locked(endpoint):
                return None
            identity = None
        else:
            identity = self._allocate_identity_locked(mode.lease)
            if identity is None:
                return None
        now = _clock()
        session = _EndpointSession(
            endpoint=endpoint, started_at=now, identity=identity, mono=mode.mono
        )
        carried = self._carried.pop(endpoint, None)
        if (
            identity is not None
            and carried is not None
            and carried[0] == identity.lease.boot_id
            and now - carried[2] <= LEASE_TTL
        ):
            key = (endpoint.ip, endpoint.port)
            for source_id, cursor in carried[1].items():
                source = self._sources.get(source_id)
                if (
                    source is not None
                    and key not in source.stopped_endpoints
                    and endpoint not in source.completed_endpoints
                    and protocol.address_matches(source.target, endpoint.address)
                ):
                    session.cursors[source_id] = cursor
                    session.joined[source_id] = 0
        self._sessions[endpoint] = session
        self._send_begin_locked(session, now)
        return session

    def _restart_session_locked(
        self, session: _EndpointSession, lease: StreamLease
    ) -> Optional[_EndpointSession]:
        """End ``session`` (END under its own identity) and continue its
        sources from their cursors under the next generation of ``lease``.
        None when ``lease`` has no generation left (the cursors are kept for a
        session under a later lease of the same boot)."""
        identity = self._allocate_identity_locked(lease)
        self._sessions.pop(session.endpoint, None)
        self._end_session_locked(session)
        now = _clock()
        if identity is None:
            if session.cursors:
                self._carried[session.endpoint] = (
                    lease.boot_id, dict(session.cursors), now
                )
            return None
        restarted = _EndpointSession(
            endpoint=session.endpoint,
            started_at=now,
            identity=identity,
            cursors=dict(session.cursors),
            joined={source_id: 0 for source_id in session.cursors},
            gains=dict(session.gains),
            limit=session.limit,
            mono=session.mono,
            empty_since=None if session.cursors else now,
        )
        self._sessions[session.endpoint] = restarted
        self._send_begin_locked(restarted, now)
        return restarted

    def _resplice_locked(self, source_id: str) -> None:
        """Bring a just-added source in at once on Stream Gen 2 sessions that
        already sent its start ahead (sdk-multi-stream.md §6.14): the other
        sources go back to now, the session ENDs its generation and BEGINs the
        next one of the same lease, and the rebuilt mix follows. Elsewhere the
        source joins at the next unsent block. One-shots that ended inside
        the re-sent span are not sent again."""
        now = _clock()
        for endpoint, session in tuple(self._sessions.items()):
            if (
                session.identity is None
                or session.data_blocks_sent == 0
                or session.cursors.get(source_id) != 0
                or session.joined.get(source_id) != session.frames_sent
            ):
                continue
            played = max(0, int((now - session.started_at) * SAMPLE_RATE))
            back = session.frames_sent - played
            if back < RESPLICE_MIN_QUEUED * SAMPLE_RATE:
                continue
            identity = self._allocate_identity_locked(session.identity.lease)
            if identity is None:
                continue
            position = played
            cursors: dict[str, int] = {}
            for other_id, cursor in session.cursors.items():
                other = self._sources.get(other_id)
                if (
                    other is None
                    or other_id == source_id
                    or session.joined.get(other_id, 0) >= position
                ):
                    cursors[other_id] = 0  # starts at the rebuilt point
                    continue
                rewound = cursor - back
                if rewound < 0:
                    rewound = rewound % other.frames if other.loop and other.frames else 0
                cursors[other_id] = rewound
            self._sessions.pop(endpoint)
            self._end_session_locked(session)
            rebuilt = _EndpointSession(
                endpoint=endpoint,
                started_at=now,
                identity=identity,
                cursors=cursors,
                joined={other_id: 0 for other_id in cursors},
                gains=dict(session.gains),
                limit=session.limit,
                mono=session.mono,
            )
            self._sessions[endpoint] = rebuilt
            self._send_begin_locked(rebuilt, now)

    def _allocate_identity_locked(
        self, lease: StreamLease
    ) -> Optional[StreamSessionIdentity]:
        """Next generation under ``lease``, monotonic and never reused. When
        none is left the client is told once to obtain a new ticket."""
        last = self._last_generation.get(lease, 0)
        if last >= MAX_GENERATION:
            if lease not in self._exhausted_leases:
                self._exhausted_leases.add(lease)
                logger.warning(
                    "stream generation exhausted for lease %s; requesting a new lease",
                    lease,
                )
                self._on_generation_exhausted()
            return None
        generation = last + 1
        self._last_generation[lease] = generation
        return StreamSessionIdentity(lease, generation)

    def _send_begin_locked(self, session: _EndpointSession, now: float) -> None:
        """BEGIN to the endpoint's current address (it may have moved since the
        session began)."""
        session.last_begin_at = now
        if session.identity is None:
            self._sink.stream_begin_endpoint(session.endpoint)
        else:
            self._sink.stream_begin_v2(session.endpoint, session.identity)

    def _end_session_locked(self, session: _EndpointSession) -> None:
        """Send END in the session's own format and identity."""
        self._last_ended[session.endpoint] = session.identity
        if session.identity is None:
            self._sink.stream_end_endpoint(session.endpoint)
            self._record_legacy_end_locked(session.endpoint)
        else:
            self._sink.stream_end_v2(session.endpoint, session.identity)

    def _pump_locked(self) -> None:
        now = _clock()
        for endpoint, session in list(self._sessions.items()):
            if session.identity is not None:
                if (
                    session.cursors
                    and session.last_data_at is not None
                    and now - session.last_data_at >= OWN_GAP_RESTART
                ):
                    # we could not send for a while: the device may have timed
                    # the generation out, so continue under the next one
                    restarted = self._restart_session_locked(
                        session, session.identity.lease
                    )
                    if restarted is None:
                        continue
                    session = restarted
                if now - session.last_begin_at >= BEGIN_REPEAT_INTERVAL:
                    self._send_begin_locked(session, now)
            wanted = int((now - session.started_at + self._send_ahead) * SAMPLE_RATE)
            while session.cursors and session.frames_sent < wanted:
                frames = self._chunk_frames_locked(session)
                if frames <= 0:
                    break
                data, finished = self._mix_locked(session, frames)
                if session.identity is None:
                    self._sink.stream_data_endpoint(endpoint, session.offset, data)
                else:
                    if session.data_blocks_sent < BEGIN_REPEAT_COUNT:
                        self._send_begin_locked(session, now)
                    self._sink.stream_data_v2(
                        endpoint, session.identity, session.offset, data
                    )
                session.data_blocks_sent += 1
                session.last_data_at = now
                session.offset += len(data)
                session.frames_sent += frames
                for source_id in finished:
                    source = self._sources.get(source_id)
                    if source is not None:
                        source.completed_endpoints.add(endpoint)
                        source.played = True
                    session.cursors.pop(source_id, None)
                    session.joined.pop(source_id, None)
                    self._complete_if_unreferenced_locked(source_id)

            if not session.cursors:
                if session.empty_since is None:
                    session.empty_since = now
                elif now - session.empty_since >= SESSION_END_LINGER:
                    self._sessions.pop(endpoint, None)
                    self._end_session_locked(session)

    def _chunk_frames_locked(self, session: _EndpointSession) -> int:
        maximum = 0
        for source_id, cursor in session.cursors.items():
            source = self._sources.get(source_id)
            if source is None:
                continue
            if source.loop and source.frames:
                return CHUNK_FRAMES
            maximum = max(maximum, source.frames - cursor)
        return min(CHUNK_FRAMES, max(0, maximum))

    def _mix_locked(
        self, session: _EndpointSession, frames: int
    ) -> tuple[bytes, set[str]]:
        mixed = [0.0] * (frames * CHANNELS)
        finished: set[str] = set()
        for source_id, initial_cursor in list(session.cursors.items()):
            source = self._sources.get(source_id)
            if source is None or source.frames == 0:
                finished.add(source_id)
                continue
            # one haptic output: the device averages L and R, so a panned
            # source would only get weaker (sdk-multi-stream.md §6.15)
            pan = 0.0 if session.mono else source.pan
            left_gain, right_gain = _channel_gains(source.gain, pan)
            # a new source starts at its own gains (its PCM carries the onset);
            # a changed gain / pan ramps linearly over this chunk (no zipper)
            left0, right0 = session.gains.get(source_id, (left_gain, right_gain))
            session.gains[source_id] = (left_gain, right_gain)
            step_l = (left_gain - left0) / frames
            step_r = (right_gain - right0) / frames
            ramp = step_l != 0.0 or step_r != 0.0
            pcm = source.pcm
            cursor = initial_cursor
            for frame in range(frames):
                if cursor >= source.frames:
                    if source.loop:
                        cursor = 0
                    else:
                        finished.add(source_id)
                        break
                base = cursor * CHANNELS
                if ramp:
                    gl = left0 + step_l * (frame + 1)
                    gr = right0 + step_r * (frame + 1)
                else:
                    gl, gr = left_gain, right_gain
                mixed[frame * CHANNELS] += pcm[base] * gl
                mixed[frame * CHANNELS + 1] += pcm[base + 1] * gr
                cursor += 1
            session.cursors[source_id] = cursor
            if not source.loop and cursor >= source.frames:
                finished.add(source_id)
        for source_id in tuple(session.gains):
            if source_id in finished or source_id not in session.cursors:
                del session.gains[source_id]

        _limit(session, mixed, frames)
        samples = (_saturate_pcm16(value) for value in mixed)
        return struct.pack(f"<{frames * CHANNELS}h", *samples), finished

    def _remove_source_from_sessions_locked(self, source_id: str) -> None:
        now = _clock()
        for session in self._sessions.values():
            session.cursors.pop(source_id, None)
            session.joined.pop(source_id, None)
            if not session.cursors:
                session.empty_since = session.empty_since or now

    def _complete_if_unreferenced_locked(self, source_id: str) -> None:
        if any(source_id in session.cursors for session in self._sessions.values()):
            return
        source = self._sources.get(source_id)
        if source is not None and source.waiting_endpoints:
            return
        if source_id in self._sources:
            source = self._sources.pop(source_id)
            source.playback._set_state(PlaybackStatus.STOPPED)

    def _set_source_property(self, source_id: str, name: str, value) -> None:
        with self._lock:
            source = self._sources.get(source_id)
            if source is not None:
                setattr(source, name, value)

    def _record_legacy_end_locked(self, endpoint: StreamEndpoint) -> None:
        self._last_legacy_end_at[endpoint] = _clock()

    def _legacy_guard_locked(self, endpoint: StreamEndpoint) -> bool:
        """True while a Stream Gen 1 END to this endpoint is too recent for
        BEGIN."""
        latest = self._last_legacy_end_at.get(endpoint)
        return latest is not None and _clock() - latest < LEGACY_END_TO_BEGIN_GUARD


def _mode_of(result: EndpointModeResult) -> Union[EndpointStreamMode, DeferredReason]:
    return DeferredReason.UNKNOWN_CLASS if result is None else result


def _session_current(
    session: _EndpointSession, mode: Union[EndpointStreamMode, DeferredReason, None]
) -> bool:
    """Whether ``session`` may keep streaming to an endpoint now in ``mode``:
    Stream Gen 1 stays Gen 1; Gen 2 needs the lease it began under to still be
    the endpoint's usable lease."""
    if not isinstance(mode, EndpointStreamMode):
        return False
    if session.identity is None:
        return mode.lease is None
    return mode.lease == session.identity.lease


def _limit(session: _EndpointSession, mixed: list[float], frames: int) -> None:
    """Keep the summed sources in range without a hard clamp: past
    LIMIT_THRESHOLD, a gain reduction to LIMIT_TARGET / peak (attack within the chunk, release
    by LIMIT_RELEASE_PER_CHUNK per chunk), ramped per sample, then a soft knee."""
    peak = max((abs(v) for v in mixed), default=0.0)
    want = LIMIT_TARGET / peak if peak > LIMIT_THRESHOLD else 1.0
    start = session.limit
    end = want if want < start else min(want, start + LIMIT_RELEASE_PER_CHUNK)
    session.limit = end
    # an idle limiter passes a block within LIMIT_THRESHOLD bit-identically
    # (no knee; +32768 saturates to 32767 afterwards); during release
    # (coefficient < 1) the knee still shapes the tail
    if start == 1.0 and end == 1.0 and peak <= LIMIT_THRESHOLD:
        return
    step = (end - start) / frames
    span = FULL_SCALE - LIMIT_KNEE
    for frame in range(frames):
        g = start + step * (frame + 1)
        for ch in range(CHANNELS):
            i = frame * CHANNELS + ch
            v = mixed[i] * g
            a = abs(v)
            if a > LIMIT_KNEE:
                v = math.copysign(LIMIT_KNEE + span * math.tanh((a - LIMIT_KNEE) / span), v)
            mixed[i] = v


def _normalize_pcm16(pcm: bytes, *, sample_rate: int, channels: int) -> tuple[int, ...]:
    if sample_rate <= 0:
        raise ValueError("sample_rate must be positive")
    if channels not in (1, 2):
        raise ValueError("channels must be 1 or 2")
    bytes_per_frame = channels * 2
    if len(pcm) % bytes_per_frame:
        raise ValueError("PCM16 data must contain complete frames")
    if not pcm:
        return ()
    if sample_rate == SAMPLE_RATE:
        # no resampling: C-level copies (the per-sample loop below took ~210 ms
        # for 8.5 s of stereo, holding the GIL and starving the mixer thread)
        values = array("h")
        values.frombytes(pcm)
        if sys.byteorder != "little":
            values.byteswap()
        if channels == 2:
            return tuple(values)
        stereo = array("h", bytes(4 * len(values)))
        stereo[0::2] = values
        stereo[1::2] = values
        return tuple(stereo)
    values = [item[0] for item in struct.iter_unpack("<h", pcm)]
    input_frames = len(values) // channels
    output_frames = max(1, round(input_frames * SAMPLE_RATE / sample_rate))
    output: list[int] = []
    for output_index in range(output_frames):
        position = output_index * sample_rate / SAMPLE_RATE
        left_index = min(input_frames - 1, int(math.floor(position)))
        right_index = min(input_frames - 1, left_index + 1)
        fraction = position - left_index
        if channels == 1:
            value = round(values[left_index] * (1.0 - fraction) + values[right_index] * fraction)
            output.extend((value, value))
        else:
            for channel in range(CHANNELS):
                a = values[left_index * CHANNELS + channel]
                b = values[right_index * CHANNELS + channel]
                output.append(round(a * (1.0 - fraction) + b * fraction))
    return tuple(output)


def _channel_gains(gain: float, pan: float) -> tuple[float, float]:
    return (
        gain * (1.0 if pan <= 0.0 else 1.0 - pan),
        gain * (1.0 if pan >= 0.0 else 1.0 + pan),
    )


def _saturate_pcm16(value: float) -> int:
    return max(-32768, min(32767, round(value)))


def _clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))


def source_gain(*factors: float) -> float:
    """The strength sent for these factors (clip_gain, event gain, call gain):
    ``clamp(product, 0, 1)``. Negative factors count as 0; a product that is
    NaN or infinite is 0 (contracts kit-format.md "Strength formula")."""
    value = 1.0
    for factor in factors:
        value *= max(0.0, float(factor))
    if not math.isfinite(value):
        return 0.0
    return min(1.0, value)
