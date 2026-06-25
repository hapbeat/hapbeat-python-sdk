# Getting Started (Python)

Drive a Hapbeat device from a Python script in a few lines.

## Prerequisites

- Python 3.10+
- A Hapbeat device powered on and joined to the **same Wi-Fi/LAN** as your computer.
- A **kit deployed to the device** with [Hapbeat Studio](https://devtools.hapbeat.com).
  The kit defines the event ids you can play (e.g. `sample-kit.sine_100hz`).

## Install

```bash
pip install hapbeat-python-sdk        # library + CLI in your environment
pipx install hapbeat-python-sdk   # CLI only (hapbeat scan / play / launchpad), isolated
```

> `pipx` installs the `hapbeat` command in its own environment, so it is great
> for the CLI and the launchpad — but `import hapbeat` from your own scripts
> (and the `examples/`) needs a regular `pip install` in a venv.

## No-code: the launchpad

Prefer clicking to coding? Run the launchpad and a browser page opens with
buttons for play / metronome / breathing / Morse:

```bash
hapbeat launchpad
```

## Fire your first event

```python
import hapbeat

hb = hapbeat.connect(app_name="MyApp")
hb.play("sample-kit.sine_100hz", gain=0.5)
hb.close()
```

- `connect()` opens a UDP broadcast socket and starts a keep-alive so the
  device OLED shows your `app_name`.
- `play(event_id, gain)` sends a PLAY instruction. `gain` is 0..1; omit it to
  use the kit's authored intensity.

## Find devices

```python
with hapbeat.connect() as hb:
    for d in hb.discover(timeout=1.5):
        print(d.ip, d.address, d.firmware_version)
```

## Keep intensities out of your firing code

Use an `EventMap` so `play("id")` resolves the default gain from the kit
manifest (the *tuning* side, separate from *firing*):

```python
em = hapbeat.EventMap.from_manifest("my-kit/my-kit-manifest.json")
with hapbeat.connect(event_map=em) as hb:
    hb.play("sample-kit.sine_100hz")      # fires at the manifest's intensity
```

## Targeting specific devices

```python
hb.play("sample-kit.sine_100hz", target="player_1/chest")   # one device
hb.play("sample-kit.sine_100hz", target="*/chest")           # all chest devices
hb.play("sample-kit.sine_100hz")                              # broadcast to all
```

See the addressing spec in `hapbeat-contracts/specs/device-addressing.md`.

## Command vs clip (one call, two modes)

The same `play(id)` branches on the manifest bucket the event came from:

- `events` → **command**: the device plays a clip it already has installed.
- `stream_events` → **clip**: the SDK reads the WAV from the kit's
  `stream-clips/` and streams it over UDP.

Keep the kit inside your project and load it with `connect(kit=...)`:

```python
hb = hapbeat.connect(app_name="MyApp", kit="kits/my-kit")
hb.play("sample-kit.sine_100hz")   # command
hb.play("rain.loop")    # clip (streamed); hb.stop("rain.loop") ends it
```

Author clips as 16 kHz mono PCM16. Full example:
[examples/clip_project](https://github.com/hapbeat/hapbeat-python-sdk/tree/master/examples/clip_project).

## Next steps

- [Examples](https://github.com/hapbeat/hapbeat-python-sdk/tree/master/examples) —
  complete sample apps: a psychophysics experiment, breathing pacer, haptic
  metronome, live trigger pad, task notifier, and Morse transmitter.
- [OSC bridge](osc.md) — drive Hapbeat from TouchOSC / Max / TouchDesigner with no code.
- `hapbeat --help` — the CLI (`scan`, `play`, `stop-all`, `osc-bridge`).
