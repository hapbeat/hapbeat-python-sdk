# osc_remote — phone as a wireless haptic remote (TouchOSC)

Drive Hapbeat from **any OSC tool with no code on the tool side**. The headline
use case: a phone running **TouchOSC** becomes a wireless haptic remote — an
operator taps buttons on the phone and participants feel cues. Great for **live
performance, exhibition booths, and Wizard-of-Oz studies** (the wireless cousin
of [haptic_pad.py](../haptic_pad.py)).

```
TouchOSC (phone)  --/hapbeat/play "demo.rumble"-->  hapbeat osc-bridge  --UDP-->  device(s)
   buttons, no code              over Wi-Fi            (this machine)      Wi-Fi
```

## Why the haptic file matters here

The phone just sends an **event id** (`/hapbeat/play demo.rumble`). Everything
else — which device/body part it targets, how strong, command vs streamed clip —
comes from the **haptic file** ([haptics.json](haptics.json)) loaded by the
bridge. So you re-aim or re-balance cues by editing one file, never the phone.

`haptics.json` references the kit and adds per-event `target` / `gain`:

```json
{
  "kit": "kits/demo-kit",
  "events": {
    "demo.tap":    { "target": "player_1/chest", "gain": 0.8 },
    "demo.rumble": { "target": "*/back",          "gain": 0.6 }
  }
}
```

This folder is self-contained: copy `osc_remote/` anywhere, swap `kits/demo-kit`
for your own kit, and run. (`gain` 0.8 / `*/back` etc. are placeholders — set
`target` to match your device, or `""` to broadcast to all for a first test.)

## Run

```bash
# 1) start the bridge with the haptic file (command + clip both route)
hapbeat osc-bridge --haptics examples/osc_remote/haptics.json

# 2a) test from the keyboard (no phone needed)
pip install "hapbeat-python-sdk[osc]"
python examples/osc_remote/send_demo.py        # 1 = tap, 2 = rumble

# 2b) or point TouchOSC at  <this-machine-ip>:7702  and map buttons to:
#     /hapbeat/play   with string arg "demo.tap" / "demo.rumble"
#     /hapbeat/stop-all
```

## OSC addresses (the bridge listens for these)

| Address | Args | Effect |
|---|---|---|
| `/hapbeat/play` | `event_id` `[target]` `[target_time_us]` `[gain]` | play an event (target/gain from the haptic file if omitted) |
| `/hapbeat/stop` | `event_id` `[target]` | stop one event |
| `/hapbeat/stop-all` | `[target]` | stop everything |
| `/hapbeat/ping` | — | discovery / keep-alive |

Omit `target` to let the haptic file decide; send one to override per message.

## TouchOSC layout (sketch)

Two push buttons + one panic button:

- Button "Tap"   → OSC message `/hapbeat/play` with string `demo.tap`
- Button "Rumble"→ OSC message `/hapbeat/play` with string `demo.rumble`
- Button "Stop"  → OSC message `/hapbeat/stop-all`

In TouchOSC's connection settings, set the host to the machine running the
bridge and the port to `7702`.
