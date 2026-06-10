"""Text to haptic Morse code.

Transmits text as Morse vibrations: a dit is a 1-unit buzz, a dah a 3-unit
buzz, with standard gaps (1 unit between elements, 3 between letters, 7
between words). A hands-on demo of conveying symbolic information through a
single actuator, and of duration control with play()/stop().

Two ways to make dits and dahs:
    - One looping event (recommended): play() starts the buzz, stop() ends it
      after the right duration. Use a kit event with loop enabled, or a clip
      longer than a dah.
    - Two one-shot events: pass --dit-event and --dah-event ids whose clips
      are already short/long; the script then only fires them.

Run:
    python examples/morse_text.py "SOS"
    python examples/morse_text.py --wpm 18 --event rumble.loop "HELLO WORLD"
    echo PAGING DR HAPBEAT | python examples/morse_text.py --stdin

Timing follows the PARIS standard: one unit = 1.2 / WPM seconds.
"""

from __future__ import annotations

import argparse
import sys
import time

import hapbeat

MORSE = {
    "A": ".-", "B": "-...", "C": "-.-.", "D": "-..", "E": ".", "F": "..-.",
    "G": "--.", "H": "....", "I": "..", "J": ".---", "K": "-.-", "L": ".-..",
    "M": "--", "N": "-.", "O": "---", "P": ".--.", "Q": "--.-", "R": ".-.",
    "S": "...", "T": "-", "U": "..-", "V": "...-", "W": ".--", "X": "-..-",
    "Y": "-.--", "Z": "--..",
    "0": "-----", "1": ".----", "2": "..---", "3": "...--", "4": "....-",
    "5": ".....", "6": "-....", "7": "--...", "8": "---..", "9": "----.",
    ".": ".-.-.-", ",": "--..--", "?": "..--..", "!": "-.-.--", "/": "-..-.",
    "-": "-....-", "@": ".--.-.", "=": "-...-", "+": ".-.-.",
}


def sleep_for(seconds: float) -> None:
    deadline = time.perf_counter() + seconds
    while (remaining := deadline - time.perf_counter()) > 0:
        time.sleep(remaining - 0.002 if remaining > 0.002 else 0)


def send_element(hb: hapbeat.Hapbeat, args: argparse.Namespace,
                 symbol: str, unit: float) -> None:
    """Transmit one '.' or '-' (without the trailing intra-letter gap)."""
    duration = unit if symbol == "." else 3 * unit
    if args.dit_event and args.dah_event:
        hb.play(args.dit_event if symbol == "." else args.dah_event,
                gain=args.gain)
        sleep_for(duration)
    else:
        hb.play(args.event, gain=args.gain)
        sleep_for(duration)
        hb.stop(args.event)


def send_text(hb: hapbeat.Hapbeat, args: argparse.Namespace,
              text: str, unit: float) -> None:
    words = [w for w in text.upper().split() if w]
    for wi, word in enumerate(words):
        if wi:
            sleep_for(7 * unit)  # word gap
        for ci, ch in enumerate(word):
            code = MORSE.get(ch)
            if code is None:
                print(f"\n(skipping unsupported character {ch!r})")
                continue
            if ci:
                sleep_for(3 * unit)  # letter gap
            print(f"{ch} {code}  ", end="", flush=True)
            for ei, symbol in enumerate(code):
                if ei:
                    sleep_for(unit)  # element gap
                send_element(hb, args, symbol, unit)
        print()


def main() -> int:
    parser = argparse.ArgumentParser(description="Transmit text as haptic Morse.")
    parser.add_argument("text", nargs="?", default=None,
                        help="text to transmit (or use --stdin)")
    parser.add_argument("--stdin", action="store_true",
                        help="read lines from stdin and transmit each")
    parser.add_argument("--wpm", type=float, default=12.0,
                        help="speed in words per minute (default 12)")
    parser.add_argument("--event", default="buzz",
                        help="looping buzz event id (started/stopped per element)")
    parser.add_argument("--dit-event", default=None,
                        help="one-shot short event (use with --dah-event)")
    parser.add_argument("--dah-event", default=None,
                        help="one-shot long event (use with --dit-event)")
    parser.add_argument("--gain", type=float, default=0.7)
    parser.add_argument("--target", default="",
                        help="device address; empty = all (broadcast)")
    args = parser.parse_args()

    if args.wpm <= 0:
        parser.error("--wpm must be > 0")
    if bool(args.dit_event) != bool(args.dah_event):
        parser.error("--dit-event and --dah-event must be given together")
    if not args.stdin and not args.text:
        parser.error("give some text, or --stdin")

    unit = 1.2 / args.wpm
    with hapbeat.connect(app_name="MorseText", default_target=args.target) as hb:
        devices = hb.discover(timeout=1.0)
        if devices:
            print("Devices: " + ", ".join(d.address or d.ip for d in devices))
        else:
            print("No device replied to PING -- continuing anyway (UDP broadcast).")
        print(f"{args.wpm:.0f} WPM (unit {unit * 1000:.0f} ms)\n")

        try:
            if args.stdin:
                for line in sys.stdin:
                    line = line.strip()
                    if line:
                        send_text(hb, args, line, unit)
            else:
                send_text(hb, args, args.text, unit)
        except KeyboardInterrupt:
            print("\nStopped.")
        finally:
            hb.stop_all()
    return 0


if __name__ == "__main__":
    sys.exit(main())
