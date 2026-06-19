"""ClipStreamer — pace a PCM16 clip out as STREAM_BEGIN / STREAM_DATA* / END.

The device ring buffer holds only ~256 ms (4096 frames @ 16 kHz), so a whole
clip cannot be burst at once. Data is sent in frame-aligned chunks, each
scheduled ~``send_ahead_sec`` before its playback time, so the device buffer
never overflows. STREAM_END is deferred until the clip has fully drained.

A stream is session-level (one at a time): starting a new clip cancels the
previous one, matching the "1 session = 1 stream" rule in the protocol. Pacing
runs on a daemon thread; ``stop()`` cancels it and sends STREAM_END exactly
once. This mirrors the web SDK's ClipStreamer (TypeScript) one-for-one.
"""

from __future__ import annotations

import threading
import time
from typing import Protocol

DEFAULT_SEND_AHEAD = 0.15  # seconds (< 0.256 s device ring)
MAX_DATA_BYTES = 1024      # per STREAM_DATA payload (well under the 1472 cap)
_END_DRAIN_PAD = 0.08      # extra wait after last frame's playback before END


class StreamSink(Protocol):
    """The subset of the transport a ClipStreamer needs."""

    def stream_begin(self, *, sample_rate: int, channels: int,
                     total_samples: int, gain: float, target: str) -> None: ...

    def stream_data(self, offset: int, data: bytes) -> None: ...

    def stream_end(self) -> None: ...


class _Session:
    __slots__ = ("stop", "ended")

    def __init__(self) -> None:
        self.stop = threading.Event()
        self.ended = False


class ClipStreamer:
    def __init__(self, sink: StreamSink, *, send_ahead_sec: float = DEFAULT_SEND_AHEAD) -> None:
        self._sink = sink
        self._send_ahead = send_ahead_sec
        self._lock = threading.Lock()
        self._active: _Session | None = None
        self._thread: threading.Thread | None = None

    # ── Public API ──────────────────────────────────────────────────
    def play(self, pcm: bytes, *, sample_rate: int, channels: int,
             gain: float = 1.0, target: str = "") -> None:
        """Stream a PCM16 byte buffer, real-time paced. Cancels any active clip."""
        self.stop()
        channels = max(1, channels)
        sample_rate = sample_rate or 16000
        bytes_per_frame = channels * 2
        chunk_bytes = max(bytes_per_frame, MAX_DATA_BYTES - (MAX_DATA_BYTES % bytes_per_frame))
        total_frames = len(pcm) // bytes_per_frame

        session = _Session()
        with self._lock:
            self._active = session

        # STREAM_BEGIN goes out synchronously, before the pacing thread starts.
        self._sink.stream_begin(
            sample_rate=sample_rate, channels=channels,
            total_samples=total_frames, gain=gain, target=target,
        )

        def run() -> None:
            t0 = time.perf_counter()
            offset = 0
            n = len(pcm)
            while offset < n:
                if session.stop.is_set():
                    return  # stop() owns the END in this case
                played_sec = (offset / bytes_per_frame) / sample_rate
                wait = t0 + played_sec - self._send_ahead - time.perf_counter()
                if wait > 0 and session.stop.wait(wait):
                    return
                end = min(offset + chunk_bytes, n)
                self._sink.stream_data(offset, pcm[offset:end])
                offset = end
            # all data queued -- END after the clip has fully played out
            total_sec = total_frames / sample_rate
            end_wait = max(0.0, t0 + total_sec + _END_DRAIN_PAD - time.perf_counter())
            session.stop.wait(end_wait)
            self._end_session(session)

        thread = threading.Thread(target=run, name="hapbeat-clip", daemon=True)
        with self._lock:
            self._thread = thread
        thread.start()

    def stop(self) -> None:
        """Cancel the active clip (if any) and tell the device to stop."""
        with self._lock:
            session = self._active
            thread = self._thread
        if session is not None:
            self._end_session(session)
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1.0)

    def is_active(self) -> bool:
        with self._lock:
            return self._active is not None and not self._active.ended

    # ── Internals ───────────────────────────────────────────────────
    def _end_session(self, session: _Session) -> None:
        with self._lock:
            if session.ended:
                return
            session.ended = True
            if self._active is session:
                self._active = None
        session.stop.set()
        self._sink.stream_end()
