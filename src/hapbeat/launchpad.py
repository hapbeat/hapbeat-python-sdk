"""Browser launchpad -- try Hapbeat from a single local web page.

Browsers cannot send raw UDP, so this serves a tiny local web page backed by
a stdlib ``http.server`` that relays button presses to the SDK
(``hapbeat.play`` etc.). Open one page and fire one-shots, run a metronome,
a breathing pacer, or send Morse -- start and stop them live, no per-example
launching.

Run it from the CLI::

    hapbeat launchpad                # opens http://127.0.0.1:7100
    hapbeat launchpad --port 8000 --no-open

Zero dependencies (stdlib only). The continuous activities (metronome /
breathing / morse) are deliberately compact mirrors of the standalone
examples; the examples remain the place to read and copy full code.
"""

from __future__ import annotations

import json
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Optional

from .hapbeat import Hapbeat, connect

# Compact Morse table (a subset; the morse_text example has the full one).
_MORSE = {
    "A": ".-", "B": "-...", "C": "-.-.", "D": "-..", "E": ".", "F": "..-.",
    "G": "--.", "H": "....", "I": "..", "J": ".---", "K": "-.-", "L": ".-..",
    "M": "--", "N": "-.", "O": "---", "P": ".--.", "Q": "--.-", "R": ".-.",
    "S": "...", "T": "-", "U": "..-", "V": "...-", "W": ".--", "X": "-..-",
    "Y": "-.--", "Z": "--..",
    "0": "-----", "1": ".----", "2": "..---", "3": "...--", "4": "....-",
    "5": ".....", "6": "-....", "7": "--...", "8": "---..", "9": "----.",
    ".": ".-.-.-", ",": "--..--", "?": "..--..", "/": "-..-.",
}


# ── helpers ──────────────────────────────────────────────────────────
def _num(d: dict, key: str, default: float,
         lo: Optional[float] = None, hi: Optional[float] = None) -> float:
    try:
        v = float(d.get(key, default))
    except (TypeError, ValueError):
        v = default
    if lo is not None:
        v = max(lo, v)
    if hi is not None:
        v = min(hi, v)
    return v


def _sleep_until(stop: threading.Event, deadline: float) -> bool:
    """Sleep to an absolute perf_counter deadline; True if stopped early."""
    while True:
        remaining = deadline - time.perf_counter()
        if remaining <= 0:
            return False
        if stop.wait(min(remaining, 0.05)):
            return True


# ── single-slot background engine ────────────────────────────────────
class Engine:
    """Runs at most one continuous activity; starting a new one stops it."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._stop: Optional[threading.Event] = None
        self._thread: Optional[threading.Thread] = None
        self.current: Optional[str] = None
        self.info: dict = {}

    def start(self, name: str, fn: Callable[[threading.Event], None],
              info: dict) -> None:
        self.stop()
        stop = threading.Event()
        with self._lock:
            self._stop = stop
            self.current = name
            self.info = info

        def wrapped() -> None:
            try:
                fn(stop)
            finally:
                with self._lock:
                    if self._stop is stop:
                        self.current = None
                        self.info = {}

        self._thread = threading.Thread(target=wrapped, name=f"launchpad-{name}",
                                        daemon=True)
        self._thread.start()

    def stop(self) -> None:
        with self._lock:
            stop, thread = self._stop, self._thread
            self._stop = self._thread = None
            self.current = None
            self.info = {}
        if stop is not None:
            stop.set()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1.0)

    def status(self) -> dict:
        with self._lock:
            return {"running": self.current, "info": dict(self.info)}


# ── application (one shared Hapbeat + one Engine) ────────────────────
class App:
    def __init__(self, hb: Hapbeat) -> None:
        self.hb = hb
        self.engine = Engine()

    # one-shots
    def devices(self) -> dict:
        found = self.hb.discover(timeout=1.0)
        return {"devices": [{"ip": d.ip, "address": d.address,
                             "name": d.name, "firmware": d.firmware_version}
                            for d in found]}

    def play(self, p: dict) -> dict:
        event = str(p.get("event", "")).strip()
        if not event:
            return {"ok": False, "error": "event id is empty"}
        ok = self.hb.play(event, _num(p, "gain", 0.5, 0, 1),
                          target=str(p.get("target", "")))
        return {"ok": bool(ok)}

    def stop_all(self) -> dict:
        self.engine.stop()
        self.hb.stop_all()
        return {"ok": True}

    def status(self) -> dict:
        return self.engine.status()

    # continuous activities
    def metronome(self, p: dict) -> dict:
        event = str(p.get("event", "")).strip()
        if not event:
            return {"ok": False, "error": "event id is empty"}
        accent_event = str(p.get("accent_event", "")).strip() or event
        bpm = _num(p, "bpm", 100, 20, 400)
        beats = int(_num(p, "beats", 4, 0, 32))
        gain = _num(p, "gain", 0.5, 0, 1)
        accent_gain = _num(p, "accent_gain", 1.0, 0, 1)
        target = str(p.get("target", ""))

        def run(stop: threading.Event) -> None:
            interval = 60.0 / bpm
            t = time.perf_counter() + 0.2
            beat = 0
            while not stop.is_set():
                if _sleep_until(stop, t):
                    break
                if time.perf_counter() - t > interval:  # re-anchor after a stall
                    t = time.perf_counter()
                accented = beats > 0 and beat == 0
                self.hb.play(accent_event if accented else event,
                             gain=accent_gain if accented else gain, target=target)
                t += interval
                if beats > 0:
                    beat = (beat + 1) % beats

        self.engine.start("metronome", run, {"bpm": bpm, "beats": beats})
        return {"ok": True}

    def breathing(self, p: dict) -> dict:
        event = str(p.get("event", "")).strip()
        if not event:
            return {"ok": False, "error": "event id is empty"}
        inhale = _num(p, "inhale", 4, 0.5, 60)
        hold = _num(p, "hold", 0, 0, 60)
        exhale = _num(p, "exhale", 4, 0.5, 60)
        rest = _num(p, "rest", 0, 0, 60)
        peak = _num(p, "peak", 0.8, 0, 1)
        floor = _num(p, "floor", 0.15, 0, 1)
        tick_interval = 1.0 / _num(p, "tick_rate", 2.0, 0.2, 20)
        target = str(p.get("target", ""))
        phases = [("IN", inhale, lambda x: floor + (peak - floor) * x),
                  ("HOLD", hold, lambda x: peak),
                  ("OUT", exhale, lambda x: peak - (peak - floor) * x),
                  ("REST", rest, lambda x: floor)]

        def run(stop: threading.Event) -> None:
            t = time.perf_counter() + 0.3
            while not stop.is_set():
                for _label, duration, gain_at in phases:
                    if duration <= 0:
                        continue
                    n = max(1, round(duration / tick_interval))
                    for i in range(n):
                        if _sleep_until(stop, t + i * tick_interval):
                            return
                        x = i / (n - 1) if n > 1 else 1.0
                        self.hb.play(event, gain=gain_at(x), target=target)
                    t += duration

        self.engine.start("breathing", run,
                          {"pattern": [inhale, hold, exhale, rest]})
        return {"ok": True}

    def morse(self, p: dict) -> dict:
        event = str(p.get("event", "")).strip()
        text = str(p.get("text", "")).strip()
        if not event:
            return {"ok": False, "error": "event id is empty"}
        if not text:
            return {"ok": False, "error": "text is empty"}
        unit = 1.2 / _num(p, "wpm", 12, 1, 60)
        gain = _num(p, "gain", 0.7, 0, 1)
        target = str(p.get("target", ""))

        def run(stop: threading.Event) -> None:
            for wi, word in enumerate(text.upper().split()):
                if wi and _sleep_until(stop, time.perf_counter() + 7 * unit):
                    return
                sent = False
                for ch in word:
                    code = _MORSE.get(ch)
                    if not code:
                        continue
                    if sent and stop.wait(3 * unit):
                        return
                    sent = True
                    for ei, sym in enumerate(code):
                        if ei and stop.wait(unit):
                            return
                        self.hb.play(event, gain=gain, target=target)
                        if stop.wait(unit if sym == "." else 3 * unit):
                            self.hb.stop(event)
                            return
                        self.hb.stop(event)

        self.engine.start("morse", run, {"text": text})
        return {"ok": True}


# ── HTTP handler ─────────────────────────────────────────────────────
class _BaseHandler(BaseHTTPRequestHandler):
    app: App  # set on the subclass by build_server

    def log_message(self, *_args) -> None:  # keep the console quiet
        pass

    def _json(self, obj: dict, code: int = 200) -> None:
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            return json.loads(raw or b"{}")
        except json.JSONDecodeError:
            return {}

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/" or self.path.startswith("/?"):
            body = PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/api/devices":
            self._json(self.app.devices())
        elif self.path == "/api/status":
            self._json(self.app.status())
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self) -> None:  # noqa: N802
        routes = {
            "/api/play": self.app.play,
            "/api/metronome": self.app.metronome,
            "/api/breathing": self.app.breathing,
            "/api/morse": self.app.morse,
        }
        if self.path in routes:
            try:
                self._json(routes[self.path](self._body()))
            except Exception as exc:  # noqa: BLE001 -- report, never 500-crash
                self._json({"ok": False, "error": str(exc)}, 200)
        elif self.path == "/api/stop":
            self._json(self.app.stop_all())
        elif self.path == "/api/engine/stop":
            self.app.engine.stop()
            self._json({"ok": True})
        else:
            self._json({"error": "not found"}, 404)


def build_server(app: App, host: str, port: int) -> ThreadingHTTPServer:
    handler = type("LaunchpadHandler", (_BaseHandler,), {"app": app})
    return ThreadingHTTPServer((host, port), handler)


def serve(host: str = "127.0.0.1", port: int = 7100, *,
          udp_port: int = 7700, target: str = "",
          open_browser: bool = True) -> int:
    # bind_port=0: receive on an ephemeral port and leave UDP 7700 to
    # hapbeat-helper, so the launchpad and Hapbeat Studio can run together.
    hb = connect(port=udp_port, app_name="Launchpad", default_target=target,
                 bind_port=0)
    app = App(hb)
    server = build_server(app, host, port)
    url = f"http://{host}:{server.server_address[1]}/"
    print(f"Hapbeat launchpad: {url}")
    print("Open it in a browser. Ctrl-C to stop.")
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001
            pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down")
    finally:
        app.engine.stop()
        server.shutdown()
        server.server_close()
        hb.close()
    return 0


# ── the single page (ASCII only -- cp932 consoles) ───────────────────
PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Hapbeat Launchpad</title>
<style>
  :root {
    --bg: #0e1116; --panel: #171c24; --panel2: #1d242e; --line: #2a323d;
    --text: #e6edf3; --muted: #8b97a6; --accent: #5b8cff; --accent2: #7c5cff;
    --ok: #3fb950; --warn: #d29922; --danger: #f85149;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; font: 15px/1.5 -apple-system, Segoe UI, Roboto, sans-serif;
    color: var(--text);
    background: radial-gradient(1200px 600px at 70% -10%, #1b2740 0%, var(--bg) 55%);
    min-height: 100vh;
  }
  header {
    padding: 28px 24px 8px; max-width: 1080px; margin: 0 auto;
  }
  header h1 { margin: 0 0 4px; font-size: 26px; letter-spacing: -0.02em; }
  header h1 .dot {
    background: linear-gradient(90deg, var(--accent), var(--accent2));
    -webkit-background-clip: text; background-clip: text; color: transparent;
  }
  header p { margin: 0; color: var(--muted); }
  main { max-width: 1080px; margin: 0 auto; padding: 16px 24px 60px; }
  .bar {
    display: flex; gap: 12px; flex-wrap: wrap; align-items: end;
    background: var(--panel); border: 1px solid var(--line);
    border-radius: 14px; padding: 14px 16px; margin: 14px 0 22px;
  }
  .field { display: flex; flex-direction: column; gap: 4px; }
  .field label { font-size: 12px; color: var(--muted); }
  input, select {
    background: var(--panel2); color: var(--text);
    border: 1px solid var(--line); border-radius: 9px; padding: 8px 10px;
    font: inherit; min-width: 0;
  }
  input:focus, select:focus { outline: 2px solid var(--accent); border-color: transparent; }
  input[type=range] { padding: 0; }
  .mono { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
  .grid {
    display: grid; gap: 18px;
    grid-template-columns: repeat(auto-fill, minmax(320px, 1fr));
  }
  .card {
    background: var(--panel); border: 1px solid var(--line);
    border-radius: 16px; padding: 18px; display: flex; flex-direction: column; gap: 12px;
  }
  .card h2 { margin: 0; font-size: 17px; }
  .card .desc { color: var(--muted); font-size: 13px; margin: -6px 0 2px; }
  .row { display: flex; gap: 10px; flex-wrap: wrap; align-items: end; }
  .row .field { flex: 1; }
  button {
    appearance: none; border: 1px solid transparent; border-radius: 10px;
    padding: 10px 14px; font: inherit; font-weight: 600; cursor: pointer;
    color: #fff; background: linear-gradient(180deg, var(--accent), #3f6fe0);
  }
  button:hover { filter: brightness(1.07); }
  button:active { transform: translateY(1px); }
  button.ghost { background: var(--panel2); color: var(--text); border-color: var(--line); }
  button.stop { background: linear-gradient(180deg, #ff6a60, var(--danger)); }
  button.run.active { background: linear-gradient(180deg, #ffb648, var(--warn)); }
  .cmd {
    display: flex; align-items: center; gap: 8px; margin-top: 2px;
    background: #0a0d12; border: 1px solid var(--line); border-radius: 9px;
    padding: 7px 9px;
  }
  .cmd code {
    flex: 1; min-width: 0; overflow-x: auto; white-space: nowrap;
    font-family: ui-monospace, Consolas, monospace; font-size: 12px; color: #9fb0c3;
  }
  .cmd .copy {
    background: var(--panel2); color: var(--muted); border: 1px solid var(--line);
    border-radius: 7px; padding: 4px 9px; font-size: 12px; font-weight: 500;
    flex: none;
  }
  .stopall { margin-left: auto; }
  .pill {
    display: inline-flex; align-items: center; gap: 8px; font-size: 13px;
    background: var(--panel2); border: 1px solid var(--line);
    border-radius: 999px; padding: 6px 12px; color: var(--muted);
  }
  .pill .led { width: 9px; height: 9px; border-radius: 50%; background: var(--muted); }
  .pill.online .led { background: var(--ok); box-shadow: 0 0 8px var(--ok); }
  .pill.offline .led { background: var(--danger); }
  #log {
    margin-top: 22px; background: #0a0d12; border: 1px solid var(--line);
    border-radius: 12px; padding: 12px 14px; height: 130px; overflow: auto;
    font-family: ui-monospace, Consolas, monospace; font-size: 12.5px; color: #9fb0c3;
  }
  #log .err { color: var(--danger); }
  #log .ok { color: var(--ok); }
  .hint { color: var(--muted); font-size: 12.5px; margin-top: 6px; }
  a { color: var(--accent); }
</style>
</head>
<body>
<header>
  <h1><span class="dot">Hapbeat</span> Launchpad</h1>
  <p>One page to try the SDK. Buttons hit a local Python server that drives the
     device over Wi-Fi UDP. Each card shows the equivalent terminal command --
     the button runs exactly that, so you can copy it and run it in a shell.</p>
</header>
<main>
  <div class="bar">
    <div class="field" style="flex:2; min-width:220px;">
      <label>Default event id (used when a card's field is blank)</label>
      <input id="defEvent" class="mono" placeholder="e.g. impact.hit" value="">
    </div>
    <div class="field">
      <label>Target (blank = all)</label>
      <input id="target" class="mono" placeholder="*/chest" value="">
    </div>
    <div class="field">
      <button class="ghost" onclick="scan()">Scan devices</button>
    </div>
    <span id="devpill" class="pill offline"><span class="led"></span><span id="devtext">no scan yet</span></span>
    <button class="stop stopall" onclick="stopAll()">STOP ALL</button>
  </div>

  <div class="grid">
    <!-- Quick play -->
    <div class="card">
      <h2>Quick play</h2>
      <div class="desc">Fire a single event. Move the slider for intensity.</div>
      <div class="row">
        <div class="field" style="flex:2;"><label>Event id</label>
          <input id="play_event" class="mono" placeholder="(default)"></div>
        <div class="field"><label>Gain <span id="play_gain_v">0.50</span></label>
          <input id="play_gain" type="range" min="0" max="1" step="0.05" value="0.5"
                 oninput="play_gain_v.textContent=(+this.value).toFixed(2)"></div>
      </div>
      <button onclick="play()">Play</button>
      <div class="cmd"><code id="play_cmd"></code>
        <button class="copy" onclick="copyCmd('play_cmd')">copy</button></div>
    </div>

    <!-- Metronome -->
    <div class="card">
      <h2>Metronome</h2>
      <div class="desc">Steady beat with an accented downbeat.</div>
      <div class="row">
        <div class="field"><label>BPM</label>
          <input id="met_bpm" type="number" value="100" min="20" max="400"></div>
        <div class="field"><label>Beats/bar</label>
          <input id="met_beats" type="number" value="4" min="0" max="32"></div>
      </div>
      <div class="field"><label>Event id</label>
        <input id="met_event" class="mono" placeholder="(default)"></div>
      <button id="met_btn" class="run" onclick="toggle('metronome','met_btn',metParams)">Start</button>
      <div class="cmd"><code id="met_cmd"></code>
        <button class="copy" onclick="copyCmd('met_cmd')">copy</button></div>
    </div>

    <!-- Breathing -->
    <div class="card">
      <h2>Breathing pacer</h2>
      <div class="desc">Intensity ramps up on inhale, down on exhale.</div>
      <div class="row">
        <div class="field"><label>Preset</label>
          <select id="brk_preset" onchange="applyPreset()">
            <option value="4,4,4,4">Box 4-4-4-4</option>
            <option value="4,7,8,0">4-7-8</option>
            <option value="5.5,0,5.5,0">Coherent 5.5</option>
          </select></div>
        <div class="field"><label>Pattern in,hold,out,rest</label>
          <input id="brk_pattern" class="mono" value="4,4,4,4"></div>
      </div>
      <div class="field"><label>Event id</label>
        <input id="brk_event" class="mono" placeholder="(default)"></div>
      <button id="brk_btn" class="run" onclick="toggle('breathing','brk_btn',brkParams)">Start</button>
      <div class="cmd"><code id="brk_cmd"></code>
        <button class="copy" onclick="copyCmd('brk_cmd')">copy</button></div>
    </div>

    <!-- Morse -->
    <div class="card">
      <h2>Morse</h2>
      <div class="desc">Send text as vibration. Use a looping buzz event.</div>
      <div class="row">
        <div class="field" style="flex:2;"><label>Text</label>
          <input id="mor_text" value="SOS"></div>
        <div class="field"><label>WPM</label>
          <input id="mor_wpm" type="number" value="12" min="1" max="60"></div>
      </div>
      <div class="field"><label>Event id</label>
        <input id="mor_event" class="mono" placeholder="(default)"></div>
      <button id="mor_btn" class="run" onclick="toggle('morse','mor_btn',morParams)">Send</button>
      <div class="cmd"><code id="mor_cmd"></code>
        <button class="copy" onclick="copyCmd('mor_cmd')">copy</button></div>
    </div>
  </div>

  <p class="hint">Event ids must exist in the kit deployed to the device (via
    Hapbeat Studio). If nothing buzzes but Scan finds the device, the id is
    probably not in the kit.</p>
  <div id="log"></div>
</main>

<script>
const $ = id => document.getElementById(id);
function defEvent(){ return $('defEvent').value.trim(); }
function target(){ return $('target').value.trim(); }
function evOr(id){ const v = $(id).value.trim(); return v || defEvent(); }

function log(msg, cls){
  const el = $('log'); const line = document.createElement('div');
  if (cls) line.className = cls;
  const t = new Date().toLocaleTimeString();
  line.textContent = '[' + t + '] ' + msg; el.appendChild(line);
  el.scrollTop = el.scrollHeight;
}
async function api(path, body){
  try {
    const r = await fetch(path, {method:'POST', headers:{'Content-Type':'application/json'},
                                 body: JSON.stringify(body||{})});
    return await r.json();
  } catch(e){ log('request failed: ' + e, 'err'); return {ok:false, error:String(e)}; }
}
function note(res, what){
  if (res && res.ok === false) log(what + ': ' + (res.error||'failed'), 'err');
  else log(what, 'ok');
}

async function scan(){
  log('scanning...');
  try {
    const r = await fetch('/api/devices'); const d = await r.json();
    const n = (d.devices||[]).length;
    const pill = $('devpill');
    pill.className = 'pill ' + (n ? 'online' : 'offline');
    $('devtext').textContent = n ? (n + ' device(s): ' +
      d.devices.map(x => x.address || x.ip).join(', ')) : 'no device replied';
    log(n ? ('found ' + n + ' device(s)') : 'no device replied (UDP still broadcasts)',
        n ? 'ok' : 'err');
  } catch(e){ log('scan failed: ' + e, 'err'); }
}
async function play(){
  const ev = evOr('play_event');
  if (!ev) return log('set an event id (card or default)', 'err');
  note(await api('/api/play', {event: ev, gain: +$('play_gain').value, target: target()}),
       'play ' + ev);
}
async function stopAll(){
  note(await api('/api/stop'), 'stop all');
  ['met_btn','brk_btn','mor_btn'].forEach(setStart);
}

function metParams(){ return {event: evOr('met_event'), bpm:+$('met_bpm').value,
  beats:+$('met_beats').value, target: target()}; }
function brkParams(){ const p=$('brk_pattern').value.split(',').map(Number);
  return {event: evOr('brk_event'), inhale:p[0], hold:p[1]||0, exhale:p[2]||0,
          rest:p[3]||0, target: target()}; }
function morParams(){ return {event: evOr('mor_event'), text:$('mor_text').value,
  wpm:+$('mor_wpm').value, target: target()}; }

function applyPreset(){ $('brk_pattern').value = $('brk_preset').value; renderCmds(); }

// The equivalent terminal command per card -- exactly what the button runs.
function evShow(id){ return evOr(id) || '<event>'; }
function tgt(){ const t = target(); return t ? ' --target ' + t : ''; }
function renderCmds(){
  $('play_cmd').textContent =
    'hapbeat play ' + evShow('play_event') +
    ' --gain ' + (+$('play_gain').value).toFixed(2) + tgt();
  $('met_cmd').textContent =
    'python examples/metronome.py --event ' + evShow('met_event') +
    ' --bpm ' + (+$('met_bpm').value) + ' --beats ' + (+$('met_beats').value) + tgt();
  $('brk_cmd').textContent =
    'python examples/breathing_pacer.py --event ' + evShow('brk_event') +
    ' --pattern ' + ($('brk_pattern').value || '4,4,4,4') + tgt();
  $('mor_cmd').textContent =
    'python examples/morse_text.py "' + ($('mor_text').value || 'TEXT') +
    '" --event ' + evShow('mor_event') + ' --wpm ' + (+$('mor_wpm').value) + tgt();
}
async function copyCmd(id){
  try { await navigator.clipboard.writeText($(id).textContent);
        log('copied: ' + $(id).textContent, 'ok'); }
  catch(e){ log('copy failed -- select the command text manually', 'err'); }
}
document.addEventListener('input', renderCmds);
function setStart(id){ const b=$(id); b.classList.remove('active');
  b.textContent = id==='mor_btn' ? 'Send' : 'Start'; }
function setStop(id){ const b=$(id); b.classList.add('active'); b.textContent='Stop'; }

async function toggle(kind, btnId, paramsFn){
  const b = $(btnId);
  if (b.classList.contains('active')){
    note(await api('/api/engine/stop'), 'stop ' + kind);
    setStart(btnId); return;
  }
  const ev = paramsFn().event;
  if (!ev) return log('set an event id (card or default)', 'err');
  const res = await api('/api/' + kind, paramsFn());
  if (res.ok === false){ note(res, kind); return; }
  ['met_btn','brk_btn','mor_btn'].forEach(setStart);
  setStop(btnId); log('start ' + kind, 'ok');
}

// Reflect server truth (auto-stop, morse finishing) every ~1.5s.
async function poll(){
  try {
    const r = await fetch('/api/status'); const s = await r.json();
    const map = {metronome:'met_btn', breathing:'brk_btn', morse:'mor_btn'};
    Object.values(map).forEach(setStart);
    if (s.running && map[s.running]) setStop(map[s.running]);
  } catch(e){}
}
setInterval(poll, 1500);
renderCmds();
scan();
</script>
</body>
</html>
"""
