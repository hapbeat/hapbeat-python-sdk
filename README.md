# Hapbeat Python SDK

Drive [Hapbeat](https://hapbeat.com) haptic devices from Python over Wi-Fi UDP.
For researchers (PsychoPy / Jupyter / ROS), media artists, and anyone
prototyping haptics in Python.

> **📚 Docs**: <https://devtools.hapbeat.com/docs/sdk-integration/>

A script can drive Hapbeat with a few lines. The fire side (`play` / `stop`)
and the tuning side (`EventMap`) are kept orthogonal and linked only by
event id.

## Install

```bash
pip install hapbeat-python-sdk        # core (zero dependencies, stdlib socket only)
pip install "hapbeat-python-sdk[osc]"   # + generic OSC bridge (TouchOSC / Max / TD)
```

With **pipx** you get the `hapbeat` CLI (including the launchpad below) in an
isolated environment: `pipx install hapbeat-python-sdk`. Note that pipx does *not* make
`import hapbeat` available to your own scripts — for the `examples/` use a
normal `pip install` in a venv.

## Try everything from one page (launchpad)

```bash
hapbeat launchpad          # opens http://127.0.0.1:7100 in your browser
```

A single local web page to fire events, run a metronome, a breathing pacer,
or send Morse — start and stop them live, no per-example launching. It serves
a tiny stdlib HTTP server that relays button presses to the device over UDP
(browsers can't send UDP directly). Works great with `pipx install hapbeat-python-sdk`.

## Quick start

```python
import hapbeat

hb = hapbeat.connect(app_name="MyExperiment")  # opens the UDP socket + keep-alive
hb.play("sample-kit.sine_100hz", gain=0.3)   # fire event "sample-kit.sine_100hz" at gain 0.3
hb.play("sample-kit.sine_100hz")             # gain omitted -> 1 (the kit's clip_gain as authored)
hb.play("sample-kit.sine_100hz", pan=-1)     # pan: -1 left / 0 centre / +1 right
hb.stop("sample-kit.sine_100hz")
hb.stop_all()
hb.close()
```

or as a context manager:

```python
with hapbeat.connect(app_name="MyExperiment") as hb:
    hb.play("sample-kit.sine_100hz")
```

`"sample-kit.sine_100hz"` must be an event id present in the **kit deployed to the
device** (via [Hapbeat Studio](https://devtools.hapbeat.com)). The SDK sends
the *instruction*; the waveform lives in the kit on the device.

## Discovery

```python
for dev in hb.discover(timeout=1.5):
    print(dev.ip, dev.address, dev.firmware_version)
```

### How packets are sent

Commands are unicast to devices that answered a PING and use broadcast only
while no device is known. Clip streams are stricter: they start only on exact
PONG-reported endpoints and remain `Deferred` while none matches; stream packets
are never broadcast. Wi-Fi APs hold broadcast frames
until their next DTIM beacon (100–300 ms) whenever any client on the AP is
power-saving, which is heard as late haptics and stuttering streams; unicast is
not batched that way. The 5-second keep-alive PING keeps that table current, so
leave `keepalive=True`.

Pass `connect(unicast=False)` to always broadcast — worth it when many devices
must fire in lockstep, since one broadcast reaches them all at once while N
unicasts go out one after another.

A clip stream needs a stream lease from each device. The SDK requests it by
unicast only from the devices it streams to (and keeps it for 15 s after the
last stream), so other applications on the LAN are unaffected. While a stream
cannot start, `playback.deferred_reason` says why: `NO_RESOLVED_ENDPOINT` (no
device matches the target yet), `UNKNOWN_CLASS` (the device has not answered
the lease request yet), `LEASE_UNAVAILABLE` (the lease is being renewed) or
`SUPERSEDED` (another application started streaming to that device later).
`hb.stream_endpoints()` / `add_stream_endpoint_listener()` report each device's
stream class (`Device.stream_class`) and lease state, and `Device.caps` holds
what the device reported about itself (`None` until it answers; `DeviceCaps.NONE`
for firmware that predates capability reports). A superseded device is handed
back automatically once the other application stops; call
`hb.reacquire_stream_ownership()` to take it back while that application is
still running.

## EventMap — the tuning side (optional)

Keep per-event strengths in one place and let `play("id")` resolve them,
so firing code never hard-codes them:

```python
em = hapbeat.EventMap.from_manifest("my-kit/my-kit-manifest.json")
hb = hapbeat.connect(event_map=em)
hb.play("sample-kit.sine_100hz")        # uses the kit manifest's clip_gain for this event
```

`EventMap` reads the kit manifest (schema 2.0.0) `parameters.clip_gain` — the
material strength of each event (0..1, default 1). The value sent is
`clamp(clip_gain × app_gain, 0, 1)`, where `app_gain` is the haptic file's
per-event `gain` times the `gain` you pass to `play`. `gain` multiplies
`clip_gain` (it never replaces it) and is not capped at 1: on a `clip_gain` of
0.5, `gain=2` sends 1.0. You can also build an EventMap by hand:
`EventMap.from_dict({"sample-kit.sine_100hz": 0.5})` (per-event gains, no kit).

### Haptic file — add targeting on top of the kit

The Studio-generated manifest holds kit content (clip_gain / clip), but not
app-side concerns like **which device/body part** an event goes to. Put those
in a *haptic file* (an EventMap overlay that references the kit), so `play(id)`
resolves the target without the caller passing it:

```json
// haptics.json
{
  "kit": "kits/my-kit",
  "events": {
    "sample-kit.sine_100hz": { "target": "player_1/pos_neck", "gain": 0.8 },
    "rain.loop":  { "target": "*/pos_back" }
  }
}
```

Targets are matched **positionally**: segment *i* of the target is compared
with segment *i* of the device address, `*` stands for one **whole** segment
(`pos_*` is compared literally — use `player_1/*`), and a short target
front-matches. Device addresses are always `player_<N>/<position>/group_<M>`,
defaulting to `player_1` / `group_1`, so a group has to be written
`*/*/group_2`: plain `group_2` is compared against the *player* slot and never
matches.

```python
hb = hapbeat.connect(app_name="MyApp", haptics="haptics.json")
hb.play("sample-kit.sine_100hz")     # goes to player_1/pos_neck at clip_gain × 0.8 — from the file
```

## Two playback modes: command and clip

The same `play(id)` call branches on the manifest:

| Manifest bucket | Mode | What happens |
|---|---|---|
| `events` | **command** | the SDK sends a PLAY; the **device** plays its installed clip |
| `stream_events` | **clip** | the SDK reads the WAV from the kit's `stream-clips/` and **streams** it over UDP |

Put the kit inside your project and call events by id — the per-event details
(clip_gain, loop, command vs clip, which WAV) live in the kit, not your code:

```
my-app/
  app.py
  kits/my-kit/
    my-kit-manifest.json     # the "haptic file" -> EventMap
    install-clips/           # command clips (flashed to the device via Studio)
    stream-clips/*.wav       # clip-mode WAVs the SDK streams
```

```python
import hapbeat
hb = hapbeat.connect(app_name="MyApp", kit="kits/my-kit")
hb.play("sample-kit.sine_100hz")     # command -> device plays its installed clip
rain = hb.play("rain.loop")  # clip -> returns a StreamPlayback
rain.pan = -0.5
rain.stop()                  # stops only this logical source
```

You can also stream an ad-hoc PCM16 buffer (e.g. a synthesized stereo cue where
L/R amplitude conveys direction):

```python
left = hb.stream_pcm(pcm_bytes, sample_rate=16000, channels=2,
                     target="player_1/left", loop=True)
right = hb.stream_pcm(other_pcm, target="player_1/right", loop=True)
left.gain = 0.5
```

PCM16 input is normalized to **16 kHz stereo** inside the shared endpoint mixer.
Sources aimed at one endpoint are mixed into one wire session; sources aimed at
different endpoints run independently. A full runnable example is in
[examples/clip_project/](examples/clip_project/).

## Generic OSC bridge

Any OSC tool (TouchOSC, Max/MSP, TouchDesigner, a DAW) can drive Hapbeat
without code. Run the bridge and send `/hapbeat/play <event_id> [target] [time] [gain] [pan]`:

```bash
hapbeat osc-bridge --listen 7702
```

See [docs/osc.md](docs/osc.md) for the address spec.

## CLI

```bash
hapbeat scan                       # list devices on the LAN
hapbeat play sample-kit.sine_100hz --gain 0.3
hapbeat stop-all
hapbeat launchpad                  # browser UI for all of the above
```

## Examples

Ready-to-run sample applications live in [examples/](examples/):
a psychophysics experiment, a breathing pacer, a haptic metronome,
a live trigger pad, a task-completion notifier, and a Morse transmitter.
Each is a single stdlib-only file — see [examples/README.md](examples/README.md).

## For AI coding agents

Working with Claude / Cursor / Copilot? Hand your agent [AGENTS.md](AGENTS.md) —
a single self-contained file with the SDK's model, API, project layout, and
pitfalls. One file is enough to get the whole picture.

> Use the Hapbeat Python SDK. Read `AGENTS.md` and follow its API and best practices.

## License

MIT © Hapbeat
