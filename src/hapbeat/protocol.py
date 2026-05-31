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

# ── Audio formats (streaming) ───────────────────────────────────────
AUDIO_FORMAT_PCM16 = 0
AUDIO_FORMAT_IMA_ADPCM = 1


# ── Header ──────────────────────────────────────────────────────────
def build_header(command_type: int, seq: int, payload_length: int) -> bytes:
    """Build the 8-byte common header (little-endian).

    Layout: magic(u16) version(u8) command_type(u8) seq(u16) payload_length(u16)
    """
    return struct.pack(
        "<HBBHH", MAGIC, VERSION, command_type, seq & 0xFFFF, payload_length
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
) -> bytes:
    """PLAY (0x01).

    Wire payload (see contracts/specs/device-addressing.md §5.1):
        event_id(null-term) + target(null-term) + target_time(i64) + gain(f32)

    ``target=""`` broadcasts to every device.
    """
    payload = (
        event_id.encode("utf-8") + b"\x00"
        + target.encode("utf-8") + b"\x00"
        + struct.pack("<qf", int(target_time_us), float(gain))
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


def build_ping(seq: int, timestamp_us: int) -> bytes:
    """PING (0x10). Payload: timestamp(i64, microseconds)."""
    return build_packet(CMD_PING, seq, struct.pack("<q", int(timestamp_us)))


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


# ── Streaming builders (here for L2; not needed for level-1 fire) ────
def build_stream_begin(
    seq: int,
    *,
    sample_rate: int = 16000,
    channels: int = 1,
    fmt: int = AUDIO_FORMAT_IMA_ADPCM,
    total_samples: int = 0,
    gain: float = 1.0,
    target: str = "",
) -> bytes:
    """STREAM_BEGIN (0x30)."""
    payload = struct.pack(
        "<HBBIf", sample_rate, channels, fmt, total_samples, float(gain)
    )
    if target:
        payload += target.encode("utf-8") + b"\x00"
    return build_packet(CMD_STREAM_BEGIN, seq, payload)


def build_stream_data(seq: int, offset: int, data: bytes) -> bytes:
    """STREAM_DATA (0x31). Uses the larger MTU-safe size cap."""
    payload = struct.pack("<I", offset) + data
    total = HEADER_SIZE + len(payload)
    if total > MAX_STREAM_PACKET_SIZE:
        raise ValueError(
            f"stream packet {total} exceeds MTU cap {MAX_STREAM_PACKET_SIZE}"
        )
    return build_header(CMD_STREAM_DATA, seq, len(payload)) + payload


def build_stream_end(seq: int) -> bytes:
    """STREAM_END (0x32). No payload."""
    return build_packet(CMD_STREAM_END, seq, b"")


# ── Response parsers ────────────────────────────────────────────────
def _read_cstr(buf: bytes) -> tuple[str, bytes]:
    idx = buf.find(b"\x00")
    if idx < 0:
        return buf.decode("utf-8", errors="replace"), b""
    return buf[:idx].decode("utf-8", errors="replace"), buf[idx + 1:]


def parse_pong(data: bytes) -> Optional[dict]:
    """Parse a PONG (0x11).

    Bridge form is 16 bytes (timestamp, server_time). Devices append an
    extended form (name / address / firmware_version, plus optional
    trailing volume bytes). Returns ``None`` if not a valid PONG.
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
    if len(rest) >= 3:
        out["volume_steps"] = rest[2]
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
