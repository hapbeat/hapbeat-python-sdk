"""Haptic metronome with live control, accents, gap training, and tempo ramps.

A silent metronome you feel instead of hear: practice rooms, drummers with
in-ears, dancers, and runners doing cadence training. Beats are scheduled on
an absolute timeline (no cumulative drift), with a hybrid sleep so timing
stays tight even on Windows.

Live keys (while running):
    + / -    tempo +2 / -2 BPM        [ / ]    tempo -10 / +10 BPM
    t        tap tempo (3+ taps)      space    pause / resume
    q        quit

Features:
    --beats 2+2+3   accent grouping for odd meters (7/8 as 2+2+3); a plain
                    number accents beat 1; 0 disables accents (cadence mode)
    --gap 4:2       gap training -- 4 bars on, 2 bars muted; keep time
                    through the silence and check yourself on re-entry
    --ramp 150:180:300   linear tempo ramp (cadence training)
    --count-in 1    count-in bars before the piece starts

Prerequisites:
    - A Hapbeat device on the same Wi-Fi/LAN.
    - A kit with a short one-shot "tick" event (pass its id with --event).

Run:
    python examples/metronome.py --bpm 120
    python examples/metronome.py --bpm 96 --beats 2+2+3 --bars 64
    python examples/metronome.py --ramp 150:180:300 --beats 0
    python examples/metronome.py --bpm 80 --gap 4:4 --count-in 1
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from collections import deque

import hapbeat


def parse_beats(text: str) -> tuple[int, set[int]]:
    """Parse --beats into (bar_length, accented beat indices).

    "4" -> (4, {0});  "2+2+3" -> (7, {0, 2, 4});  "0" -> (0, set()).
    """
    groups = [int(g) for g in text.split("+")]
    if len(groups) == 1:
        n = groups[0]
        if n < 0:
            raise ValueError("--beats must be >= 0")
        return (n, {0} if n > 0 else set())
    if any(g <= 0 for g in groups):
        raise ValueError("--beats groups must all be > 0")
    accents, pos = set(), 0
    for g in groups:
        accents.add(pos)
        pos += g
    return pos, accents


def parse_ramp(text: str) -> tuple[float, float, float]:
    """Parse --ramp "START:END:SECONDS"."""
    start, end, secs = (float(s) for s in text.split(":"))
    if start <= 0 or end <= 0 or secs <= 0:
        raise ValueError("--ramp values must all be > 0")
    return start, end, secs


def parse_gap(text: str) -> tuple[int, int]:
    """Parse --gap "PLAY:MUTE" bar counts."""
    play, mute = (int(s) for s in text.split(":"))
    if play <= 0 or mute <= 0:
        raise ValueError("--gap needs PLAY > 0 and MUTE > 0")
    return play, mute


def bpm_at(elapsed: float, start: float, end: float, ramp_secs: float) -> float:
    """Tempo at `elapsed` seconds into a linear ramp (clamped at the end)."""
    if ramp_secs <= 0:
        return start
    frac = min(1.0, elapsed / ramp_secs)
    return start + (end - start) * frac


def gain01(s: str) -> float:
    v = float(s)
    if not 0.0 <= v <= 1.0:
        raise argparse.ArgumentTypeError("gain must be in 0..1")
    return v


class KeyPoller:
    """Non-blocking single-key polling, Windows console + POSIX tty.

    On POSIX the terminal goes into cbreak mode for the lifetime of the
    context (Ctrl-C still raises KeyboardInterrupt). When stdin is not a
    tty (piped/redirected), polling is disabled and poll() returns None.
    """

    def __enter__(self) -> "KeyPoller":
        self._mode = "off"
        try:
            import msvcrt  # noqa: F401

            self._mode = "win"
        except ImportError:
            if sys.stdin.isatty():
                import termios
                import tty

                self._fd = sys.stdin.fileno()
                self._old = termios.tcgetattr(self._fd)
                tty.setcbreak(self._fd)
                self._mode = "posix"
        return self

    def __exit__(self, *exc) -> None:
        if self._mode == "posix":
            import termios

            termios.tcsetattr(self._fd, termios.TCSADRAIN, self._old)

    def poll(self) -> str | None:
        if self._mode == "win":
            import msvcrt

            if msvcrt.kbhit():
                ch = msvcrt.getwch()
                if ch in ("\x00", "\xe0"):  # extended key: discard its code
                    msvcrt.getwch()
                    return None
                if ch == "\x03":
                    raise KeyboardInterrupt
                return ch.lower()
        elif self._mode == "posix":
            import select

            # os.read on the raw fd -- sys.stdin.read would buffer the tail
            # of escape sequences where select() can no longer see it.
            if select.select([self._fd], [], [], 0)[0]:
                ch = os.read(self._fd, 1).decode(errors="ignore")
                if ch == "\x1b":  # drain the rest of an escape sequence
                    while select.select([self._fd], [], [], 0)[0]:
                        os.read(self._fd, 32)
                    return None
                return ch.lower()
        return None


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Haptic metronome (live keys, accents, gap training, ramps)."
    )
    parser.add_argument("--bpm", type=float, default=100.0,
                        help="tempo in beats per minute (default 100)")
    parser.add_argument("--ramp", default=None, metavar="START:END:SECONDS",
                        help="linear tempo ramp, e.g. 150:180:300; overrides --bpm")
    parser.add_argument("--beats", default="4",
                        help="beats per bar, or accent groups like 2+2+3 "
                             "(0 = no accent, default 4)")
    parser.add_argument("--subdiv", type=int, default=1,
                        help="subdivisions per beat (default 1 = none)")
    parser.add_argument("--gap", default=None, metavar="PLAY:MUTE",
                        help="gap training: PLAY bars on, MUTE bars silent")
    parser.add_argument("--count-in", type=int, default=0, metavar="BARS",
                        help="count-in bars before the piece (default 0)")
    parser.add_argument("--bars", type=int, default=0,
                        help="stop after N bars (0 = no limit)")
    parser.add_argument("--minutes", type=float, default=0.0,
                        help="run time in minutes (0 = until q / Ctrl-C)")
    parser.add_argument("--event", default="tick",
                        help="event id of a short one-shot in your kit (default: tick)")
    parser.add_argument("--accent-event", default=None,
                        help="separate event id for accented beats (default: --event)")
    parser.add_argument("--gain", type=gain01, default=0.5,
                        help="gain of normal beats, 0..1 (default 0.5)")
    parser.add_argument("--accent-gain", type=gain01, default=1.0,
                        help="gain of accented beats, 0..1 (default 1.0)")
    parser.add_argument("--subdiv-gain", type=gain01, default=0.25,
                        help="gain of subdivision ticks, 0..1 (default 0.25)")
    parser.add_argument("--target", default="",
                        help="device address; empty = all (broadcast)")
    args = parser.parse_args()

    if args.subdiv < 1:
        parser.error("--subdiv must be >= 1")
    try:
        bar_len, accents = parse_beats(args.beats)
        ramp = parse_ramp(args.ramp) if args.ramp else None
        gap = parse_gap(args.gap) if args.gap else None
    except ValueError as exc:
        parser.error(str(exc))
    if bar_len == 0 and (gap or args.bars or args.count_in):
        parser.error("--gap/--bars/--count-in need bars; use --beats N > 0")

    start_bpm, end_bpm, ramp_secs = ramp if ramp else (args.bpm, args.bpm, 0.0)
    if start_bpm <= 0:
        parser.error("BPM must be > 0")
    accent_event = args.accent_event or args.event
    deadline_s = args.minutes * 60.0 if args.minutes > 0 else None
    marks_template = "".join(">" if i in accents else "." for i in range(max(bar_len, 1)))

    with hapbeat.connect(app_name="Metronome", default_target=args.target) as hb, \
            KeyPoller() as keys:
        manual_bpm: float | None = None
        paused = False
        pause_began = 0.0
        quit_flag = False
        taps: deque[float] = deque(maxlen=5)

        def handle_key(ch: str) -> None:
            nonlocal manual_bpm, paused, pause_began, quit_flag, next_t, start
            current = manual_bpm if manual_bpm is not None else bpm_at(
                time.perf_counter() - start, start_bpm, end_bpm, ramp_secs)
            if ch in ("+", "="):
                manual_bpm = min(400.0, current + 2)
            elif ch == "-":
                manual_bpm = max(20.0, current - 2)
            elif ch == "]":
                manual_bpm = min(400.0, current + 10)
            elif ch == "[":
                manual_bpm = max(20.0, current - 10)
            elif ch == "t":
                now = time.perf_counter()
                if taps and now - taps[-1] > 2.5:
                    taps.clear()
                taps.append(now)
                if len(taps) >= 3:
                    diffs = [b - a for a, b in zip(taps, list(taps)[1:])]
                    manual_bpm = max(20.0, min(400.0, 60.0 / (sum(diffs) / len(diffs))))
            elif ch == " ":
                if not paused:
                    paused = True
                    pause_began = time.perf_counter()
                else:
                    paused = False
                    # Freeze the timeline: the ramp position and --minutes
                    # budget must not advance while paused.
                    start += time.perf_counter() - pause_began
                    next_t = time.perf_counter() + 0.2
            elif ch == "q":
                quit_flag = True

        def wait_until(deadline: float) -> None:
            """Sleep to `deadline`, polling keys; freezes while paused."""
            was_paused = False
            while True:
                ch = keys.poll()
                if ch:
                    handle_key(ch)
                if quit_flag:
                    return
                if paused:
                    print("\r[paused] space to resume, q to quit" + " " * 20,
                          end="", flush=True)
                    time.sleep(0.05)
                    was_paused = True
                    continue
                if was_paused:  # resume: pick up the re-anchored beat time
                    was_paused = False
                    deadline = next_t
                remaining = deadline - time.perf_counter()
                if remaining <= 0:
                    return
                time.sleep(min(0.05, remaining - 0.02) if remaining > 0.02 else 0)

        try:
            devices = hb.discover(timeout=1.0)
            if devices:
                print("Devices: " + ", ".join(d.address or d.ip for d in devices))
            else:
                print("No device replied to PING -- continuing anyway (UDP broadcast).")

            tempo_txt = (f"{start_bpm:.0f}->{end_bpm:.0f} BPM over {ramp_secs:.0f}s"
                         if ramp else f"{start_bpm:.0f} BPM")
            print(f"{tempo_txt}, beats {args.beats}, subdiv {args.subdiv}"
                  + (f", gap {args.gap}" if gap else "") + ".")
            print("keys: +/- = +-2 BPM   [/] = +-10   t = tap   space = pause   q = quit")

            start = time.perf_counter() + 0.5
            next_t = start
            if args.count_in:
                for b in range(args.count_in * bar_len):
                    wait_until(next_t)
                    if quit_flag:
                        raise KeyboardInterrupt
                    hb.play(accent_event, gain=args.accent_gain)
                    print(f"\rcount-in {b + 1}/{args.count_in * bar_len}   ",
                          end="", flush=True)
                    next_t += 60.0 / start_bpm
                start = next_t  # the piece (and any ramp) starts here

            beat_in_bar = 0
            bar_idx = 0
            while not quit_flag:
                elapsed = next_t - start
                if deadline_s is not None and elapsed >= deadline_s:
                    break
                if args.bars and bar_idx >= args.bars:
                    break
                bpm = manual_bpm if manual_bpm is not None else bpm_at(
                    elapsed, start_bpm, end_bpm, ramp_secs)
                beat_interval = 60.0 / bpm
                muted = gap is not None and bar_idx % sum(gap) >= gap[0]

                wait_until(next_t)
                if quit_flag:
                    break
                # After a stall (laptop sleep, heavy load), re-anchor instead
                # of machine-gunning the backlog of missed beats.
                if time.perf_counter() - next_t > beat_interval:
                    next_t = time.perf_counter()

                accented = beat_in_bar in accents
                if not muted:
                    if accented:
                        hb.play(accent_event, gain=args.accent_gain)
                    else:
                        hb.play(args.event, gain=args.gain)
                pos = beat_in_bar if bar_len > 0 else 0
                print(f"\r{bpm:6.1f} BPM  bar {bar_idx + 1:>3}  beat {pos + 1:>2} "
                      f"{marks_template}{' [mute]' if muted else '       '}",
                      end="", flush=True)

                for s in range(1, args.subdiv):
                    wait_until(next_t + beat_interval * s / args.subdiv)
                    if quit_flag or paused:
                        break
                    if not muted:
                        hb.play(args.event, gain=args.subdiv_gain)

                next_t += beat_interval
                if bar_len > 0:
                    beat_in_bar = (beat_in_bar + 1) % bar_len
                    if beat_in_bar == 0:
                        bar_idx += 1
            print("\nDone.")
        except KeyboardInterrupt:
            print("\nStopped.")
        finally:
            hb.stop_all()
    return 0


if __name__ == "__main__":
    sys.exit(main())
