"""Hapbeat Layer 1 (SDK -> device) UDP protocol.

Pure, dependency-free packet builders and parsers. This module is the
*single source of truth* for the wire format inside the Python SDK and is
the part that must stay byte-for-byte compatible with
``hapbeat-contracts/specs/message-format.md``.

Every multi-byte field is little-endian. Command packets are capped at
512 bytes; streaming packets at MTU.

The reference implementation in other languages:
    - C#:     hapbeat-unity-sdk/Runtime/HapbeatProtocol.cs
    - Python: hapbeat-helper/src/hapbeat_helper/protocol.py
"""

from __future__ import annotations

import struct
from typing import Optional

# ── Header constants ────────────────────────────────────────────────
MAGIC = 0x4842  # ASCII "HB"
VERSION = 0x01
# STREAM_BEGIN/DATA/END to stream-session-v2 receivers carry header version 2
# (contracts stream-session-v2.md); every other command stays version 1.
STREAM_VERSION = 0x02
HEADER_SIZE = 8
MAX_PACKET_SIZE = 512          # command packets
MAX_STREAM_PACKET_SIZE = 1472  # 1500 MTU - 20 IP - 8 UDP

# App name shown on the device OLED is capped to the display grid width
# (contracts/specs/display-layout.md, 16 chars).
MAX_APP_NAME_LEN = 16

# ── Command types (SDK -> device) ───────────────────────────────────
CMD_PLAY = 0x01
CMD_STOP = 0x02
CMD_STOP_ALL = 0x03
CMD_PING = 0x10
CMD_CONNECT_STATUS = 0x20
CMD_STREAM_BEGIN = 0x30
CMD_STREAM_DATA = 0x31
CMD_STREAM_END = 0x32

# ── Response types (device -> SDK) ──────────────────────────────────
CMD_PONG = 0x11
CMD_ERROR = 0xFF

# ── Stream session v2 (contracts stream-session-v2.md) ──────────────
STREAM_ENVELOPE_SIZE = 16  # device_boot_id(u64) lease_ticket(u32) generation(u32)
STREAM_LEASE_TAIL_MARKER = b"HBS2"
STREAM_LEASE_TAIL_SIZE = 32
STREAM_LEASE_TAIL_VERSION = 1
STREAM_LEASE_FLAG_VALID = 0x01
STREAM_LEASE_FLAG_SUPERSEDED = 0x02

# PONG ``lease_tail``: how the bytes after volume_wiper classify
LEASE_TAIL_ABSENT = "absent"        # no HBS2 marker (pre-v2 firmware when matched)
LEASE_TAIL_VALID = "valid"          # well-formed 32-byte HBS2 tail
LEASE_TAIL_MALFORMED = "malformed"  # HBS2 marker, wrong length/version/reserved/flags

# ── Audio formats (streaming) ───────────────────────────────────────
AUDIO_FORMAT_PCM16 = 0
AUDIO_FORMAT_IMA_ADPCM = 1


# ── Header ──────────────────────────────────────────────────────────
def build_header(
    command_type: int, seq: int, payload_length: int, version: int = VERSION
) -> bytes:
    """Build the 8-byte common header (little-endian).

    Layout: magic(u16) version(u8) command_type(u8) seq(u16) payload_length(u16)
    """
    return struct.pack(
        "<HBBHH", MAGIC, version, command_type, seq & 0xFFFF, payload_length
    )


def parse_header(data: bytes) -> Optional[dict]:
    """Parse the common header. Returns ``None`` on bad magic/version/length."""
    if len(data) < HEADER_SIZE:
        return None
    magic, version, cmd, seq, payload_len = struct.unpack(
        "<HBBHH", data[:HEADER_SIZE]
    )
    if magic != MAGIC or version != VERSION:
        return None
    return {"command_type": cmd, "seq": seq, "payload_length": payload_len}


def build_packet(command_type: int, seq: int, payload: bytes = b"") -> bytes:
    """Assemble a full command packet and enforce the 512-byte cap."""
    total = HEADER_SIZE + len(payload)
    if total > MAX_PACKET_SIZE:
        raise ValueError(
            f"packet size {total} exceeds maximum {MAX_PACKET_SIZE} bytes"
        )
    return build_header(command_type, seq, len(payload)) + payload


def parse_packet(data: bytes) -> Optional[tuple[int, int, bytes]]:
    """Parse a packet into ``(command_type, seq, payload)`` or ``None``."""
    hdr = parse_header(data)
    if hdr is None:
        return None
    end = HEADER_SIZE + hdr["payload_length"]
    if len(data) < end:
        return None
    return hdr["command_type"], hdr["seq"], data[HEADER_SIZE:end]


# ── Command builders ────────────────────────────────────────────────
def build_play(
    seq: int,
    event_id: str,
    *,
    target: str = "",
    target_time_us: int = 0,
    gain: float = 1.0,
    pan: float = 0.0,
) -> bytes:
    """PLAY (0x01).

    Wire payload (see contracts/specs/device-addressing.md §5.1):
        event_id(null-term) + target(null-term) + target_time(i64) + gain(f32)
        + pan(f32)

    ``target=""`` broadcasts to every device.

    ``pan`` is the left/right balance (-1 left, 0 centre, +1 right) applied by
    the device mixer as linear balance (contracts message-format.md §0x01,
    DEC-055). It is the *trailing optional* field of the payload — receivers
    that predate it treat its absence as 0.0 — but we always write it, and
    clamp to [-1, 1] here so an out-of-range value can never reach the wire.
    """
    pan = max(-1.0, min(1.0, float(pan)))
    payload = (
        event_id.encode("utf-8") + b"\x00"
        + target.encode("utf-8") + b"\x00"
        + struct.pack("<qff", int(target_time_us), float(gain), pan)
    )
    return build_packet(CMD_PLAY, seq, payload)


def build_stop(seq: int, event_id: str, *, target: str = "") -> bytes:
    """STOP (0x02). Payload: event_id(null-term) + target(null-term)."""
    payload = event_id.encode("utf-8") + b"\x00" + target.encode("utf-8") + b"\x00"
    return build_packet(CMD_STOP, seq, payload)


def build_stop_all(seq: int, *, target: str = "") -> bytes:
    """STOP_ALL (0x03). Payload: target(null-term)."""
    payload = target.encode("utf-8") + b"\x00"
    return build_packet(CMD_STOP_ALL, seq, payload)


def build_ping(
    seq: int, timestamp_us: int, client_incarnation: Optional[int] = None
) -> bytes:
    """PING (0x10). Payload: timestamp(i64, microseconds).

    With ``client_incarnation`` (nonzero u64) this is the 16-byte extended PING
    of stream-session-v2.md: v2 receivers answer with an HBS2 lease tail,
    pre-v2 firmware ignores the extra bytes and answers an ordinary PONG.
    """
    payload = struct.pack("<q", int(timestamp_us))
    if client_incarnation is not None:
        if not 0 < client_incarnation <= 0xFFFFFFFFFFFFFFFF:
            raise ValueError("client_incarnation must be a nonzero uint64")
        payload += struct.pack("<Q", client_incarnation)
    return build_packet(CMD_PING, seq, payload)


def build_connect_status(
    seq: int,
    *,
    connected: bool = True,
    group: int = 0,
    app_name: str = "",
    device_name: str = "",
) -> bytes:
    """CONNECT_STATUS (0x20). Periodic keep-alive shown on the device OLED.

    Wire payload matches the proven Unity layout
    (HapbeatProtocol.cs ``BuildConnectStatusPayload``), which is what the
    firmware actually parses:
        connected(u8) + group(u8) + app_name(null-term) + device_name(null-term)
    """
    payload = (
        bytes([1 if connected else 0, group & 0xFF])
        + app_name.encode("utf-8") + b"\x00"
        + device_name.encode("utf-8") + b"\x00"
    )
    return build_packet(CMD_CONNECT_STATUS, seq, payload)


# ── Streaming builders (clip-mode playback) ─────────────────────────
#
# v1 (``build_stream_*``) is for pre-v2 firmware only: header version 1, no
# envelope, END without payload (contracts message-format.md "v1 stream").
# v2 (``build_stream_*_v2``) starts every payload with the 16-byte
# device_boot_id / lease_ticket / generation envelope, header version 2.
def _stream_begin_body(
    sample_rate: int, channels: int, fmt: int, total_samples: int,
    gain: float, target: str,
) -> bytes:
    body = struct.pack(
        "<HBBIf", sample_rate, channels, fmt, total_samples, float(gain)
    )
    if target:
        body += target.encode("utf-8") + b"\x00"
    return body


def _build_stream_packet(
    command_type: int, seq: int, payload: bytes, version: int
) -> bytes:
    """Stream packets use the larger MTU-safe size cap."""
    total = HEADER_SIZE + len(payload)
    if total > MAX_STREAM_PACKET_SIZE:
        raise ValueError(
            f"stream packet {total} exceeds MTU cap {MAX_STREAM_PACKET_SIZE}"
        )
    return build_header(command_type, seq, len(payload), version) + payload


def build_stream_begin(
    seq: int,
    *,
    sample_rate: int = 16000,
    channels: int = 1,
    fmt: int = AUDIO_FORMAT_PCM16,
    total_samples: int = 0,
    gain: float = 1.0,
    target: str = "",
) -> bytes:
    """STREAM_BEGIN (0x30), v1 (pre-v2 firmware).

    Default format is PCM16 — clip streaming sends uncompressed 16-bit PCM
    (ADPCM is reserved but not produced by the SDK).
    """
    payload = _stream_begin_body(
        sample_rate, channels, fmt, total_samples, gain, target
    )
    return build_packet(CMD_STREAM_BEGIN, seq, payload)


def build_stream_data(seq: int, offset: int, data: bytes) -> bytes:
    """STREAM_DATA (0x31), v1. Uses the larger MTU-safe size cap."""
    payload = struct.pack("<I", offset) + data
    return _build_stream_packet(CMD_STREAM_DATA, seq, payload, VERSION)


def build_stream_end(seq: int) -> bytes:
    """STREAM_END (0x32), v1. No payload."""
    return build_packet(CMD_STREAM_END, seq, b"")


def build_stream_envelope(boot_id: int, lease_ticket: int, generation: int) -> bytes:
    """The 16-byte v2 stream envelope. Every field must be nonzero."""
    if not 0 < boot_id <= 0xFFFFFFFFFFFFFFFF:
        raise ValueError("device_boot_id must be a nonzero uint64")
    if not 0 < lease_ticket <= 0xFFFFFFFF:
        raise ValueError("lease_ticket must be a nonzero uint32")
    if not 0 < generation <= 0xFFFFFFFF:
        raise ValueError("generation must be a nonzero uint32")
    return struct.pack("<QII", boot_id, lease_ticket, generation)


def build_stream_begin_v2(
    seq: int,
    boot_id: int,
    lease_ticket: int,
    generation: int,
    *,
    sample_rate: int = 16000,
    channels: int = 1,
    fmt: int = AUDIO_FORMAT_PCM16,
    total_samples: int = 0,
    gain: float = 1.0,
    target: str = "",
) -> bytes:
    """STREAM_BEGIN (0x30), v2: envelope + the v1 BEGIN body."""
    payload = build_stream_envelope(boot_id, lease_ticket, generation)
    payload += _stream_begin_body(
        sample_rate, channels, fmt, total_samples, gain, target
    )
    return _build_stream_packet(CMD_STREAM_BEGIN, seq, payload, STREAM_VERSION)


def build_stream_data_v2(
    seq: int, boot_id: int, lease_ticket: int, generation: int,
    offset: int, data: bytes,
) -> bytes:
    """STREAM_DATA (0x31), v2: envelope + byte offset(u32) + audio."""
    payload = build_stream_envelope(boot_id, lease_ticket, generation)
    payload += struct.pack("<I", offset & 0xFFFFFFFF) + data
    return _build_stream_packet(CMD_STREAM_DATA, seq, payload, STREAM_VERSION)


def build_stream_end_v2(
    seq: int, boot_id: int, lease_ticket: int, generation: int
) -> bytes:
    """STREAM_END (0x32), v2: exactly the envelope, no body."""
    payload = build_stream_envelope(boot_id, lease_ticket, generation)
    return _build_stream_packet(CMD_STREAM_END, seq, payload, STREAM_VERSION)


# ── Device addressing ───────────────────────────────────────────────
def address_matches(target: Optional[str], device_address: Optional[str]) -> bool:
    """Does ``device_address`` accept a packet aimed at ``target``?

    A port of the firmware's ``addressMatch()``
    (hapbeat-device-firmware/src/address_match.cpp), which is the authority:
    the device re-applies this filter on receipt, so the SDK only uses it to
    pick unicast destinations, never to decide what plays.

    Rules (contracts/specs/device-addressing.md §4.3):

    - an empty target matches every address;
    - both strings are split on ``/`` and compared **positionally**, so
      ``group_2`` alone is compared against the *player* slot and never
      matches — write ``*/*/group_2``;
    - a ``*`` target segment matches any single address segment;
    - fewer target segments than address segments is a front-match (extra
      address segments do not cause a mismatch);
    - more target segments than address segments is a mismatch;
    - a single trailing ``/`` is ignored (``"player_1/" == "player_1"``),
      because firmware's pointer walk exits on the terminator rather than
      producing an empty segment. Splitting naively here would drop a device
      the firmware *would* have accepted.
    """
    if not target:
        return True

    target_segments = target.split("/")
    address_segments = (device_address or "").split("/")

    # Only the last empty segment is dropped, and only once: "a//" really does
    # compare an empty segment in firmware, and still mismatches.
    count = len(target_segments)
    if count > 1 and target_segments[count - 1] == "":
        count -= 1

    for i in range(count):
        if i >= len(address_segments):
            return False  # target longer than address = mismatch
        if target_segments[i] != "*" and target_segments[i] != address_segments[i]:
            return False

    return True  # front-match or exact match


# ── Response parsers ────────────────────────────────────────────────
def _read_cstr(buf: bytes) -> tuple[str, bytes]:
    idx = buf.find(b"\x00")
    if idx < 0:
        return buf.decode("utf-8", errors="replace"), b""
    return buf[:idx].decode("utf-8", errors="replace"), buf[idx + 1:]


def _classify_lease_tail(tail: bytes, out: dict) -> None:
    """Classify the bytes after volume_wiper (stream-session-v2.md "Legacy
    receiver fallback"): no HBS2 marker -> absent; a marker (or a truncated
    prefix of one) with a wrong length / version / reserved / flags ->
    malformed; otherwise the valid 32-byte lease tail."""
    head = tail[:4]
    if not head or head != STREAM_LEASE_TAIL_MARKER[: len(head)]:
        out["lease_tail"] = LEASE_TAIL_ABSENT
        return
    known_flags = STREAM_LEASE_FLAG_VALID | STREAM_LEASE_FLAG_SUPERSEDED
    if (
        len(tail) != STREAM_LEASE_TAIL_SIZE
        or tail[4] != STREAM_LEASE_TAIL_VERSION
        or tail[5] & ~known_flags
        or struct.unpack_from("<H", tail, 6)[0] != 0
    ):
        out["lease_tail"] = LEASE_TAIL_MALFORMED
        return
    flags = tail[5]
    incarnation, boot_id, ticket, high_water = struct.unpack_from("<QQII", tail, 8)
    out["lease_tail"] = LEASE_TAIL_VALID
    out["echoed_client_incarnation"] = incarnation
    out["device_boot_id"] = boot_id
    out["lease_ticket"] = ticket
    out["high_water_ticket"] = high_water
    out["lease_valid"] = bool(flags & STREAM_LEASE_FLAG_VALID) and boot_id != 0 and ticket != 0
    out["lease_superseded"] = bool(flags & STREAM_LEASE_FLAG_SUPERSEDED)


def parse_pong(data: bytes) -> Optional[dict]:
    """Parse a PONG (0x11).

    Bridge form is 16 bytes (timestamp, server_time). Devices append an
    extended form (name / address / firmware_version, then volume_level and
    volume_wiper). A v2 receiver's direct reply to an extended PING ends with
    the 32-byte HBS2 stream lease tail; ``lease_tail`` says how the bytes
    after volume_wiper classify (``LEASE_TAIL_*``). Returns ``None`` if not a
    valid PONG.
    """
    hdr = parse_header(data)
    if hdr is None or hdr["command_type"] != CMD_PONG:
        return None
    payload = data[HEADER_SIZE:]
    if len(payload) < 16:
        return None

    timestamp, server_time = struct.unpack("<qq", payload[:16])
    out: dict = {
        "seq": hdr["seq"],
        "timestamp": timestamp,
        "server_time": server_time,
    }
    rest = payload[16:]
    if rest:
        out["device_name"], rest = _read_cstr(rest)
    if rest:
        out["address"], rest = _read_cstr(rest)
    if rest:
        out["firmware_version"], rest = _read_cstr(rest)
    if len(rest) >= 1:
        out["volume_level"] = rest[0]
    if len(rest) >= 2:
        out["volume_wiper"] = rest[1]
    _classify_lease_tail(rest[2:], out)
    return out


def parse_error(data: bytes) -> Optional[dict]:
    """Parse an ERROR (0xFF). Payload: error_code(u16) + message(null-term)."""
    parsed = parse_packet(data)
    if parsed is None or parsed[0] != CMD_ERROR:
        return None
    payload = parsed[2]
    if len(payload) < 2:
        return None
    (code,) = struct.unpack("<H", payload[:2])
    message, _ = _read_cstr(payload[2:])
    return {"error_code": code, "message": message}
