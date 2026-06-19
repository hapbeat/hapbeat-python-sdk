# clip_project — project-style example (command + clip)

The recommended way to structure a Hapbeat Python app, mirroring the Unity SDK:
the **kit lives inside your project**, and your code calls events **by id only**.
Per-event details (intensity, loop, command vs clip) are authored in the kit —
the *haptic file* — not in your firing code.

```
clip_project/
  app.py                          the caller (plays events by id)
  kits/
    demo-kit/
      demo-kit-manifest.json      the haptic file -> EventMap
      install-clips/              command clips (flashed to the device via Studio)
      stream-clips/
        rumble.wav                clip-mode WAV the SDK streams
```

## Two modes, one call

`hb.play(event_id)` branches on the manifest, so the call site is identical:

| Manifest bucket | Mode | What happens |
|---|---|---|
| `events` | command | the SDK sends a PLAY; the **device** plays its installed clip |
| `stream_events` | clip | the SDK reads the WAV from `stream-clips/` and **streams** it over UDP |

## Run

```bash
python examples/clip_project/app.py
```

- The **clip** event (`demo.rumble`) works immediately — the SDK streams
  `stream-clips/rumble.wav`.
- The **command** event (`demo.tap`) needs the kit deployed to the device with
  [Hapbeat Studio](https://devtools.hapbeat.com) first (the clip must be
  installed on the device).

## How the caller stays clean

```python
import hapbeat
hb = hapbeat.connect(app_name="ClipDemo", kit="kits/demo-kit")
hb.play("demo.rumble")          # intensity 0.6, streamed — all from the kit
hb.play("demo.tap")             # command — device plays its clip
```

`connect(kit=...)` loads the EventMap from the kit folder and resolves clip WAV
paths from `<kit>/stream-clips/`. Authoring (which WAV, how loud) happens in
Studio; your code never hard-codes intensities or file paths.

Author clips as **16 kHz mono PCM16** (the device plays at 16 kHz; the SDK does
not resample).
