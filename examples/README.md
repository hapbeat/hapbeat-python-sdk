# Examples

Self-contained sample applications built on the level-1 fire API
(`play` / `stop` / `EventMap` / discovery). Each one is a single file with no
dependencies beyond the SDK itself -- copy it out and make it yours.

> **Just want to click around?** Run `hapbeat launchpad` for a single browser
> page that fires events and runs a metronome / breathing pacer / Morse live,
> with no per-example launching. The files below are where you read and copy
> the full code.

| Sample | For | Highlights | Kit needs | Default event |
|---|---|---|---|---|
| [minimal.py](minimal.py) | everyone | the smallest possible script: discover, play, stop | 1 short one-shot | `impact.hit` |
| [psychophysics_experiment.py](psychophysics_experiment.py) | researchers | detection experiment: constant stimuli or adaptive staircase, catch trials, RT, CSV + reproducibility sidecar | 1 short one-shot | `impact.hit` |
| [breathing_pacer.py](breathing_pacer.py) | wellbeing / biofeedback | guided breathing: gain-ramped ticks, inhale markers, pattern morphing, tick log for ECG/PPG sync | 1 short, soft one-shot | `tick` |
| [metronome.py](metronome.py) | musicians / runners | silent metronome: live tempo keys + tap tempo, odd meters (2+2+3), gap training, ramps, count-in | 1 short one-shot | `tick` |
| [haptic_pad.py](haptic_pad.py) | live performance / Wizard-of-Oz | keyboard soundboard: loop toggling, per-pad trim, session record & replay, device re-scan | mapping source (below) | n/a |
| [task_notifier.py](task_notifier.py) | developers / ML | wrap any command: success/failure buzz, heartbeat, output-regex pulses, stall watchdog, `--test` dry run | 1 short one-shot | `impact.hit` |
| [morse_text.py](morse_text.py) | accessibility / learners | text to Morse vibrations: Farnsworth timing, Koch receive trainer (`--quiz`), play()/stop() duration control | looping buzz (or 2 one-shots) | `buzz` |

## Before you run anything

1. Power a Hapbeat device and join it to the **same Wi-Fi/LAN** as your computer.
2. Deploy a **kit** with [Hapbeat Studio](https://devtools.hapbeat.com) -- the kit
   defines which event ids exist on the device.
3. Pass the event id(s) from *your* kit via `--event` (the defaults in the
   table are placeholders, not guarantees).

UDP is fire-and-forget: if you feel nothing but `hapbeat scan` sees the
device, the usual cause is an event id that does not exist in the deployed
kit -- check the kit in Studio. `task_notifier.py --test` is a quick
end-to-end check.

## Gain vs. kit intensity

`play(event_id, gain)` sends an absolute wire gain, clamped to 0..1 by the
SDK. When `gain` is omitted, the event fires at **1.0** -- unless an
`EventMap` is bound, in which case the kit manifest's authored `intensity`
is used. The samples that take gain flags validate the 0..1 range up front
so what you see is what the device gets (`haptic_pad`'s `--master` is a
multiplier instead and is clamped to its 0.1..1.0 range).

## Common flags

- `--event <id>` -- which kit event to fire (see each file's `--help`).
  `haptic_pad.py` maps keys instead, via `--map` / `--manifest` / `--events`;
  `minimal.py` is a plain script with a constant at the top.
- `--target <address>` -- limit to one device or position, e.g.
  `player_1/chest` or `*/chest`; empty targets every device (broadcast).

## haptic_pad mapping file

```json
{
  "1": {"event": "impact.hit", "gain": 0.6},
  "2": {"event": "rumble.loop", "gain": 0.8, "loop": true},
  "3": "heartbeat.slow"
}
```

`"loop": true` marks events whose kit clip loops -- the pad then toggles them
(press to start, press again to stop). The `s` key saves your live gain
trims back to a file in this format.

## Analysing experiment output

```python
import pandas as pd
df = pd.read_csv("results_P01_20260610_120000.csv")
df.groupby("level")["detected"].mean()       # psychometric points
```

Run the same session again with the `seed` recorded in the `.meta.json`
sidecar to reproduce the exact trial order.
