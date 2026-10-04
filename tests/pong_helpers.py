"""Build device PONG replies for tests (no sockets): matched replies to the
SDK's own extended PING, with or without the stream-session-v2 HBS2 tail."""

from __future__ import annotations

import struct
from typing import Optional

from hapbeat import protocol

VALID = protocol.STREAM_LEASE_FLAG_VALID
SUPERSEDED = protocol.STREAM_LEASE_FLAG_SUPERSEDED


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
    seq: int, timestamp: int, address: str, tail: bytes = b""
) -> bytes:
    payload = struct.pack("<qq", timestamp, 1)
    payload += b"hb\x00" + address.encode() + b"\x00v1\x00" + bytes([5, 50])
    payload += tail
    return protocol.build_header(protocol.CMD_PONG, seq, len(payload)) + payload


def last_ping(hb) -> tuple[int, int, int]:
    """(seq, timestamp, incarnation) of the newest outstanding PING."""
    seq, pending = max(hb._pending_pings.items(), key=lambda kv: kv[1].timestamp_us)
    return seq, pending.timestamp_us, pending.client_incarnation


def reply(
    hb,
    ip: str,
    address: str,
    *,
    lease: Optional[tuple[int, int]] = None,
    flags: int = VALID,
    tail: Optional[bytes] = None,
    ping: Optional[tuple[int, int, int]] = None,
) -> dict:
    """Deliver a direct reply to ``ping`` (default: a fresh ``hb.ping()``).

    ``lease=(boot_id, ticket)`` answers like v2 firmware (HBS2 tail echoing
    the PING's incarnation); without it like pre-v2 firmware. ``tail``
    overrides the raw bytes after volume_wiper.
    """
    if ping is None:
        hb.ping()
        ping = last_ping(hb)
    seq, timestamp, incarnation = ping
    if tail is None:
        tail = b"" if lease is None else lease_tail(incarnation, *lease, flags=flags)
    pong = protocol.parse_pong(pong_packet(seq, timestamp, address, tail))
    hb._on_pong(pong, ip)
    return pong
