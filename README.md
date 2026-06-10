# Hapbeat Python SDK

Drive [Hapbeat](https://hapbeat.com) haptic devices from Python over Wi-Fi UDP.
For researchers (PsychoPy / Jupyter / ROS), media artists, and anyone
prototyping haptics in Python.

> **📚 Docs**: <https://devtools.hapbeat.com/docs/sdk-integration/>

This is the **level-1** SDK: a script can drive Hapbeat with a few lines. The
fire side (`play` / `stop`) and the tuning side (`EventMap`) are kept
orthogonal and linked only by event id — the same design as the Hapbeat Unity
SDK.

## Install

```bash
pip install hapbeat            # core (zero dependencies, stdlib socket only)
pip install "hapbeat[osc]"     # + generic OSC bridge (TouchOSC / Max / TD)
```

> PyPI publish is pending; until then install from source:
> `pip install -e .` inside a clone, or `pip install git+https://github.com/hapbeat/hapbeat-python-sdk.git`.

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
```

## Examples

Ready-to-run sample applications live in [examples/](examples/):
a psychophysics experiment, a breathing pacer, a haptic metronome,
a live trigger pad, a task-completion notifier, and a Morse transmitter.
Each is a single stdlib-only file — see [examples/README.md](examples/README.md).

## License

MIT © Hapbeat
