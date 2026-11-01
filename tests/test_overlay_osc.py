"""Tests for the haptic-file overlay (EventMap.from_file) + per-event target
resolution in play(), and the OSC bridge handler dispatch."""

from __future__ import annotations

import json
import struct
import wave
from pathlib import Path

import pytest

import hapbeat
import hapbeat.hapbeat as hapbeat_mod
from hapbeat import protocol
from hapbeat.eventmap import EventMap
from hapbeat.osc import OscBridge


def make_kit(tmp_path: Path) -> Path:
    kit = tmp_path / "demo-kit"
    (kit / "stream-clips").mkdir(parents=True)
    with wave.open(str(kit / "stream-clips" / "buzz.wav"), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(struct.pack("<400h", *([1000] * 400)))
    manifest = {
        "schema_version": "2.0.0", "name": "demo-kit",
        "events": {"tap.short": {"clip": "tap.wav", "parameters": {"clip_gain": 0.5}}},
        "stream_events": {"rumble.long": {"clip": "buzz.wav", "parameters": {"clip_gain": 0.4}}},
    }
    (kit / "demo-kit-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return kit


def write_haptics(tmp_path: Path, kit: Path) -> Path:
    hf = tmp_path / "haptics.json"
    hf.write_text(json.dumps({
        "kit": kit.name,  # relative to the haptic file
        "events": {
            "tap.short":   {"target": "player_1/chest", "gain": 0.8},
            "rumble.long": {"target": "*/back"},
        },
    }), encoding="utf-8")
    return hf


def play_target(pkt: bytes) -> str:
    """Extract the target field from a PLAY packet payload."""
    _cmd, _seq, payload = protocol.parse_packet(pkt)
    parts = payload.split(b"\x00")
    return parts[1].decode()  # event_id, target, ...


# ── EventMap.from_file ──────────────────────────────────────────────
def test_from_file_merges_kit_and_overlay(tmp_path):
    kit = make_kit(tmp_path)
    em = EventMap.from_file(write_haptics(tmp_path, kit))
    assert em.kit_dir is not None  # resolved from the referenced kit
    tap = em.get("tap.short")
    assert tap.target == "player_1/chest"
    assert tap.gain == 0.8             # overlay gain is kept as the event gain
    assert tap.clip_gain == 0.5        # ...and never written over the manifest clip_gain
    assert tap.streaming is False
    rumble = em.get("rumble.long")
    assert rumble.target == "*/back"
    assert rumble.streaming is True and rumble.clip == "buzz.wav"
    assert rumble.clip_gain == 0.4 and rumble.gain == 1.0


def test_from_file_without_kit_is_pure_overlay(tmp_path):
    hf = tmp_path / "h.json"
    hf.write_text(json.dumps({"events": {"a.b": {"target": "x/y", "gain": 0.3}}}),
                  encoding="utf-8")
    em = EventMap.from_file(hf)
    assert em.kit_dir is None
    ev = em.get("a.b")
    assert ev.target == "x/y" and ev.gain == 0.3 and ev.clip_gain == 1.0 and ev.streaming is False


# ── play() honours per-event target ─────────────────────────────────
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


@pytest.fixture
def fake_client(monkeypatch):
    monkeypatch.setattr(hapbeat_mod, "UdpClient", FakeUdpClient)


def test_play_uses_overlay_target(tmp_path, fake_client):
    kit = make_kit(tmp_path)
    em = EventMap.from_file(write_haptics(tmp_path, kit))
    with hapbeat.connect(event_map=em, keepalive=False) as hb:
        hb.play("tap.short")                      # command event
        assert play_target(hb._client.sent[-1]) == "player_1/chest"
        hb.play("tap.short", target="other/spot")  # call-site overrides
        assert play_target(hb._client.sent[-1]) == "other/spot"


def test_connect_haptics_shortcut(tmp_path, fake_client):
    kit = make_kit(tmp_path)
    hf = write_haptics(tmp_path, kit)
    with hapbeat.connect(haptics=hf, keepalive=False) as hb:
        assert hb.event_map is not None
        assert hb.event_map.get("tap.short").target == "player_1/chest"


# ── OSC bridge handler dispatch (no python-osc needed) ──────────────
class FakeHb:
    def __init__(self):
        self.calls: list[tuple] = []

    def play(self, event_id, gain=1.0, *, target=None, target_time_us=0, pan=0.0):
        self.calls.append(("play", event_id, gain, target, pan))

    def stop(self, event_id, *, target=None):
        self.calls.append(("stop", event_id, target))

    def stop_all(self, *, target=None):
        self.calls.append(("stop_all", target))

    def ping(self):
        self.calls.append(("ping",))


def test_osc_handlers_pass_none_target_when_omitted():
    hb = FakeHb()
    br = OscBridge(hb)
    br._handle_play("/hapbeat/play", "demo.tap")          # no target arg
    br._handle_play("/hapbeat/play", "demo.tap", "x/y")   # explicit target
    # full arg list: target, target_time_us, gain, pan
    br._handle_play("/hapbeat/play", "demo.tap", "", 0, 0.4, -0.5)
    br._handle_stop_all("/hapbeat/stop-all")
    assert hb.calls[0] == ("play", "demo.tap", 1.0, None, 0.0)    # None -> overlay decides
    assert hb.calls[1] == ("play", "demo.tap", 1.0, "x/y", 0.0)   # explicit wins
    assert hb.calls[2] == ("play", "demo.tap", 0.4, None, -0.5)   # pan is the 5th OSC arg
    assert hb.calls[3] == ("stop_all", None)
