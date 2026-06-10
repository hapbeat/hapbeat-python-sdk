"""Run any command and feel the result — haptic notifications for long jobs.

Wraps a shell command (a build, a test suite, an ML training run) and fires a
haptic pattern when it finishes: one medium pulse on success, three strong
pulses on failure. Optionally taps you softly every N seconds while the job is
still running, so you know it has not died while you are away from the screen.

The wrapper is transparent: the child's stdout/stderr pass through and its
exit code is preserved, so it drops into CI scripts and shell pipelines.

Prerequisites:
    - A Hapbeat device on the same Wi-Fi/LAN.
    - A kit with a short one-shot event (pass its id with --event).

Run:
    python examples/task_notifier.py -- pytest -x
    python examples/task_notifier.py --heartbeat 30 -- python train.py
    python examples/task_notifier.py --event alert.soft -- cargo build --release

Put wrapper options before the "--"; everything after it is the command.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import threading
import time

import hapbeat


def fire_pulses(hb: hapbeat.Hapbeat, event_id: str, gain: float,
                count: int, interval: float) -> None:
    for i in range(count):
        if i:
            time.sleep(interval)
        hb.play(event_id, gain=gain)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run a command; notify success/failure through haptics.",
        epilog='Example: python task_notifier.py --heartbeat 30 -- pytest -x',
    )
    parser.add_argument("--event", default="impact.hit",
                        help="event id used for all pulses (default impact.hit)")
    parser.add_argument("--fail-event", default=None,
                        help="separate event id for failure (default: --event)")
    parser.add_argument("--success-gain", type=float, default=0.6)
    parser.add_argument("--fail-gain", type=float, default=1.0)
    parser.add_argument("--success-pulses", type=int, default=1)
    parser.add_argument("--fail-pulses", type=int, default=3)
    parser.add_argument("--pulse-interval", type=float, default=0.3,
                        help="seconds between pulses (default 0.3)")
    parser.add_argument("--heartbeat", type=float, default=0.0, metavar="SECONDS",
                        help="while running, tap softly every N seconds (0 = off)")
    parser.add_argument("--heartbeat-gain", type=float, default=0.15)
    parser.add_argument("--target", default="",
                        help="device address; empty = all (broadcast)")
    parser.add_argument("command", nargs=argparse.REMAINDER,
                        help='the command to run, after a "--" separator')
    args = parser.parse_args()

    cmd = args.command
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]
    if not cmd:
        parser.error('no command given -- usage: task_notifier.py [options] -- <command...>')

    with hapbeat.connect(app_name="TaskNotifier", default_target=args.target) as hb:
        stop_heartbeat = threading.Event()
        if args.heartbeat > 0:
            def beat() -> None:
                while not stop_heartbeat.wait(args.heartbeat):
                    hb.play(args.event, gain=args.heartbeat_gain)

            threading.Thread(target=beat, name="notifier-heartbeat",
                             daemon=True).start()

        started = time.perf_counter()
        try:
            rc = subprocess.run(cmd).returncode
        except FileNotFoundError:
            print(f"task_notifier: command not found: {cmd[0]}", file=sys.stderr)
            rc = 127
        except KeyboardInterrupt:
            rc = 130
        finally:
            stop_heartbeat.set()
        elapsed = time.perf_counter() - started

        if rc == 0:
            fire_pulses(hb, args.event, args.success_gain,
                        args.success_pulses, args.pulse_interval)
            print(f"task_notifier: success in {elapsed:.1f}s")
        else:
            fire_pulses(hb, args.fail_event or args.event, args.fail_gain,
                        args.fail_pulses, args.pulse_interval)
            print(f"task_notifier: exit code {rc} after {elapsed:.1f}s")
        # Let the last pulse leave the socket before close().
        time.sleep(0.1)
    return rc


if __name__ == "__main__":
    sys.exit(main())
