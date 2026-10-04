"""Stream session v2 (contracts stream-session-v2.md, sdk-multi-stream.md
§6.8-6.9 / §7 case 6) with a fake transport: wire vectors, per-device class
detection from matched replies, lease + generation lifecycle, the legacy
fallback and its per-device 300 ms guard. No sockets, no live haptics."""

from __future__ import annotations

import json
import struct
import time
from pathlib import Path

import pytest

from hapbeat import PlaybackStatus, protocol
from hapbeat import hapbeat as hapbeat_mod
from hapbeat.stream import (
    LEGACY_END_TO_BEGIN_GUARD,
    LEGACY_STREAM_MODE,
    SESSION_END_LINGER,
    EndpointStreamMode,
    StreamEndpoint,
    StreamHub,
)
from hapbeat.stream_lease import (
    EndpointClass,
    StreamLease,
    V2_LEASE_REFRESH_INTERVAL,
)

from .pong_helpers import SUPERSEDED, last_ping, lease_tail, pong_packet, reply
from .test_hapbeat import fake_client  # noqa: F401 (pytest fixture)

FIXTURES = Path(__file__).resolve().parents[3] / "repos-core" / "hapbeat-contracts" / "fixtures"


def load_fixture(name: str) -> dict:
    path = FIXTURES / name
    if not path.exists():
        pytest.skip(f"contracts fixture not found: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _wait_until(predicate, timeout: float = 1.5) -> None:
    deadline = time.perf_counter() + timeout
    while not predicate() and time.perf_counter() < deadline:
        time.sleep(0.005)
    assert predicate()


# ── packet decoding (parse_packet only accepts header version 1) ────
def decode(packet: bytes) -> dict:
    _magic, version, cmd, seq, length = struct.unpack("<HBBHH", packet[:8])
    payload = packet[8:8 + length]
    out = {"version": version, "cmd": cmd, "seq": seq, "payload": payload}
    if version == protocol.STREAM_VERSION:
        boot, ticket, generation = struct.unpack("<QII", payload[:16])
        out["identity"] = (boot, ticket, generation)
        out["body"] = payload[16:]
    else:
        out["identity"] = None
        out["body"] = payload
    return out


STREAM_CMDS = (protocol.CMD_STREAM_BEGIN, protocol.CMD_STREAM_DATA, protocol.CMD_STREAM_END)


def stream_log(hb, ip: str | None = None) -> list[dict]:
    """Every stream packet sent so far, with its destination."""
    out = []
    for packet, addr in list(hb._client.sent):
        entry = decode(packet)
        if entry["cmd"] in STREAM_CMDS and (ip is None or addr == ip):
            entry["addr"] = addr
            out.append(entry)
    return out


def of(entries, cmd):
    return [e for e in entries if e["cmd"] == cmd]


def begin_target(entry: dict) -> str:
    return entry["body"][12:].rstrip(b"\x00").decode()


def make_hb() -> hapbeat_mod.Hapbeat:
    return hapbeat_mod.Hapbeat(keepalive=False, stream_send_ahead=0.02).open()


LOOP_PCM = struct.pack("<h", 1000) * 4096
SHORT_PCM = struct.pack("<h", 1000) * 64  # 4 ms


# ── wire vectors (fixtures/stream-session-v2.json) ──────────────────
def test_wire_vectors_match_contract_fixture():
    fx = load_fixture("stream-session-v2.json")
    boot = int(fx["boot_id"], 16)
    ticket, generation, seq = fx["ticket"], fx["generation"], fx["seq"]
    packets = {p["name"]: bytes.fromhex(p["hex"]) for p in fx["packets"]}

    assert protocol.build_stream_envelope(boot, ticket, generation) == bytes.fromhex(
        fx["envelope_hex"]
    )
    assert protocol.build_stream_begin_v2(
        seq, boot, ticket, generation,
        sample_rate=16000, channels=2, total_samples=0, gain=1.0, target="*",
    ) == packets["begin"]
    assert protocol.build_stream_data_v2(
        seq, boot, ticket, generation, 0, bytes.fromhex("1000f0ff")
    ) == packets["data"]
    assert protocol.build_stream_end_v2(seq, boot, ticket, generation) == packets["end"]


def test_envelope_rejects_zero_fields():
    for args in ((0, 1, 1), (1, 0, 1), (1, 1, 0)):
        with pytest.raises(ValueError):
            protocol.build_stream_envelope(*args)


def test_legacy_v1_stream_has_no_envelope():
    begin = decode(protocol.build_stream_begin(1, channels=2, target="a"))
    end = decode(protocol.build_stream_end(2))
    assert begin["version"] == end["version"] == protocol.VERSION
    assert len(begin["body"]) == 12 + 2
    assert end["payload"] == b""


# ── extended PING / PONG tail ───────────────────────────────────────
def test_extended_ping_carries_incarnation_and_strictly_increasing_timestamps(fake_client):  # noqa: F811
    hb = make_hb()
    try:
        hb.ping()
        hb.ping()
        first, second = hb._client.discovery_sent[-2:]
        p1, p2 = decode(first)["payload"], decode(second)["payload"]
        assert len(p1) == len(p2) == 16
        ts1, inc1 = struct.unpack("<qQ", p1)
        ts2, inc2 = struct.unpack("<qQ", p2)
        assert ts2 > ts1
        assert inc1 == inc2 == hb._client_incarnation != 0
    finally:
        hb.close()


def test_reconnect_issues_a_fresh_incarnation(fake_client):  # noqa: F811
    hb = make_hb()
    first = hb._client_incarnation
    hb.close()
    hb.open()
    try:
        assert hb._client_incarnation not in (0, first)
        assert hb._pending_pings == {}
    finally:
        hb.close()


def test_parse_pong_classifies_the_lease_tail():
    def tail_of(tail: bytes) -> dict:
        return protocol.parse_pong(pong_packet(1, 5, "p", tail))

    valid = tail_of(lease_tail(9, 0xAB, 4, high_water=3))
    assert valid["lease_tail"] == protocol.LEASE_TAIL_VALID
    assert (valid["echoed_client_incarnation"], valid["device_boot_id"],
            valid["lease_ticket"], valid["high_water_ticket"]) == (9, 0xAB, 4, 3)
    assert valid["lease_valid"] and not valid["lease_superseded"]
    assert tail_of(lease_tail(9, 0xAB, 4, flags=SUPERSEDED | 1))["lease_superseded"]
    # flag set but ticket 0 (allocation failed) is not a usable lease
    assert not tail_of(lease_tail(9, 0xAB, 0))["lease_valid"]

    assert tail_of(b"")["lease_tail"] == protocol.LEASE_TAIL_ABSENT
    assert tail_of(b"\x07xyz")["lease_tail"] == protocol.LEASE_TAIL_ABSENT
    bare = protocol.parse_pong(protocol.build_header(protocol.CMD_PONG, 1, 16)
                               + struct.pack("<qq", 5, 1))
    assert bare["lease_tail"] == protocol.LEASE_TAIL_ABSENT

    good = lease_tail(9, 0xAB, 4)
    for bad in (good[:-1], good + b"\x00", b"HB",
                lease_tail(9, 0xAB, 4, version=2),
                lease_tail(9, 0xAB, 4, reserved=1),
                lease_tail(9, 0xAB, 4, flags=0x05)):
        assert tail_of(bad)["lease_tail"] == protocol.LEASE_TAIL_MALFORMED


# ── class detection rules ───────────────────────────────────────────
def _class(hb, ip: str):
    state = hb._stream_states.get(ip)
    return state.endpoint_class if state else EndpointClass.UNKNOWN


def test_unsolicited_and_unmatched_pongs_never_classify(fake_client):  # noqa: F811
    hb = make_hb()
    try:
        ip, address = "192.0.2.10", "player_1/chest"
        # identity-update PONG (seq 0, timestamp 0), no tail
        hb._on_pong(protocol.parse_pong(pong_packet(0, 0, address)), ip)
        hb.ping()
        seq, ts, inc = last_ping(hb)
        reply(hb, ip, address, ping=(seq, ts + 1, inc))        # wrong timestamp
        reply(hb, ip, address, ping=((seq + 7) & 0xFFFF, ts, inc))  # other request
        assert _class(hb, ip) is EndpointClass.UNKNOWN
        assert [d.address for d in hb.devices] == [address]   # still discovered
        playback = hb.stream_pcm(LOOP_PCM, loop=True)
        assert playback.status is PlaybackStatus.DEFERRED
        time.sleep(0.05)
        assert stream_log(hb) == []
    finally:
        hb.close()


def test_malformed_tail_leaves_the_device_pending(fake_client):  # noqa: F811
    hb = make_hb()
    try:
        ip = "192.0.2.10"
        reply(hb, ip, "player_1/chest", tail=lease_tail(1, 1, 1, version=9))
        assert _class(hb, ip) is EndpointClass.UNKNOWN
        assert hb.stream_pcm(LOOP_PCM, loop=True).status is PlaybackStatus.DEFERRED
    finally:
        hb.close()


def test_matched_reply_without_tail_is_legacy_and_with_tail_is_v2(fake_client):  # noqa: F811
    hb = make_hb()
    try:
        reply(hb, "192.0.2.10", "player_1/left")
        reply(hb, "192.0.2.11", "player_1/right", lease=(0xB0, 3))
        assert _class(hb, "192.0.2.10") is EndpointClass.LEGACY
        assert _class(hb, "192.0.2.11") is EndpointClass.V2
        playback = hb.stream_pcm(LOOP_PCM, loop=True)
        _wait_until(lambda: of(stream_log(hb, "192.0.2.10"), protocol.CMD_STREAM_DATA)
                    and of(stream_log(hb, "192.0.2.11"), protocol.CMD_STREAM_DATA))
        playback.stop()
        legacy, v2 = stream_log(hb, "192.0.2.10"), stream_log(hb, "192.0.2.11")
        assert {e["version"] for e in legacy} == {protocol.VERSION}
        assert {e["version"] for e in v2} == {protocol.STREAM_VERSION}
        assert {e["identity"] for e in v2} == {(0xB0, 3, 1)}
        assert all(e["addr"] in ("192.0.2.10", "192.0.2.11") for e in stream_log(hb))
    finally:
        hb.close()


def test_late_old_boot_reply_does_not_roll_the_lease_back(fake_client):  # noqa: F811
    hb = make_hb()
    try:
        ip, address = "192.0.2.10", "player_1/chest"
        hb.ping()
        old_ping = last_ping(hb)
        reply(hb, ip, address, lease=(0xB2, 1))      # newer request, new boot
        # the older request's reply arrives late: old boot / legacy-looking
        reply(hb, ip, "player_9/stale", lease=(0xB1, 5), ping=old_ping)
        reply(hb, ip, address, ping=old_ping)
        state = hb._stream_states[ip]
        assert state.endpoint_class is EndpointClass.V2
        assert state.lease == StreamLease(0xB2, 1)
        # a discarded HBS2 reply does not roll the address back either
        assert hb.devices[0].address == address
    finally:
        hb.close()


def test_unsolicited_pong_keeps_an_established_lease(fake_client):  # noqa: F811
    hb = make_hb()
    try:
        ip = "192.0.2.10"
        reply(hb, ip, "player_1/chest", lease=(0xB1, 2))
        hb._on_pong(protocol.parse_pong(pong_packet(0, 0, "player_1/chest")), ip)
        assert hb._stream_states[ip].lease == StreamLease(0xB1, 2)
    finally:
        hb.close()


def test_reply_for_another_incarnation_is_discarded(fake_client):  # noqa: F811
    hb = make_hb()
    try:
        ip = "192.0.2.10"
        hb.ping()
        seq, ts, inc = last_ping(hb)
        tail = lease_tail(inc ^ 1, 0xB1, 2)
        reply(hb, ip, "player_1/chest", tail=tail, ping=(seq, ts, inc))
        assert hb.devices == []  # whole PONG dropped
    finally:
        hb.close()


def test_invalid_or_superseded_lease_defers_and_is_never_legacy(fake_client):  # noqa: F811
    hb = make_hb()
    try:
        reply(hb, "192.0.2.10", "player_1/a", lease=(0xB1, 2), flags=0)
        reply(hb, "192.0.2.11", "player_1/b", lease=(0xB1, 3), flags=1 | SUPERSEDED)
        assert _class(hb, "192.0.2.10") is EndpointClass.V2
        assert _class(hb, "192.0.2.11") is EndpointClass.V2
        assert hb.stream_pcm(LOOP_PCM, loop=True).status is PlaybackStatus.DEFERRED
        # a later periodic reply of the same incarnation keeps the latch
        reply(hb, "192.0.2.11", "player_1/b", lease=(0xB1, 3))
        assert hb._stream_states["192.0.2.11"].lease_superseded
        time.sleep(0.05)
        assert stream_log(hb) == []
    finally:
        hb.close()


def test_keepalive_is_capped_while_a_device_is_v2(fake_client):  # noqa: F811
    hb = hapbeat_mod.Hapbeat(keepalive=False, keepalive_interval=30.0).open()
    try:
        reply(hb, "192.0.2.10", "player_1/a")
        assert hb._keepalive_wait() == 30.0
        reply(hb, "192.0.2.11", "player_1/b", lease=(0xB1, 1))
        assert hb._keepalive_wait() == V2_LEASE_REFRESH_INTERVAL
    finally:
        hb.close()


# ── routing fixture (sdk-multi-stream-routing.json) on v2 devices ───
def test_routing_fixture_over_v2_devices(fake_client):  # noqa: F811
    fx = load_fixture("sdk-multi-stream-routing.json")
    hb = make_hb()
    try:
        devices = {d["id"]: d for d in fx["devices"]}
        for n, device in enumerate(fx["devices"], start=1):
            reply(hb, device["ip"], device["address"], lease=(0xD0 + n, n))
        playbacks = {
            s["id"]: hb.stream_pcm(LOOP_PCM, target=s["target"], loop=True)
            for s in fx["sources"]
        }
        for s in fx["sources"]:
            assert playbacks[s["id"]].status.value == s["expected_status"]
        for session in fx["expected_sessions"]:
            ip = devices[session["endpoint_id"]]["ip"]
            _wait_until(lambda ip=ip: of(stream_log(hb, ip), protocol.CMD_STREAM_DATA))
            begins = of(stream_log(hb, ip), protocol.CMD_STREAM_BEGIN)
            assert len(begins) == 1
            assert begin_target(begins[0]) == session["stream_begin_target"]
        for playback in playbacks.values():
            playback.stop()
        log = stream_log(hb)
        assert log and all(e["addr"] in {d["ip"] for d in fx["devices"]} for e in log)
        assert all(e["version"] == protocol.STREAM_VERSION for e in log)
    finally:
        hb.close()


# ── StreamHub lifecycle with a recording sink ───────────────────────
class RecordingSink:
    def __init__(self):
        self.events: list[tuple] = []  # (kind, endpoint, identity, offset)

    def stream_begin_endpoint(self, endpoint):
        self.events.append(("begin", endpoint, None, None, time.perf_counter()))

    def stream_data_endpoint(self, endpoint, offset, data):
        self.events.append(("data", endpoint, None, offset, time.perf_counter()))

    def stream_end_endpoint(self, endpoint):
        self.events.append(("end", endpoint, None, None, time.perf_counter()))

    def stream_begin_v2(self, endpoint, identity):
        self.events.append(("begin", endpoint, identity, None, time.perf_counter()))

    def stream_data_v2(self, endpoint, identity, offset, data):
        self.events.append(("data", endpoint, identity, offset, time.perf_counter()))

    def stream_end_v2(self, endpoint, identity):
        self.events.append(("end", endpoint, identity, None, time.perf_counter()))

    def kinds(self, endpoint, kind):
        return [e for e in self.events if e[1] == endpoint and e[0] == kind]


V2_EP = StreamEndpoint("192.0.2.20", "player_1/v2")
LEGACY_EP = StreamEndpoint("192.0.2.21", "player_1/legacy")
LEASE = StreamLease(0xB00D, 7)


def modes_of(table: dict):
    return lambda endpoint: table.get(endpoint)


def test_v2_restarts_immediately_after_end_with_the_next_generation():
    sink = RecordingSink()
    hub = StreamHub(sink, lambda: [V2_EP], send_ahead_sec=10.0,
                    mode_provider=modes_of({V2_EP: EndpointStreamMode(LEASE)}))
    try:
        first = hub.play_pcm(SHORT_PCM, target="player_1")
        _wait_until(lambda: sink.kinds(V2_EP, "end"))
        second = hub.play_pcm(SHORT_PCM, target="player_1")
        # BEGIN goes out synchronously inside play_pcm: no 300 ms cooldown
        begins = sink.kinds(V2_EP, "begin")
        assert len(begins) == 2
        assert begins[1][4] - sink.kinds(V2_EP, "end")[0][4] < 0.1
        assert second.status is not PlaybackStatus.DEFERRED
        assert [b[2].generation for b in begins] == [1, 2]
        assert {b[2].lease for b in begins} == {LEASE}
        assert first.status is PlaybackStatus.STOPPED
    finally:
        hub.close()


def test_legacy_guard_applies_only_to_the_legacy_endpoint():
    sink = RecordingSink()
    modes = {V2_EP: EndpointStreamMode(LEASE), LEGACY_EP: LEGACY_STREAM_MODE}
    hub = StreamHub(sink, lambda: [V2_EP, LEGACY_EP], send_ahead_sec=10.0,
                    mode_provider=modes_of(modes))
    try:
        hub.play_pcm(SHORT_PCM, target="player_1")
        _wait_until(lambda: sink.kinds(V2_EP, "end") and sink.kinds(LEGACY_EP, "end"))
        legacy_end = sink.kinds(LEGACY_EP, "end")[0][4]
        hub.play_pcm(SHORT_PCM, target="player_1")
        assert len(sink.kinds(V2_EP, "begin")) == 2        # immediate
        assert len(sink.kinds(LEGACY_EP, "begin")) == 1    # guarded
        _wait_until(lambda: len(sink.kinds(LEGACY_EP, "begin")) == 2)
        assert sink.kinds(LEGACY_EP, "begin")[1][4] - legacy_end >= LEGACY_END_TO_BEGIN_GUARD
        assert all(e[2] is None for e in sink.events if e[1] == LEGACY_EP)
        assert all(e[2] is not None for e in sink.events if e[1] == V2_EP)
    finally:
        hub.close()


class V2ReceiverModel:
    """The receiver ordering rules of stream-session-v2.md, for one boot."""

    def __init__(self, boot_id: int):
        self.boot_id = boot_id
        self.high_water = (0, 0)
        self.active = None
        self.expected = 0
        self.resets = 0
        self.stopped_active = 0
        self.accepted: list[tuple] = []

    def feed(self, kind, identity, offset=None, length=0):
        if identity.lease.boot_id != self.boot_id:
            return
        key = (identity.lease.ticket, identity.generation)
        if kind == "begin":
            if key > self.high_water:
                self.high_water, self.active, self.expected = key, key, 0
                self.resets += 1
        elif kind == "data":
            if key == self.active and ((offset - self.expected) & 0xFFFFFFFF) < 2**31:
                self.expected = (offset + length) & 0xFFFFFFFF
                self.accepted.append((key, offset))
        elif kind == "end":
            if key == self.active:
                self.active = None
                self.stopped_active += 1
            elif key > self.high_water:
                self.high_water, self.active = key, None


def test_delayed_and_reordered_old_generation_packets_do_not_touch_the_new_one():
    sink = RecordingSink()
    hub = StreamHub(sink, lambda: [V2_EP], send_ahead_sec=10.0,
                    mode_provider=modes_of({V2_EP: EndpointStreamMode(LEASE)}))
    try:
        hub.play_pcm(SHORT_PCM, target="player_1")
        _wait_until(lambda: sink.kinds(V2_EP, "end"))
        old = list(sink.events)
        playback = hub.play_pcm(LOOP_PCM, target="player_1", loop=True)
        _wait_until(lambda: len(sink.kinds(V2_EP, "data")) >= len(old) + 2)
        playback.stop()
        new = [e for e in sink.events[len(old):] if e[0] != "end"]
    finally:
        hub.close()

    old_gen = old[0][2].generation
    assert {e[2].generation for e in new} == {old_gen + 1}
    assert [e[3] for e in new if e[0] == "data"][0] == 0

    def run(order):
        rx = V2ReceiverModel(LEASE.boot_id)
        for kind, _ep, identity, offset, _t in order:
            rx.feed(kind, identity, offset, 4 if kind == "data" else 0)
        return rx

    # new session first, then every old packet arrives late (END, BEGIN, DATA
    # duplicated); nothing old may reset, feed or stop the new generation
    rx = run(new + old + old)
    assert rx.active == (LEASE.ticket, old_gen + 1)
    assert rx.resets == 1 and rx.stopped_active == 0
    assert {key for key, _ in rx.accepted} == {(LEASE.ticket, old_gen + 1)}
    # old END overtaken by the new BEGIN, old DATA interleaved
    rx = run([new[0]] + old[::-1] + new[1:])
    assert rx.active == (LEASE.ticket, old_gen + 1) and rx.stopped_active == 0


# ── lease / class changes on a running session ──────────────────────
def _running(hb, ip):
    return of(stream_log(hb, ip), protocol.CMD_STREAM_DATA)


def test_class_change_ends_in_old_format_then_begins_in_new(fake_client):  # noqa: F811
    hb = make_hb()
    ip, address = "192.0.2.10", "player_1/chest"
    try:
        reply(hb, ip, address)  # legacy firmware
        playback = hb.stream_pcm(LOOP_PCM, loop=True)
        _wait_until(lambda: _running(hb, ip))
        mark = len(stream_log(hb, ip))
        reply(hb, ip, address, lease=(0xB1, 4))  # firmware updated to v2
        _wait_until(lambda: any(e["version"] == 2 and e["cmd"] == protocol.CMD_STREAM_DATA
                                for e in stream_log(hb, ip)[mark:]))
        after = stream_log(hb, ip)[mark:]
        first_v2 = next(i for i, e in enumerate(after) if e["version"] == 2)
        legacy_end = [i for i, e in enumerate(after)
                      if e["cmd"] == protocol.CMD_STREAM_END and e["version"] == 1]
        assert legacy_end and legacy_end[0] < first_v2
        assert after[legacy_end[0]]["payload"] == b""
        assert after[first_v2]["cmd"] == protocol.CMD_STREAM_BEGIN
        assert after[first_v2]["identity"] == (0xB1, 4, 1)
        assert all(e["version"] == 2 for e in after[first_v2:])

        # rollback to pre-v2 firmware: v2 END (old identity), then v1 BEGIN
        # straight away (the guard follows legacy ENDs only; let the one from
        # the legacy -> v2 change above elapse first)
        time.sleep(LEGACY_END_TO_BEGIN_GUARD)
        mark = len(stream_log(hb, ip))
        reply(hb, ip, address)
        after = stream_log(hb, ip)[mark:]
        assert after[0]["cmd"] == protocol.CMD_STREAM_END
        assert after[0]["identity"] == (0xB1, 4, 1)
        assert after[1]["cmd"] == protocol.CMD_STREAM_BEGIN and after[1]["version"] == 1
        assert playback.status is PlaybackStatus.ACTIVE
    finally:
        hb.close()


def test_reboot_ends_old_identity_and_begins_under_the_new_lease(fake_client):  # noqa: F811
    hb = make_hb()
    ip, address = "192.0.2.10", "player_1/chest"
    try:
        reply(hb, ip, address, lease=(0xB1, 4))
        hb.stream_pcm(LOOP_PCM, loop=True)
        _wait_until(lambda: _running(hb, ip))
        mark = len(stream_log(hb, ip))
        reply(hb, ip, address, lease=(0xB2, 1))  # new boot id
        after = stream_log(hb, ip)[mark:]
        assert (after[0]["cmd"], after[0]["identity"]) == (protocol.CMD_STREAM_END, (0xB1, 4, 1))
        assert (after[1]["cmd"], after[1]["identity"]) == (protocol.CMD_STREAM_BEGIN, (0xB2, 1, 1))
        _wait_until(lambda: len(stream_log(hb, ip)) > mark + 2)
        assert {e["identity"] for e in stream_log(hb, ip)[mark + 1:]
                if e["cmd"] != protocol.CMD_STREAM_END} == {(0xB2, 1, 1)}
    finally:
        hb.close()


def test_superseded_lease_ends_the_session_and_does_not_retake(fake_client):  # noqa: F811
    hb = make_hb()
    ip, address = "192.0.2.10", "player_1/chest"
    try:
        reply(hb, ip, address, lease=(0xB1, 4))
        playback = hb.stream_pcm(LOOP_PCM, loop=True)
        _wait_until(lambda: _running(hb, ip))
        mark = len(stream_log(hb, ip))
        reply(hb, ip, address, lease=(0xB1, 4), flags=1 | SUPERSEDED)
        reply(hb, ip, address, lease=(0xB1, 4))  # periodic reply, same incarnation
        time.sleep(SESSION_END_LINGER)
        after = stream_log(hb, ip)[mark:]
        ends = of(after, protocol.CMD_STREAM_END)
        assert len(ends) == 1 and ends[0]["identity"] == (0xB1, 4, 1)
        assert after[-1] is ends[0]  # nothing (no DATA, no BEGIN) after END
        assert of(after, protocol.CMD_STREAM_BEGIN) == []
        assert playback.status is PlaybackStatus.DEFERRED
    finally:
        hb.close()
