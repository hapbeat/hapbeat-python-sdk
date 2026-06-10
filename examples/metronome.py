"""Haptic metronome with accents, subdivisions, and tempo ramps.

A silent metronome you feel instead of hear: practice rooms, drummers with
in-ears, dancers, and runners doing cadence training (ramp from your current
step rate to a goal over a few minutes).

Beats are scheduled on an absolute timeline (no cumulative drift), with a
hybrid sleep so timing stays tight even on Windows.

Prerequisites:
    - A Hapbeat device on the same Wi-Fi/LAN.
    - A kit with a short one-shot "tick" event (pass its id with --event).

Run:
    python examples/metronome.py --bpm 120
    python examples/metronome.py --bpm 96 --beats 3 --minutes 10
    python examples/metronome.py --ramp 150:180:300 --beats 0   # cadence trainer
    python examples/metronome.py --bpm 60 --subdiv 2            # eighth notes
"""

from __future__ import annotations

import argparse
import sys
import time

import hapbeat


def sleep_until(deadline: float) -> None:
    """Hybrid sleep: coarse sleep, then yield-spin the last ~2 ms."""
    while (remaining := deadline - time.perf_counter()) > 0:
        time.sleep(remaining - 0.002 if remaining > 0.002 else 0)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Haptic metronome (accents, subdivisions, tempo ramp)."
    )
    parser.add_argument("--bpm", type=float, default=100.0,
                        help="tempo in beats per minute (default 100)")
    parser.add_argument("--ramp", default=None, metavar="START:END:SECONDS",
                        help="linear tempo ramp, e.g. 150:180:300; overrides --bpm")
    parser.add_argument("--beats", type=int, default=4,
                        help="beats per bar; beat 1 is accented (0 = no accent)")
    parser.add_argument("--subdiv", type=int, default=1,
                        help="subdivisions per beat (default 1 = none)")
    parser.add_argument("--minutes", type=float, default=0.0,
                        help="run time in minutes (0 = until Ctrl-C)")
    parser.add_argument("--event", default="tick",
                        help="event id of a short one-shot in your kit")
    parser.add_argument("--accent-event", default=None,
                        help="separate event id for accented beats (default: --event)")
    parser.add_argument("--gain", type=float, default=0.5,
                        help="gain of normal beats (default 0.5)")
    parser.add_argument("--accent-gain", type=float, default=1.0,
                        help="gain of accented beats (default 1.0)")
    parser.add_argument("--subdiv-gain", type=float, default=0.25,
                        help="gain of subdivision ticks (default 0.25)")
    parser.add_argument("--target", default="",
                        help="device address; empty = all (broadcast)")
    args = parser.parse_args()

    if args.subdiv < 1:
        parser.error("--subdiv must be >= 1")

    if args.ramp:
        try:
            start_bpm, end_bpm, ramp_secs = (float(s) for s in args.ramp.split(":"))
        except ValueError:
            parser.error("--ramp must be START:END:SECONDS, e.g. 150:180:300")
    else:
        start_bpm = end_bpm = args.bpm
        ramp_secs = 0.0
    if start_bpm <= 0 or end_bpm <= 0:
        parser.error("BPM must be > 0")

    def bpm_at(elapsed: float) -> float:
        if ramp_secs <= 0:
            return start_bpm
        frac = min(1.0, elapsed / ramp_secs)
        return start_bpm + (end_bpm - start_bpm) * frac

    accent_event = args.accent_event or args.event
    deadline_s = args.minutes * 60.0 if args.minutes > 0 else None

    with hapbeat.connect(app_name="Metronome", default_target=args.target) as hb:
        devices = hb.discover(timeout=1.0)
        if devices:
            print("Devices: " + ", ".join(d.address or d.ip for d in devices))
        else:
            print("No device replied to PING -- continuing anyway (UDP broadcast).")

        tempo_txt = (f"{start_bpm:.0f}->{end_bpm:.0f} BPM over {ramp_secs:.0f}s"
                     if args.ramp else f"{start_bpm:.0f} BPM")
        print(f"{tempo_txt}, {args.beats or 'no'} beats/bar, "
              f"subdiv {args.subdiv}. Ctrl-C stops.")

        start = time.perf_counter() + 0.5
        next_t = start
        beat_in_bar = 0  # 0-based; 0 is the accent
        try:
            while True:
                elapsed = next_t - start
                if deadline_s is not None and elapsed >= deadline_s:
                    break
                accented = args.beats > 0 and beat_in_bar == 0
                bpm = bpm_at(elapsed)
                beat_interval = 60.0 / bpm

                sleep_until(next_t)
                if accented:
                    hb.play(accent_event, gain=args.accent_gain)
                else:
                    hb.play(args.event, gain=args.gain)
                marks = "".join(
                    ">" if args.beats > 0 and i == 0 else "."
                    for i in range(max(args.beats, 1))
                )
                pos = beat_in_bar if args.beats > 0 else 0
                print(f"\r{bpm:6.1f} BPM  beat {pos + 1:>2} {marks}", end="", flush=True)

                for s in range(1, args.subdiv):
                    sleep_until(next_t + beat_interval * s / args.subdiv)
                    hb.play(args.event, gain=args.subdiv_gain)

                next_t += beat_interval
                if args.beats > 0:
                    beat_in_bar = (beat_in_bar + 1) % args.beats
            print("\nDone.")
        except KeyboardInterrupt:
            print("\nStopped.")
        finally:
            hb.stop_all()
    return 0


if __name__ == "__main__":
    sys.exit(main())
