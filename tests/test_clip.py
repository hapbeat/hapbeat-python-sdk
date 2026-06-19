"""Tests for clip-mode streaming: WAV read, EventMap clip, ClipStreamer, and
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
from hapbeat.clip import ClipStreamer
from hapbeat.eventmap import EventMap
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

    def add_pong_listener(self, cb): pass
    def remove_pong_listener(self, cb): pass
    def open(self): pass
    def close(self): pass

    def send(self, pkt, addr=None):
        self.sent.append(pkt)
        return True

    def types(self) -> list[int]:
        return [protocol.parse_header(p)["command_type"] for p in self.sent]


class FakeSink:
    def __init__(self):
        self.begin = None
        self.data: list[tuple[int, bytes]] = []
        self.ends = 0

    def stream_begin(self, **kw): self.begin = kw
    def stream_data(self, offset, data): self.data.append((offset, bytes(data)))
    def stream_end(self): self.ends += 1


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


# ── ClipStreamer ────────────────────────────────────────────────────
def _drain(cs: ClipStreamer, timeout: float = 1.5) -> None:
    """Wait for a clip to finish streaming naturally, then join the thread."""
    deadline = time.perf_counter() + timeout
    while cs.is_active() and time.perf_counter() < deadline:
        time.sleep(0.01)
    cs.stop()


def test_clip_streamer_begin_data_end(tmp_path):
    sink = FakeSink()
    # large send-ahead => all chunks go out immediately; END after playout.
    cs = ClipStreamer(sink, send_ahead_sec=10.0)
    pcm = b"\x01\x02" * 2000  # 2000 mono frames (~0.125 s)
    cs.play(pcm, sample_rate=16000, channels=1, gain=0.5, target="*/chest")
    _drain(cs)
    assert sink.begin == {"sample_rate": 16000, "channels": 1,
                          "total_samples": 2000, "gain": 0.5, "target": "*/chest"}
    assert sink.ends == 1  # END sent exactly once
    # data offsets are contiguous and cover the whole buffer
    reassembled = b"".join(d for _, d in sink.data)
    offsets = [o for o, _ in sink.data]
    assert reassembled == pcm
    assert offsets == sorted(offsets)
    assert offsets[0] == 0


def test_clip_streamer_chunks_frame_aligned_stereo():
    sink = FakeSink()
    cs = ClipStreamer(sink, send_ahead_sec=10.0)
    pcm = b"\x00\x00\x11\x11" * 1000  # stereo: 4 bytes/frame
    cs.play(pcm, sample_rate=16000, channels=2)
    _drain(cs)
    assert sink.data  # something was sent
    # every chunk is a whole number of 4-byte stereo frames
    for _off, d in sink.data:
        assert len(d) % 4 == 0


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
        before = len(hb._client.sent)
        hb.play("rumble.long")
        # STREAM_BEGIN is sent synchronously by play_clip
        assert hb._client.types()[before] == protocol.CMD_STREAM_BEGIN
        time.sleep(0.3)  # let the pacing thread drain
        kinds = set(hb._client.types()[before:])
        assert protocol.CMD_STREAM_DATA in kinds
        assert protocol.CMD_STREAM_END in kinds


def test_stop_clip_ends_stream(tmp_path, fake_client):
    em = EventMap.from_kit(make_kit(tmp_path))
    with hapbeat.connect(event_map=em, keepalive=False, stream_send_ahead=10.0) as hb:
        hb.play("rumble.long")          # send_ahead huge -> nothing paced yet
        before = len(hb._client.sent)
        hb.stop("rumble.long")          # should force STREAM_END
        assert protocol.CMD_STREAM_END in hb._client.types()[before - 1:]


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
