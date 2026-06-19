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
hapbeat osc-bridge --listen 7702       # OSC in on 7702, relays to UDP 7700
```

Or embed it:

```python
import hapbeat
from hapbeat.osc import OscBridge

hb = hapbeat.connect(app_name="osc")
OscBridge(hb, listen_port=7702).serve_forever()
```

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

## Example: TouchOSC

Add a button that sends `/hapbeat/play` with a string argument `impact.hit`
and a float `0.5`, to your computer's IP on port `7702`. Run the bridge on
that computer and the button fires the haptic event.
