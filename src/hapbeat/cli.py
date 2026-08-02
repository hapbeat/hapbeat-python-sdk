"""``hapbeat`` command-line interface.

Examples::

    hapbeat scan
    hapbeat play sample-kit.sine_100hz --gain 0.3
    hapbeat stop sample-kit.sine_100hz
    hapbeat stop-all
    hapbeat osc-bridge --listen 7702
    hapbeat launchpad
"""

from __future__ import annotations

import argparse
import sys

from . import __version__
from .eventmap import EventMap
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
    p_osc.add_argument("--haptics", default=None,
                       help="haptic file (overlay) so OSC events route command/clip + use per-event targets")
    p_osc.add_argument("--kit", default=None,
                       help="kit folder (alternative to --haptics; intensity/clip only, no targeting)")
    _add_common(p_osc)

    p_lp = sub.add_parser(
        "launchpad",
        help="serve a local web page to try features from the browser",
    )
    p_lp.add_argument("--host", default="127.0.0.1", help="HTTP bind host (default 127.0.0.1)")
    p_lp.add_argument("--http-port", type=int, default=7100, help="HTTP port (default 7100)")
    p_lp.add_argument("--no-open", action="store_true", help="do not open a browser")
    _add_common(p_lp)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    # 新しい版が出ていれば stderr に 1 行だけ (同じ版では 2 回目を出さない)。
    # キャッシュが無ければ裏で取りに行くだけでコマンドは待たせない。
    # ライブラリ import 側では絶対に呼ばない — CLI 実行時のみ (DEC-053 §5.4)。
    from . import update_check
    update_check.notify_cli(__version__)

    if args.command == "osc-bridge":
        from .osc import OscBridge

        event_map = None
        if args.haptics:
            event_map = EventMap.from_file(args.haptics)
        elif args.kit:
            event_map = EventMap.from_kit(args.kit)
        hb = connect(port=args.port, app_name="hapbeat-osc",
                     default_target=args.target, event_map=event_map)
        try:
            mode = "command+clip" if event_map else "command only"
            print(f"OSC bridge: listening on :{args.listen}, relaying to UDP {args.port} ({mode})")
            OscBridge(hb, listen_port=args.listen).serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            hb.close()
        return 0

    if args.command == "launchpad":
        from .launchpad import serve

        return serve(host=args.host, port=args.http_port, udp_port=args.port,
                     target=args.target, open_browser=not args.no_open)

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
