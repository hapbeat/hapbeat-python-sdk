# Changelog

All notable changes to `hapbeat-python-sdk` (import name: `hapbeat`).

## 0.1.0 — first public release

Python SDK for driving Hapbeat haptic devices over Wi-Fi UDP. Zero runtime
dependencies for the core (stdlib socket only); the OSC bridge is an optional
extra.

### Fire API (level-1)
- `connect()` / `Hapbeat.play` / `stop` / `stop_all` / `ping` / `connect_status`.
- UDP broadcast transport with device discovery (PING/PONG).
- Receive socket binds an **ephemeral local port by default** so the SDK runs
  alongside `hapbeat-helper` / Hapbeat Studio (which own UDP 7700).

### Tuning side (EventMap)
- `EventMap.from_manifest` / `from_kit` / `from_dict` — read kit manifest
  (schema 2.0.0) `events` (command) and `stream_events` (clip) buckets.
- **Haptic file** `EventMap.from_file("haptics.json")` — an overlay that
  references a kit and adds per-event `target` / `gain`, so `play(id)` resolves
  the target without the caller passing it (mirrors the Unity SDK EventMap).

### Command and clip playback
- `play(id)` auto-branches on the manifest: **command** (device plays its
  installed clip) vs **clip** (the SDK streams the WAV over UDP).
- `ClipStreamer` — paced STREAM_BEGIN/DATA/END (256 ms ring aware),
  session-level single stream; `play_clip` / `play_clip_file` / `stream_pcm`
  (ad-hoc PCM, e.g. stereo directional cues) / `preload_clips`.
- 16 kHz mono PCM16 WAVs (no runtime resample; non-16 kHz warns).

### Tools
- Generic **OSC bridge** (`/hapbeat/*`), optional dep via `pip install
  "hapbeat-python-sdk[osc]"`; `hapbeat osc-bridge --haptics/--kit` routes
  command/clip and applies per-event targets.
- Browser **launchpad** (`hapbeat launchpad`): a local web page to try events /
  metronome / breathing / Morse, each card showing the equivalent CLI command.
- `hapbeat` CLI: `scan` / `play` / `stop` / `stop-all` / `ping` / `osc-bridge` /
  `launchpad`.

### Examples
- `minimal`, `clip_project` (kit-in-project), `osc_remote` (TouchOSC remote),
  `psychophysics_experiment`, `breathing_pacer`, `metronome`, `haptic_pad`,
  `task_notifier`, `morse_text` — single-file, stdlib-only.

Not yet built (level-2): realtime gain/pan binding + multi-source mixing for
clips; mDNS discovery; high-level trigger abstractions.
