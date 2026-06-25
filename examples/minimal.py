"""Minimal level-1 example: fire a haptic event from a plain Python script.

Prerequisites:
    - A Hapbeat device powered on and joined to the same Wi-Fi/LAN.
    - A kit deployed to the device (via Hapbeat Studio) that contains an event
      whose id matches the one below.

Run:
    python examples/minimal.py
"""

import time

import hapbeat

EVENT_ID = "sample-kit.sine_100hz"  # <- change to an event id present in your kit

with hapbeat.connect(app_name="PyExample") as hb:
    # Optional: see which devices are on the network.
    for dev in hb.discover(timeout=1.5):
        print(f"found {dev.ip}  {dev.address or '?'}  fw={dev.firmware_version or '?'}")

    # Fire at full gain (1.0), then again softer. To fire at the kit's
    # authored intensity instead, bind an EventMap:
    #   hb = hapbeat.connect(event_map=hapbeat.EventMap.from_manifest("..."))
    hb.play(EVENT_ID)
    time.sleep(0.5)
    hb.play(EVENT_ID, gain=0.3)
    time.sleep(0.5)

    hb.stop_all()
    print("done")
