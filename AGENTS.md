# Hapbeat Python SDK — context for AI coding agents

Single self-contained reference so an AI coding agent can use this SDK correctly
from one file. Import name: `hapbeat`. Distribution name: `hapbeat-python-sdk`.

- last-verified-against: 0.1.0
- Source of truth is the code: public API in `src/hapbeat/__init__.py` + method
  signatures, the wire format in `src/hapbeat/protocol.py`, CLI flags in
  `src/hapbeat/cli.py`. If this file disagrees with the code, the code wins.
- Canonical docs: https://devtools.hapbeat.com/docs/sdk-integration/python-sdk/

## What it is

A thin SDK to drive Hapbeat haptic devices over Wi-Fi UDP. For research
(PsychoPy / Jupyter / ROS), media art, and prototyping. No cloud; works on the
LAN. Core has zero runtime dependencies (stdlib socket only); the OSC bridge is
an optional extra.

## Core model: fire vs editing, linked by event id

- **Fire side** (your code): *when/where* to play — `play` / `stop` / etc.
- **Editing side** (the kit + haptic file): *what/how* — intensity, loop,
  command-vs-clip, which WAV, target.
- They are linked only by **event id**. Keep intensities/targets out of firing
  code; put them in the kit (Studio-authored) and the haptic file.

Two layers on the editing side:
- **kit manifest** (`<kit>-manifest.json`, schema 2.0.0): Studio-generated kit
  content — `intensity`, `loop`, `clip`, and which bucket (command vs clip). No
  targeting.
- **haptic file** (`haptics.json`): an app-authored overlay that references a
  kit and adds per-event `target` and a `gain` override.

## Install

```bash
pip install hapbeat-python-sdk          # library + CLI
pip install "hapbeat-python-sdk[osc]"   # + OSC bridge (needs python-osc)
pipx install hapbeat-python-sdk         # CLI/launchpad only, isolated
```
`pipx` exposes the `hapbeat` CLI but does NOT make `import hapbeat` available to
your scripts — for code, use a venv `pip install`.

## Quick start

```python
import hapbeat

with hapbeat.connect(app_name="MyApp") as hb:   # context manager closes cleanly
    hb.play("impact.hit", gain=0.5)             # gain 0..1; omit to use EventMap default
```

## Communication model

- Wi-Fi UDP broadcast; no ACK ("late is worse than dropped"). Devices self-filter
  by group/target.
- `connect()` binds an **ephemeral** local receive port by default
  (`bind_port=0`) so it coexists with hapbeat-helper / Hapbeat Studio (which own
  UDP 7700). Pass `bind_port=7700` only to receive the device's unsolicited
  broadcasts (a daemon's job).
- A keep-alive (when `app_name` is set) shows the app name on the device OLED.
- The device command port is UDP 7700; device config is TCP 7701 (helper's job).

## command vs clip (same `play(id)`, branches on the manifest)

| manifest bucket | mode | what happens | pre-deploy |
|---|---|---|---|
| `events` | command | SDK sends PLAY; the device plays its installed clip | yes (flash kit in Studio) |
| `stream_events` | clip | SDK reads the WAV from `<kit>/stream-clips/` and streams it over UDP | no |

- Branch is decided by the bound EventMap. No EventMap => everything is command.
- An id in both buckets (Studio "BOTH") => clip wins.
- Rule of thumb: prototype with clip, ship with command.

## EventMap / haptic file

```python
em = hapbeat.EventMap.from_file("haptics.json")   # overlay (kit + per-event target/gain) — recommended
em = hapbeat.EventMap.from_kit("kits/my-kit")     # kit only (intensity/clip; no targeting)
em = hapbeat.EventMap.from_manifest(path_or_dict) # a single manifest
em = hapbeat.EventMap.from_dict({"impact.hit": 0.5})  # gains by hand (command only)
```

`haptics.json`:
```json
{
  "kit": "kits/my-kit",
  "events": {
    "impact.hit": { "target": "player_1/chest", "gain": 0.8 },
    "rain.loop":  { "target": "*/back" }
  }
}
```
`kit` is resolved relative to the haptic file. Per-event keys: `target`, `gain`
(overrides the manifest `intensity`), `loop`, `note`.

`EventDef` fields: `event_id`, `intensity`, `loop`, `device_wiper`, `streaming`,
`clip`, `target`, `note`, `mode` (`"clip"`|`"command"`).

## Project layout (recommended)

```
my-app/
  app.py                         # your code: play(id) only
  haptics.json                   # the haptic file (target/gain over a kit)
  kits/my-kit/
    my-kit-manifest.json         # Studio-generated kit content
    install-clips/               # command clips (flashed to the device via Studio)
    stream-clips/*.wav           # clip-mode WAVs the SDK streams
```
```python
hb = hapbeat.connect(app_name="MyApp", haptics="haptics.json")
hb.play("impact.hit")     # target/strength come from the haptic file
```

## Target syntax (device-addressing)

`player_1/chest` (one), `*/chest` (all chest), `group_<N>` suffix, `""` = broadcast.
Resolution order at play time: call-site `target=` > the event's `target` (haptic
file) > the connection `default_target`.

## Public API cheat-sheet

`hapbeat.connect(*, port=7700, broadcast_addr="255.255.255.255", app_name="",
device_name="", group=0, default_target="", event_map=None, keepalive=True,
bind_port=0, kit=None, haptics=None, clip_base=None, stream_send_ahead=0.15) ->
Hapbeat`

`Hapbeat`:
- `play(event_id, gain=None, *, target=None, target_time_us=0) -> bool`
- `stop(event_id, *, target=None) -> bool`
- `stop_all(*, target=None) -> bool`
- `ping() -> bool`
- `connect_status(*, connected=True) -> bool`
- `play_clip(event_id, gain=None, *, target=None) -> bool`
- `play_clip_file(path, gain=1.0, *, target=None) -> bool`
- `stream_pcm(pcm: bytes, *, sample_rate=16000, channels=1, gain=1.0, target=None)`
- `preload_clips()`
- `discover(timeout=1.0) -> list[Device]`, `devices` (property)
- `open()` / `close()` / context manager
`Device`: `ip`, `name`, `address`, `firmware_version`, `last_seen`.

## CLI

`hapbeat scan | play <id> [--gain] | stop <id> | stop-all | ping | osc-bridge | launchpad`
- common: `--port` (UDP, default 7700), `--target`
- `osc-bridge [--listen 7702] [--haptics FILE | --kit DIR]`
- `launchpad [--host 127.0.0.1] [--http-port 7100] [--no-open]`

## OSC bridge

Relays `/hapbeat/*` from any OSC tool (e.g. TouchOSC on a phone) to devices.
Addresses: `/hapbeat/play <id> [target] [target_time_us] [gain]`,
`/hapbeat/stop <id> [target]`, `/hapbeat/stop-all [target]`, `/hapbeat/ping`.
With `--haptics`, OSC events route command/clip and pick up per-event targets, so
a sender only needs the event id. The bridge listen port (default 7702) is what
the phone targets — not the device's UDP 7700.

## clip streaming requirements

- WAVs must be **16 kHz mono PCM16**; the SDK does not resample (non-16 kHz warns).
- One stream at a time (session-level); a new clip cancels the previous.
- `gain` is folded into STREAM_BEGIN.gain only (applied once on the device — no
  double-apply); the PCM bytes are sent unchanged.

## Best practices

- Put intensity in the kit/EventMap and target in the haptic file; keep them out
  of firing code so play(id) stays a one-liner.
- Set `app_name` so the device OLED shows who is connected.
- Use the context manager (or call `close()`); it tells the device the app left.
- `preload_clips()` to avoid first-play file-read latency for clip events.

## Pitfalls / FAQ

- `pipx install` gives the CLI but not `import hapbeat` — use a venv for code.
- Nothing buzzes but `hapbeat scan` finds the device => the event id is not in the
  deployed kit (the #1 cause), or `target` does not match your device (try `""`).
- command events need the kit flashed to the device in Studio (the SDK never reads
  `install-clips/`); clip events work with no deploy.
- `gain` is an absolute 0..1 value (the device clamps). Exception: `haptic_pad.py`
  `--master` is a multiplier.
- Multi-homed PC: broadcast may exit the wrong NIC; ensure the Hapbeat LAN's NIC
  has the route.
- An id in both `events` and `stream_events` => clip wins.

## Not implemented

Realtime gain/pan modulation during a clip, multi-source mixing, live (mic)
capture streaming, and mDNS discovery are not built. Discovery is broadcast
PING/PONG only.

## Examples to copy from

`examples/` are single-file/folder templates. Start with `minimal.py`, then
`clip_project/` (project layout + command/clip), then `osc_remote/` (haptic file
+ OSC). See `examples/README.md`.
