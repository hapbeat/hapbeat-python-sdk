"""Release check for unicast sending -- run it, listen, answer two questions.

    python examples/verify_unicast.py

Power on ONE Hapbeat on the same Wi-Fi as this PC. Nothing else to set up: the
tone is synthesized here, so no kit has to be deployed.

What it checks, in order:

1. Vibration quality. A *continuous* 100 Hz sine is the harshest case for the
   transport -- every late or lost packet is audible -- so the script streams
   one while alternating broadcast and unicast. Broadcast is expected to
   stutter every 100-300 ms (the AP holds group-addressed frames until its next
   DTIM beacon whenever any client on it is power-saving); unicast should be
   smooth.

2. Windows ICMP survival. Unicasting at a device that is powered off makes
   Windows raise ConnectionResetError on the *next* recvfrom(). Before v0.2.0
   that killed the receive thread and the SDK went deaf to every PONG until the
   process restarted. The script keeps streaming while you power-cycle the
   device, and reports whether it is found again.

Verdict is printed at the end: PASS means the release is good to publish.
"""

from __future__ import annotations

import array
import math
import sys
import time

import hapbeat

TONE_HZ = 100.0           # continuous low sine = worst case for the transport
SAMPLE_RATE = 16000       # the device plays 16 kHz PCM16
AMPLITUDE = 0.8           # headroom; loudness comes from GAIN below
GAIN = 0.6                # wire gain (0..1) -- raise if you can barely feel it
PHASE_SECONDS = 5         # per A/B phase: long enough to hear, short enough to compare
ICMP_STREAM_SECONDS = 90  # long enough to power-cycle inside


def sine_pcm(seconds: float) -> bytes:
    """A continuous 100 Hz sine as mono PCM16, phase-continuous throughout."""
    n = int(SAMPLE_RATE * seconds)
    step = 2.0 * math.pi * TONE_HZ / SAMPLE_RATE
    buf = array.array(
        "h", (int(AMPLITUDE * 32767 * math.sin(step * i)) for i in range(n))
    )
    if sys.byteorder == "big":  # the wire format is little-endian
        buf.byteswap()
    return buf.tobytes()


def rule(title: str) -> None:
    print("\n" + "=" * 66 + f"\n  {title}\n" + "=" * 66, flush=True)


def countdown(seconds: int, label: str) -> None:
    for left in range(seconds, 0, -1):
        print(f"\r  {label}  {left:2d}s left ", end="", flush=True)
        time.sleep(1)
    print("\r" + " " * 46 + "\r", end="", flush=True)


def ab_phase(hb: hapbeat.Hapbeat, pcm: bytes, unicast: bool) -> None:
    hb.unicast = unicast
    hb.stream_pcm(pcm, sample_rate=SAMPLE_RATE, channels=1, gain=GAIN)
    # _stream_dests is internal, but it is the one thing worth showing: it
    # proves the two phases really took different paths.
    dests = hb._stream_dests  # noqa: SLF001
    where = "broadcast" if dests is None else f"unicast -> {dests}"
    label = "[A] UNICAST  " if unicast else "[B] BROADCAST"
    print(f"  {label}  ({where})", flush=True)
    countdown(PHASE_SECONDS, label)
    time.sleep(0.3)


def main() -> int:
    print("Hapbeat unicast release check")
    print("\nPower on the Hapbeat and put it on the same Wi-Fi as this PC.")
    input("Press Enter when ready: ")

    hb = hapbeat.connect(app_name="VerifyUnicast")
    try:
        rule("0. Discovery")
        devices = hb.discover(timeout=2.0)
        if not devices:
            print("  FAIL: no device found.")
            print("    - is the Hapbeat on the same Wi-Fi as this PC?")
            print("    - on a multi-NIC PC, does the Hapbeat's NIC have the route?")
            print("    - hapbeat-helper / Studio may keep running; they coexist")
            return 1
        for d in devices:
            print(f"  found {d.ip}  address={d.address!r}  fw={d.firmware_version}")

        rule("1. Vibration quality -- continuous 100 Hz sine, A/B")
        print("  Same tone, only the send path changes.")
        print("  [B] is expected to stutter; [A] should be smooth.\n")
        pcm = sine_pcm(PHASE_SECONDS)
        for _ in range(2):
            ab_phase(hb, pcm, unicast=False)
            ab_phase(hb, pcm, unicast=True)
        hb.stop_all()

        answer = input("\n  Was [A] UNICAST smoother than [B]? [y/n]: ")
        quality_ok = answer.strip().lower().startswith("y")

        rule("2. Windows ICMP -- does a power cycle lose the device?")
        hb.unicast = True
        print(f"  Streaming by unicast for {ICMP_STREAM_SECONDS}s. While it runs:\n")
        print("    1. after ~10s, switch the Hapbeat OFF")
        print("    2. leave it off 10-20s (we keep unicasting at a dead host)")
        print("    3. switch it back ON\n")
        input("  Press Enter to start: ")

        hb.stream_pcm(sine_pcm(ICMP_STREAM_SECONDS), sample_rate=SAMPLE_RATE,
                      channels=1, gain=GAIN)
        seen_zero = False
        recovered = False
        for elapsed in range(ICMP_STREAM_SECONDS):
            alive = len(hb._alive_devices())  # noqa: SLF001
            if alive == 0:
                seen_zero = True
            elif seen_zero:
                recovered = True
            note = "device is OFF" if alive == 0 else ("RECOVERED" if recovered else "")
            print(f"\r  [{elapsed:3d}s] alive devices: {alive}  {note:<16}",
                  end="", flush=True)
            if recovered and elapsed > 5:
                time.sleep(5)  # a few more seconds so you can feel it resume
                break
            time.sleep(1)
        print()
        hb.stop_all()

        rule("Result")
        icmp_ok = seen_zero and recovered
        if not seen_zero:
            print("  ?  ICMP: never saw the device go away -- did you power it off?")
            print("     Re-run and make sure it is off while the stream is running.")
        elif icmp_ok:
            print("  OK ICMP: device was found again after the power cycle")
        else:
            print("  NG ICMP: device never came back after power-on")
            print("     -> the receive thread died. Do NOT publish this release.")
        print(f"  {'OK' if quality_ok else 'NG'} quality: unicast smoother than "
              f"broadcast = {'yes' if quality_ok else 'no'}")

        ok = icmp_ok and quality_ok
        print("\n  VERDICT: " + ("PASS -- good to publish" if ok else "FAIL / needs a look"))
        return 0 if ok else 1
    finally:
        hb.close()


if __name__ == "__main__":
    raise SystemExit(main())
