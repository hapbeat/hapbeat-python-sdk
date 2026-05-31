"""Contract round-trip tests for the Layer 1 wire format.

These pin the byte layout to hapbeat-contracts/specs/message-format.md so a
refactor can't silently drift from the wire spec the firmware expects.
"""

import struct

import pytest

from hapbeat import protocol


def test_header_round_trip():
    hdr = protocol.build_header(protocol.CMD_PLAY, 42, 100)
    assert protocol.parse_header(hdr) == {
        "command_type": protocol.CMD_PLAY,
        "seq": 42,
        "payload_length": 100,
    }


def test_header_magic_and_size():
    hdr = protocol.build_header(protocol.CMD_PING, 1, 0)
    assert len(hdr) == protocol.HEADER_SIZE
    assert struct.unpack("<H", hdr[:2])[0] == protocol.MAGIC  # 0x4842 "HB"
    assert hdr[2] == protocol.VERSION


def test_play_layout():
    pkt = protocol.build_play(7, "explosion", target="player_1/chest", gain=0.5)
    cmd, seq, payload = protocol.parse_packet(pkt)
    assert cmd == protocol.CMD_PLAY and seq == 7
    prefix = b"explosion\x00player_1/chest\x00"
    assert payload.startswith(prefix)
    target_time, gain = struct.unpack("<qf", payload[len(prefix):])
    assert target_time == 0
    assert abs(gain - 0.5) < 1e-6


def test_play_broadcast_empty_target():
    pkt = protocol.build_play(1, "boom")
    _, _, payload = protocol.parse_packet(pkt)
    assert payload.startswith(b"boom\x00\x00")  # event_id\0 then empty target\0


def test_stop_layout():
    _, _, payload = protocol.parse_packet(protocol.build_stop(3, "boom", target="p1"))
    assert payload == b"boom\x00p1\x00"


def test_stop_all_layout():
    _, _, payload = protocol.parse_packet(protocol.build_stop_all(4, target="*/chest"))
    assert payload == b"*/chest\x00"


def test_ping_layout():
    _, _, payload = protocol.parse_packet(protocol.build_ping(5, 123456789))
    assert struct.unpack("<q", payload)[0] == 123456789


def test_connect_status_layout():
    # Must match HapbeatProtocol.cs: connected(u8) group(u8) appName\0 deviceName\0
    pkt = protocol.build_connect_status(
        9, connected=True, group=3, app_name="MyApp", device_name="host"
    )
    cmd, _, payload = protocol.parse_packet(pkt)
    assert cmd == protocol.CMD_CONNECT_STATUS
    assert payload == bytes([1, 3]) + b"MyApp\x00host\x00"


def test_packet_size_cap():
    with pytest.raises(ValueError):
        protocol.build_play(1, "x" * 600)  # blows past the 512-byte command cap


def test_parse_pong_extended():
    payload = struct.pack("<qq", 12345, 67890)
    payload += b"hapbeat-test\x00player_1/chest\x00v1.2.3\x00"
    payload += bytes([100, 50, 7])
    pkt = protocol.build_header(protocol.CMD_PONG, 1, len(payload)) + payload
    out = protocol.parse_pong(pkt)
    assert out["timestamp"] == 12345
    assert out["server_time"] == 67890
    assert out["device_name"] == "hapbeat-test"
    assert out["address"] == "player_1/chest"
    assert out["firmware_version"] == "v1.2.3"
    assert out["volume_level"] == 100


def test_parse_pong_rejects_wrong_magic():
    bad = b"\x00\x00" + bytes([protocol.VERSION, protocol.CMD_PONG]) + b"\x00" * 20
    assert protocol.parse_pong(bad) is None


def test_parse_error():
    payload = struct.pack("<H", 7) + b"too old\x00"
    pkt = protocol.build_header(protocol.CMD_ERROR, 1, len(payload)) + payload
    out = protocol.parse_error(pkt)
    assert out == {"error_code": 7, "message": "too old"}
