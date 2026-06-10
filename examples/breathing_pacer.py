"""Haptic guided-breathing pacer.

Guides slow breathing entirely through touch: a soft tick repeats at a steady
rate while its intensity ramps up as you inhale, holds at the top, ramps down
as you exhale, and rests at the bottom. Useful for relaxation training,
calm-tech prototypes, and HRV/biofeedback study conditions where the
participant must keep their eyes closed (no screen, no audio).

Prerequisites:
    - A Hapbeat device on the same Wi-Fi/LAN.
    - A kit with a short, soft one-shot event (pass its id with --event).

Run:
    python examples/breathing_pacer.py --preset box --minutes 3
    python examples/breathing_pacer.py --preset 478
    python examples/breathing_pacer.py --pattern 5.5,0,5.5,0 --minutes 5

Patterns are "inhale,hold,exhale,hold" durations in seconds.
"""

from __future__ import annotations

import argparse
import sys
import time

import hapbeat

PRESETS = {
    "box": (4.0, 4.0, 4.0, 4.0),        # box breathing
    "478": (4.0, 7.0, 8.0, 0.0),        # 4-7-8 relaxation
    "coherent": (5.5, 0.0, 5.5, 0.0),   # ~5.5 breaths/min coherence
}


def sleep_until(deadline: float) -> None:
    """Hybrid sleep: coarse sleep, then yield-spin the last ~2 ms."""
    while (remaining := deadline - time.perf_counter()) > 0:
        time.sleep(remaining - 0.002 if remaining > 0.002 else 0)


def run_phase(
    hb: hapbeat.Hapbeat,
    event_id: str,
    label: str,
    duration: float,
    gain_at,  # callable: progress 0..1 -> gain
    tick_interval: float,
    phase_start: float,
) -> None:
    """Fire ramped ticks from phase_start for `duration` seconds."""
    n_ticks = max(1, round(duration / tick_interval))
    for i in range(n_ticks):
        t = phase_start + i * tick_interval
        sleep_until(t)
        progress = i / (n_ticks - 1) if n_ticks > 1 else 1.0
        gain = gain_at(progress)
        hb.play(event_id, gain=gain)
        remaining = max(0.0, phase_start + duration - time.perf_counter())
        bar = "#" * round(gain * 20)
        print(f"\r{label:<8} {remaining:4.1f}s  [{bar:<20}]", end="", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Haptic guided-breathing pacer.")
    parser.add_argument("--preset", choices=sorted(PRESETS), default="box",
                        help="breathing pattern preset (default box)")
    parser.add_argument("--pattern", default=None,
                        help='custom "inhale,hold,exhale,hold" seconds, e.g. 4,7,8,0')
    parser.add_argument("--minutes", type=float, default=3.0,
                        help="session length in minutes (whole cycles, default 3)")
    parser.add_argument("--event", default="tick",
                        help="event id of a short, soft one-shot in your kit")
    parser.add_argument("--tick-rate", type=float, default=2.0,
                        help="ticks per second (default 2)")
    parser.add_argument("--peak-gain", type=float, default=0.8,
                        help="gain at full inhale (default 0.8)")
    parser.add_argument("--floor-gain", type=float, default=0.15,
                        help="gain at full exhale (default 0.15)")
    parser.add_argument("--target", default="",
                        help="device address; empty = all (broadcast)")
    args = parser.parse_args()

    if args.tick_rate <= 0:
        parser.error("--tick-rate must be > 0")
    if args.pattern:
        parts = [float(s) for s in args.pattern.split(",") if s.strip()]
        if len(parts) == 3:
            parts.append(0.0)
        if len(parts) != 4:
            parser.error("--pattern needs 3 or 4 comma-separated durations")
        pattern = tuple(parts)
    else:
        pattern = PRESETS[args.preset]
    if sum(pattern) <= 0:
        parser.error("--pattern must have a positive total duration")

    inhale, hold_top, exhale, hold_bottom = pattern
    peak, floor = args.peak_gain, args.floor_gain
    tick_interval = 1.0 / args.tick_rate
    cycle = sum(pattern)
    phases = [
        ("IN",   inhale,      lambda p: floor + (peak - floor) * p),
        ("HOLD", hold_top,    lambda p: peak),
        ("OUT",  exhale,      lambda p: peak - (peak - floor) * p),
        ("REST", hold_bottom, lambda p: floor),
    ]

    with hapbeat.connect(app_name="BreathPacer", default_target=args.target) as hb:
        devices = hb.discover(timeout=1.0)
        if devices:
            print("Devices: " + ", ".join(d.address or d.ip for d in devices))
        else:
            print("No device replied to PING -- continuing anyway (UDP broadcast).")

        n_cycles = max(1, round(args.minutes * 60.0 / cycle))
        print(f"Pattern {pattern} -> {cycle:.1f}s/cycle, "
              f"{n_cycles} cycles (~{n_cycles * cycle / 60.0:.1f} min). Ctrl-C stops.")

        start = time.perf_counter() + 1.0  # short lead-in
        try:
            for c in range(n_cycles):
                print(f"\ncycle {c + 1}/{n_cycles}")
                t = start + c * cycle
                for label, duration, gain_at in phases:
                    if duration <= 0:
                        continue
                    run_phase(hb, args.event, label, duration, gain_at,
                              tick_interval, t)
                    t += duration
            print("\nSession complete.")
        except KeyboardInterrupt:
            print("\nStopped.")
        finally:
            hb.stop_all()
    return 0


if __name__ == "__main__":
    sys.exit(main())
