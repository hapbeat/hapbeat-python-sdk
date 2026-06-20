# Generic OSC bridge

Drive Hapbeat from any OSC-capable tool — **TouchOSC, Max/MSP, TouchDesigner,
vvvv, DAWs** — without writing code. The bridge listens for OSC and relays it
to devices over UDP.

This is the *transport* form of OSC (you speak directly to Hapbeat).
App-specific schemas (e.g. VRChat avatar parameters) are handled by their own
dedicated bridge that translates into these same calls.

## Run it

```bash
pip install "hapbeat-python-sdk[osc]"

# plain (command-only, target via OSC arg)
hapbeat osc-bridge --listen 7702

# with a haptic file: OSC events route command/clip and pick up per-event
# target + gain, so the sender only needs the event id (recommended)
hapbeat osc-bridge --listen 7702 --haptics haptics.json
# or a kit folder (intensity/clip only, no targeting): --kit kits/my-kit
```

Or embed it:

```python
import hapbeat
from hapbeat.osc import OscBridge

hb = hapbeat.connect(app_name="osc", haptics="haptics.json")  # or kit="kits/my-kit"
OscBridge(hb, listen_port=7702).serve_forever()
```

With a haptic file loaded, `/hapbeat/play <id>` (no target) streams clip events
and routes command events to the target authored in the file. Without one,
every `/hapbeat/play` is a command broadcast.

## OSC address map

Defined in `hapbeat-contracts/specs/message-format.md` §6. Trailing arguments
are optional (sensible defaults are used).

| Address | Arguments |
|---|---|
| `/hapbeat/play` | `event_id` `[target]` `[target_time_us]` `[gain]` |
| `/hapbeat/stop` | `event_id` `[target]` |
| `/hapbeat/stop-all` | `[target]` |
| `/hapbeat/ping` | — |

- `event_id` — string; must exist in the kit on the device.
- `target` — device address; empty string = broadcast (all devices).
- `gain` — float 0..1.

## Example: TouchOSC as a wireless haptic remote

A phone running TouchOSC becomes a no-code haptic remote: buttons send
`/hapbeat/play <event_id>` to your computer's IP on port `7702`, the bridge
relays to the devices. Great for live performance, exhibitions, and
Wizard-of-Oz studies. Load a haptic file so each button just names an event
and the file decides target/strength/clip:

```bash
hapbeat osc-bridge --listen 7702 --haptics haptics.json
```

A full runnable example (haptic file + a keyboard sender to test without a
phone) is in
[examples/osc_remote/](https://github.com/hapbeat/hapbeat-python-sdk/tree/master/examples/osc_remote).
