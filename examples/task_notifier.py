"""Run any command and feel the result -- haptic notifications for long jobs.

Wraps a shell command (a build, a test suite, an ML training run) and fires a
haptic pattern when it finishes: one medium pulse on success, three strong
pulses on failure. While the job runs it can tap you on a schedule
(--heartbeat), on matching output lines (--match), or when output goes
silent for too long (--stall) -- the three failure smells of a long job.

The wrapper is transparent: the child's stdout/stderr pass through, its exit
code is preserved, and all of the wrapper's own messages go to stderr, so it
drops into shell pipelines. Cancelling with Ctrl-C is treated as a deliberate
act -- no failure buzz.

Prerequisites:
    - A Hapbeat device on the same Wi-Fi/LAN.
    - A kit with a short one-shot event (pass its id with --event).

Run:
    python examples/task_notifier.py -- pytest -x
    python examples/task_notifier.py --heartbeat 30 -- python train.py
    python examples/task_notifier.py --match "Epoch [0-9]+ done|loss: nan" \
        --stall 300 -- python -u train.py
    python examples/task_notifier.py --test        # feel the patterns, no job

Put wrapper options before the "--"; everything after it is the command.

Notes on --match/--stall: the child's output is piped through the wrapper,
which makes many programs switch to block buffering (matches arrive late).
PYTHONUNBUFFERED=1 is injected for Python children; for others, pass their
own unbuffered flag (e.g. `python -u`, `stdbuf -oL`). \\r progress bars may
render degraded while piped.
"""

from __future__ import annotations

import argparse
import locale
import os
import re
import shutil
import subprocess
import sys
import threading
import time

import hapbeat

STATUS_CONTROL_C_EXIT = 0xC000013A  # Windows child killed by Ctrl-C


def split_command(remainder: list[str]) -> list[str]:
    """Strip the leading "--" argparse leaves in a REMAINDER capture."""
    if remainder and remainder[0] == "--":
        return remainder[1:]
    return remainder


def gain01(s: str) -> float:
    v = float(s)
    if not 0.0 <= v <= 1.0:
        raise argparse.ArgumentTypeError("gain must be in 0..1")
    return v


def log(msg: str) -> None:
    print(f"task_notifier: {msg}", file=sys.stderr)


def fire_pulses(hb: hapbeat.Hapbeat, event_id: str, gain: float,
                count: int, interval: float) -> None:
    for i in range(count):
        if i:
            time.sleep(interval)
        hb.play(event_id, gain=gain)


def run_piped(hb: hapbeat.Hapbeat, cmd: list[str],
              args: argparse.Namespace) -> int:
    """Run the child with piped output for --match / --stall watching.

    A daemon thread drains the pipe in chunks (so \\r-only progress bars
    still pass through and count as output) while the main thread just polls
    the child -- that keeps Ctrl-C responsive even on Windows, where a
    blocking pipe read cannot be interrupted.
    """
    pattern = re.compile(args.match) if args.match else None
    # Child output arrives in the console's legacy encoding on Windows
    # (cp932 etc.); decoding as utf-8 would make Japanese --match patterns
    # silently never match.
    encoding = locale.getpreferredencoding(False)
    env = os.environ.copy()
    env.setdefault("PYTHONUNBUFFERED", "1")
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, env=env)
    state = {"last_output": time.monotonic(), "stall_fired": False}
    done = threading.Event()

    def on_line(line_bytes: bytes) -> None:
        line = line_bytes.decode(encoding, errors="replace")
        now = time.monotonic()
        if pattern.search(line) and now - on_line.last_match >= args.match_cooldown:
            on_line.last_match = now
            hb.play(args.match_event or args.event, gain=args.match_gain)

    on_line.last_match = 0.0

    def reader() -> None:
        buf = b""
        while True:
            chunk = proc.stdout.read1(8192)
            if not chunk:
                break
            sys.stdout.buffer.write(chunk)
            sys.stdout.buffer.flush()
            state["last_output"] = time.monotonic()
            state["stall_fired"] = False
            if pattern is not None:
                parts = re.split(rb"[\r\n]", buf + chunk)
                buf = parts[-1]
                for ln in parts[:-1]:
                    if ln:
                        on_line(ln)
        if pattern is not None and buf:
            on_line(buf)

    def watchdog() -> None:
        while not done.wait(1.0):
            if not state["stall_fired"] and \
                    time.monotonic() - state["last_output"] > args.stall:
                state["stall_fired"] = True  # re-armed when output resumes
                log(f"no output for {args.stall:.0f}s")
                fire_pulses(hb, args.event, args.stall_gain, 2, 0.2)

    reader_thread = threading.Thread(target=reader, name="notifier-reader",
                                     daemon=True)
    reader_thread.start()
    if args.stall:
        threading.Thread(target=watchdog, name="notifier-stall",
                         daemon=True).start()
    try:
        while proc.poll() is None:
            time.sleep(0.2)
        reader_thread.join(timeout=2.0)  # let the output tail drain
        return proc.wait()
    except KeyboardInterrupt:
        # The child usually received the same Ctrl-C; give it time to clean
        # up (checkpoints etc.) while the reader keeps draining its output.
        try:
            proc.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            proc.terminate()
            try:
                proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                pass
        return 130
    finally:
        done.set()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run a command; notify success/failure through haptics.",
        epilog='Example: python task_notifier.py --heartbeat 30 -- pytest -x',
    )
    parser.add_argument("--event", default="sample-kit.sine_100hz",
                        help="event id of a short one-shot in your kit (default: sample-kit.sine_100hz)")
    parser.add_argument("--fail-event", default=None,
                        help="separate event id for failure (default: --event)")
    parser.add_argument("--success-gain", type=gain01, default=0.6,
                        help="gain of the success pulse, 0..1 (default 0.6)")
    parser.add_argument("--fail-gain", type=gain01, default=1.0,
                        help="gain of the failure pulses, 0..1 (default 1.0)")
    parser.add_argument("--success-pulses", type=int, default=1,
                        help="pulses on success (default 1)")
    parser.add_argument("--fail-pulses", type=int, default=3,
                        help="pulses on failure (default 3)")
    parser.add_argument("--pulse-interval", type=float, default=0.3,
                        help="seconds between pulses (default 0.3)")
    parser.add_argument("--heartbeat", type=float, default=0.0, metavar="SECONDS",
                        help="while running, tap softly every N seconds (0 = off)")
    parser.add_argument("--heartbeat-gain", type=gain01, default=0.15,
                        help="gain of the heartbeat tap (default 0.15)")
    parser.add_argument("--match", default=None, metavar="REGEX",
                        help="pulse whenever a child output line matches "
                             "(pipes the child's output -- see module notes)")
    parser.add_argument("--match-event", default=None,
                        help="event id for --match pulses (default: --event)")
    parser.add_argument("--match-gain", type=gain01, default=0.4,
                        help="gain of --match pulses (default 0.4)")
    parser.add_argument("--match-cooldown", type=float, default=5.0,
                        help="min seconds between --match pulses (default 5)")
    parser.add_argument("--stall", type=float, default=0.0, metavar="SECONDS",
                        help="double-pulse if output is silent this long "
                             "(0 = off; implies piped output)")
    parser.add_argument("--stall-gain", type=gain01, default=0.8,
                        help="gain of the stall alert (default 0.8)")
    parser.add_argument("--min-duration", type=float, default=0.0, metavar="SECONDS",
                        help="skip the success buzz for jobs shorter than this "
                             "(failures always notify)")
    parser.add_argument("--no-preflight", action="store_true",
                        help="skip the startup device scan")
    parser.add_argument("--test", action="store_true",
                        help="play the success then failure pattern and exit "
                             "(verify device + event id before a long job)")
    parser.add_argument("--target", default="",
                        help="device address; empty = all (broadcast)")
    parser.add_argument("command", nargs=argparse.REMAINDER,
                        help='the command to run, after a "--" separator')
    args = parser.parse_args()

    cmd = split_command(args.command)
    if not cmd and not args.test:
        parser.error('no command given -- usage: task_notifier.py [options] -- <command...>')
    if args.match:
        try:
            re.compile(args.match)
        except re.error as exc:
            parser.error(f"bad --match regex: {exc}")

    with hapbeat.connect(app_name="TaskNotifier", default_target=args.target) as hb:
        if not args.no_preflight:
            try:
                found = hb.discover(timeout=1.0)
            except KeyboardInterrupt:
                log("cancelled")
                return 130
            if not found:
                log("warning: no Hapbeat device replied to PING -- "
                    "notifications may go nowhere")

        if args.test:
            log("success pattern...")
            fire_pulses(hb, args.event, args.success_gain,
                        args.success_pulses, args.pulse_interval)
            time.sleep(1.0)
            log("failure pattern...")
            fire_pulses(hb, args.fail_event or args.event, args.fail_gain,
                        args.fail_pulses, args.pulse_interval)
            time.sleep(0.2)
            return 0

        # Resolve .cmd/.bat on Windows (CreateProcess skips PATHEXT).
        resolved = shutil.which(cmd[0])
        if resolved:
            cmd = [resolved] + cmd[1:]

        stop_heartbeat = threading.Event()
        if args.heartbeat > 0:
            def beat() -> None:
                while not stop_heartbeat.wait(args.heartbeat):
                    hb.play(args.event, gain=args.heartbeat_gain)

            threading.Thread(target=beat, name="notifier-heartbeat",
                             daemon=True).start()

        started = time.perf_counter()
        try:
            if args.match or args.stall:
                rc = run_piped(hb, cmd, args)
            else:
                rc = subprocess.run(cmd).returncode
        except FileNotFoundError:
            log(f"command not found: {cmd[0]}")
            rc = 127
        except KeyboardInterrupt:
            rc = 130
        finally:
            stop_heartbeat.set()
        elapsed = time.perf_counter() - started

        if rc < 0:  # POSIX signal death: report shell-style 128+N, not -N & 0xFF
            rc = 128 - rc
        if rc in (130, STATUS_CONTROL_C_EXIT):
            # The user cancelled on purpose; they're at the keyboard already.
            log(f"cancelled after {elapsed:.1f}s")
            rc = 130
        elif rc == 0:
            if elapsed >= args.min_duration:
                fire_pulses(hb, args.event, args.success_gain,
                            args.success_pulses, args.pulse_interval)
            log(f"success in {elapsed:.1f}s")
        else:
            fire_pulses(hb, args.fail_event or args.event, args.fail_gain,
                        args.fail_pulses, args.pulse_interval)
            log(f"exit code {rc} after {elapsed:.1f}s")
        # Let the last pulse leave the socket before close().
        time.sleep(0.1)
    return rc


if __name__ == "__main__":
    sys.exit(main())
