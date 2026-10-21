"""Quick start: make a Hapbeat buzz in 3 lines -- no kit, no Studio deploy.

The SDK streams a WAV from your PC straight to every Hapbeat on the LAN, so
nothing has to be installed on the device first. hit_80hz.wav (next to this
file) is a 0.25 s, 80 Hz pulse; any PCM16 WAV of your own works the same way.

Prerequisites:
    - pip install hapbeat-python-sdk
    - A Hapbeat device powered on and joined to the same Wi-Fi/LAN.

Run:
    python examples/quickstart_clip.py

Next step: fire events by id from a kit deployed with Hapbeat Studio
(see minimal.py and clip_project/).
"""

import time
from pathlib import Path

import hapbeat

HIT_WAV = Path(__file__).with_name("hit_80hz.wav")

with hapbeat.connect(app_name="QuickStart") as hb:
    hb.play_clip_file(HIT_WAV, gain=0.6)
    time.sleep(1.0)  # keep the connection open while the clip streams
