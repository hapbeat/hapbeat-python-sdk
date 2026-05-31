"""``hapbeat`` command-line interface.

Examples::

    hapbeat scan
    hapbeat play impact.hit --gain 0.3
    hapbeat stop impact.hit
    hapbeat stop-all
    hapbeat osc-bridge --listen 7702
"""

from __future__ import annotations

import argparse
import sys

from . import __version__
from .hapbeat import connect


def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--port", type=int, default=7700, help="UDP port (default 7700)")
    p.add_argument("--target", default="", help="device address; empty = all (broadcast)")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hapbeat", description="Drive Hapbeat devices.")
    parser.add_argument("--version", action="version", version=f"hapbeat {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p_play = sub.add_parser("play", help="play an event by id")
    p_play.add_argument("event_id")
    p_play.add_argument("--gain", type=float, default=None, help="0..1 (default: kit baseline)")
    _add_common(p_play)

    p_stop = sub.add_parser("stop", help="stop one event id")
    p_stop.add_argument("event_id")
    _add_common(p_stop)

    p_stop_all = sub.add_parser("stop-all", help="stop everything")
    _add_common(p_stop_all)

    p_ping = sub.add_parser("ping", help="broadcast a keep-alive ping")
    _add_common(p_ping)

    p_scan = sub.add_parser("scan", help="discover devices on the LAN")
    p_scan.add_argument("--timeout", type=float, default=1.5)
    _add_common(p_scan)

    p_osc = sub.add_parser("osc-bridge", help="relay /hapbeat/* OSC to devices")
    p_osc.add_argument("--listen", type=int, default=7702, help="OSC listen port (default 7702)")
    _add_common(p_osc)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.command == "osc-bridge":
        from .osc import OscBridge

        hb = connect(port=args.port, app_name="hapbeat-osc")
        try:
            print(f"OSC bridge: listening on :{args.listen}, relaying to UDP {args.port}")
            OscBridge(hb, listen_port=args.listen).serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            hb.close()
        return 0

    hb = connect(port=args.port, default_target=getattr(args, "target", ""), keepalive=False)
    try:
        if args.command == "play":
            ok = hb.play(args.event_id, args.gain)
            print(f"play {args.event_id} gain={args.gain if args.gain is not None else 'baseline'} -> {'sent' if ok else 'FAILED'}")
        elif args.command == "stop":
            print("sent" if hb.stop(args.event_id) else "FAILED")
        elif args.command == "stop-all":
            print("sent" if hb.stop_all() else "FAILED")
        elif args.command == "ping":
            print("sent" if hb.ping() else "FAILED")
        elif args.command == "scan":
            devices = hb.discover(timeout=args.timeout)
            if not devices:
                print("no devices replied")
            for d in devices:
                print(f"{d.ip:<16} {d.address or '?':<20} {d.name or '?':<16} fw={d.firmware_version or '?'}")
    finally:
        hb.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
