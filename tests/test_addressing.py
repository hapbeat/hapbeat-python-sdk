"""Device addressing: target/address matching + unicast destination routing.

``address_matches`` decides which devices a packet is unicast to, so it must
agree with the firmware's ``addressMatch()`` — a mismatch here means either a
device is skipped that should have played (silent loss) or one is written to
that ignores the packet anyway (harmless, just waste). The table below is
transcribed from hapbeat-contracts/specs/device-addressing.md §4.2.
"""

from __future__ import annotations

import time

import pytest

from hapbeat import protocol
from hapbeat import hapbeat as hapbeat_mod

from .test_hapbeat import fake_client  # noqa: F401 (pytest fixture)


# (target, device address, expected) — contracts device-addressing.md §4.2
MATCH_TABLE = [
    ("", "player_1/pos_neck", True),                       # empty = all devices
    ("player_1", "player_1/pos_neck", True),               # front match
    ("player_1", "player_2/pos_neck", False),              # player mismatch
    ("player_1/pos_neck", "player_1/pos_neck", True),      # exact
    ("player_1/pos_neck", "player_1/pos_r_wrist", False),  # position mismatch
    ("*/pos_neck", "player_1/pos_neck", True),             # wildcard
    ("*/pos_neck", "player_2/pos_neck", True),
    # Firmware only treats a segment that is entirely "*" as a wildcard
    # (address_match.cpp: `t_len == 1 && *tp == '*'`), so "pos_*" is a literal.
    ("player_1/pos_*", "player_1/pos_neck", False),
    ("player_1/*", "player_1/pos_neck", True),             # whole-segment wildcard
    ("red", "red/player_1/pos_neck", True),                # prefixed address
    ("red/*/player_1", "red/alpha/player_1/pos_neck", True),
    ("player_1/pos_neck/group_1", "player_1/pos_neck/group_1", True),
    ("player_1/pos_neck/group_1", "player_1/pos_neck/group_2", False),
    ("player_1/pos_neck", "player_1/pos_neck/group_1", True),  # no group = any group
    ("*/*/group_1", "player_2/pos_chest/group_1", True),   # group only
    ("player_1/pos_neck/group_1", "player_1/pos_neck", False),  # target longer
]


@pytest.mark.parametrize("target,address,expected", MATCH_TABLE)
def test_address_matches_spec_table(target, address, expected):
    assert protocol.address_matches(target, address) is expected


def test_group_alone_never_matches():
    """The trap the positional rule sets: "group_2" lands in the player slot."""
    assert protocol.address_matches("group_2", "player_1/pos_neck/group_2") is False
    assert protocol.address_matches("*/*/group_2", "player_1/pos_neck/group_2") is True


def test_trailing_slash_matches_firmware():
    """Firmware's pointer walk ends at the terminator, so one trailing '/' is
    not an empty segment. Splitting naively would drop a device the firmware
    would have accepted."""
    assert protocol.address_matches("player_1/", "player_1/pos_neck") is True
    assert protocol.address_matches("player_1//", "player_1/pos_neck") is False


# ── Unicast routing ────────────────────────────────────────────────
def _seen(hb, ip: str, address: str = "player_1/pos_neck") -> None:
    hb._on_pong({"address": address}, ip)


def test_broadcasts_until_a_device_is_known(fake_client):  # noqa: F811
    hb = hapbeat_mod.Hapbeat(keepalive=False).open()
    hb._client.sent.clear()
    hb.play("kit.evt")
    assert [addr for _, addr in hb._client.sent] == [None]  # None = broadcast


def test_unicasts_to_known_devices(fake_client):  # noqa: F811
    hb = hapbeat_mod.Hapbeat(keepalive=False).open()
    _seen(hb, "192.168.1.10")
    _seen(hb, "192.168.1.11", "player_2/pos_neck")
    hb._client.sent.clear()
    hb.play("kit.evt")
    assert sorted(addr for _, addr in hb._client.sent) == ["192.168.1.10", "192.168.1.11"]


def test_no_double_delivery(fake_client):  # noqa: F811
    """Never unicast *and* broadcast: firmware older than the (src ip, seq)
    dedupe would fire the same PLAY twice."""
    hb = hapbeat_mod.Hapbeat(keepalive=False).open()
    _seen(hb, "192.168.1.10")
    hb._client.sent.clear()
    hb.play("kit.evt")
    assert len(hb._client.sent) == 1
    assert hb._client.sent[0][1] == "192.168.1.10"


def test_target_filters_unicast_destinations(fake_client):  # noqa: F811
    hb = hapbeat_mod.Hapbeat(keepalive=False).open()
    _seen(hb, "192.168.1.10", "player_1/pos_neck/group_1")
    _seen(hb, "192.168.1.11", "player_2/pos_neck/group_1")
    hb._client.sent.clear()
    hb.play("kit.evt", target="player_1")
    assert [addr for _, addr in hb._client.sent] == ["192.168.1.10"]


def test_unknown_address_fails_open(fake_client):  # noqa: F811
    """A device that never reported an address stays a destination — the
    device applies the real filter, and dropping it would lose the command."""
    hb = hapbeat_mod.Hapbeat(keepalive=False).open()
    hb._on_pong({}, "192.168.1.10")  # PONG without the address extension
    hb._client.sent.clear()
    hb.play("kit.evt", target="player_9")
    assert [addr for _, addr in hb._client.sent] == ["192.168.1.10"]


def test_all_mismatched_falls_back_to_broadcast(fake_client):  # noqa: F811
    """Not a skip: a stale cached address must not swallow a STOP, or a
    looping clip would never stop."""
    hb = hapbeat_mod.Hapbeat(keepalive=False).open()
    _seen(hb, "192.168.1.10", "player_1/pos_neck/group_1")
    hb._client.sent.clear()
    hb.stop("kit.evt", target="player_7")
    assert [addr for _, addr in hb._client.sent] == [None]


def test_expired_device_is_dropped(fake_client):  # noqa: F811
    hb = hapbeat_mod.Hapbeat(keepalive=False, device_ttl=5.0).open()
    _seen(hb, "192.168.1.10")
    hb._devices["192.168.1.10"].last_seen = time.monotonic() - 10.0
    hb._client.sent.clear()
    hb.stop_all()
    assert [addr for _, addr in hb._client.sent] == [None]  # back to broadcast


def test_unicast_disabled_always_broadcasts(fake_client):  # noqa: F811
    hb = hapbeat_mod.Hapbeat(keepalive=False, unicast=False).open()
    _seen(hb, "192.168.1.10")
    hb._client.sent.clear()
    hb.play("kit.evt")
    assert [addr for _, addr in hb._client.sent] == [None]


def test_ping_and_connect_status_stay_broadcast(fake_client):  # noqa: F811
    """Discovery must reach devices we have not heard from yet."""
    hb = hapbeat_mod.Hapbeat(keepalive=False, app_name="T").open()
    _seen(hb, "192.168.1.10")
    hb._client.sent.clear()
    hb.ping()
    hb.connect_status(connected=True)
    assert [addr for _, addr in hb._client.sent] == [None, None]


# ── Stream sessions ────────────────────────────────────────────────
def test_stream_snapshot_is_reused_for_data_and_end(fake_client):  # noqa: F811
    hb = hapbeat_mod.Hapbeat(keepalive=False).open()
    _seen(hb, "192.168.1.10")
    hb._client.sent.clear()
    hb.stream_begin(sample_rate=16000, channels=1, total_samples=100,
                    gain=1.0, target="")
    hb.stream_data(0, b"\x00\x00")
    hb.stream_end()
    assert [addr for _, addr in hb._client.sent] == ["192.168.1.10"] * 3


def test_stream_skips_when_target_matches_nobody(fake_client):  # noqa: F811
    """Unlike a command, a filtered-out stream is dropped rather than
    broadcast — hundreds of packets of airtime for devices that would ignore
    them, and a lost stream leaves nothing stuck."""
    hb = hapbeat_mod.Hapbeat(keepalive=False).open()
    _seen(hb, "192.168.1.10", "player_1/pos_neck/group_1")
    hb._client.sent.clear()
    hb.stream_begin(sample_rate=16000, channels=1, total_samples=100,
                    gain=1.0, target="player_5")
    hb.stream_data(0, b"\x00\x00")
    assert hb._client.sent == []


def test_stream_broadcasts_when_no_device_is_known(fake_client):  # noqa: F811
    hb = hapbeat_mod.Hapbeat(keepalive=False).open()
    hb._client.sent.clear()
    hb.stream_begin(sample_rate=16000, channels=1, total_samples=100,
                    gain=1.0, target="")
    hb.stream_data(0, b"\x00\x00")
    assert [addr for _, addr in hb._client.sent] == [None, None]
