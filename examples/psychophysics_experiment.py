"""Vibrotactile detection experiment (method of constant stimuli).

A self-contained psychophysics session: presents a haptic event at a set of
intensity levels in random order (plus catch trials with no stimulus), records
yes/no responses and reaction times, and writes a tidy CSV you can analyse in
pandas / R / JASP.

This is the kind of pilot you would otherwise build in PsychoPy; here the
whole loop is a plain Python script so it also runs in a terminal over SSH.

Prerequisites:
    - A Hapbeat device on the same Wi-Fi/LAN.
    - A kit deployed to the device containing a short one-shot event
      (pass its id with --event).

Run:
    python examples/psychophysics_experiment.py --event impact.hit
    python examples/psychophysics_experiment.py --levels 0.05,0.1,0.2,0.4 \
        --trials 10 --catch-rate 0.25 --out session1.csv

Notes:
    - Reaction time is measured from the UDP send to the keypress; budget a
      few ms of network/actuation latency. For publication-grade timing use a
      dedicated stimulus framework and treat this as a pilot tool.
"""

from __future__ import annotations

import argparse
import csv
import random
import sys
import time
from datetime import datetime

import hapbeat


def read_key() -> str:
    """Blocking single-key read (no Enter needed), Windows + POSIX."""
    try:
        import msvcrt

        ch = msvcrt.getwch()
    except ImportError:
        import termios
        import tty

        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            ch = sys.stdin.read(1)
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
    if ch == "\x03":  # Ctrl-C arrives as a raw byte in raw/console mode
        raise KeyboardInterrupt
    return ch.lower()


def ask_yes_no(prompt: str) -> tuple[str, float]:
    """Show a prompt, return ('y'|'n', seconds_until_keypress)."""
    print(prompt, end="", flush=True)
    t0 = time.perf_counter()
    while True:
        ch = read_key()
        if ch in ("y", "n"):
            print(ch)
            return ch, time.perf_counter() - t0


def run_trial(
    hb: hapbeat.Hapbeat,
    event_id: str,
    level: float,
    is_catch: bool,
    foreperiod: tuple[float, float],
) -> dict:
    time.sleep(random.uniform(*foreperiod))
    onset = time.perf_counter()
    if not is_catch:
        hb.play(event_id, gain=level)
    response, _ = ask_yes_no("  Felt it? [y/n] ")
    rt_ms = (time.perf_counter() - onset) * 1000.0
    return {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "event_id": event_id,
        "level": 0.0 if is_catch else level,
        "is_catch": int(is_catch),
        "response": response,
        "detected": int(response == "y"),
        "rt_ms": round(rt_ms, 1),
    }


def summarize(records: list[dict]) -> None:
    print("\n--- Summary ---")
    by_level: dict[float, list[dict]] = {}
    catches = [r for r in records if r["is_catch"]]
    for r in records:
        if not r["is_catch"]:
            by_level.setdefault(r["level"], []).append(r)

    print(f"{'level':>7}  {'n':>3}  {'detected':>9}  {'mean RT (hits)':>14}")
    for level in sorted(by_level):
        rows = by_level[level]
        hits = [r for r in rows if r["detected"]]
        rate = 100.0 * len(hits) / len(rows)
        mean_rt = sum(r["rt_ms"] for r in hits) / len(hits) if hits else float("nan")
        rt_txt = f"{mean_rt:.0f} ms" if hits else "-"
        print(f"{level:>7.2f}  {len(rows):>3}  {rate:>8.0f}%  {rt_txt:>14}")
    if catches:
        fa = sum(r["detected"] for r in catches)
        print(f"\ncatch trials: {len(catches)}, false alarms: {fa} "
              f"({100.0 * fa / len(catches):.0f}%)")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Vibrotactile detection experiment (constant stimuli)."
    )
    parser.add_argument("--event", default="impact.hit",
                        help="event id in the deployed kit (short one-shot)")
    parser.add_argument("--levels", default="0.05,0.1,0.2,0.4,0.7,1.0",
                        help="comma-separated gain levels (0..1)")
    parser.add_argument("--trials", type=int, default=8,
                        help="trials per level (default 8)")
    parser.add_argument("--catch-rate", type=float, default=0.2,
                        help="catch trials as a fraction of stimulus trials")
    parser.add_argument("--foreperiod", default="1.0,2.5",
                        help="random pre-stimulus delay range in seconds")
    parser.add_argument("--practice", type=int, default=2,
                        help="practice trials at max level (not recorded)")
    parser.add_argument("--out", default=None,
                        help="CSV output path (default results_<timestamp>.csv)")
    parser.add_argument("--seed", type=int, default=None,
                        help="random seed for a reproducible trial order")
    parser.add_argument("--target", default="",
                        help="device address; empty = all (broadcast)")
    args = parser.parse_args()

    levels = [float(s) for s in args.levels.split(",") if s.strip()]
    if not levels:
        parser.error("--levels needs at least one value")
    fp_lo, fp_hi = (float(s) for s in args.foreperiod.split(","))
    out_path = args.out or f"results_{datetime.now():%Y%m%d_%H%M%S}.csv"
    if args.seed is not None:
        random.seed(args.seed)

    trials: list[tuple[float, bool]] = [
        (lv, False) for lv in levels for _ in range(args.trials)
    ]
    n_catch = round(len(trials) * args.catch_rate)
    trials += [(0.0, True)] * n_catch
    random.shuffle(trials)

    records: list[dict] = []
    with hapbeat.connect(app_name="PsyExperiment", default_target=args.target) as hb:
        devices = hb.discover(timeout=1.0)
        if devices:
            print("Devices: " + ", ".join(d.address or d.ip for d in devices))
        else:
            print("No device replied to PING -- continuing anyway (UDP broadcast).")

        print(f"\n{len(trials)} trials ({n_catch} catch), levels: {levels}")
        print("After each beep-less pause, answer whether you felt a vibration.")
        print("Press any key to start.")
        read_key()

        try:
            for i in range(args.practice):
                print(f"\nPractice {i + 1}/{args.practice}")
                run_trial(hb, args.event, max(levels), False, (fp_lo, fp_hi))

            for i, (level, is_catch) in enumerate(trials, start=1):
                print(f"\nTrial {i}/{len(trials)}")
                rec = run_trial(hb, args.event, level, is_catch, (fp_lo, fp_hi))
                rec["trial"] = i
                records.append(rec)
        except KeyboardInterrupt:
            print("\nInterrupted -- saving completed trials.")
        finally:
            hb.stop_all()

    if records:
        fields = ["trial", "timestamp", "event_id", "level", "is_catch",
                  "response", "detected", "rt_ms"]
        with open(out_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fields)
            writer.writeheader()
            writer.writerows(records)
        print(f"\nSaved {len(records)} trials to {out_path}")
        summarize(records)
    else:
        print("No trials recorded.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
