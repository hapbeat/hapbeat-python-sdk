"""Subnet-directed broadcast: route maths, fan-out, and the discovery split.

Background: ``255.255.255.255`` leaves a multi-homed host through the single
interface with the lowest metric, which on a machine with Hyper-V / WSL2 /
Docker is often a virtual switch with no Hapbeat behind it. Discovery therefore
fans out across every local subnet, and playback is pinned to the one a device
answered on.

The rule these tests exist to protect: **only PING and CONNECT_STATUS may take
the fan-out.** Firmware older than v0.3.0 does not de-duplicate PLAY/STOP by
sequence number, so a device reachable on two destinations would fire twice.
"""

from __future__ import annotations

import pytest

from hapbeat import client as client_mod
from hapbeat import hapbeat as hapbeat_mod
from hapbeat import protocol
from hapbeat.client import (
    DEFAULT_BROADCAST,
    BroadcastRoute,
    UdpClient,
    enumerate_broadcast_routes,
)

from .test_hapbeat import fake_client  # noqa: F401 (pytest fixture)


# ── Route maths ─────────────────────────────────────────────────────
# (local ip, prefix, expected broadcast). A /24 is NOT the only case: assuming
# ".255" is exactly the bug this replaces.
@pytest.mark.parametrize("ip,prefix,expected", [
    ("192.168.0.205", 24, "192.168.0.255"),
    ("192.168.11.7", 24, "192.168.11.255"),
    ("10.1.2.3", 8, "10.255.255.255"),
    ("172.17.192.1", 16, "172.17.255.255"),
    ("192.168.1.10", 25, "192.168.1.127"),    # /25 ends in .127, not .255
    ("192.168.1.200", 25, "192.168.1.255"),   # upper half of the same /25
    ("192.168.1.10", 30, "192.168.1.11"),
    ("169.254.170.152", 16, "169.254.255.255"),
])
def test_broadcast_address_comes_from_the_real_mask(monkeypatch, ip, prefix, expected):
    monkeypatch.setattr(client_mod, "local_ipv4_subnets", lambda: [(ip, prefix)])
    routes = enumerate_broadcast_routes()
    assert [r.addr for r in routes] == [expected, DEFAULT_BROADCAST]


def test_limited_broadcast_is_always_kept_as_catch_all(monkeypatch):
    # SoftAP setups and platforms that cannot report a mask depend on it, and
    # dropping it would be a regression for every single-NIC user.
    monkeypatch.setattr(client_mod, "local_ipv4_subnets", lambda: [])
    routes = enumerate_broadcast_routes()
    assert [r.addr for r in routes] == [DEFAULT_BROADCAST]
    assert routes[-1].limited


def test_same_subnet_interfaces_collapse_to_one_destination(monkeypatch):
    # A docked laptop has Ethernet and Wi-Fi on one subnet. Without dedup the
    # device receives every discovery packet twice.
    monkeypatch.setattr(client_mod, "local_ipv4_subnets",
                        lambda: [("192.168.0.67", 24), ("192.168.0.223", 24)])
    assert [r.addr for r in enumerate_broadcast_routes()] == [
        "192.168.0.255", DEFAULT_BROADCAST]


def test_distinct_subnets_each_get_a_destination(monkeypatch):
    monkeypatch.setattr(client_mod, "local_ipv4_subnets",
                        lambda: [("192.168.0.205", 24), ("172.17.192.1", 16)])
    assert [r.addr for r in enumerate_broadcast_routes()] == [
        "192.168.0.255", "172.17.255.255", DEFAULT_BROADCAST]


def test_route_membership():
    route = BroadcastRoute("192.168.0.255", network=0xC0A80000, mask=0xFFFFFF00)
    assert route.contains("192.168.0.42")
    assert not route.contains("192.168.1.42")
    assert not route.contains("nonsense")
    # The catch-all matches nobody: it must never win the lock, or playback
    # would go back to the limited broadcast that failed to reach the device.
    assert not BroadcastRoute(DEFAULT_BROADCAST, limited=True).contains("192.168.0.42")


# ── Transport behaviour ─────────────────────────────────────────────
class FakeSocket:
    """Records datagrams; can be told which destinations fail."""

    def __init__(self, failing=()):
        self.sent = []
        self.failing = set(failing)

    def sendto(self, packet, addr):
        if addr[0] in self.failing:
            raise OSError(101, "Network is unreachable")
        self.sent.append((packet, addr[0]))

    @property
    def destinations(self):
        return [dst for _, dst in self.sent]


def make_client(monkeypatch, subnets, failing=()):
    monkeypatch.setattr(client_mod, "local_ipv4_subnets", lambda: list(subnets))
    udp = UdpClient()
    udp._routes = enumerate_broadcast_routes()
    sock = FakeSocket(failing)
    udp._sock = sock
    return udp, sock


SUBNETS = [("192.168.0.205", 24), ("172.17.192.1", 16)]


def test_discovery_fans_out_until_a_device_answers(monkeypatch):
    udp, sock = make_client(monkeypatch, SUBNETS)
    udp.send_discovery(b"ping")
    assert sock.destinations == ["192.168.0.255", "172.17.255.255", DEFAULT_BROADCAST]


def test_plain_send_never_fans_out(monkeypatch):
    # The critical rule: one PLAY produces exactly one datagram.
    udp, sock = make_client(monkeypatch, SUBNETS)
    udp.send(b"play")
    assert sock.destinations == [DEFAULT_BROADCAST]


def test_pong_pins_broadcasts_to_that_subnet(monkeypatch):
    udp, sock = make_client(monkeypatch, SUBNETS)
    udp._lock_route_for("192.168.0.42")

    udp.send(b"play")
    assert sock.destinations == ["192.168.0.255"]

    # Discovery stops probing once pinned — one datagram, not three.
    sock.sent.clear()
    udp.send_discovery(b"ping")
    assert sock.destinations == ["192.168.0.255"]


def test_first_reply_wins(monkeypatch):
    udp, _ = make_client(monkeypatch, SUBNETS)
    udp._lock_route_for("192.168.0.42")
    udp._lock_route_for("172.17.0.9")
    assert udp.broadcast_destination() == "192.168.0.255"


def test_reply_from_an_unknown_subnet_leaves_the_catch_all_in_place(monkeypatch):
    # e.g. a device reached through a route we did not enumerate. Falling back
    # to the limited broadcast is the pre-existing behaviour, not a failure.
    udp, _ = make_client(monkeypatch, SUBNETS)
    udp._lock_route_for("10.9.9.9")
    assert udp.broadcast_destination() == DEFAULT_BROADCAST


def test_explicit_broadcast_addr_is_honoured_verbatim(monkeypatch):
    # Naming a destination is also the escape hatch for pinning it by hand,
    # which is why no extra setting exists.
    monkeypatch.setattr(client_mod, "local_ipv4_subnets", lambda: list(SUBNETS))
    udp = UdpClient(broadcast_addr="192.168.5.255")
    assert not udp._auto_routes
    sock = FakeSocket()
    udp._sock = sock
    udp.send_discovery(b"ping")
    udp.send(b"play")
    assert sock.destinations == ["192.168.5.255", "192.168.5.255"]


def test_single_nic_host_is_unchanged(monkeypatch):
    # Regression guard: one interface must behave exactly as it did before.
    udp, sock = make_client(monkeypatch, [("192.168.0.205", 24)])
    udp.send_discovery(b"ping")
    assert sock.destinations == ["192.168.0.255", DEFAULT_BROADCAST]
    udp._lock_route_for("192.168.0.42")
    sock.sent.clear()
    udp.send(b"play")
    assert sock.destinations == ["192.168.0.255"]


# ── Send failures must not be terminal, and must not flood ──────────
def test_send_failure_keeps_the_socket_and_logs_once(monkeypatch, caplog):
    udp, sock = make_client(monkeypatch, [("192.168.0.205", 24)],
                            failing={DEFAULT_BROADCAST})
    with caplog.at_level("WARNING", logger="hapbeat"):
        for _ in range(50):
            assert udp.send(b"play") is False
    # The socket survives a failed send — a lost datagram says nothing about it.
    assert udp._sock is sock
    assert sum("failed" in r.message for r in caplog.records) == 1


def test_recovery_is_reported_and_re_arms_the_warning(monkeypatch, caplog):
    udp, sock = make_client(monkeypatch, [("192.168.0.205", 24)],
                            failing={DEFAULT_BROADCAST})
    with caplog.at_level("INFO", logger="hapbeat"):
        udp.send(b"play")
        sock.failing.clear()
        udp.send(b"play")
        sock.failing.add(DEFAULT_BROADCAST)
        udp.send(b"play")
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    recovered = [r for r in caplog.records if "recovered" in r.message]
    assert len(recovered) == 1
    assert len(warnings) == 2  # a later, unrelated outage is still reported


def test_failure_state_is_tracked_per_destination(monkeypatch, caplog):
    # One permanently dead destination must not silence a different one that
    # breaks later, and must not be re-reported when a healthy destination
    # succeeds in between.
    udp, sock = make_client(monkeypatch, [("192.168.0.205", 24)],
                            failing={"10.0.0.9"})
    with caplog.at_level("INFO", logger="hapbeat"):
        udp.send(b"play", "10.0.0.9")
        for _ in range(5):
            udp.send(b"play", "192.168.0.42")   # healthy — no recovery spam
        udp.send(b"play", "10.0.0.9")           # still dead — already reported
        sock.failing.add("192.168.0.42")
        udp.send(b"play", "192.168.0.42")       # newly dead — must be reported
    messages = [r.message for r in caplog.records]
    assert sum("10.0.0.9" in m for m in messages) == 1
    assert sum("192.168.0.42" in m for m in messages) == 1


def test_discovery_survives_one_dead_interface(monkeypatch, caplog):
    # A virtual switch that rejects the send must not stop the others; that is
    # the very interface the fan-out exists to work around.
    udp, sock = make_client(monkeypatch, SUBNETS, failing={"172.17.255.255"})
    with caplog.at_level("INFO", logger="hapbeat"):
        for _ in range(5):
            assert udp.send_discovery(b"ping") is True
    assert sock.destinations == ["192.168.0.255", DEFAULT_BROADCAST] * 5
    # Silent: a host almost always has an adapter that cannot carry a broadcast
    # (Bluetooth PAN, Wi-Fi Direct). Warning per fan-out — or flapping between
    # "failed" and "recovered" because the *other* destinations succeed — would
    # be a worse log than the one this replaces.
    assert caplog.records == []


def test_discovery_warns_once_when_every_destination_fails(monkeypatch, caplog):
    # Distinct from the case above: nothing got out at all, so discovery really
    # is dead and an operator needs to know.
    all_dead = {"192.168.0.255", "172.17.255.255", DEFAULT_BROADCAST}
    udp, sock = make_client(monkeypatch, SUBNETS, failing=all_dead)
    with caplog.at_level("WARNING", logger="hapbeat"):
        for _ in range(5):
            assert udp.send_discovery(b"ping") is False
    assert len(caplog.records) == 1

    # A later, separate outage must still be reported rather than swallowed by
    # the first one's flag.
    sock.failing.clear()
    assert udp.send_discovery(b"ping") is True
    sock.failing |= all_dead
    with caplog.at_level("WARNING", logger="hapbeat"):
        assert udp.send_discovery(b"ping") is False
    assert len(caplog.records) == 2


# ── The playback/discovery split at the API level ───────────────────
def test_only_ping_and_connect_status_use_the_fan_out(fake_client):  # noqa: F811
    hb = hapbeat_mod.Hapbeat(app_name="Test", keepalive=False).open()
    try:
        hb._devices["192.168.0.42"] = hapbeat_mod.Device(ip="192.168.0.42")

        hb.ping()
        hb.connect_status(connected=True)
        assert len(hb._client.discovery_sent) == 2

        hb._client.discovery_sent.clear()
        hb.play("sample-kit.sine_100hz")
        hb.stop("sample-kit.sine_100hz")
        hb.stop_all()
        hb.stream_begin(sample_rate=16000, channels=1, total_samples=16,
                        gain=1.0, target="")
        hb.stream_data(0, b"\x00\x00")
        hb.stream_end()

        # Not one playback packet took the fan-out.
        assert hb._client.discovery_sent == []
        commands = [protocol.parse_header(p)["command_type"]
                    for p, _ in hb._client.sent]
        assert protocol.CMD_PLAY in commands
    finally:
        hb.close()
