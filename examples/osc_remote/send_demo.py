"""Tiny OSC sender that mimics a TouchOSC phone remote (for testing the bridge).

The real use case is a phone (TouchOSC, etc.) sending /hapbeat/* over Wi-Fi to
the bridge -- no code on the phone. This script lets you test the same path
from a keyboard without a phone: number keys send the same OSC messages a
TouchOSC button would.

Setup (two terminals):

    # 1) the bridge (loads the haptic file so events route + target by id)
    hapbeat osc-bridge --haptics examples/osc_remote/haptics.json

    # 2) this sender (or point TouchOSC at the bridge host:7702 instead)
    python examples/osc_remote/send_demo.py

Keys:  1 = demo.tap   2 = demo.rumble   space = stop all   q = quit

Requires python-osc:  pip install "hapbeat-python-sdk[osc]"
"""

import sys

OSC_HOST = "127.0.0.1"   # the bridge host (same machine here)
OSC_PORT = 7702          # the bridge's --listen port


def read_key() -> str:
    try:
        import msvcrt

        ch = msvcrt.getwch()
        if ch in ("\x00", "\xe0"):
            msvcrt.getwch()
            return ""
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
    return ch.lower()


def main() -> int:
    try:
        from pythonosc.udp_client import SimpleUDPClient
    except ImportError:
        print('python-osc is required: pip install "hapbeat-python-sdk[osc]"',
              file=sys.stderr)
        return 1

    client = SimpleUDPClient(OSC_HOST, OSC_PORT)
    pads = {"1": "demo.tap", "2": "demo.rumble"}
    print(f"OSC remote -> {OSC_HOST}:{OSC_PORT}")
    print("  [1] demo.tap   [2] demo.rumble   [space] stop all   [q] quit")
    print("  (a TouchOSC button mapped to /hapbeat/play with arg 'demo.tap' does the same)")
    try:
        while True:
            ch = read_key()
            if ch == "q":
                break
            if ch == " ":
                client.send_message("/hapbeat/stop-all", [])
                print("\rstop all" + " " * 30, end="", flush=True)
            elif ch in pads:
                # /hapbeat/play <event_id> -- omit target so the bridge's
                # haptic file decides where it goes (per-event target).
                client.send_message("/hapbeat/play", [pads[ch]])
                print(f"\rsent /hapbeat/play {pads[ch]}" + " " * 12, end="", flush=True)
    except KeyboardInterrupt:
        pass
    print("\nbye")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
