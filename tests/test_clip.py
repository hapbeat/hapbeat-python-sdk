"""Tests for clip-mode streaming: WAV read, EventMap clip, StreamHub, and
the Hapbeat play()/stop() command-vs-clip branch (no real sockets)."""

from __future__ import annotations

import json
import struct
import time
import wave
from pathlib import Path

import pytest

import hapbeat
import hapbeat.hapbeat as hapbeat_mod
from hapbeat import protocol
from hapbeat.eventmap import EventMap
from hapbeat.stream import (
    DeferredReason,
    PlaybackStatus,
    StreamEndpoint,
    StreamHub,
)
from hapbeat.wav import read_wav_pcm16


# ── helpers ─────────────────────────────────────────────────────────
def write_wav(path: Path, frames: int = 400, channels: int = 1,
              sample_rate: int = 16000) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(struct.pack("<%dh" % (frames * channels),
                                  *([1000] * (frames * channels))))


def make_kit(tmp_path: Path) -> Path:
    kit = tmp_path / "demo-kit"
    (kit / "stream-clips").mkdir(parents=True)
    write_wav(kit / "stream-clips" / "buzz.wav", frames=600)
    manifest = {
        "schema_version": "2.0.0", "name": "demo-kit",
        "events": {"tap.short": {"clip": "tap.wav", "parameters": {"intensity": 0.8}}},
        "stream_events": {"rumble.long": {"clip": "buzz.wav", "parameters": {"intensity": 0.5}}},
    }
    (kit / "demo-kit-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return kit


class FakeUdpClient:
    def __init__(self, *a, **kw):
        self.sent: list[bytes] = []
        self.destinations: list[str | None] = []

    def add_pong_listener(self, cb): pass
    def remove_pong_listener(self, cb): pass
    def open(self): pass
    def close(self): pass

    def send(self, pkt, addr=None):
        self.sent.append(pkt)
        self.destinations.append(addr)
        return True

    def types(self) -> list[int]:
        return [protocol.parse_header(p)["command_type"] for p in self.sent]


class FakeEndpointSink:
    def __init__(self):
        self.begins: list[StreamEndpoint] = []
        self.data: list[tuple[StreamEndpoint, int, bytes]] = []
        self.ends: list[StreamEndpoint] = []

    def stream_begin_endpoint(self, endpoint): self.begins.append(endpoint)
    def stream_data_endpoint(self, endpoint, offset, data):
        self.data.append((endpoint, offset, bytes(data)))
    def stream_end_endpoint(self, endpoint): self.ends.append(endpoint)


# ── WAV ─────────────────────────────────────────────────────────────
def test_read_wav_pcm16(tmp_path):
    p = tmp_path / "a.wav"
    write_wav(p, frames=320, channels=1)
    wav = read_wav_pcm16(p)
    assert wav.sample_rate == 16000
    assert wav.channels == 1
    assert len(wav.data) == 320 * 2  # frames * 2 bytes (mono PCM16)


def test_read_wav_warns_on_non_16k(tmp_path):
    p = tmp_path / "b.wav"
    write_wav(p, frames=100, sample_rate=44100)
    with pytest.warns(UserWarning):
        read_wav_pcm16(p)


# ── EventMap clip ───────────────────────────────────────────────────
def test_eventmap_from_kit_marks_clip(tmp_path):
    em = EventMap.from_kit(make_kit(tmp_path))
    assert em.kit_dir is not None
    rumble = em.get("rumble.long")
    assert rumble.streaming is True and rumble.mode == "clip"
    assert rumble.clip == "buzz.wav"
    tap = em.get("tap.short")
    assert tap.streaming is False and tap.mode == "command"


# ── StreamHub ───────────────────────────────────────────────────────
def _wait_until(predicate, timeout: float = 1.5) -> None:
    deadline = time.perf_counter() + timeout
    while not predicate() and time.perf_counter() < deadline:
        time.sleep(0.01)
    assert predicate()


def test_two_targets_use_separate_exact_endpoint_sessions():
    left = StreamEndpoint("192.0.2.10", "player_1/left")
    right = StreamEndpoint("192.0.2.11", "player_1/right")
    sink = FakeEndpointSink()
    hub = StreamHub(sink, lambda: [left, right], send_ahead_sec=10.0)
    try:
        hub.play_pcm(b"\x10\x00" * 1024, target="player_1/left")
        hub.play_pcm(b"\x20\x00" * 1024, target="player_1/right")
        _wait_until(lambda: {entry[0] for entry in sink.data} == {left, right})
        assert sink.begins == [left, right]
        assert all(endpoint in (left, right) for endpoint, _, _ in sink.data)
    finally:
        hub.close()


def test_same_endpoint_sources_mix_in_one_session_and_stop_independently():
    endpoint = StreamEndpoint("192.0.2.10", "player_1/chest")
    sink = FakeEndpointSink()
    hub = StreamHub(sink, lambda: [endpoint], send_ahead_sec=0.0)
    frame = struct.pack("<h", 10_000)
    try:
        first = hub.play_pcm(frame * 4096, target="player_1", loop=True)
        second = hub.play_pcm(frame * 4096, target="player_1", loop=True)
        _wait_until(
            lambda: any(
                struct.unpack_from("<hh", data) == (20_000, 20_000)
                for _, _, data in sink.data
            )
        )
        assert sink.begins == [endpoint]

        first.stop()
        assert first.status is PlaybackStatus.STOPPED
        assert second.status is PlaybackStatus.ACTIVE
        assert sink.ends == []
        second.stop()
        _wait_until(lambda: sink.ends == [endpoint])
        assert sink.ends == [endpoint]
        second.stop()
        assert sink.ends == [endpoint]
    finally:
        hub.close()


def test_deferred_source_starts_from_beginning_after_pong():
    endpoints: list[StreamEndpoint] = []
    endpoint = StreamEndpoint("192.0.2.10", "player_1/chest")
    sink = FakeEndpointSink()
    hub = StreamHub(sink, lambda: list(endpoints), send_ahead_sec=10.0)
    pcm = struct.pack("<hh", 1234, 2345)
    try:
        playback = hub.play_pcm(pcm, channels=2, target="player_1")
        assert playback.status is PlaybackStatus.DEFERRED
        assert playback.deferred_reason is DeferredReason.NO_RESOLVED_ENDPOINT
        assert sink.begins == [] and sink.data == []

        endpoints.append(endpoint)
        hub.reconcile()
        _wait_until(lambda: bool(sink.data))
        assert playback.status in (PlaybackStatus.ACTIVE, PlaybackStatus.STOPPED)
        assert sink.begins == [endpoint]
        assert struct.unpack_from("<hh", sink.data[0][2]) == (1234, 2345)
    finally:
        hub.close()


def test_short_gap_reuses_session_instead_of_end_then_begin():
    endpoint = StreamEndpoint("192.0.2.10", "player_1/chest")
    sink = FakeEndpointSink()
    hub = StreamHub(sink, lambda: [endpoint], send_ahead_sec=10.0)
    try:
        first = hub.play_pcm(struct.pack("<h", 1000) * 64, target="player_1")
        _wait_until(lambda: first.status is PlaybackStatus.STOPPED)
        second = hub.play_pcm(struct.pack("<h", 2000) * 64, target="player_1")
        _wait_until(lambda: second.status is PlaybackStatus.STOPPED)
        assert sink.begins == [endpoint]
        assert sink.ends == []
        _wait_until(lambda: sink.ends == [endpoint])
    finally:
        hub.close()


def test_source_after_end_waits_one_beacon_before_new_begin():
    endpoint = StreamEndpoint("192.0.2.10", "player_1/chest")
    sink = FakeEndpointSink()
    hub = StreamHub(sink, lambda: [endpoint], send_ahead_sec=10.0)
    try:
        first = hub.play_pcm(struct.pack("<h", 1000) * 64, target="player_1")
        _wait_until(lambda: first.status is PlaybackStatus.STOPPED)
        _wait_until(lambda: sink.ends == [endpoint])

        second = hub.play_pcm(struct.pack("<h", 2000) * 64, target="player_1")
        assert second.status is PlaybackStatus.DEFERRED
        assert sink.begins == [endpoint]
        time.sleep(0.2)
        assert sink.begins == [endpoint]
        _wait_until(lambda: sink.begins == [endpoint, endpoint])
    finally:
        hub.close()


def test_empty_pcm_is_rejected_without_opening_a_session():
    endpoint = StreamEndpoint("192.0.2.10", "player_1/chest")
    sink = FakeEndpointSink()
    hub = StreamHub(sink, lambda: [endpoint])
    try:
        with pytest.raises(ValueError, match="at least one"):
            hub.play_pcm(b"", target="player_1")
        assert sink.begins == [] and sink.ends == []
    finally:
        hub.close()


def test_stop_key_with_target_keeps_same_event_on_other_target_active():
    left = StreamEndpoint("192.0.2.10", "player_1/left")
    right = StreamEndpoint("192.0.2.11", "player_1/right")
    sink = FakeEndpointSink()
    hub = StreamHub(sink, lambda: [left, right], send_ahead_sec=0.0)
    pcm = struct.pack("<h", 1000) * 256
    try:
        left_playback = hub.play_pcm(
            pcm, target="player_1/left", loop=True, key="kit.loop"
        )
        right_playback = hub.play_pcm(
            pcm, target="player_1/right", loop=True, key="kit.loop"
        )
        assert hub.stop_key("kit.loop", target="player_1/left") == 1
        assert left_playback.status is PlaybackStatus.STOPPED
        assert right_playback.status is PlaybackStatus.ACTIVE
    finally:
        hub.close()


def test_finite_source_is_not_restarted_on_endpoint_that_finished_early():
    first_endpoint = StreamEndpoint("192.0.2.10", "player_1/left")
    second_endpoint = StreamEndpoint("192.0.2.11", "player_1/right")
    endpoints = [first_endpoint]
    sink = FakeEndpointSink()
    hub = StreamHub(sink, lambda: list(endpoints), send_ahead_sec=0.0)
    pcm = struct.pack("<h", 1000) * 1600  # 100 ms
    try:
        playback = hub.play_pcm(pcm, target="player_1")
        time.sleep(0.03)
        endpoints.append(second_endpoint)
        hub.reconcile()
        _wait_until(lambda: playback.status is PlaybackStatus.STOPPED)
        _wait_until(lambda: len(sink.ends) == 2)
        assert sink.begins.count(first_endpoint) == 1
        assert sink.begins.count(second_endpoint) == 1
        assert sink.ends.count(first_endpoint) == 1
        assert sink.ends.count(second_endpoint) == 1
    finally:
        hub.close()


def test_route_change_continues_existing_session_without_end_or_begin():
    old_endpoint = StreamEndpoint("192.0.2.10", "player_1/chest")
    new_endpoint = StreamEndpoint("192.0.2.99", "player_1/chest")
    endpoints = [old_endpoint]
    sink = FakeEndpointSink()
    hub = StreamHub(sink, lambda: list(endpoints), send_ahead_sec=0.0)
    pcm = struct.pack("<h", 1000) * 1024
    try:
        playback = hub.play_pcm(pcm, target="player_1", loop=True)
        _wait_until(lambda: any(ep == old_endpoint for ep, _, _ in sink.data))
        old_offsets = [offset for ep, offset, _ in sink.data if ep == old_endpoint]

        endpoints[:] = [new_endpoint]
        hub.reconcile()
        _wait_until(lambda: any(ep == new_endpoint for ep, _, _ in sink.data))
        new_offsets = [offset for ep, offset, _ in sink.data if ep == new_endpoint]

        assert sink.begins == [old_endpoint]
        assert sink.ends == []
        assert min(new_offsets) > max(old_offsets)
        assert playback.status is PlaybackStatus.ACTIVE
    finally:
        hub.close()


def test_source_gain_and_pan_are_applied_before_saturation():
    endpoint = StreamEndpoint("192.0.2.10", "player_1/chest")
    sink = FakeEndpointSink()
    hub = StreamHub(sink, lambda: [endpoint], send_ahead_sec=0.0)
    pcm = struct.pack("<hh", 30_000, 30_000) * 256
    try:
        left = hub.play_pcm(pcm, channels=2, gain=1.0, pan=-1.0, loop=True)
        right = hub.play_pcm(pcm, channels=2, gain=1.0, pan=1.0, loop=True)
        _wait_until(
            lambda: any(
                struct.unpack_from("<hh", data) == (30_000, 30_000)
                for _, _, data in sink.data
            )
        )
        left.stop()
        right.stop()
    finally:
        hub.close()


# ── Hapbeat play()/stop() branch ────────────────────────────────────
@pytest.fixture
def fake_client(monkeypatch):
    monkeypatch.setattr(hapbeat_mod, "UdpClient", FakeUdpClient)


def test_play_command_sends_play(tmp_path, fake_client):
    em = EventMap.from_kit(make_kit(tmp_path))
    with hapbeat.connect(event_map=em, keepalive=False, stream_send_ahead=0.0) as hb:
        hb.play("tap.short")
        assert hb._client.types()[-1] == protocol.CMD_PLAY


def test_play_clip_streams(tmp_path, fake_client):
    em = EventMap.from_kit(make_kit(tmp_path))
    with hapbeat.connect(event_map=em, keepalive=False, stream_send_ahead=0.0) as hb:
        hb._on_pong({"address": "player_1/chest"}, "192.0.2.10")
        before = len(hb._client.sent)
        playback = hb.play("rumble.long")
        # STREAM_BEGIN is sent synchronously by play_clip
        assert hb._client.types()[before] == protocol.CMD_STREAM_BEGIN
        assert playback.status in (PlaybackStatus.ACTIVE, PlaybackStatus.STOPPED)
        time.sleep(0.7)  # let playback drain plus the anti-reorder END linger
        kinds = set(hb._client.types()[before:])
        assert protocol.CMD_STREAM_DATA in kinds
        assert protocol.CMD_STREAM_END in kinds


def test_stop_clip_ends_stream(tmp_path, fake_client):
    em = EventMap.from_kit(make_kit(tmp_path))
    with hapbeat.connect(event_map=em, keepalive=False, stream_send_ahead=10.0) as hb:
        hb._on_pong({"address": "player_1/chest"}, "192.0.2.10")
        hb.play("rumble.long")
        before = len(hb._client.sent)
        hb.stop("rumble.long")          # should force STREAM_END
        time.sleep(0.35)
        assert protocol.CMD_STREAM_END in hb._client.types()[before:]


def test_clip_missing_base_returns_false(tmp_path, fake_client):
    # EventMap built from a dict has no kit_dir -> cannot resolve the WAV
    em = EventMap({"x.y": hapbeat.EventDef(event_id="x.y", streaming=True, clip="z.wav")})
    with hapbeat.connect(event_map=em, keepalive=False) as hb:
        assert hb.play_clip("x.y") is False


# ── stream wire layout vs contracts ─────────────────────────────────
def test_stream_begin_layout_pcm16_default():
    pkt = protocol.build_stream_begin(1, sample_rate=16000, channels=2,
                                      total_samples=4096, gain=0.5, target="*/chest")
    cmd, _seq, payload = protocol.parse_packet(pkt)
    assert cmd == protocol.CMD_STREAM_BEGIN
    sr, ch, fmt, total, gain = struct.unpack("<HBBIf", payload[:12])
    assert (sr, ch, fmt, total) == (16000, 2, protocol.AUDIO_FORMAT_PCM16, 4096)
    assert abs(gain - 0.5) < 1e-6
    assert payload[12:] == b"*/chest\x00"


def test_stream_data_and_end_layout():
    data = b"\xaa\xbb\xcc\xdd"
    pkt = protocol.build_stream_data(2, 1024, data)
    cmd, _seq, payload = protocol.parse_packet(pkt)
    assert cmd == protocol.CMD_STREAM_DATA
    (offset,) = struct.unpack("<I", payload[:4])
    assert offset == 1024 and payload[4:] == data
    end = protocol.build_stream_end(3)
    cmd, _seq, payload = protocol.parse_packet(end)
    assert cmd == protocol.CMD_STREAM_END and payload == b""
