"""Project-style example: one kit, two playback modes, called by event id.

This shows the recommended Hapbeat Python project layout, mirroring the Unity
SDK: the kit (the *haptic file* = manifest + audio) lives inside your project,
and your code only ever calls events by id. The per-event details (intensity,
loop, command vs clip) are authored in the kit -- not in your firing code.

    clip_project/
        app.py                          <- this file (the caller)
        kits/
            demo-kit/
                demo-kit-manifest.json  <- the "haptic file" (EventMap source)
                install-clips/          <- command clips, flashed to the device
                stream-clips/           <- clip-mode WAVs the SDK streams
                    rumble.wav

Two modes, both invoked by the same play(id) call -- the manifest decides which:
  - command (manifest `events`)        -> device plays its installed clip
  - clip    (manifest `stream_events`) -> the SDK streams the WAV over UDP

Prerequisites:
    - A Hapbeat device on the same Wi-Fi/LAN.
    - For the command event (demo.tap): deploy the kit to the device with
      Hapbeat Studio so the clip is installed. (The clip event needs no deploy
      -- the SDK streams the WAV from stream-clips/.)

Run:
    python examples/clip_project/app.py
"""

import time
from pathlib import Path

import hapbeat

KIT = Path(__file__).resolve().parent / "kits" / "demo-kit"


def main() -> int:
    # connect(kit=...) loads the EventMap from the kit folder, so command/clip
    # events and clip WAV paths all resolve from one place.
    with hapbeat.connect(app_name="ClipDemo", kit=KIT) as hb:
        devices = hb.discover(timeout=1.0)
        print("Devices: " + (", ".join(d.address or d.ip for d in devices)
                             or "none (UDP still broadcasts)"))

        # Warm the clip cache so the first stream has no file-read latency.
        hb.preload_clips()

        # Command mode: just an event id. The device plays its installed clip.
        # (Needs the kit deployed to the device via Studio.)
        print("play demo.tap (command)")
        hb.play("demo.tap")
        time.sleep(1.0)

        # Clip mode: SAME call. The manifest marks demo.rumble as a stream
        # event, so the SDK reads stream-clips/rumble.wav and streams it.
        print("play demo.rumble (clip, streamed)")
        hb.play("demo.rumble")
        time.sleep(1.0)

        # Override the authored intensity at the call site if you need to.
        print("play demo.rumble at gain 0.3")
        hb.play("demo.rumble", gain=0.3)
        time.sleep(1.0)

        # Ad-hoc stereo cue (no manifest): per-channel amplitude = L/R balance.
        print("stream an ad-hoc stereo cue (right-biased)")
        import struct
        sr = 16000
        frames = bytearray()
        for i in range(int(sr * 0.3)):
            env = 1.0 - i / (sr * 0.3)
            left = int(2000 * env)
            right = int(8000 * env)
            frames += struct.pack("<hh", left, right)
        hb.stream_pcm(bytes(frames), sample_rate=sr, channels=2)
        time.sleep(0.6)

        hb.stop_all()
        print("done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
