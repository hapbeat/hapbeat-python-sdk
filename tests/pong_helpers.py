"""Build device PONG replies for tests (no sockets): matched replies to the
SDK's own unicast extended PING, with or without the Stream Gen 2 HBS2 tail."""

from __future__ import annotations

import struct
from typing import Optional

from hapbeat import protocol

VALID = protocol.STREAM_LEASE_FLAG_VALID
SUPERSEDED = protocol.STREAM_LEASE_FLAG_SUPERSEDED
REISSUED = protocol.STREAM_LEASE_FLAG_REISSUED


def lease_tail(
    incarnation: int,
    boot_id: int,
    ticket: int,
    *,
    flags: int = VALID,
    high_water: int = 0,
    version: int = protocol.STREAM_LEASE_TAIL_VERSION,
    reserved: int = 0,
) -> bytes:
    return (
        protocol.STREAM_LEASE_TAIL_MARKER
        + struct.pack("<BBH", version, flags, reserved)
        + struct.pack("<QQII", incarnation, boot_id, ticket, high_water)
    )


def pong_packet(
    seq: int, timestamp: int, address: str, tail: bytes = b"", firmware: str = "v1"
) -> bytes:
    payload = struct.pack("<qq", timestamp, 1)
    payload += b"hb\x00" + address.encode() + b"\x00" + firmware.encode() + b"\x00"
    payload += bytes([5, 50]) + tail
    return protocol.build_header(protocol.CMD_PONG, seq, len(payload)) + payload


def last_ping(hb, ip: Optional[str] = None) -> tuple[int, int, int]:
    """(seq, timestamp, incarnation) of the newest outstanding PING; with
    ``ip``, of the newest extended (lease) PING sent to that address."""
    pending = [
        (seq, ping) for seq, ping in hb._pending_pings.items()
        if ip is None or (ping.extended and ping.ip == ip)
    ]
    seq, newest = max(pending, key=lambda kv: kv[1].timestamp_us)
    return seq, newest.timestamp_us, newest.client_incarnation


def reply(
    hb,
    ip: str,
    address: str,
    *,
    lease: Optional[tuple[int, int]] = None,
    flags: int = VALID,
    high_water: int = 0,
    tail: Optional[bytes] = None,
    ping: Optional[tuple[int, int, int]] = None,
    firmware: str = "v1",
) -> dict:
    """Deliver a direct reply to ``ping`` (default: a fresh unicast lease PING
    ``hb._lease_ping(ip)``).

    ``lease=(boot_id, ticket)`` answers like Stream Gen 2 firmware (HBS2 tail
    echoing the PING's incarnation); without it like older firmware. ``tail``
    overrides the raw bytes after volume_wiper.
    """
    if ping is None:
        hb._lease_ping(ip)
        ping = last_ping(hb, ip)
    seq, timestamp, incarnation = ping
    if tail is None:
        tail = (
            b"" if lease is None
            else lease_tail(incarnation, *lease, flags=flags, high_water=high_water)
        )
    pong = protocol.parse_pong(pong_packet(seq, timestamp, address, tail, firmware))
    hb._on_pong(pong, ip)
    return pong
