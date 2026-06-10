"""Interactive haptic pad — fire events live from the keyboard.

A "soundboard" for haptics: number keys 1-9 fire events instantly. Built for
live performances, exhibition booths, and Wizard-of-Oz studies where an
operator triggers sensations on a participant by hand.

Key mapping comes from (first match wins):
    --map pad.json          explicit key -> event mapping (see below)
    --manifest <path>       first 9 events of a kit manifest, gains from
                            their authored intensity
    --events a,b,c          plain list, keys 1..n, gain 1.0

pad.json format:
    {
      "1": {"event": "impact.hit", "gain": 0.6},
      "2": "rumble.loop",
      "3": {"event": "heartbeat.slow"}
    }

Controls:
    1-9      fire the mapped event
    + / -    master gain up / down (multiplies every pad)
    space    stop all
    m        show the mapping
    q        quit

Run:
    python examples/haptic_pad.py --manifest my-kit/my-kit-manifest.json
    python examples/haptic_pad.py --events impact.hit,rumble.loop
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass

import hapbeat

PAD_KEYS = "123456789"
MASTER_STEP = 0.1
MASTER_MIN, MASTER_MAX = 0.1, 1.0


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
    if ch == "\x03":
        raise KeyboardInterrupt
    return ch


@dataclass
class Pad:
    key: str
    event_id: str
    gain: float = 1.0
    loop: bool = False


def load_pads(args: argparse.Namespace) -> list[Pad]:
    if args.map:
        with open(args.map, encoding="utf-8") as f:
            raw = json.load(f)
        pads = []
        for key, spec in raw.items():
            if isinstance(spec, str):
                spec = {"event": spec}
            pads.append(Pad(key=str(key)[:1], event_id=spec["event"],
                            gain=float(spec.get("gain", 1.0))))
        return pads

    if args.manifest:
        em = hapbeat.EventMap.from_manifest(args.manifest)
        pads = []
        for key, event_id in zip(PAD_KEYS, em.ids()):
            ev = em.get(event_id)
            pads.append(Pad(key=key, event_id=event_id,
                            gain=ev.intensity, loop=ev.loop))
        return pads

    if args.events:
        ids = [s.strip() for s in args.events.split(",") if s.strip()]
        return [Pad(key=k, event_id=e) for k, e in zip(PAD_KEYS, ids)]

    return []


def show_mapping(pads: list[Pad], master: float) -> None:
    print(f"\nmaster gain {master:.1f}")
    for pad in pads:
        loop_txt = "  (loop)" if pad.loop else ""
        print(f"  [{pad.key}] {pad.event_id:<28} gain {pad.gain:.2f}{loop_txt}")
    print("  [+/-] master gain   [space] stop all   [m] mapping   [q] quit")


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
    parser.add_argument("--target", default="",
                        help="device address; empty = all (broadcast)")
    args = parser.parse_args()

    pads = load_pads(args)
    if not pads:
        parser.error("no pads -- give --map, --manifest, or --events")
    by_key = {p.key: p for p in pads}
    master = max(MASTER_MIN, min(MASTER_MAX, args.master))

    with hapbeat.connect(app_name="HapticPad", default_target=args.target) as hb:
        devices = hb.discover(timeout=1.0)
        if devices:
            print("Devices: " + ", ".join(d.address or d.ip for d in devices))
        else:
            print("No device replied to PING -- continuing anyway (UDP broadcast).")

        show_mapping(pads, master)
        try:
            while True:
                ch = read_key()
                if ch == "q":
                    break
                if ch == " ":
                    hb.stop_all()
                    print("\rstop all" + " " * 40, end="", flush=True)
                elif ch in ("+", "="):
                    master = min(MASTER_MAX, round(master + MASTER_STEP, 2))
                    print(f"\rmaster gain {master:.1f}" + " " * 30, end="", flush=True)
                elif ch == "-":
                    master = max(MASTER_MIN, round(master - MASTER_STEP, 2))
                    print(f"\rmaster gain {master:.1f}" + " " * 30, end="", flush=True)
                elif ch == "m":
                    show_mapping(pads, master)
                elif ch in by_key:
                    pad = by_key[ch]
                    gain = pad.gain * master
                    hb.play(pad.event_id, gain=gain)
                    print(f"\r[{pad.key}] {pad.event_id}  gain {gain:.2f}" + " " * 16,
                          end="", flush=True)
        except KeyboardInterrupt:
            pass
        finally:
            hb.stop_all()
            print("\nbye")
    return 0


if __name__ == "__main__":
    sys.exit(main())
