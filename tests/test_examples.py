"""Tests for the pure logic inside examples/ (no device, no sockets).

The examples are standalone files outside the package, so they are loaded
with importlib. Anything that fires haptics is exercised through a
FakeHapbeat that records calls instead of sending UDP.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import random
import sys
from pathlib import Path

import pytest

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
ALL_EXAMPLES = sorted(p.name for p in EXAMPLES.glob("*.py"))
CLI_EXAMPLES = [
    "psychophysics_experiment", "breathing_pacer", "metronome",
    "haptic_pad", "task_notifier", "morse_text",
]


def load(name: str):
    spec = importlib.util.spec_from_file_location(name, EXAMPLES / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    # Registration keeps dataclasses working (they look up sys.modules).
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


class FakeHapbeat:
    """Records (action, event_id, gain) instead of sending UDP."""

    def __init__(self) -> None:
        self.ops: list[tuple] = []

    def play(self, event_id, gain=None, **kw):
        self.ops.append(("play", event_id, gain))
        return True

    def stop(self, event_id, **kw):
        self.ops.append(("stop", event_id))
        return True

    def stop_all(self, **kw):
        self.ops.append(("stop_all",))
        return True


# ── repo-wide conventions ───────────────────────────────────────────
@pytest.mark.parametrize("name", ALL_EXAMPLES + ["README.md"])
def test_examples_are_ascii(name):
    # Windows cp932 consoles choke on non-ASCII output; keep samples pure ASCII.
    data = (EXAMPLES / name).read_bytes()
    bad = [b for b in data if b > 127]
    assert not bad, f"{name} contains non-ASCII bytes"


@pytest.mark.parametrize("name", CLI_EXAMPLES)
def test_help_exits_cleanly(name, monkeypatch, capsys):
    mod = load(name)
    monkeypatch.setattr(sys, "argv", [name, "--help"])
    with pytest.raises(SystemExit) as exc:
        mod.main()
    assert exc.value.code in (0, None)
    assert capsys.readouterr().out.isascii()


# ── morse_text ──────────────────────────────────────────────────────
def test_morse_table_and_koch_order():
    morse = load("morse_text")
    for ch, code in morse.MORSE.items():
        assert code and set(code) <= {".", "-"}, (ch, code)
    codes = list(morse.MORSE.values())
    assert len(codes) == len(set(codes)), "duplicate codes"
    for ch in morse.KOCH_ORDER:
        assert ch in morse.MORSE, f"Koch char {ch!r} missing from MORSE"


def test_farnsworth_reduces_to_standard_at_equal_speeds():
    morse = load("morse_text")
    unit = 1.2 / 18.0
    letter, word = morse.farnsworth_gaps(18.0, 18.0)
    assert letter == pytest.approx(3 * unit)
    assert word == pytest.approx(7 * unit)


def test_farnsworth_5_18_matches_arrl_example():
    # ta = (60*18 - 37.2*5) / (5*18) = 9.9333.. s  (ARRL QEX Apr 1990)
    morse = load("morse_text")
    letter, word = morse.farnsworth_gaps(18.0, 5.0)
    ta = (60 * 18 - 37.2 * 5) / (5 * 18)
    assert letter == pytest.approx(3 * ta / 19)
    assert word == pytest.approx(7 * ta / 19)
    assert letter > 3 * (1.2 / 18.0)  # gaps actually stretched


def test_send_text_timing_structure(monkeypatch):
    morse = load("morse_text")
    hb = FakeHapbeat()
    sleeps: list[float] = []
    monkeypatch.setattr(morse, "sleep_for", lambda s: sleeps.append(round(s, 6)))
    args = argparse.Namespace(event="buzz", gain=0.7, dit_event=None, dah_event=None)
    unit = 0.1
    # letter_gap deliberately differs from the dah length (0.3) so the
    # assertion below really proves the inter-letter gap was slept.
    morse.send_text(hb, args, "ET E", unit, 0.45, 0.7, echo=False)
    # E = dit (play, sleep 1u, stop); T = dah (play, sleep 3u, stop)
    plays = [op for op in hb.ops if op[0] == "play"]
    assert len(plays) == 3
    assert sleeps.count(pytest.approx(0.1)) >= 1     # dit duration
    assert pytest.approx(0.3) in sleeps              # dah duration
    assert pytest.approx(0.45) in sleeps             # letter gap
    assert pytest.approx(0.7) in sleeps              # word gap
    # play/stop alternate strictly in loop mode
    actions = [op[0] for op in hb.ops]
    assert actions == ["play", "stop"] * 3


def test_send_text_skips_unsupported_without_extra_gap(monkeypatch, capsys):
    morse = load("morse_text")
    hb = FakeHapbeat()
    sleeps: list[float] = []
    monkeypatch.setattr(morse, "sleep_for", lambda s: sleeps.append(round(s, 6)))
    args = argparse.Namespace(event="buzz", gain=0.7, dit_event=None, dah_event=None)
    unit = 0.1
    # Leading unsupported char must not produce a letter gap before "E".
    morse.send_text(hb, args, "#E", unit, 3 * unit, 7 * unit, echo=False)
    assert pytest.approx(0.3) not in sleeps
    assert "skipping" in capsys.readouterr().out


# ── metronome ───────────────────────────────────────────────────────
def test_parse_beats():
    met = load("metronome")
    assert met.parse_beats("4") == (4, {0})
    assert met.parse_beats("0") == (0, set())
    assert met.parse_beats("2+2+3") == (7, {0, 2, 4})
    with pytest.raises(ValueError):
        met.parse_beats("-1")
    with pytest.raises(ValueError):
        met.parse_beats("2+0+3")


def test_parse_ramp_and_gap():
    met = load("metronome")
    assert met.parse_ramp("150:180:300") == (150.0, 180.0, 300.0)
    with pytest.raises(ValueError):
        met.parse_ramp("0:100:10")
    assert met.parse_gap("4:2") == (4, 2)
    with pytest.raises(ValueError):
        met.parse_gap("4:0")


def test_bpm_at_clamps_at_ramp_end():
    met = load("metronome")
    assert met.bpm_at(0.0, 100, 160, 60) == pytest.approx(100)
    assert met.bpm_at(30.0, 100, 160, 60) == pytest.approx(130)
    assert met.bpm_at(999.0, 100, 160, 60) == pytest.approx(160)
    assert met.bpm_at(50.0, 120, 120, 0) == pytest.approx(120)


# ── breathing_pacer ─────────────────────────────────────────────────
def test_parse_pattern():
    pacer = load("breathing_pacer")
    assert pacer.parse_pattern("4,7,8") == (4.0, 7.0, 8.0, 0.0)
    assert pacer.parse_pattern("4,4,4,4") == (4.0, 4.0, 4.0, 4.0)
    for bad in ("1,2", "1,2,3,4,5", "4,-1,4,0", "0,0,0,0", "4,,8,2"):
        with pytest.raises(ValueError):
            pacer.parse_pattern(bad)


def test_interp_pattern_endpoints():
    pacer = load("breathing_pacer")
    a, b = (4.0, 4.0, 4.0, 4.0), (4.0, 7.0, 8.0, 0.0)
    assert pacer.interp_pattern(a, b, 0.0) == a
    assert pacer.interp_pattern(a, b, 1.0) == b
    mid = pacer.interp_pattern(a, b, 0.5)
    assert mid == (4.0, 5.5, 6.0, 2.0)


# ── psychophysics ───────────────────────────────────────────────────
def test_build_trials_counts_and_determinism():
    psy = load("psychophysics_experiment")
    levels = [0.1, 0.4]
    t1 = psy.build_trials(levels, 5, 0.2, random.Random(42))
    t2 = psy.build_trials(levels, 5, 0.2, random.Random(42))
    assert t1 == t2  # same seed -> same order
    assert len(t1) == 10 + round(10 * 0.2)
    assert sum(1 for _, c in t1 if c) == 2
    assert all(lv == 0.0 for lv, c in t1 if c)


def test_staircase_1up2down_moves_and_reverses():
    psy = load("psychophysics_experiment")
    st = psy.Staircase(start=0.4, factor=2.0, n_down=2)
    assert st.update(True) is False          # 1st hit: no move yet
    assert st.level == pytest.approx(0.4)
    st.update(True)                          # 2nd hit: level halves
    assert st.level == pytest.approx(0.2)
    rev = st.update(False)                   # miss: level doubles = reversal
    assert rev is True
    assert st.level == pytest.approx(0.4)
    assert st.reversals == [pytest.approx(0.2)]


def test_staircase_threshold_geometric_mean():
    psy = load("psychophysics_experiment")
    st = psy.Staircase(start=0.4, factor=2.0, n_down=1)
    st.reversals = [0.1, 0.4, 0.2, 0.2]  # few reversals -> all used
    assert st.threshold() == pytest.approx((0.1 * 0.4 * 0.2 * 0.2) ** 0.25)


def test_interpolate_50():
    psy = load("psychophysics_experiment")
    points = [(0.1, 0.0), (0.2, 0.25), (0.4, 0.75), (0.8, 1.0)]
    thr = psy.interpolate_50(points)
    assert thr == pytest.approx(0.3)
    assert psy.interpolate_50([(0.1, 0.6), (0.2, 0.9)]) is None


# ── haptic_pad ──────────────────────────────────────────────────────
def make_pad_args(**kw):
    return argparse.Namespace(**{"map": None, "manifest": None, "events": None, **kw})


def test_load_pads_three_sources(tmp_path):
    pad = load("haptic_pad")
    manifest = {
        "schema_version": "2.0.0", "name": "test-kit",
        "events": {"impact.hit": {"clip": "a.wav", "parameters": {"intensity": 0.8}}},
        "stream_events": {"rain.loop": {"clip": "c.wav",
                                        "parameters": {"intensity": 0.3, "loop": True}}},
    }
    mpath = tmp_path / "m.json"
    mpath.write_text(json.dumps(manifest), encoding="utf-8")
    pads = pad.load_pads(make_pad_args(manifest=str(mpath)))
    assert [(p.key, p.event_id, p.gain, p.loop) for p in pads] == [
        ("1", "impact.hit", 0.8, False), ("2", "rain.loop", 0.3, True)]

    jpath = tmp_path / "pad.json"
    jpath.write_text(json.dumps({
        "1": {"event": "x.y", "gain": 0.5, "loop": True}, "2": "z.w"}), encoding="utf-8")
    pads = pad.load_pads(make_pad_args(map=str(jpath)))
    assert pads[0].loop is True and pads[1].gain == 1.0

    pads = pad.load_pads(make_pad_args(events="a.b, c.d"))
    assert [p.key for p in pads] == ["1", "2"]


def test_load_pads_rejects_bad_maps(tmp_path):
    pad = load("haptic_pad")
    cases = [
        {"10": "too.long"},                      # multi-char key
        {"1": {"gain": 0.5}},                    # missing event
        {"q": "quit.collision"},                 # reserved control key
    ]
    for raw in cases:
        p = tmp_path / "bad.json"
        p.write_text(json.dumps(raw), encoding="utf-8")
        with pytest.raises(ValueError):
            pad.load_pads(make_pad_args(map=str(p)))


# ── task_notifier ───────────────────────────────────────────────────
def test_split_command():
    tn = load("task_notifier")
    assert tn.split_command(["--", "pytest", "-x"]) == ["pytest", "-x"]
    assert tn.split_command(["pytest", "-x"]) == ["pytest", "-x"]
    assert tn.split_command([]) == []


def test_fire_pulses_counts(monkeypatch):
    tn = load("task_notifier")
    hb = FakeHapbeat()
    monkeypatch.setattr(tn.time, "sleep", lambda s: None)
    tn.fire_pulses(hb, "e", 0.5, 3, 0.1)
    assert hb.ops == [("play", "e", 0.5)] * 3
