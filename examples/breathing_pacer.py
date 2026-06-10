"""Haptic guided-breathing pacer.

Guides slow breathing entirely through touch: a soft tick repeats at a steady
rate while its intensity ramps up as you inhale, holds at the top, ramps down
as you exhale, and rests at the bottom. An extra double-tick marks the start
of each inhale so the direction is unambiguous even with eyes closed. Useful
for relaxation training, calm-tech prototypes, and HRV/biofeedback study
conditions with no screen and no audio.

Session design features:
    --ramp-to    morph the pattern over the session (e.g. start at 4,4,4,4
                 and arrive at 4,7,8,0) -- the standard way to ease a
                 participant into slower breathing.
    --log        write one CSV row per tick (epoch + monotonic timestamps)
                 so the stimulus timeline can be aligned with ECG/PPG
                 recordings. Rows log the UDP *send* time; the actuator adds
                 a few ms of LAN latency.
    lead-in /    three slow pulses announce the start; a triple pulse marks
    end cue      the end, so the wearer can tell "finished" from "crashed".

Prerequisites:
    - A Hapbeat device on the same Wi-Fi/LAN.
    - A kit with a short, soft one-shot event (pass its id with --event).
      Clips of ~50 ms or less keep the double-tick marker crisp.

Run:
    python examples/breathing_pacer.py --preset box --minutes 3
    python examples/breathing_pacer.py --preset 478 --cycles 10
    python examples/breathing_pacer.py --pattern 4,4,4,4 --ramp-to 4,7,8,0 \
        --minutes 5 --log session.csv

Patterns are "inhale,hold,exhale,hold" durations in seconds.
"""

from __future__ import annotations

import argparse
import csv
import math
import sys
import time

import hapbeat

PRESETS = {
    "box": (4.0, 4.0, 4.0, 4.0),        # box breathing
    "478": (4.0, 7.0, 8.0, 0.0),        # 4-7-8 relaxation
    "coherent": (5.5, 0.0, 5.5, 0.0),   # ~5.5 breaths/min coherence
}


def parse_pattern(text: str) -> tuple[float, float, float, float]:
    """Parse "inhale,hold,exhale[,hold]" into a 4-tuple of seconds."""
    parts = []
    for s in text.split(","):
        if not s.strip():  # "4,,8,2" is ambiguous -- write the 0 explicitly
            raise ValueError("pattern has an empty value (write 0 explicitly)")
        parts.append(float(s))
    if len(parts) == 3:
        parts.append(0.0)
    if len(parts) != 4:
        raise ValueError("pattern needs 3 or 4 comma-separated durations")
    if any(p < 0 for p in parts):
        raise ValueError("pattern durations must be >= 0")
    if sum(parts) <= 0:
        raise ValueError("pattern must have a positive total duration")
    return tuple(parts)  # type: ignore[return-value]


def interp_pattern(
    start: tuple[float, ...], end: tuple[float, ...], frac: float
) -> tuple[float, ...]:
    """Element-wise linear interpolation between two patterns."""
    return tuple(a + (b - a) * frac for a, b in zip(start, end))


def sleep_until(deadline: float) -> None:
    """Hybrid sleep: coarse sleep, then yield-spin the last ~20 ms.

    The wide margin matters on Windows/Python 3.10 where time.sleep can
    overshoot by the 15.6 ms timer resolution.
    """
    while (remaining := deadline - time.perf_counter()) > 0:
        time.sleep(remaining - 0.02 if remaining > 0.02 else 0)


def run_phase(
    hb: hapbeat.Hapbeat,
    event_id: str,
    label: str,
    duration: float,
    gain_at,  # callable: progress 0..1 -> gain
    tick_interval: float,
    phase_start: float,
    skip_first: bool = False,
    log=None,
    cycle: int = 0,
) -> None:
    """Fire ramped ticks from phase_start for `duration` seconds."""
    n_ticks = max(1, round(duration / tick_interval))
    start_i = 0
    if skip_first:  # the boundary double-tick replaces the earliest ticks
        start_i = max(1, math.ceil(0.16 / tick_interval))
        if start_i >= n_ticks:
            return  # marks covered the whole (very short) phase
    for i in range(start_i, n_ticks):
        t = phase_start + i * tick_interval
        sleep_until(t)
        progress = i / (n_ticks - 1) if n_ticks > 1 else 1.0
        gain = gain_at(progress)
        hb.play(event_id, gain=gain)
        if log is not None:
            log.writerow([f"{time.time():.3f}", f"{time.perf_counter():.3f}",
                          cycle, label, f"{gain:.3f}", event_id])
        remaining = max(0.0, phase_start + duration - time.perf_counter())
        bar = "#" * round(gain * 20)
        print(f"\r{label:<8} {remaining:4.1f}s  [{bar:<20}]", end="", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Haptic guided-breathing pacer.")
    parser.add_argument("--preset", choices=sorted(PRESETS), default="box",
                        help="breathing pattern preset (default box)")
    parser.add_argument("--pattern", default=None,
                        help='custom "inhale,hold,exhale,hold" seconds, e.g. 4,7,8,0')
    parser.add_argument("--ramp-to", default=None, metavar="PATTERN",
                        help="morph linearly from the start pattern to this one "
                             "over the session, e.g. 4,7,8,0")
    parser.add_argument("--minutes", type=float, default=3.0,
                        help="session length in minutes (whole cycles, default 3)")
    parser.add_argument("--cycles", type=int, default=0,
                        help="number of breath cycles (overrides --minutes)")
    parser.add_argument("--event", default="tick",
                        help="event id of a short, soft one-shot in your kit (default: tick)")
    parser.add_argument("--event-in", default=None,
                        help="separate event id for the inhale half (default: --event)")
    parser.add_argument("--event-out", default=None,
                        help="separate event id for the exhale half (default: --event)")
    parser.add_argument("--tick-rate", type=float, default=2.0,
                        help="ticks per second (default 2)")
    parser.add_argument("--peak-gain", type=float, default=0.8,
                        help="gain at full inhale, 0..1 (default 0.8)")
    parser.add_argument("--floor-gain", type=float, default=0.15,
                        help="gain at full exhale, 0..1 (default 0.15)")
    parser.add_argument("--lead-in", type=float, default=4.0,
                        help="seconds before the first cycle; >= 3 adds three "
                             "ready pulses (default 4)")
    parser.add_argument("--no-phase-marks", action="store_true",
                        help="disable the double-tick marker at each inhale start")
    parser.add_argument("--log", default=None, metavar="CSV",
                        help="write one row per tick for syncing with "
                             "physiological recordings")
    parser.add_argument("--target", default="",
                        help="device address; empty = all (broadcast)")
    args = parser.parse_args()

    if args.tick_rate <= 0:
        parser.error("--tick-rate must be > 0")
    if not 0.0 <= args.floor_gain < args.peak_gain <= 1.0:
        parser.error("gains must satisfy 0 <= floor < peak <= 1 (the wire "
                     "clamps at 1.0, so larger peaks would flatten the ramp)")
    if args.lead_in < 0:
        parser.error("--lead-in must be >= 0")
    if args.cycles < 0:
        parser.error("--cycles must be >= 0")
    if args.cycles == 0 and args.minutes <= 0:
        parser.error("--minutes must be > 0 (or give --cycles)")
    try:
        pattern = parse_pattern(args.pattern) if args.pattern else PRESETS[args.preset]
        end_pattern = parse_pattern(args.ramp_to) if args.ramp_to else pattern
    except ValueError as exc:
        parser.error(str(exc))

    peak, floor = args.peak_gain, args.floor_gain
    ev_in = args.event_in or args.event
    ev_out = args.event_out or args.event
    tick_interval = 1.0 / args.tick_rate
    avg_cycle = (sum(pattern) + sum(end_pattern)) / 2.0
    n_cycles = args.cycles or max(1, round(args.minutes * 60.0 / avg_cycle))

    # Judge tick resolution against what will actually be scheduled -- with
    # --ramp-to a phase can shrink toward 0 in the final cycles.
    scheduled: list[float] = []
    for c in range(n_cycles):
        frac = c / (n_cycles - 1) if n_cycles > 1 else 0.0
        pat = interp_pattern(pattern, end_pattern, frac) if args.ramp_to else pattern
        scheduled += [p for p in pat if p > 0]
    shortest = min(scheduled)
    if tick_interval > shortest / 3:
        print(f"warning: tick interval {tick_interval:.2f}s is coarse for a "
              f"{shortest:.1f}s phase -- the ramp may collapse to single pulses")

    # (label, pattern index, event id, gain curve over phase progress 0..1)
    phases = [
        ("IN",   0, ev_in,  lambda p: floor + (peak - floor) * p),
        ("HOLD", 1, ev_in,  lambda p: peak),
        ("OUT",  2, ev_out, lambda p: peak - (peak - floor) * p),
        ("REST", 3, ev_out, lambda p: floor),
    ]

    log_file = None
    log = None
    if args.log:
        log_file = open(args.log, "w", newline="", encoding="utf-8")
        log_file.write(f"# pattern={pattern} ramp_to={end_pattern if args.ramp_to else None} "
                       f"tick_rate={args.tick_rate} peak={peak} floor={floor}\n")
        log_file.write(f"# session_start_epoch={time.time():.3f}\n")
        log_file.write("# timestamps are UDP send times, not actuator times\n")
        log = csv.writer(log_file)
        log.writerow(["epoch_ts", "mono_ts", "cycle", "phase", "gain", "event_id"])

    with hapbeat.connect(app_name="BreathPacer", default_target=args.target) as hb:
        finished = False
        try:
            devices = hb.discover(timeout=1.0)
            if devices:
                print("Devices: " + ", ".join(d.address or d.ip for d in devices))
            else:
                print("No device replied to PING -- continuing anyway (UDP broadcast).")

            ramp_txt = f" -> {end_pattern}" if args.ramp_to else ""
            print(f"Pattern {pattern}{ramp_txt}, {n_cycles} cycles "
                  f"(~{n_cycles * avg_cycle / 60.0:.1f} min). Ctrl-C stops.")
            print(f"Session start epoch: {time.time():.3f}")

            t = time.perf_counter() + 0.5
            if args.lead_in >= 3.0:  # three ready pulses, 1 s apart
                for k in range(3):
                    sleep_until(t + k)
                    hb.play(ev_in, gain=0.4)
                    print(f"\rready{'.' * (k + 1):<4}", end="", flush=True)
            t += args.lead_in

            for c in range(n_cycles):
                frac = c / (n_cycles - 1) if n_cycles > 1 else 0.0
                pat = interp_pattern(pattern, end_pattern, frac) if args.ramp_to else pattern
                print(f"\ncycle {c + 1}/{n_cycles}"
                      + (f"  ({pat[0]:.1f},{pat[1]:.1f},{pat[2]:.1f},{pat[3]:.1f})"
                         if args.ramp_to else ""))
                for label, idx, event_id, gain_at in phases:
                    duration = pat[idx]
                    if duration <= 0:
                        continue
                    mark = label == "IN" and not args.no_phase_marks
                    if mark:  # double-tick: unmistakable "start inhaling now"
                        for pulse in range(2):
                            sleep_until(t + 0.08 * pulse)
                            hb.play(ev_in, gain=peak)
                            if log is not None:  # one row per pulse, send time
                                log.writerow([f"{time.time():.3f}",
                                              f"{time.perf_counter():.3f}",
                                              c, "IN_MARK", f"{peak:.3f}", ev_in])
                    run_phase(hb, event_id, label, duration, gain_at,
                              tick_interval, t, skip_first=mark, log=log, cycle=c)
                    t += duration
            finished = True
            for k in range(3):  # end cue: finished, not crashed
                sleep_until(t + 0.15 * k)
                hb.play(ev_out, gain=0.5)
            print("\nSession complete.")
        except KeyboardInterrupt:
            print("\nStopped.")
        finally:
            hb.stop_all()
            if log_file is not None:
                log_file.close()
                print(f"Tick log {'saved' if finished else 'saved (partial)'}: {args.log}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
