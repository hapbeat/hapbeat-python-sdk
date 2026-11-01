"""Strength formula (kit-format.md "Strength formula", DEC-103): the value
sent is ``clamp(clip_gain × app_gain, 0, 1)``. ``clip_gain`` comes from the kit
manifest, ``app_gain`` is the haptic file's per-event ``gain`` times the call's
``gain``. Sends are captured by a fake transport (no sockets, no device)."""

from __future__ import annotations

import json
import struct
import wave
from pathlib import Path

import pytest

import hapbeat
import hapbeat.hapbeat as hapbeat_mod
from hapbeat import protocol
from hapbeat.eventmap import EventDef, EventMap, send_gain


class FakeUdpClient:
    def __init__(self, *a, **kw):
        self.sent: list[bytes] = []

    def add_pong_listener(self, cb): pass
    def remove_pong_listener(self, cb): pass
    def add_packet_listener(self, cb): pass
    def remove_packet_listener(self, cb): pass
    def open(self): pass
    def close(self): pass

    def send(self, pkt, addr=None):
        self.sent.append(pkt)
        return True

    def send_discovery(self, pkt):
        return True


@pytest.fixture
def fake_client(monkeypatch):
    monkeypatch.setattr(hapbeat_mod, "UdpClient", FakeUdpClient)


def play_gain(pkt: bytes) -> float:
    """The gain field of a PLAY packet (… target_time i64, gain f32, pan f32)."""
    cmd, _seq, payload = protocol.parse_packet(pkt)
    assert cmd == protocol.CMD_PLAY
    (gain,) = struct.unpack("<f", payload[-8:-4])
    return gain


def make_kit(tmp_path: Path, clip_gain: float) -> Path:
    kit = tmp_path / "demo-kit"
    (kit / "stream-clips").mkdir(parents=True)
    with wave.open(str(kit / "stream-clips" / "buzz.wav"), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(struct.pack("<400h", *([1000] * 400)))
    manifest = {
        "schema_version": "2.0.0", "name": "demo-kit",
        "events": {"tap.short": {"clip": "tap.wav",
                                 "parameters": {"clip_gain": clip_gain}}},
        "stream_events": {"rumble.long": {"clip": "buzz.wav",
                                          "parameters": {"clip_gain": clip_gain}}},
    }
    (kit / "demo-kit-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return kit


CASES = [
    # (clip_gain, call gain, expected sent value)
    (0.5, 1.0, 0.5),
    (0.5, 2.0, 1.0),
    (0.8, 2.0, 1.0),   # capped at 1
    (0.5, 0.0, 0.0),
]


@pytest.mark.parametrize("clip_gain,gain,expected", CASES)
def test_play_command_sends_clip_gain_times_gain(tmp_path, fake_client,
                                                 clip_gain, gain, expected):
    em = EventMap.from_kit(make_kit(tmp_path, clip_gain))
    with hapbeat.connect(event_map=em, keepalive=False) as hb:
        hb.play("tap.short", gain)
        assert play_gain(hb._client.sent[-1]) == pytest.approx(expected)


@pytest.mark.parametrize("clip_gain,gain,expected", CASES)
def test_play_clip_scales_source_by_clip_gain_times_gain(tmp_path, fake_client,
                                                         clip_gain, gain, expected):
    em = EventMap.from_kit(make_kit(tmp_path, clip_gain))
    with hapbeat.connect(event_map=em, keepalive=False) as hb:
        playback = hb.play("rumble.long", gain)
        assert playback.gain == pytest.approx(expected)
        playback.stop()


def test_play_without_kit_uses_clip_gain_one(fake_client):
    with hapbeat.connect(keepalive=False) as hb:
        hb.play("unknown.event", 1.5)
        assert play_gain(hb._client.sent[-1]) == pytest.approx(1.0)
        hb.play("unknown.event", 0.4)
        assert play_gain(hb._client.sent[-1]) == pytest.approx(0.4)


def test_negative_gain_sends_zero(fake_client):
    with hapbeat.connect(keepalive=False) as hb:
        hb.play("unknown.event", -0.5)
        assert play_gain(hb._client.sent[-1]) == 0.0


def test_haptic_file_gain_multiplies_call_gain_and_clip_gain(tmp_path, fake_client):
    kit = make_kit(tmp_path, 0.8)
    hf = tmp_path / "haptics.json"
    hf.write_text(json.dumps({
        "kit": kit.name,
        "events": {"tap.short": {"gain": 1.5}, "rumble.long": {"gain": 1.5}},
    }), encoding="utf-8")
    em = EventMap.from_file(hf)
    ev = em.get("tap.short")
    assert ev.clip_gain == 0.8 and ev.gain == 1.5  # kept apart, not baked in
    with hapbeat.connect(event_map=em, keepalive=False) as hb:
        hb.play("tap.short", 0.5)
        assert play_gain(hb._client.sent[-1]) == pytest.approx(0.6)
        playback = hb.play("rumble.long", 0.5)
        assert playback.gain == pytest.approx(0.6)
        playback.stop()


def test_send_gain_does_not_cap_factors():
    ev = EventDef(event_id="a.b", clip_gain=0.25, gain=2.0)
    assert send_gain(ev, 1.5) == pytest.approx(0.75)
    assert send_gain(None, 1.5) == 1.0
    assert EventMap({"a.b": ev}).gain_for("a.b") == pytest.approx(0.5)
