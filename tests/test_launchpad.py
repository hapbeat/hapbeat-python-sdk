"""Tests for the browser launchpad (no device, no real sockets).

The HTTP layer is exercised against a real ThreadingHTTPServer bound to an
ephemeral port with a FakeHapbeat injected, so routing and JSON handling are
covered without touching the network or a device.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.request

import pytest

from hapbeat import launchpad


class FakeHapbeat:
    def __init__(self):
        self.ops = []

    def play(self, event_id, gain=None, target="", **kw):
        self.ops.append(("play", event_id, gain))
        return True

    def stop(self, event_id, **kw):
        self.ops.append(("stop", event_id))
        return True

    def stop_all(self, **kw):
        self.ops.append(("stop_all",))
        return True

    def discover(self, timeout=1.0):
        return []


# ── pure helpers ────────────────────────────────────────────────────
def test_page_is_ascii_and_complete():
    data = launchpad.PAGE.encode("utf-8")
    assert all(b < 128 for b in data), "launchpad page must be ASCII"
    assert "<html" in launchpad.PAGE and "/api/play" in launchpad.PAGE


def test_num_coerces_and_clamps():
    assert launchpad._num({"g": "0.5"}, "g", 0.1) == 0.5
    assert launchpad._num({}, "g", 0.3) == 0.3
    assert launchpad._num({"g": "bad"}, "g", 0.3) == 0.3
    assert launchpad._num({"g": 5}, "g", 0.3, 0, 1) == 1.0
    assert launchpad._num({"g": -5}, "g", 0.3, 0, 1) == 0.0


def test_morse_table_ascii():
    for ch, code in launchpad._MORSE.items():
        assert code and set(code) <= {".", "-"}


# ── engine ──────────────────────────────────────────────────────────
def test_engine_start_stop_single_slot():
    eng = launchpad.Engine()
    eng.start("a", lambda stop: stop.wait(2.0), {"x": 1})
    time.sleep(0.05)
    assert eng.status()["running"] == "a"
    # Starting a new activity stops the previous one.
    eng.start("b", lambda stop: stop.wait(2.0), {"y": 2})
    time.sleep(0.05)
    assert eng.status()["running"] == "b"
    eng.stop()
    assert eng.status()["running"] is None


def test_app_play_validates_event():
    app = launchpad.App(FakeHapbeat())
    assert app.play({"event": "", "gain": 0.5})["ok"] is False
    assert app.play({"event": "impact.hit", "gain": 0.5})["ok"] is True
    assert app.hb.ops[-1] == ("play", "impact.hit", 0.5)


def test_app_metronome_fires_then_stops():
    app = launchpad.App(FakeHapbeat())
    r = app.metronome({"event": "tick", "bpm": 600, "beats": 4})
    assert r["ok"] is True
    time.sleep(0.4)
    app.engine.stop()
    plays = [op for op in app.hb.ops if op[0] == "play"]
    assert len(plays) >= 2
    assert app.status()["running"] is None


def test_app_morse_emits_play_stop_pairs():
    app = launchpad.App(FakeHapbeat())
    app.morse({"event": "buzz", "text": "E", "wpm": 60})  # 'E' = single dit
    time.sleep(0.3)
    app.engine.stop()
    assert ("play", "buzz", 0.7) in app.hb.ops
    assert ("stop", "buzz") in app.hb.ops


# ── HTTP integration ────────────────────────────────────────────────
@pytest.fixture
def server():
    app = launchpad.App(FakeHapbeat())
    srv = launchpad.build_server(app, "127.0.0.1", 0)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    port = srv.server_address[1]
    yield app, f"http://127.0.0.1:{port}"
    srv.shutdown()
    srv.server_close()


def _post(url, obj):
    req = urllib.request.Request(url, data=json.dumps(obj).encode(),
                                 headers={"Content-Type": "application/json"},
                                 method="POST")
    with urllib.request.urlopen(req, timeout=2) as r:
        return json.loads(r.read())


def _get(url):
    with urllib.request.urlopen(url, timeout=2) as r:
        return r.read()


def test_http_index_and_play(server):
    app, base = server
    assert b"<html" in _get(base + "/")
    res = _post(base + "/api/play", {"event": "impact.hit", "gain": 0.4})
    assert res["ok"] is True
    assert app.hb.ops[-1] == ("play", "impact.hit", 0.4)


def test_http_stop_all(server):
    app, base = server
    assert _post(base + "/api/stop", {})["ok"] is True
    assert ("stop_all",) in app.hb.ops


def test_http_devices(server):
    _app, base = server
    data = json.loads(_get(base + "/api/devices"))
    assert data == {"devices": []}
