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
# STREAM_BEGIN/DATA/END to Stream Gen 2 receivers carry header version 2
# (contracts stream-session.md); every other command stays version 1.
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
CMD_CAPS_REQ = 0x12
CMD_CONNECT_STATUS = 0x20
CMD_STREAM_BEGIN = 0x30
CMD_STREAM_DATA = 0x31
CMD_STREAM_END = 0x32

# ── Response types (device -> SDK) ──────────────────────────────────
CMD_PONG = 0x11
CMD_CAPS = 0x13
CMD_ERROR = 0xFF

# The SDK's major version. Keep it equal to the major of ``version`` in
# pyproject.toml: a device whose CAPS ``generation`` is greater gets one
# "update the SDK" warning (contracts versioning.md §3.3).
SDK_MAJOR = 1

# ── Stream Gen 2 (contracts stream-session.md) ──────────────────────
STREAM_ENVELOPE_SIZE = 16  # device_boot_id(u64) lease_ticket(u32) generation(u32)
STREAM_LEASE_TAIL_MARKER = b"HBS2"
STREAM_LEASE_TAIL_SIZE = 32
STREAM_LEASE_TAIL_VERSION = 1
STREAM_LEASE_FLAG_VALID = 0x01
STREAM_LEASE_FLAG_SUPERSEDED = 0x02
STREAM_LEASE_FLAG_REISSUED = 0x04

# PONG ``lease_tail``: how the bytes after volume_wiper classify
LEASE_TAIL_ABSENT = "absent"    # nothing after volume_wiper (Stream Gen 1 when matched)
LEASE_TAIL_VALID = "valid"      # >= 32 bytes, HBS2, extension_version 1 (Stream Gen 2)
LEASE_TAIL_IGNORED = "ignored"  # anything else: no class or lease change

# ── CAPS (contracts message-format.md §0x12 / §0x13) ────────────────
CAPS_VERSION = 1
_CAPS_FORMATS = ("pcm16", "adpcm", "opus")
_CAPS_FEATURES = ("play_pan", "error_reply")
# known TLV types and the value length each needs (0x02 is a string)
_CAPS_TYPE_SIZES = {0x01: 1, 0x03: 1, 0x04: 1, 0x05: 4, 0x06: 1, 0x07: 1, 0x08: 4}

# ── ERROR codes (contracts message-format.md §0xFF) ─────────────────
ERR_UNSUPPORTED_VERSION = 0x0001
ERR_UNKNOWN_COMMAND = 0x0002
ERR_MALFORMED = 0x0003
ERR_STREAM_GEN_UNSUPPORTED = 0x0010
ERR_STREAM_LEASE_INVALID = 0x0011
ERR_STREAM_SUPERSEDED = 0x0012
ERR_STREAM_NOT_STARTED = 0x0013
ERR_STREAM_TIMED_OUT = 0x0014
ERR_STREAM_STOPPED = 0x0015
ERR_STREAM_ENDED = 0x0016
ERR_STREAM_TARGET_MISMATCH = 0x0017
# codes whose ``detail`` is the rejected packet's 16-byte envelope
STREAM_ERROR_CODES = range(ERR_STREAM_LEASE_INVALID, ERR_STREAM_TARGET_MISMATCH + 1)

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

    The 8-byte PING is discovery only. With ``client_incarnation`` (nonzero
    u64) this is the 16-byte extended PING of stream-session.md, which
    requests a stream lease and is sent by unicast only: Stream Gen 2
    receivers answer with an HBS2 lease tail, older firmware ignores the extra
    bytes and answers an ordinary PONG.
    """
    payload = struct.pack("<q", int(timestamp_us))
    if client_incarnation is not None:
        if not 0 < client_incarnation <= 0xFFFFFFFFFFFFFFFF:
            raise ValueError("client_incarnation must be a nonzero uint64")
        payload += struct.pack("<Q", client_incarnation)
    return build_packet(CMD_PING, seq, payload)


def build_caps_req(seq: int) -> bytes:
    """CAPS_REQ (0x12). Payload: caps_version(u8). Exact unicast only."""
    return build_packet(CMD_CAPS_REQ, seq, bytes([CAPS_VERSION]))


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
# Stream Gen 1 (``build_stream_*``) is for firmware before Stream Gen 2 only:
# header version 1, no envelope, END without payload (contracts
# message-format.md "Stream Gen 1"). Stream Gen 2 (``build_stream_*_v2``)
# starts every payload with the 16-byte device_boot_id / lease_ticket /
# generation envelope, header version 2.
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
    """STREAM_BEGIN (0x30), Stream Gen 1 (firmware before Stream Gen 2).

    Default format is PCM16 — clip streaming sends uncompressed 16-bit PCM
    (ADPCM is reserved but not produced by the SDK).
    """
    payload = _stream_begin_body(
        sample_rate, channels, fmt, total_samples, gain, target
    )
    return build_packet(CMD_STREAM_BEGIN, seq, payload)


def build_stream_data(seq: int, offset: int, data: bytes) -> bytes:
    """STREAM_DATA (0x31), Stream Gen 1. Uses the larger MTU-safe size cap."""
    payload = struct.pack("<I", offset) + data
    return _build_stream_packet(CMD_STREAM_DATA, seq, payload, VERSION)


def build_stream_end(seq: int) -> bytes:
    """STREAM_END (0x32), Stream Gen 1. No payload."""
    return build_packet(CMD_STREAM_END, seq, b"")


def build_stream_envelope(boot_id: int, lease_ticket: int, generation: int) -> bytes:
    """The 16-byte Stream Gen 2 envelope. Every field must be nonzero."""
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
    """STREAM_BEGIN (0x30), Stream Gen 2: envelope + the Stream Gen 1 BEGIN body."""
    payload = build_stream_envelope(boot_id, lease_ticket, generation)
    payload += _stream_begin_body(
        sample_rate, channels, fmt, total_samples, gain, target
    )
    return _build_stream_packet(CMD_STREAM_BEGIN, seq, payload, STREAM_VERSION)


def build_stream_data_v2(
    seq: int, boot_id: int, lease_ticket: int, generation: int,
    offset: int, data: bytes,
) -> bytes:
    """STREAM_DATA (0x31), Stream Gen 2: envelope + byte offset(u32) + audio."""
    payload = build_stream_envelope(boot_id, lease_ticket, generation)
    payload += struct.pack("<I", offset & 0xFFFFFFFF) + data
    return _build_stream_packet(CMD_STREAM_DATA, seq, payload, STREAM_VERSION)


def build_stream_end_v2(
    seq: int, boot_id: int, lease_ticket: int, generation: int
) -> bytes:
    """STREAM_END (0x32), Stream Gen 2: exactly the envelope, no body."""
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
    """Classify the bytes after volume_wiper (stream-session.md "Receiver
    classification", 1.x rules): none -> absent (Stream Gen 1); at least 32
    bytes starting with HBS2 and extension_version 1 -> valid (Stream Gen 2,
    only the first 32 bytes are read; the reserved field and flag bits other
    than 0-2 are not checked); anything else -> ignored."""
    if not tail:
        out["lease_tail"] = LEASE_TAIL_ABSENT
        return
    if (
        len(tail) < STREAM_LEASE_TAIL_SIZE
        or tail[:4] != STREAM_LEASE_TAIL_MARKER
        or tail[4] != STREAM_LEASE_TAIL_VERSION
    ):
        out["lease_tail"] = LEASE_TAIL_IGNORED
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
    out["lease_reissued"] = bool(flags & STREAM_LEASE_FLAG_REISSUED)


def parse_pong(data: bytes) -> Optional[dict]:
    """Parse a PONG (0x11).

    Bridge form is 16 bytes (timestamp, server_time). Devices append an
    extended form (name / address / firmware_version, then volume_level and
    volume_wiper). A Stream Gen 2 receiver's direct reply to an extended PING
    ends with the 32-byte HBS2 stream lease tail; ``lease_tail`` says how the
    bytes after volume_wiper classify (``LEASE_TAIL_*``). Only the
    ``payload_length`` bytes after the header are the payload; anything after
    them in the datagram is ignored. Returns ``None`` if not a valid PONG.
    """
    parsed = parse_packet(data)
    if parsed is None or parsed[0] != CMD_PONG:
        return None
    seq, payload = parsed[1], parsed[2]
    if len(payload) < 16:
        return None

    timestamp, server_time = struct.unpack("<qq", payload[:16])
    out: dict = {
        "seq": seq,
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


def _bit_names(value: int, names: tuple[str, ...]) -> list[str]:
    return [name for index, name in enumerate(names) if value & (1 << index)]


def parse_caps_payload(payload: bytes) -> dict:
    """Read a CAPS payload (message-format.md §0x13).

    Unknown types are skipped by their length; a known type longer than
    defined is read from its start, a shorter one is ignored; of repeated
    types the first long enough one wins; a truncated entry is ignored. Only
    the fields present are returned (``caps_version``, ``generation``,
    ``firmware_version``, ``stream_gens``, ``stream_formats``,
    ``lease_ttl_s``, ``lease_slots``, ``inactivity_timeout_s``,
    ``haptic_channels``, ``address_max_len``, ``features``).
    """
    caps: dict = {"caps_version": payload[0]} if payload else {}
    offset, seen = 2, set()
    while offset + 2 <= len(payload):
        kind, size = payload[offset], payload[offset + 1]
        value = payload[offset + 2:offset + 2 + size]
        offset += 2 + size
        if len(value) < size or kind in seen:
            continue
        if kind == 0x02:
            seen.add(kind)
            caps["firmware_version"] = value.decode("utf-8", errors="replace")
            continue
        needed = _CAPS_TYPE_SIZES.get(kind)
        if needed is None or len(value) < needed:
            continue
        seen.add(kind)
        if kind == 0x01:
            caps["generation"] = value[0]
        elif kind == 0x03:
            caps["stream_gens"] = [gen for gen in (1, 2) if value[0] & (1 << (gen - 1))]
        elif kind == 0x04:
            caps["stream_formats"] = _bit_names(value[0], _CAPS_FORMATS)
        elif kind == 0x05:
            caps["lease_ttl_s"], caps["lease_slots"], caps["inactivity_timeout_s"] = value[:3]
        elif kind == 0x06:
            caps["haptic_channels"] = value[0]
        elif kind == 0x07:
            caps["address_max_len"] = value[0]
        elif kind == 0x08:
            caps["features"] = _bit_names(struct.unpack_from("<I", value)[0], _CAPS_FEATURES)
    return caps


def parse_caps(data: bytes) -> Optional[dict]:
    """Parse a CAPS (0x13) datagram, see :func:`parse_caps_payload`."""
    parsed = parse_packet(data)
    if parsed is None or parsed[0] != CMD_CAPS or not parsed[2]:
        return None
    return parse_caps_payload(parsed[2])


def parse_error_payload(payload: bytes) -> Optional[dict]:
    """Read an ERROR payload (message-format.md §0xFF): error_code(u16) +
    message(null-term), then, when at least 4 more bytes follow,
    ref_command(u8) + ref_seq(u16) + detail_length(u8) + detail. A 0.x-form
    ERROR has only ``error_code`` and ``message``."""
    if len(payload) < 2:
        return None
    (code,) = struct.unpack_from("<H", payload)
    end = payload.find(b"\x00", 2)
    if end < 0:
        return {"error_code": code, "message": payload[2:].decode("utf-8", errors="replace")}
    out: dict = {"error_code": code,
                 "message": payload[2:end].decode("utf-8", errors="replace")}
    offset = end + 1
    if len(payload) >= offset + 4:
        ref_command, ref_seq, detail_length = struct.unpack_from("<BHB", payload, offset)
        out["ref_command"] = ref_command
        out["ref_seq"] = ref_seq
        out["detail"] = payload[offset + 4:offset + 4 + detail_length]
    return out


def parse_error(data: bytes) -> Optional[dict]:
    """Parse an ERROR (0xFF) datagram, see :func:`parse_error_payload`."""
    parsed = parse_packet(data)
    if parsed is None or parsed[0] != CMD_ERROR:
        return None
    return parse_error_payload(parsed[2])
