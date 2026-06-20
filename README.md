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

hb = hapbeat.connect(app_name="MyExperiment")  # opens UDP broadcast + keep-alive
hb.play("impact.hit", gain=0.3)   # fire event "impact.hit" at gain 0.3
hb.play("impact.hit")             # gain omitted -> kit baseline intensity
hb.stop("impact.hit")
hb.stop_all()
hb.close()
```

or as a context manager:

```python
with hapbeat.connect(app_name="MyExperiment") as hb:
    hb.play("impact.hit")
```

`"impact.hit"` must be an event id present in the **kit deployed to the
device** (via [Hapbeat Studio](https://devtools.hapbeat.com)). The SDK sends
the *instruction*; the waveform lives in the kit on the device.

## Discovery

```python
for dev in hb.discover(timeout=1.5):
    print(dev.ip, dev.address, dev.firmware_version)
```

## EventMap — the tuning side (optional)

Keep per-event default gains in one place and let `play("id")` resolve them,
so firing code never hard-codes intensities:

```python
em = hapbeat.EventMap.from_manifest("my-kit/my-kit-manifest.json")
hb = hapbeat.connect(event_map=em)
hb.play("impact.hit")        # uses the kit manifest's intensity for this event
```

`EventMap` reads the kit manifest (schema 2.0.0) `intensity` as the baseline
gain. You can also build one by hand: `EventMap.from_dict({"impact.hit": 0.5})`.

### Haptic file — add targeting on top of the kit

The Studio-generated manifest holds kit content (intensity / clip), but not
app-side concerns like **which device/body part** an event goes to. Put those
in a *haptic file* (an EventMap overlay that references the kit), so `play(id)`
resolves the target without the caller passing it:

```json
// haptics.json
{
  "kit": "kits/my-kit",
  "events": {
    "impact.hit": { "target": "player_1/chest", "gain": 0.8 },
    "rain.loop":  { "target": "*/back" }
  }
}
```

```python
hb = hapbeat.connect(app_name="MyApp", haptics="haptics.json")
hb.play("impact.hit")     # goes to player_1/chest at gain 0.8 — from the file
```

## Two playback modes: command and clip

The same `play(id)` call branches on the manifest:

| Manifest bucket | Mode | What happens |
|---|---|---|
| `events` | **command** | the SDK sends a PLAY; the **device** plays its installed clip |
| `stream_events` | **clip** | the SDK reads the WAV from the kit's `stream-clips/` and **streams** it over UDP |

Put the kit inside your project and call events by id — the per-event details
(intensity, loop, command vs clip, which WAV) live in the kit, not your code:

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
hb.play("impact.hit")     # command -> device plays its installed clip
hb.play("rain.loop")      # clip    -> SDK streams stream-clips/<wav> over UDP
hb.stop("rain.loop")      # ends the active stream
```

You can also stream an ad-hoc PCM16 buffer (e.g. a synthesized stereo cue where
L/R amplitude conveys direction):

```python
hb.stream_pcm(pcm_bytes, sample_rate=16000, channels=2)
```

Author clips as **16 kHz mono PCM16** (the device plays at 16 kHz; the SDK does
not resample). A full runnable example is in
[examples/clip_project/](examples/clip_project/).

## Generic OSC bridge

Any OSC tool (TouchOSC, Max/MSP, TouchDesigner, a DAW) can drive Hapbeat
without code. Run the bridge and send `/hapbeat/play <event_id> [target] [time] [gain]`:

```bash
hapbeat osc-bridge --listen 7702
```

See [docs/osc.md](docs/osc.md) for the address spec.

## CLI

```bash
hapbeat scan                       # list devices on the LAN
hapbeat play impact.hit --gain 0.3
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
