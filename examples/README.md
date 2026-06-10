# Examples

Self-contained sample applications built on the level-1 fire API
(`play` / `stop` / `EventMap` / discovery). Each one is a single file with no
dependencies beyond the SDK itself — copy it out and make it yours.

| Sample | For | What it shows |
|---|---|---|
| [minimal.py](minimal.py) | everyone | the smallest possible script: discover, play, stop |
| [psychophysics_experiment.py](psychophysics_experiment.py) | researchers | a vibrotactile detection experiment (constant stimuli, catch trials, RT, CSV output) |
| [breathing_pacer.py](breathing_pacer.py) | wellbeing / biofeedback | guided breathing through gain-ramped ticks (box / 4-7-8 / coherent presets) |
| [metronome.py](metronome.py) | musicians / runners | silent metronome with accents, subdivisions, and tempo ramps for cadence training |
| [haptic_pad.py](haptic_pad.py) | live performance / Wizard-of-Oz | keyboard soundboard: keys 1-9 fire events, master gain, mapping from a kit manifest |
| [task_notifier.py](task_notifier.py) | developers | wrap any command; feel success/failure, soft heartbeat while it runs |
| [morse_text.py](morse_text.py) | accessibility / demos | text to Morse vibrations; duration control via play()/stop() on a looping event |

## Before you run anything

1. Power a Hapbeat device and join it to the **same Wi-Fi/LAN** as your computer.
2. Deploy a **kit** with [Hapbeat Studio](https://devtools.hapbeat.com) — the kit
   defines which event ids exist on the device.
3. Pass the event id(s) from *your* kit via `--event` (defaults like
   `impact.hit` are placeholders, not guarantees).

What kind of event each sample wants:

- **Short one-shot** (a click/impact clip): psychophysics, metronome,
  breathing pacer, task notifier, haptic pad.
- **Looping buzz** (loop enabled, or a clip longer than ~1 s): morse_text in
  its default play/stop mode. With two one-shot clips use
  `--dit-event/--dah-event` instead.

UDP is fire-and-forget: if you feel nothing but `hapbeat scan` sees the
device, the usual cause is an event id that does not exist in the deployed
kit — check the kit in Studio.

## Common flags

Every sample accepts:

- `--event <id>` — which kit event to fire (see each file's `--help` for more).
- `--target <address>` — limit to one device or position, e.g.
  `player_1/chest` or `*/chest`; empty targets every device (broadcast).
