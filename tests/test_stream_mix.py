"""Mix-bus quality of StreamHub (deterministic: _mix_locked driven directly,
no thread, no sockets): gain / pan changes are ramped (no per-chunk zipper
steps), overlapping sources are limited instead of hard-clamped, late joins
start cleanly, and PCM normalisation without resampling is exact and fast."""

from __future__ import annotations

import math
import struct
import time

from hapbeat import stream
from hapbeat.stream import CHUNK_FRAMES, SAMPLE_RATE, StreamEndpoint, StreamHub


class NullSink:
    def stream_begin_endpoint(self, endpoint): pass
    def stream_data_endpoint(self, endpoint, offset, data): pass
    def stream_end_endpoint(self, endpoint): pass


ENDPOINT = StreamEndpoint("192.0.2.10", "player_1/chest")


def sine_pcm(freq: float, seconds: float, amp: float = 0.9) -> bytes:
    n = int(seconds * SAMPLE_RATE)
    vals = []
    for i in range(n):
        v = int(amp * 32767 * math.sin(2 * math.pi * freq * i / SAMPLE_RATE))
        vals += (v, v)
    return struct.pack(f"<{len(vals)}h", *vals)


def mixer(sources):
    """A hub (no endpoints: its thread never mixes) and a session holding
    `sources` [(pcm, gain, pan)]; returns (hub, session, playbacks)."""
    hub = StreamHub(NullSink(), lambda: [], send_ahead_sec=0.0)
    session = stream._EndpointSession(endpoint=ENDPOINT)
    pbs = []
    for pcm, gain, pan in sources:
        pb = hub.play_pcm(pcm, channels=2, gain=gain, pan=pan, target="player_1")
        session.cursors[pb.id] = 0
        pbs.append(pb)
    return hub, session, pbs


def mix(hub, session, chunks: int) -> list[int]:
    out: list[int] = []
    for _ in range(chunks):
        with hub._lock:
            data, _ = hub._mix_locked(session, CHUNK_FRAMES)
        out += struct.unpack(f"<{len(data) // 2}h", data)
    return out


def max_step(x: list[int]) -> int:
    return max(abs(b - a) for a, b in zip(x, x[1:]))


def test_gain_and_pan_changes_are_ramped_without_zipper_steps():
    # a constant (DC) source makes any gain step visible as a jump
    dc = struct.pack("<hh", 20000, 20000) * (SAMPLE_RATE // 2)
    hub, session, (pb,) = mixer([(dc, 1.0, 0.0)])
    try:
        before = mix(hub, session, 1)
        pb.gain = 0.2                     # at once this would be an 18000 step
        pb.pan = 0.5
        out = before[-2:] + mix(hub, session, 2)
        left = out[0::2]
        right = out[1::2]
        # balance law: L = g (1 - pan) = 0.1, R = g = 0.2; ramped over one chunk
        assert max_step(left) <= (20000 - 2000) / CHUNK_FRAMES + 2
        assert max_step(right) <= (20000 - 4000) / CHUNK_FRAMES + 2
        assert left[-1] == 2000 and right[-1] == 4000      # settled at the new gains
    finally:
        hub.close()


def test_new_source_starts_at_its_own_gain_without_a_ramp():
    dc = struct.pack("<hh", 10000, 10000) * 1024
    hub, session, _ = mixer([(dc, 0.5, 0.0)])
    try:
        out = mix(hub, session, 1)
        assert out[0] == 5000 and out[1] == 5000
    finally:
        hub.close()


def test_four_full_scale_sources_are_limited_not_hard_clipped():
    pcm = sine_pcm(140.0, 0.5, amp=1.0)
    hub, session, _ = mixer([(pcm, 1.0, 0.0)] * 4)
    try:
        out = mix(hub, session, 20)
        left = out[0::2]
        assert max(abs(v) for v in left) <= 32767
        settled = left[CHUNK_FRAMES * 2:]
        peak = max(abs(v) for v in settled)
        assert 0.85 * 32767 <= peak <= 0.95 * 32767        # limited to the target
        assert sum(1 for v in settled if abs(v) >= 32767) == 0
        # the waveform stays a sine (no flat clipped tops): smooth steps
        clean = peak * 2 * math.sin(math.pi * 140.0 / SAMPLE_RATE)
        assert max_step(settled) <= 1.1 * clean
    finally:
        hub.close()


def test_sum_within_full_scale_passes_unchanged():
    dc = struct.pack("<hh", 15000, 15000) * 2048
    hub, session, _ = mixer([(dc, 1.0, 0.0), (dc, 1.0, 0.0)])
    try:
        out = mix(hub, session, 3)
        assert set(out) == {30000}
    finally:
        hub.close()


def test_block_between_knee_and_full_scale_is_bit_identical_when_limiter_idle():
    # 32000 > 0.95 * FS but <= FS: an idle limiter must not knee-shape it
    pcm = struct.pack("<hh", 32000, -32000) * 1024
    hub, session, _ = mixer([(pcm, 1.0, 0.0)])
    try:
        out = mix(hub, session, 2)
        assert out == [32000, -32000] * (2 * CHUNK_FRAMES)
        assert session.limit == 1.0
    finally:
        hub.close()


def test_late_join_starts_at_its_first_sample_and_keeps_the_other_continuous():
    a = sine_pcm(100.0, 1.0, amp=0.4)
    hub, session, _ = mixer([(a, 1.0, 0.0)])
    try:
        first = mix(hub, session, 3)
        b = struct.pack("<hh", 3000, 3000) * 2048
        pb = hub.play_pcm(b, channels=2, target="player_1")
        session.cursors[pb.id] = 0
        second = mix(hub, session, 1)
        ref = struct.unpack(f"<{CHUNK_FRAMES * 3 * 2 + CHUNK_FRAMES * 2}h", a[:(CHUNK_FRAMES * 4) * 4])
        expect = [v + 3000 for v in ref[CHUNK_FRAMES * 3 * 2:]]
        assert first == list(ref[:CHUNK_FRAMES * 3 * 2])
        assert second == expect
    finally:
        hub.close()


def test_normalize_without_resampling_is_exact_and_fast():
    stereo = sine_pcm(140.0, 8.5)
    t = time.perf_counter()
    out = stream._normalize_pcm16(stereo, sample_rate=SAMPLE_RATE, channels=2)
    assert time.perf_counter() - t < 0.05                 # was ~210 ms (per-sample loop)
    assert out == struct.unpack(f"<{len(stereo) // 2}h", stereo)
    mono = struct.pack("<4h", 1, -2, 3, -4)
    assert stream._normalize_pcm16(mono, sample_rate=SAMPLE_RATE, channels=1) == (1, 1, -2, -2, 3, 3, -4, -4)
