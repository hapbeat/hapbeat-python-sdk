"""Text to haptic Morse code -- transmitter, learning timing, receive trainer.

Transmits text as Morse vibrations: a dit is a 1-unit buzz, a dah a 3-unit
buzz, with standard gaps (1 unit between elements, 3 between letters, 7
between words). A hands-on demo of conveying symbolic information through a
single actuator, and of duration control with play()/stop().

Learning features:
    --farnsworth S   keep characters at --wpm but stretch the gaps so the
                     effective speed is S WPM (the standard way to learn:
                     rhythm stays fast, thinking time grows). Formulas from
                     ARRL QEX Apr 1990, "A Standard for Morse Timing Using
                     the Farnsworth Technique" (Jon Bloom, KE3Z).
    --quiz           receive training: random groups are sent, you type what
                     you felt, per-character accuracy and confusions are
                     scored. Characters come from the Koch lesson order
                     (--koch N) or an explicit --charset.

Two ways to make dits and dahs:
    - One looping event (recommended): play() starts the buzz, stop() ends it
      after the right duration. Use a kit event with loop enabled, or a clip
      longer than a dah. Note UDP has no ACK: a lost STOP merges elements
      until the next play (and at message end, stop_all cleans up).
    - Two one-shot events: pass --dit-event and --dah-event ids whose clips
      are already short/long; the script then only fires them. Make the clips
      match the --wpm you train at, or the rhythm will not line up.

Run:
    python examples/morse_text.py "SOS"
    python examples/morse_text.py --wpm 18 --farnsworth 8 "HELLO WORLD"
    python examples/morse_text.py --quiz --koch 6 --rounds 10
    echo PAGING DR HAPBEAT | python examples/morse_text.py --stdin

Timing follows the PARIS standard: one unit = 1.2 / WPM seconds.
(Purist note: real SOS is a prosign sent without letter gaps; this sample
sends it as three ordinary letters.)
"""

from __future__ import annotations

import argparse
import random
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

# Koch-method lesson order as used by LCWO (lcwo.net).
KOCH_ORDER = "KMURESNAPTLWI.JZ=FOY,VG5/Q92H38B?47C1D60X"


def gain01(s: str) -> float:
    v = float(s)
    if not 0.0 <= v <= 1.0:
        raise argparse.ArgumentTypeError("gain must be in 0..1")
    return v


def farnsworth_gaps(char_wpm: float, effective_wpm: float) -> tuple[float, float]:
    """(letter_gap, word_gap) seconds for Farnsworth timing.

    ARRL QEX Apr 1990 (KE3Z): with character speed c and overall speed s,
    the total delay to distribute over a standard 50-unit word is
    ta = (60c - 37.2s) / (sc), split 3/19 per letter gap (x4) and 7/19 for
    the word gap. At s == c this reduces to the standard 3u / 7u.
    """
    c, s = char_wpm, effective_wpm
    ta = (60.0 * c - 37.2 * s) / (s * c)
    return 3.0 * ta / 19.0, 7.0 * ta / 19.0


def sleep_for(seconds: float) -> None:
    deadline = time.perf_counter() + seconds
    while (remaining := deadline - time.perf_counter()) > 0:
        time.sleep(remaining - 0.02 if remaining > 0.02 else 0)


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


def send_text(hb: hapbeat.Hapbeat, args: argparse.Namespace, text: str,
              unit: float, letter_gap: float, word_gap: float,
              echo: bool = True) -> None:
    words = [w for w in text.upper().split() if w]
    for wi, word in enumerate(words):
        if wi:
            sleep_for(word_gap)
        sent_any = False
        for ch in word:
            code = MORSE.get(ch)
            if code is None:
                print(f"\n(skipping unsupported character {ascii(ch)})")
                continue
            if sent_any:
                sleep_for(letter_gap)
            sent_any = True
            if echo:
                print(f"{ch} {code}  ", end="", flush=True)
            for ei, symbol in enumerate(code):
                if ei:
                    sleep_for(unit)  # element gap
                send_element(hb, args, symbol, unit)
        if echo:
            print()


def run_quiz(hb: hapbeat.Hapbeat, args: argparse.Namespace, unit: float,
             letter_gap: float, word_gap: float) -> None:
    charset = args.charset.upper() if args.charset else KOCH_ORDER[:args.koch]
    bad = [c for c in charset if c not in MORSE]
    if bad:
        print(f"unsupported characters in charset: {bad}")
        return
    print(f"Receive training: {args.rounds} rounds of {args.group_len} "
          f"characters from \"{charset}\".")
    print("Type what you felt and press Enter; empty answer repeats the group.\n")

    correct_by_char: dict[str, list[int]] = {}
    confusions: dict[tuple[str, str], int] = {}
    score = 0
    total = 0
    for rnd in range(1, args.rounds + 1):
        group = "".join(random.choice(charset) for _ in range(args.group_len))
        while True:
            print(f"Round {rnd}/{args.rounds}: listen...")
            time.sleep(1.0)
            send_text(hb, args, group, unit, letter_gap, word_gap, echo=False)
            try:
                answer = input("Your copy: ").strip().upper()
            except EOFError:
                return
            if answer:
                break
            print("(repeating)")
        for i, expected in enumerate(group):
            got = answer[i] if i < len(answer) else "_"
            ok = got == expected
            correct_by_char.setdefault(expected, []).append(int(ok))
            if not ok:
                confusions[(expected, got)] = confusions.get((expected, got), 0) + 1
            score += ok
            total += 1
        print(f"  sent: {group}   got: {answer or '-'}"
              f"   {sum(g == a for g, a in zip(group, answer))}/{len(group)}\n")

    print("--- Results ---")
    print(f"overall: {score}/{total} ({100.0 * score / total:.0f}%)" if total else "no rounds")
    for ch in sorted(correct_by_char):
        rows = correct_by_char[ch]
        print(f"  {ch}: {sum(rows)}/{len(rows)}")
    if confusions:
        worst = sorted(confusions.items(), key=lambda kv: -kv[1])[:5]
        print("top confusions (sent -> typed): "
              + ", ".join(f"{a}->{b} x{n}" for (a, b), n in worst))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Transmit text as haptic Morse / receive training."
    )
    parser.add_argument("text", nargs="?", default=None,
                        help="text to transmit (or use --stdin / --quiz)")
    parser.add_argument("--stdin", action="store_true",
                        help="read lines from stdin and transmit each")
    parser.add_argument("--quiz", action="store_true",
                        help="receive training: random groups, typed answers, scoring")
    parser.add_argument("--rounds", type=int, default=10,
                        help="quiz: number of groups (default 10)")
    parser.add_argument("--group-len", type=int, default=5,
                        help="quiz: characters per group (default 5)")
    parser.add_argument("--koch", type=int, default=6,
                        help="quiz: use the first N Koch-order characters (default 6)")
    parser.add_argument("--charset", default=None,
                        help="quiz: explicit character set (overrides --koch)")
    parser.add_argument("--wpm", type=float, default=12.0,
                        help="character speed in words per minute (default 12)")
    parser.add_argument("--farnsworth", type=float, default=None, metavar="WPM",
                        help="effective speed; stretches letter/word gaps only")
    parser.add_argument("--repeat", type=int, default=1,
                        help="transmit the message N times (default 1)")
    parser.add_argument("--pause", type=float, default=2.0,
                        help="seconds between repeats (default 2)")
    parser.add_argument("--event", default="buzz",
                        help="looping buzz event id (started/stopped per element)")
    parser.add_argument("--dit-event", default=None,
                        help="one-shot short event (use with --dah-event)")
    parser.add_argument("--dah-event", default=None,
                        help="one-shot long event (use with --dit-event)")
    parser.add_argument("--gain", type=gain01, default=0.7,
                        help="vibration gain, 0..1 (default 0.7)")
    parser.add_argument("--target", default="",
                        help="device address; empty = all (broadcast)")
    args = parser.parse_args()

    if args.wpm <= 0:
        parser.error("--wpm must be > 0")
    if args.farnsworth is not None and not 0 < args.farnsworth <= args.wpm:
        parser.error("--farnsworth must be > 0 and <= --wpm")
    if bool(args.dit_event) != bool(args.dah_event):
        parser.error("--dit-event and --dah-event must be given together")
    if args.quiz:
        if not 2 <= args.koch <= len(KOCH_ORDER):
            parser.error(f"--koch must be 2..{len(KOCH_ORDER)}")
        if args.group_len < 1 or args.rounds < 1:
            parser.error("--group-len and --rounds must be >= 1")
    elif not args.stdin and not args.text:
        parser.error("give some text, or --stdin, or --quiz")
    if args.repeat < 1:
        parser.error("--repeat must be >= 1")

    unit = 1.2 / args.wpm
    if args.farnsworth and args.farnsworth < args.wpm:
        letter_gap, word_gap = farnsworth_gaps(args.wpm, args.farnsworth)
        speed_txt = (f"{args.wpm:.0f} WPM characters / "
                     f"{args.farnsworth:.0f} WPM effective (Farnsworth)")
    else:
        letter_gap, word_gap = 3 * unit, 7 * unit
        speed_txt = f"{args.wpm:.0f} WPM"

    with hapbeat.connect(app_name="MorseText", default_target=args.target) as hb:
        try:
            devices = hb.discover(timeout=1.0)
            if devices:
                print("Devices: " + ", ".join(d.address or d.ip for d in devices))
            else:
                print("No device replied to PING -- continuing anyway (UDP broadcast).")
            print(f"{speed_txt} (unit {unit * 1000:.0f} ms)\n")

            if args.quiz:
                run_quiz(hb, args, unit, letter_gap, word_gap)
            elif args.stdin:
                first = True
                for line in sys.stdin:
                    line = line.strip()
                    if not line:
                        continue
                    if not first:
                        sleep_for(word_gap)
                    first = False
                    send_text(hb, args, line, unit, letter_gap, word_gap)
            else:
                for r in range(args.repeat):
                    if r:
                        sleep_for(args.pause)
                    send_text(hb, args, args.text, unit, letter_gap, word_gap)
        except KeyboardInterrupt:
            print("\nStopped.")
        finally:
            hb.stop_all()
    return 0


if __name__ == "__main__":
    sys.exit(main())
