"""Vibrotactile detection experiment (constant stimuli or adaptive staircase).

A self-contained psychophysics session: presents a haptic event at varying
intensities, records yes/no responses and reaction times, and writes a tidy
CSV (plus a .meta.json sidecar with every parameter, the RNG seed, and the
devices that answered) so the session is fully reproducible and analysable
in pandas / R / JASP.

Two methods:
    constant    method of constant stimuli -- a fixed grid of levels, each
                presented --trials times in random order, plus catch trials.
    staircase   transformed up/down (1-up-2-down or 1-up-3-down) -- converges
                on the 70.7% / 79.4% detection threshold in far fewer trials.

This is the kind of pilot you would otherwise build in PsychoPy; here the
whole loop is a plain Python script so it also runs in a terminal over SSH.

Prerequisites:
    - A Hapbeat device on the same Wi-Fi/LAN.
    - A kit deployed to the device containing a short one-shot event
      (pass its id with --event).

Run:
    python examples/psychophysics_experiment.py --event sample-kit.sine_100hz
    python examples/psychophysics_experiment.py --method staircase \
        --subject P01 --rule 1up2down --reversals 8
    python examples/psychophysics_experiment.py --levels 0.05,0.1,0.2,0.4 \
        --trials 10 --catch-rate 0.25 --break-every 20 --out session1.csv

Notes:
    - Reaction time is measured from the UDP send to the keypress; budget a
      few ms of network/actuation latency. For publication-grade timing use a
      dedicated stimulus framework and treat this as a pilot tool.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import platform
import random
import sys
import time
from datetime import datetime

import hapbeat


def read_key() -> str:
    """Blocking single-key read (no Enter needed), Windows + POSIX.

    Extended keys (arrows, F-keys, Delete, ...) are swallowed and returned
    as "" so a stray navigation key can never count as an answer.
    """
    try:
        import msvcrt

        ch = msvcrt.getwch()
        if ch in ("\x00", "\xe0"):  # extended-key prefix: discard the code too
            msvcrt.getwch()
            ch = ""
    except ImportError:
        import select
        import termios
        import tty

        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            ch = os.read(fd, 1).decode(errors="ignore")
            if ch == "\x1b":  # drain the rest of an escape sequence
                while select.select([fd], [], [], 0.05)[0]:
                    os.read(fd, 32)
                ch = ""
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
    if ch == "\x03":  # Ctrl-C arrives as a raw byte in raw/console mode
        raise KeyboardInterrupt
    return ch.lower()


def flush_input() -> None:
    """Drop keys typed before a prompt -- anticipatory presses during the
    foreperiod must not be consumed as instant (false) responses."""
    try:
        import msvcrt

        while msvcrt.kbhit():
            msvcrt.getwch()
    except ImportError:
        import termios

        termios.tcflush(sys.stdin.fileno(), termios.TCIFLUSH)


def ask_yes_no(prompt: str) -> str:
    """Show a prompt and return 'y' or 'n'."""
    flush_input()
    print(prompt, end="", flush=True)
    while True:
        ch = read_key()
        if ch in ("y", "n"):
            print(ch)
            return ch


def build_trials(
    levels: list[float], per_level: int, catch_rate: float, rng: random.Random
) -> list[tuple[float, bool]]:
    """Constant-stimuli trial list: (level, is_catch), shuffled."""
    trials: list[tuple[float, bool]] = [
        (lv, False) for lv in levels for _ in range(per_level)
    ]
    trials += [(0.0, True)] * round(len(trials) * catch_rate)
    rng.shuffle(trials)
    return trials


class Staircase:
    """Transformed up/down staircase on a multiplicative step.

    1-up-N-down: after N consecutive detections the level divides by
    ``factor``; after one miss it multiplies. Converges at 70.7% (N=2)
    or 79.4% (N=3) detection probability.
    """

    def __init__(self, start: float, factor: float, n_down: int,
                 floor: float = 0.01, ceil: float = 1.0) -> None:
        self.level = start
        self.factor = factor
        self.n_down = n_down
        self.floor, self.ceil = floor, ceil
        self.reversals: list[float] = []
        self._streak = 0
        self._last_dir = 0

    def update(self, detected: bool) -> bool:
        """Apply one response; move the level. Returns True on a reversal."""
        if detected:
            self._streak += 1
            if self._streak < self.n_down:
                return False
            direction = -1
        else:
            direction = +1
        self._streak = 0
        is_reversal = self._last_dir != 0 and direction != self._last_dir
        if is_reversal:
            self.reversals.append(self.level)
        self._last_dir = direction
        self.level *= self.factor if direction > 0 else 1.0 / self.factor
        self.level = max(self.floor, min(self.ceil, self.level))
        return is_reversal

    def threshold(self) -> float:
        """Geometric mean of the reversal levels (first two discarded)."""
        revs = self.reversals[2:] if len(self.reversals) > 4 else self.reversals
        if not revs:
            return float("nan")
        return math.exp(sum(math.log(r) for r in revs) / len(revs))


def run_trial(
    hb: hapbeat.Hapbeat,
    event_id: str,
    level: float,
    is_catch: bool,
    foreperiod: tuple[float, float],
    rng: random.Random,
) -> dict:
    time.sleep(rng.uniform(*foreperiod))
    onset = time.perf_counter()
    if not is_catch:
        hb.play(event_id, gain=level)
    response = ask_yes_no("  Felt it? [y/n] ")
    rt_ms = (time.perf_counter() - onset) * 1000.0
    return {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "event_id": event_id,
        "level": 0.0 if is_catch else round(level, 4),
        "is_catch": int(is_catch),
        "response": response,
        "detected": int(response == "y"),
        "rt_ms": round(rt_ms, 1),
        "is_reversal": 0,
    }


def rest_gate(done: int, total: int) -> None:
    print(f"\n--- Break: {done}/{total} trials done. "
          "Rest, then press any key to continue. ---")
    flush_input()
    read_key()


def summarize_constant(records: list[dict]) -> dict:
    print("\n--- Summary ---")
    by_level: dict[float, list[dict]] = {}
    catches = [r for r in records if r["is_catch"]]
    for r in records:
        if not r["is_catch"]:
            by_level.setdefault(r["level"], []).append(r)

    fa_rate = (sum(r["detected"] for r in catches) / len(catches)) if catches else 0.0
    points: list[tuple[float, float]] = []  # (level, corrected detection rate)
    print(f"{'level':>7}  {'n':>3}  {'detected':>9}  {'corrected':>9}  {'mean RT (hits)':>14}")
    for level in sorted(by_level):
        rows = by_level[level]
        hits = [r for r in rows if r["detected"]]
        rate = len(hits) / len(rows)
        # Guessing correction against the catch false-alarm rate.
        corr = max(0.0, (rate - fa_rate) / (1.0 - fa_rate)) if fa_rate < 1.0 else 0.0
        points.append((level, corr))
        mean_rt = sum(r["rt_ms"] for r in hits) / len(hits) if hits else None
        rt_txt = f"{mean_rt:.0f} ms" if mean_rt is not None else "-"
        print(f"{level:>7.2f}  {len(rows):>3}  {100 * rate:>8.0f}%  {100 * corr:>8.0f}%  {rt_txt:>14}")
    if catches:
        print(f"catch trials: {len(catches)}, false alarms: "
              f"{sum(r['detected'] for r in catches)} ({100 * fa_rate:.0f}%)")

    threshold = interpolate_50(points)
    if threshold is not None:
        print(f"50% threshold (linear interpolation, corrected): {threshold:.3f}")
    return {"false_alarm_rate": fa_rate, "threshold_50": threshold}


def interpolate_50(points: list[tuple[float, float]]) -> float | None:
    """First 50% crossing of the (level, rate) curve, or None."""
    for (l1, p1), (l2, p2) in zip(points, points[1:]):
        if p1 < 0.5 <= p2 and p2 > p1:
            return l1 + (l2 - l1) * (0.5 - p1) / (p2 - p1)
    return None


def summarize_staircase(stair: Staircase) -> dict:
    print("\n--- Summary ---")
    print("reversal levels: " + ", ".join(f"{r:.3f}" for r in stair.reversals))
    thr = stair.threshold()
    print(f"threshold (geometric mean of reversals): {thr:.3f}"
          if not math.isnan(thr) else "threshold: n/a (no reversals)")
    return {"reversals": stair.reversals, "threshold": None if math.isnan(thr) else thr}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Vibrotactile detection experiment (constant stimuli / staircase)."
    )
    parser.add_argument("--method", choices=("constant", "staircase"),
                        default="constant", help="psychophysical procedure")
    parser.add_argument("--event", default="sample-kit.sine_100hz",
                        help="event id of a short one-shot in your kit (default: sample-kit.sine_100hz)")
    parser.add_argument("--levels", default="0.05,0.1,0.2,0.4,0.7,1.0",
                        help="constant: comma-separated gain levels, each in (0..1]")
    parser.add_argument("--trials", type=int, default=8,
                        help="constant: trials per level (default 8)")
    parser.add_argument("--start", type=float, default=0.5,
                        help="staircase: starting level (default 0.5)")
    parser.add_argument("--step-factor", type=float, default=1.5,
                        help="staircase: multiplicative step (default 1.5)")
    parser.add_argument("--rule", choices=("1up2down", "1up3down"),
                        default="1up2down", help="staircase: down rule")
    parser.add_argument("--reversals", type=int, default=8,
                        help="staircase: stop after this many reversals")
    parser.add_argument("--max-trials", type=int, default=60,
                        help="staircase: hard trial cap (default 60)")
    parser.add_argument("--catch-rate", type=float, default=0.2,
                        help="catch trials as a fraction of stimulus trials")
    parser.add_argument("--foreperiod", default="1.0,2.5",
                        help="random pre-stimulus delay range in seconds, MIN,MAX")
    parser.add_argument("--practice", type=int, default=2,
                        help="practice trials at an easy level (not recorded)")
    parser.add_argument("--break-every", type=int, default=0,
                        help="rest gate every N recorded trials (0 = none)")
    parser.add_argument("--subject", default="", help="participant id for the CSV")
    parser.add_argument("--session", default="", help="session label for the CSV")
    parser.add_argument("--out", default=None,
                        help="CSV output path (default results_[subject_]<timestamp>.csv)")
    parser.add_argument("--seed", type=int, default=None,
                        help="RNG seed; generated and recorded when omitted")
    parser.add_argument("--target", default="",
                        help="device address; empty = all (broadcast)")
    args = parser.parse_args()

    levels = [float(s) for s in args.levels.split(",") if s.strip()]
    if not levels:
        parser.error("--levels needs at least one value")
    if any(not 0.0 < lv <= 1.0 for lv in levels):
        parser.error("--levels values must be in (0..1] -- the wire gain is "
                     "clamped to 1.0, so larger values would be recorded wrong")
    if not 0.0 < args.start <= 1.0:
        parser.error("--start must be in (0..1]")
    if args.step_factor <= 1.0:
        parser.error("--step-factor must be > 1")
    fp = [float(s) for s in args.foreperiod.split(",") if s.strip()]
    if len(fp) != 2 or fp[0] < 0 or fp[0] > fp[1]:
        parser.error("--foreperiod must be MIN,MAX with 0 <= MIN <= MAX")

    seed = args.seed if args.seed is not None else random.randrange(2**32)
    rng = random.Random(seed)
    tag = f"{args.subject}_" if args.subject else ""
    out_path = args.out or f"results_{tag}{datetime.now():%Y%m%d_%H%M%S}.csv"
    meta_path = (out_path[:-4] if out_path.endswith(".csv") else out_path) + ".meta.json"
    started = datetime.now().isoformat(timespec="seconds")

    fields = ["trial", "subject", "session", "timestamp", "event_id", "level",
              "is_catch", "response", "detected", "rt_ms", "is_reversal"]
    # Open outputs before the session and save each trial as it completes:
    # a participant's data must never be lost to an unwritable path that is
    # only discovered after they answered 60 trials.
    try:
        csv_file = open(out_path, "w", newline="", encoding="utf-8")
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump({"status": "in progress", "started": started, "seed": seed}, f)
    except OSError as exc:
        parser.error(f"cannot write output files: {exc}")
    writer = csv.DictWriter(csv_file, fieldnames=fields)
    writer.writeheader()
    csv_file.flush()

    records: list[dict] = []

    def save(rec: dict) -> None:
        rec["subject"] = args.subject
        rec["session"] = args.session
        records.append(rec)
        writer.writerow(rec)
        csv_file.flush()

    stair = Staircase(args.start, args.step_factor,
                      n_down=3 if args.rule == "1up3down" else 2)
    with hapbeat.connect(app_name="PsyExperiment", default_target=args.target) as hb:
        try:
            devices = hb.discover(timeout=1.0)
            if devices:
                print("Devices: " + ", ".join(d.address or d.ip for d in devices))
            else:
                print("No device replied to PING -- continuing anyway (UDP broadcast).")

            if args.method == "constant":
                trials = build_trials(levels, args.trials, args.catch_rate, rng)
                plan = f"{len(trials)} trials, levels {levels}"
            else:
                trials = []
                plan = (f"staircase {args.rule}, start {args.start}, "
                        f"stop after {args.reversals} reversals (max {args.max_trials} trials)")
            print(f"\nMethod: {args.method} -- {plan}")
            print(f"Seed: {seed}  ->  {out_path}")
            print("After each pause, answer whether you felt a vibration.")
            print("Press any key to start.")
            flush_input()
            read_key()

            easy = max(levels) if args.method == "constant" else min(1.0, args.start * 2)
            for i in range(args.practice):
                print(f"\nPractice {i + 1}/{args.practice}")
                run_trial(hb, args.event, easy, False, (fp[0], fp[1]), rng)
            if args.practice:
                print("\nPractice done. Press any key for the main block.")
                flush_input()
                read_key()

            if args.method == "constant":
                for i, (level, is_catch) in enumerate(trials, start=1):
                    print(f"\nTrial {i}/{len(trials)}")
                    rec = run_trial(hb, args.event, level, is_catch, (fp[0], fp[1]), rng)
                    rec["trial"] = i
                    save(rec)
                    if args.break_every and i % args.break_every == 0 and i < len(trials):
                        rest_gate(i, len(trials))
            else:
                i = 0
                while len(stair.reversals) < args.reversals and i < args.max_trials:
                    i += 1
                    is_catch = rng.random() < args.catch_rate
                    print(f"\nTrial {i}  (level {stair.level:.3f}, "
                          f"{len(stair.reversals)}/{args.reversals} reversals)")
                    rec = run_trial(hb, args.event, stair.level, is_catch,
                                    (fp[0], fp[1]), rng)
                    rec["trial"] = i
                    if not is_catch:
                        rec["is_reversal"] = int(stair.update(bool(rec["detected"])))
                    save(rec)
                    if args.break_every and i % args.break_every == 0:
                        rest_gate(i, args.max_trials)
        except KeyboardInterrupt:
            print("\nInterrupted -- completed trials are already on disk.")
        finally:
            hb.stop_all()
            csv_file.close()
        device_meta = [{"ip": d.ip, "address": d.address,
                        "firmware_version": d.firmware_version} for d in hb.devices]

    if records:
        print(f"\nSaved {len(records)} trials to {out_path}")
        if args.method == "constant":
            result = summarize_constant(records)
        else:
            result = summarize_staircase(stair)
    else:
        print("No trials recorded.")
        result = {}

    meta = {
        "subject": args.subject, "session": args.session,
        "method": args.method, "event": args.event, "seed": seed,
        "args": {k: v for k, v in vars(args).items() if k != "seed"},
        "devices": device_meta,
        "started": started,
        "finished": datetime.now().isoformat(timespec="seconds"),
        "n_trials": len(records),
        "result": result,
        "versions": {"hapbeat": hapbeat.__version__,
                     "python": platform.python_version()},
    }
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)
    print(f"Session metadata saved to {meta_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
