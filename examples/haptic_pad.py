"""Interactive haptic pad -- fire events live from the keyboard.

A "soundboard" for haptics: number keys 1-9 fire events instantly. Built for
live performances, exhibition booths, and Wizard-of-Oz studies where an
operator triggers sensations on a participant by hand.

Key mapping comes from (first match wins):
    --map pad.json          explicit key -> event mapping (see below)
    --manifest <path>       first 9 events of a kit manifest, gains from
                            their authored intensity
    --events a,b,c          plain list, keys 1..n, gain 1.0

pad.json format ("loop": true marks events whose kit clip loops):
    {
      "1": {"event": "sample-kit.sine_100hz", "gain": 0.6},
      "2": {"event": "rumble.loop", "gain": 0.8, "loop": true},
      "3": "heartbeat.slow"
    }

Controls:
    1-9      fire the mapped event; press again to stop a looping one
    + / -    master gain up / down (multiplies every pad)
    [ / ]    trim the last-fired pad's own gain down / up
    space    stop all (panic button -- always one keypress)
    d        re-scan devices (shows who answered and how long ago)
    s        save the current mapping (with trims) to pad_saved.json
    m        show the mapping
    q        quit (press twice within 1.5 s -- guards against fat fingers)

Session recording (Wizard-of-Oz replication):
    --log session.csv       append every action with a timestamp
    --replay session.csv    re-fire a recorded session with original timing

Run:
    python examples/haptic_pad.py --manifest my-kit/my-kit-manifest.json
    python examples/haptic_pad.py --events sample-kit.sine_100hz,rumble.loop --log run1.csv
    python examples/haptic_pad.py --replay run1.csv
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from dataclasses import dataclass

import hapbeat

PAD_KEYS = "123456789"
RESERVED_KEYS = set("qsdm+-=[] ")  # control keys can never be pads
MASTER_STEP = 0.1
TRIM_STEP = 0.05
MASTER_MIN, MASTER_MAX = 0.1, 1.0


def read_key() -> str:
    """Blocking single-key read (no Enter needed), Windows + POSIX.

    Extended keys (arrows, F-keys, Delete, ...) are swallowed and returned
    as "" -- a stray navigation key must never fire a pad or quit the app.
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
    if ch == "\x03":
        raise KeyboardInterrupt
    return ch.lower()


@dataclass
class Pad:
    key: str
    event_id: str
    gain: float = 1.0
    loop: bool = False


def load_pads(args: argparse.Namespace) -> list[Pad]:
    """Build the pad list from --map / --manifest / --events.

    Raises ValueError on a bad mapping (duplicate or multi-character keys,
    missing "event", malformed entries) so the caller can report it cleanly.
    """
    if args.map:
        with open(args.map, encoding="utf-8") as f:
            raw = json.load(f)
        pads, seen = [], set()
        for key, spec in raw.items():
            key = str(key)
            if len(key) != 1:
                raise ValueError(f"map key {key!r} must be a single character")
            if key in RESERVED_KEYS:
                raise ValueError(f"map key {key!r} collides with a control key")
            if key in seen:
                raise ValueError(f"map key {key!r} is defined twice")
            seen.add(key)
            if isinstance(spec, str):
                spec = {"event": spec}
            if "event" not in spec:
                raise ValueError(f"map entry {key!r}: missing \"event\"")
            pads.append(Pad(key=key, event_id=spec["event"],
                            gain=float(spec.get("gain", 1.0)),
                            loop=bool(spec.get("loop", False))))
        return pads

    if args.manifest:
        em = hapbeat.EventMap.from_manifest(args.manifest)
        ids = em.ids()
        if len(ids) > len(PAD_KEYS):
            print(f"NOTE: kit has {len(ids)} events; only the first "
                  f"{len(PAD_KEYS)} fit on pads 1-9")
        pads = []
        for key, event_id in zip(PAD_KEYS, ids):
            ev = em.get(event_id)
            pads.append(Pad(key=key, event_id=event_id,
                            gain=ev.intensity, loop=ev.loop))
        return pads

    if args.events:
        ids = [s.strip() for s in args.events.split(",") if s.strip()]
        if len(ids) > len(PAD_KEYS):
            print(f"NOTE: {len(ids)} events given; only the first "
                  f"{len(PAD_KEYS)} fit on pads 1-9")
        return [Pad(key=k, event_id=e) for k, e in zip(PAD_KEYS, ids)]

    return []


def show_mapping(pads: list[Pad], master: float) -> None:
    print(f"\nmaster gain {master:.1f}")
    for pad in pads:
        loop_txt = "  (loop)" if pad.loop else ""
        print(f"  [{pad.key}] {pad.event_id:<28} gain {pad.gain:.2f}{loop_txt}")
    print("  [+/-] master  [[/]] trim last pad  [space] stop all"
          "  [d] devices  [s] save  [m] map  [q]x2 quit")


def save_pads(pads: list[Pad], path: str) -> None:
    data = {p.key: {"event": p.event_id, "gain": round(p.gain, 3),
                    **({"loop": True} if p.loop else {})} for p in pads}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def replay(hb: hapbeat.Hapbeat, path: str) -> None:
    """Re-fire a --log session with its original timing."""
    with open(path, encoding="utf-8") as f:
        rows = [r for r in csv.DictReader(line for line in f
                                          if not line.startswith("#"))]
    if not rows:
        print("nothing to replay")
        return
    print(f"replaying {len(rows)} actions from {path} (Ctrl-C stops)")
    t0 = time.perf_counter()
    prev = float("-inf")
    for row in rows:
        offset = float(row["elapsed_s"])
        if offset < prev:  # appended session: its clock restarted, rebase t0
            t0 = time.perf_counter() - offset
        prev = offset
        while (wait := t0 + offset - time.perf_counter()) > 0:
            time.sleep(min(wait, 0.1))
        if row["action"] == "play":
            hb.play(row["event_id"], gain=float(row["gain"]))
        elif row["action"] == "stop":
            hb.stop(row["event_id"])
        elif row["action"] == "stop_all":
            hb.stop_all()
        print(f"\r{offset:7.2f}s  {row['action']:<8} {row['event_id']:<28}",
              end="", flush=True)
    print("\nreplay done")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Interactive haptic pad (keyboard-triggered events)."
    )
    parser.add_argument("--map", default=None, help="key->event JSON file")
    parser.add_argument("--manifest", default=None,
                        help="kit manifest; first 9 events become pads 1-9")
    parser.add_argument("--events", default=None,
                        help="comma-separated event ids mapped to keys 1..n")
    parser.add_argument("--master", type=float, default=1.0,
                        help="initial master gain (default 1.0)")
    parser.add_argument("--log", default=None, metavar="CSV",
                        help="append every action to this session log")
    parser.add_argument("--replay", default=None, metavar="CSV",
                        help="replay a recorded session and exit")
    parser.add_argument("--save-path", default="pad_saved.json",
                        help="where the s key writes the tuned mapping")
    parser.add_argument("--target", default="",
                        help="device address; empty = all (broadcast)")
    args = parser.parse_args()

    if args.replay:
        with hapbeat.connect(app_name="HapticPad", default_target=args.target) as hb:
            try:
                replay(hb, args.replay)
            except KeyboardInterrupt:
                print("\nreplay interrupted")
            finally:
                hb.stop_all()
        return 0

    try:
        pads = load_pads(args)
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        parser.error(f"bad mapping: {exc}")
    if not pads:
        parser.error("no pads -- give --map, --manifest, or --events")
    by_key = {p.key: p for p in pads}
    master = max(MASTER_MIN, min(MASTER_MAX, args.master))

    log_file = None
    log = None
    if args.log:
        log_file = open(args.log, "a", newline="", encoding="utf-8")
        new_file = log_file.tell() == 0
        # One comment per session so appended sessions stay identifiable;
        # replay() rebases its clock at each elapsed_s restart.
        log_file.write(f"# started={time.strftime('%Y-%m-%dT%H:%M:%S')}\n")
        if new_file:
            log_file.write("elapsed_s,action,event_id,gain\n")
        log = csv.writer(log_file)
    t0 = time.perf_counter()

    def record(action: str, event_id: str = "", gain: float = 0.0) -> None:
        if log is not None:
            log.writerow([f"{time.perf_counter() - t0:.3f}", action,
                          event_id, f"{gain:.3f}"])

    status = lambda msg: print("\r" + msg.ljust(60), end="", flush=True)
    active: set[str] = set()  # loop events we believe are playing
    last_pad: Pad | None = None
    quit_armed_at = 0.0

    with hapbeat.connect(app_name="HapticPad", default_target=args.target) as hb:
        try:
            devices = hb.discover(timeout=1.0)
            if devices:
                print("Devices: " + ", ".join(d.address or d.ip for d in devices))
            else:
                print("No device replied to PING -- continuing anyway (UDP broadcast).")

            show_mapping(pads, master)
            while True:
                ch = read_key()
                if ch == "q":
                    if time.perf_counter() - quit_armed_at < 1.5:
                        break
                    quit_armed_at = time.perf_counter()
                    status("press q again to quit")
                elif ch == " ":
                    hb.stop_all()
                    active.clear()
                    record("stop_all")
                    status("stop all")
                elif ch in ("+", "="):
                    master = min(MASTER_MAX, round(master + MASTER_STEP, 2))
                    status(f"master gain {master:.1f}")
                elif ch == "-":
                    master = max(MASTER_MIN, round(master - MASTER_STEP, 2))
                    status(f"master gain {master:.1f}")
                elif ch in ("[", "]") and last_pad is not None:
                    delta = TRIM_STEP if ch == "]" else -TRIM_STEP
                    last_pad.gain = max(0.0, min(1.0, round(last_pad.gain + delta, 2)))
                    if not last_pad.loop:  # preview the new strength
                        gain = min(1.0, last_pad.gain * master)
                        hb.play(last_pad.event_id, gain=gain)
                        record("play", last_pad.event_id, gain)
                    status(f"[{last_pad.key}] {last_pad.event_id} gain {last_pad.gain:.2f}")
                elif ch == "s":
                    try:
                        save_pads(pads, args.save_path)
                        status(f"mapping saved to {args.save_path}")
                    except OSError as exc:  # a failed save must not kill the show
                        status(f"save failed: {exc}")
                elif ch == "d":
                    print()
                    found = hb.discover(timeout=0.5)
                    now = time.monotonic()
                    for d in found:
                        print(f"  {d.address or '?':<20} {d.ip:<15} "
                              f"seen {now - d.last_seen:.1f}s ago")
                    if not found:
                        print("  WARNING: no device replied")
                elif ch == "m":
                    show_mapping(pads, master)
                elif ch in by_key:
                    pad = by_key[ch]
                    last_pad = pad
                    if pad.loop and pad.event_id in active:
                        hb.stop(pad.event_id)
                        active.discard(pad.event_id)
                        record("stop", pad.event_id)
                        status(f"[{pad.key}] {pad.event_id}  stopped")
                        continue
                    gain = min(1.0, pad.gain * master)
                    hb.play(pad.event_id, gain=gain)
                    record("play", pad.event_id, gain)
                    if pad.loop:
                        active.add(pad.event_id)
                    mark = " (looping, press again to stop)" if pad.loop else ""
                    status(f"[{pad.key}] {pad.event_id}  gain {gain:.2f}{mark}")
        except KeyboardInterrupt:
            pass
        finally:
            hb.stop_all()
            record("stop_all")
            if log_file is not None:
                log_file.close()
                print(f"\nsession log: {args.log}")
            print("\nbye")
    return 0


if __name__ == "__main__":
    sys.exit(main())
