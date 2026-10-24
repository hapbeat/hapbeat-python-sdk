"""The 1.x wire rules (contracts DEC-096): PONG classification, payload_length,
CAPS, ERROR, unicast lease PINGs at TTL / 3, BEGIN repetition, the 3 s own-gap
rule, lease_reissued, cursor continuation, STREAM_STOPPED, Deferred reasons,
generation exhaustion, re-splice and mono endpoints (stream-session.md,
message-format.md, sdk-multi-stream.md §3 / §6 / §7).

Fake transport and recording sinks only: no sockets, no live haptics."""

from __future__ import annotations

import logging
import re
import struct
import time
from pathlib import Path

import pytest

from hapbeat import DeferredReason, DeviceCaps, EndpointClass, LeaseState, PlaybackStatus, protocol
from hapbeat import stream as stream_mod
from hapbeat.stream import (
    LEGACY_STREAM_MODE,
    MAX_GENERATION,
    SESSION_END_LINGER,
    EndpointStreamMode,
    StreamEndpoint,
    StreamHub,
)
from hapbeat.stream_lease import LEASE_TTL, StreamLease

from .pong_helpers import REISSUED, SUPERSEDED, VALID, reply
from .test_hapbeat import fake_client  # noqa: F401 (pytest fixture)
from .test_stream_session_v2 import (
    FIXTURES,
    LEASE,
    LOOP_PCM,
    SHORT_PCM,
    V2_EP,
    _wait_until,
    decode,
    load_fixture,
    make_hb,
    modes_of,
    of,
    stream_log,
)

ROOT = Path(__file__).resolve().parents[1]
GEN1_EP = StreamEndpoint("192.0.2.21", "player_1/gen1")
RAMP_FRAMES = 16000
RAMP_PCM = struct.pack(f"<{RAMP_FRAMES}h", *range(RAMP_FRAMES))  # 1 s, sample = frame index


# ── helpers ─────────────────────────────────────────────────────────
def discover(hb, ip: str, address: str, firmware: str = "v1") -> None:
    """A matched reply to the 8-byte discovery PING (never classifies)."""
    hb.ping()
    seq, pending = max(
        ((s, p) for s, p in hb._pending_pings.items() if not p.extended),
        key=lambda kv: kv[1].timestamp_us,
    )
    reply(hb, ip, address, ping=(seq, pending.timestamp_us, pending.client_incarnation),
          firmware=firmware)


def sent_to(hb, ip: str, cmd: int) -> list[dict]:
    return [decode(packet) for packet, addr in list(hb._client.sent)
            if addr == ip and decode(packet)["cmd"] == cmd]


def lease_pings(hb, ip: str) -> list[dict]:
    pings = sent_to(hb, ip, protocol.CMD_PING)
    assert all(len(p["payload"]) == 16 for p in pings)
    return pings


def envelope(identity: tuple[int, int, int]) -> bytes:
    return struct.pack("<QII", *identity)


def current_identity(hb, ip: str) -> tuple[int, int, int]:
    return of(stream_log(hb, ip), protocol.CMD_STREAM_BEGIN)[-1]["identity"]


def send_error(hb, ip: str, code: int, detail: bytes = b"", *, port: int = 7700,
               ref_command: int = protocol.CMD_STREAM_DATA) -> None:
    payload = (struct.pack("<H", code) + b"err\x00"
               + struct.pack("<BHB", ref_command, 1, len(detail)) + detail)
    hb._on_packet(protocol.CMD_ERROR, 1, payload, (ip, port))


def samples(entry: dict) -> tuple[int, ...]:
    """The PCM16 samples of a Stream Gen 2 DATA entry (L, R interleaved)."""
    pcm = entry["body"][4:]
    return struct.unpack(f"<{len(pcm) // 2}h", pcm)


def data_offset(entry: dict) -> int:
    return struct.unpack_from("<I", entry["body"])[0]


class DataSink:
    """Recording sink that keeps DATA bytes: (kind, endpoint, identity, offset, data)."""

    def __init__(self):
        self.events: list[tuple] = []

    def stream_begin_endpoint(self, endpoint):
        self.events.append(("begin", endpoint, None, None, b""))

    def stream_data_endpoint(self, endpoint, offset, data):
        self.events.append(("data", endpoint, None, offset, data))

    def stream_end_endpoint(self, endpoint):
        self.events.append(("end", endpoint, None, None, b""))

    def stream_begin_v2(self, endpoint, identity):
        self.events.append(("begin", endpoint, identity, None, b""))

    def stream_data_v2(self, endpoint, identity, offset, data):
        self.events.append(("data", endpoint, identity, offset, data))

    def stream_end_v2(self, endpoint, identity):
        self.events.append(("end", endpoint, identity, None, b""))

    def of(self, endpoint, kind=None):
        return [e for e in self.events if e[1] == endpoint and (kind is None or e[0] == kind)]


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def clock(monkeypatch):
    """Drive a StreamHub by hand: a fake clock and no background thread."""
    fake = Clock()
    monkeypatch.setattr(stream_mod, "_clock", fake)
    monkeypatch.setattr(StreamHub, "_ensure_thread_locked", lambda self: None)
    return fake


def tick(hub: StreamHub) -> None:
    with hub._lock:
        hub._reconcile_locked()
        hub._pump_locked()


# ── 1-4: fixtures ───────────────────────────────────────────────────
TAIL_CLASS = {
    protocol.LEASE_TAIL_ABSENT: "gen1",
    protocol.LEASE_TAIL_VALID: "gen2",
    protocol.LEASE_TAIL_IGNORED: "ignored",
}


def test_pong_classification_fixture_follows_the_1x_rules():
    cases = load_fixture("stream-session-cases.json")["pong_classification"]["cases"]
    assert cases
    for case in cases:
        pong = protocol.parse_pong(bytes.fromhex(case["hex"]))
        expected = case["expected_1x"]
        assert TAIL_CLASS[pong["lease_tail"]] == expected["class"], case["name"]
        if expected["class"] == "gen2":
            got = {
                "class": "gen2",
                "lease_valid": pong["lease_valid"],
                "superseded": pong["lease_superseded"],
                "ticket": pong["lease_ticket"],
                "high_water_ticket": pong["high_water_ticket"],
                "lease_reissued": pong["lease_reissued"],
            }
            assert got == expected, case["name"]


def test_pong_reads_only_payload_length_bytes():
    cases = load_fixture("stream-session-cases.json")["pong_classification"]["cases"]
    datagram = bytes.fromhex(next(c["hex"] for c in cases if c["name"] == "tail_32"))
    base = protocol.parse_pong(datagram)
    assert base["lease_tail"] == protocol.LEASE_TAIL_VALID
    # bytes after payload_length are not part of the PONG (or its tail)
    assert protocol.parse_pong(datagram + b"\x01" * 8) == base
    assert protocol.parse_pong(datagram[:-1]) is None


def test_caps_request_and_caps_fixture():
    fx = load_fixture("wire-extension-cases.json")
    assert protocol.build_caps_req(0x0202) == bytes.fromhex(fx["caps_req"]["hex"])
    assert fx["caps"]
    for case in fx["caps"]:
        assert protocol.parse_caps(bytes.fromhex(case["hex"])) == case["expected"], case["name"]


def test_error_fixture_and_code_table():
    fx = load_fixture("wire-extension-cases.json")
    assert fx["error"]
    for case in fx["error"]:
        expected = dict(case["expected"])
        name = expected.pop("name")
        parsed = protocol.parse_error(bytes.fromhex(case["hex"]))
        if "detail" in parsed:
            parsed["detail_hex"] = parsed.pop("detail").hex()
        assert parsed == expected, case["name"]
        assert getattr(protocol, f"ERR_{name}") == expected["error_code"]
    spec = (FIXTURES.parent / "specs" / "message-format.md").read_text(encoding="utf-8")
    table = re.findall(r"^\| (0x[0-9A-F]{4}) \| ([A-Z_]+) \|", spec, re.M)
    assert len(table) >= 11
    for code, name in table:
        assert getattr(protocol, f"ERR_{name}") == int(code, 16), name


# ── 5-8: PINGs ──────────────────────────────────────────────────────
def test_ping_lengths_match_the_fixture(fake_client):  # noqa: F811
    fx = {c["name"]: bytes.fromhex(c["hex"]) for c in
          load_fixture("wire-extension-cases.json")["ping_lengths"]}
    assert protocol.build_ping(1, 1700000000000000) == fx["ping_8"]
    assert protocol.build_ping(0x0101, 1700000000000000, 0x1122334455667788) == fx["ping_16"]
    hb = make_hb()
    try:
        hb.ping()
        hb._lease_ping("192.0.2.10")
        assert len(decode(hb._client.discovery_sent[-1])["payload"]) == 8
        assert len(lease_pings(hb, "192.0.2.10")[-1]["payload"]) == 16
    finally:
        hb.close()


def test_extended_ping_goes_by_unicast_to_streamed_endpoints_only(fake_client):  # noqa: F811
    hb = make_hb()
    try:
        discover(hb, "192.0.2.10", "player_1/a")
        discover(hb, "192.0.2.11", "player_2/b")
        assert lease_pings(hb, "192.0.2.10") == lease_pings(hb, "192.0.2.11") == []
        hb.stream_pcm(LOOP_PCM, loop=True, target="player_1")
        assert len(lease_pings(hb, "192.0.2.10")) == 1
        assert lease_pings(hb, "192.0.2.11") == []
        assert all(len(decode(p)["payload"]) == 8 for p in hb._client.discovery_sent
                   if decode(p)["cmd"] == protocol.CMD_PING)
        assert not [addr for p, addr in hb._client.sent
                    if decode(p)["cmd"] == protocol.CMD_PING and addr is None]
    finally:
        hb.close()


def test_reply_to_the_discovery_ping_does_not_classify(fake_client):  # noqa: F811
    hb = make_hb()
    try:
        discover(hb, "192.0.2.10", "player_1/a")
        assert "192.0.2.10" not in hb._stream_states
        assert hb.devices[0].stream_class is EndpointClass.UNKNOWN
        assert hb.devices[0].address == "player_1/a"   # still discovered
    finally:
        hb.close()


def test_lease_refresh_interval_is_ttl_over_3_and_lasts_one_ttl_after_the_session(fake_client):  # noqa: F811
    hb = make_hb()
    ip = "192.0.2.10"
    try:
        reply(hb, ip, "player_1/a", lease=(0xB1, 4))
        t0 = hb._last_lease_ping_at[ip]
        hb.stream_pcm(SHORT_PCM, target="player_1")
        _wait_until(lambda: of(stream_log(hb, ip), protocol.CMD_STREAM_END),
                    timeout=SESSION_END_LINGER + 1.0)
        _wait_until(lambda: hb._stream_hub._thread is None)
        ended = time.monotonic()
        count = len(lease_pings(hb, ip))

        hb._keepalive_tick(now=t0 + 4.99)
        assert len(lease_pings(hb, ip)) == count             # not before TTL / 3
        hb._keepalive_tick(now=t0 + 5.0)
        assert len(lease_pings(hb, ip)) == count + 1
        hb._keepalive_tick(now=t0 + 9.99)
        assert len(lease_pings(hb, ip)) == count + 1
        hb._keepalive_tick(now=t0 + 10.0)
        assert len(lease_pings(hb, ip)) == count + 2

        until = hb._lease_wanted_until[ip]
        assert ended + LEASE_TTL - 0.5 <= until <= ended + LEASE_TTL
        hb._keepalive_tick(now=until - 0.01)                  # still within one TTL
        assert len(lease_pings(hb, ip)) == count + 3
        hb._keepalive_tick(now=until + 5.0)                   # one TTL after: stop
        assert len(lease_pings(hb, ip)) == count + 3

        with hb._devices_lock:
            hb._devices[ip].caps = DeviceCaps(lease_ttl_s=9)
            assert hb._lease_refresh_interval_locked(ip) == 3.0
    finally:
        hb.close()


def test_unknown_endpoint_is_asked_again_every_half_second(fake_client):  # noqa: F811
    hb = make_hb()
    ip = "192.0.2.10"
    try:
        discover(hb, ip, "player_1/a")
        hb.stream_pcm(LOOP_PCM, loop=True)
        first = hb._last_lease_ping_at[ip]
        count = len(lease_pings(hb, ip))
        hb._keepalive_tick(now=first + 0.49)
        assert len(lease_pings(hb, ip)) == count
        hb._keepalive_tick(now=first + 0.5)
        assert len(lease_pings(hb, ip)) == count + 1
    finally:
        hb.close()


# ── 9-10: lease_reissued, ticket change ─────────────────────────────
def test_superseded_deferral_clears_only_with_lease_reissued(fake_client):  # noqa: F811
    hb = make_hb()
    ip, address = "192.0.2.10", "player_1/chest"
    try:
        reply(hb, ip, address, lease=(0xB1, 4))
        playback = hb.stream_pcm(LOOP_PCM, loop=True)
        _wait_until(lambda: of(stream_log(hb, ip), protocol.CMD_STREAM_DATA))
        reply(hb, ip, address, lease=(0xB1, 4), flags=VALID | SUPERSEDED, high_water=5)
        assert playback.deferred_reason is DeferredReason.SUPERSEDED
        # our entry expired and we got a new ticket: still not ours to take
        reply(hb, ip, address, lease=(0xB1, 6), flags=VALID, high_water=5)
        assert playback.deferred_reason is DeferredReason.SUPERSEDED
        mark = len(stream_log(hb, ip))
        reply(hb, ip, address, lease=(0xB1, 7), flags=VALID | REISSUED, high_water=5)
        assert playback.status is PlaybackStatus.ACTIVE
        assert hb._stream_states[ip].lease_reissued
        begins = of(stream_log(hb, ip)[mark:], protocol.CMD_STREAM_BEGIN)
        assert begins and {b["identity"][:2] for b in begins} == {(0xB1, 7)}
    finally:
        hb.close()


def _session_data(log, identity):
    return [e for e in log if e["cmd"] == protocol.CMD_STREAM_DATA and e["identity"] == identity]


def test_ticket_change_continues_a_finite_source_from_its_cursor(fake_client):  # noqa: F811
    hb = make_hb()
    ip, address = "192.0.2.10", "player_1/chest"
    try:
        reply(hb, ip, address, lease=(0xB1, 4))
        hb.stream_pcm(RAMP_PCM)
        _wait_until(lambda: len(_session_data(stream_log(hb, ip), (0xB1, 4, 1))) >= 3)
        mark = len(stream_log(hb, ip))
        reply(hb, ip, address, lease=(0xB1, 5))  # same boot and incarnation, new ticket
        _wait_until(lambda: _session_data(stream_log(hb, ip), (0xB1, 5, 1)))
        log = stream_log(hb, ip)
        after = log[mark:]
        end = next(i for i, e in enumerate(after) if e["cmd"] == protocol.CMD_STREAM_END)
        assert after[end]["identity"] == (0xB1, 4, 1)
        assert after[end + 1]["cmd"] == protocol.CMD_STREAM_BEGIN
        assert after[end + 1]["identity"] == (0xB1, 5, 1)
        last_old = samples(_session_data(log, (0xB1, 4, 1))[-1])
        first_new = _session_data(log, (0xB1, 5, 1))[0]
        assert data_offset(first_new) == 0
        assert samples(first_new)[0] == last_old[-2] + 1 > 1   # the next frame, not frame 0
    finally:
        hb.close()


def test_device_reboot_restarts_a_finite_source_from_frame_zero(fake_client):  # noqa: F811
    hb = make_hb()
    ip, address = "192.0.2.10", "player_1/chest"
    try:
        reply(hb, ip, address, lease=(0xB1, 4))
        hb.stream_pcm(RAMP_PCM)
        _wait_until(lambda: len(_session_data(stream_log(hb, ip), (0xB1, 4, 1))) >= 3)
        reply(hb, ip, address, lease=(0xB2, 1))  # new boot
        _wait_until(lambda: _session_data(stream_log(hb, ip), (0xB2, 1, 1)))
        assert samples(_session_data(stream_log(hb, ip), (0xB2, 1, 1))[0])[0] == 0
    finally:
        hb.close()


# ── 11-12 + §7.11-12: StreamHub driven by a fake clock ──────────────
def test_begin_is_repeated_before_the_first_data_blocks_and_every_500_ms(clock):
    sink = DataSink()
    hub = StreamHub(sink, lambda: [V2_EP, GEN1_EP], send_ahead_sec=0.05,
                    mode_provider=modes_of({V2_EP: EndpointStreamMode(LEASE),
                                            GEN1_EP: LEGACY_STREAM_MODE}))
    hub.play_pcm(LOOP_PCM, loop=True, target="player_1")
    for _ in range(25):
        tick(hub)
        clock.t += 0.1
    events = sink.of(V2_EP)
    identities = {e[2] for e in events}
    assert len(identities) == 1                        # one session, repeated BEGIN
    data_at = [i for i, e in enumerate(events) if e[0] == "data"]
    assert len(data_at) > 3
    for index in data_at[:3]:
        assert events[index - 1][0] == "begin"
    # periodic: at least one more BEGIN in every 0.5 s of the 2.5 s run
    begins = sum(1 for e in events if e[0] == "begin")
    assert begins >= 1 + 3 + int(2.4 / 0.5)
    assert len(sink.of(GEN1_EP, "begin")) == 1         # Stream Gen 1: never repeated
    hub.close()


def test_begin_repeat_interval_is_at_most_500_ms(clock):
    times = []

    class TimedSink(DataSink):
        def stream_begin_v2(self, endpoint, identity):
            times.append(clock.t)

    hub = StreamHub(TimedSink(), lambda: [V2_EP], send_ahead_sec=0.05,
                    mode_provider=modes_of({V2_EP: EndpointStreamMode(LEASE)}))
    hub.play_pcm(LOOP_PCM, loop=True)
    for _ in range(40):
        tick(hub)
        clock.t += 0.05
    gaps = [b - a for a, b in zip(times, times[1:])]
    assert gaps and max(gaps) <= 0.5 + 1e-9
    hub.close()


def test_own_gap_of_3_s_begins_a_new_generation_before_more_data(clock):
    sink = DataSink()
    hub = StreamHub(sink, lambda: [V2_EP], send_ahead_sec=0.05,
                    mode_provider=modes_of({V2_EP: EndpointStreamMode(LEASE)}))
    hub.play_pcm(LOOP_PCM, loop=True)
    tick(hub)
    clock.t += 0.1
    tick(hub)
    mark = len(sink.events)
    clock.t += 3.5          # the process could not run
    tick(hub)
    after = sink.events[mark:]
    assert after[0][0] == "end" and after[0][2].generation == 1
    assert after[1][0] == "begin" and after[1][2].generation == 2
    data = [e for e in after if e[0] == "data"]
    assert data and {e[2].generation for e in data} == {2}
    assert data[0][3] == 0
    # no catch-up burst: only send-ahead worth of the new generation
    assert sum(len(e[4]) for e in data) <= (0.05 * 16000 + 256) * 4
    hub.close()


def test_resplice_brings_a_new_source_in_at_once_on_gen2(clock):
    ramp = struct.pack("<4096h", *range(4096))
    sink = DataSink()
    hub = StreamHub(sink, lambda: [V2_EP], send_ahead_sec=0.5,
                    mode_provider=modes_of({V2_EP: EndpointStreamMode(LEASE)}))
    hub.play_pcm(ramp, loop=True)
    tick(hub)
    clock.t += 0.1
    tick(hub)
    mark = len(sink.events)
    hub.play_pcm(struct.pack("<h", 10000) * 1600)        # 100 ms one-shot
    after = sink.events[mark:]
    assert [e[0] for e in after[:2]] == ["end", "begin"]
    assert after[0][2].generation == 1 and after[1][2] == hub._sessions[V2_EP].identity
    assert after[1][2].lease == LEASE and after[1][2].generation == 2
    tick(hub)
    data = [e for e in sink.events[mark:] if e[0] == "data"]
    assert data[0][3] == 0
    first = struct.unpack_from("<2h", data[0][4])
    # the loop continues from "now" (0.1 s = frame 1600), the one-shot from its start
    assert first == (1600 + 10000, 1600 + 10000)
    hub.close()


def test_no_resplice_with_a_short_queue_or_on_gen1(clock):
    for endpoint, mode, ahead in ((V2_EP, EndpointStreamMode(LEASE), 0.02),
                                  (GEN1_EP, LEGACY_STREAM_MODE, 0.5)):
        sink = DataSink()
        hub = StreamHub(sink, lambda endpoint=endpoint: [endpoint], send_ahead_sec=ahead,
                        mode_provider=modes_of({endpoint: mode}))
        hub.play_pcm(LOOP_PCM, loop=True)
        tick(hub)
        hub.play_pcm(SHORT_PCM)
        tick(hub)
        assert sink.of(endpoint, "end") == []
        assert len({e[2] for e in sink.of(endpoint, "begin")}) == 1
        hub.close()


def test_mono_endpoint_ignores_pan(clock):
    for mono, expected_right in ((True, 1000), (False, 0)):
        sink = DataSink()
        hub = StreamHub(sink, lambda: [V2_EP], send_ahead_sec=0.05,
                        mode_provider=modes_of({V2_EP: EndpointStreamMode(LEASE, mono=mono)}))
        hub.play_pcm(LOOP_PCM, loop=True, pan=-1.0)
        tick(hub)
        data = sink.of(V2_EP, "data")[0][4]
        pcm = struct.unpack(f"<{len(data) // 2}h", data)
        assert set(pcm[0::2]) == {1000}
        assert set(pcm[1::2]) == {expected_right}
        hub.close()


def test_mono_comes_from_caps_haptic_channels(fake_client):  # noqa: F811
    hb = make_hb()
    ip = "192.0.2.10"
    try:
        reply(hb, ip, "player_1/a", lease=(0xB1, 4))
        endpoint = StreamEndpoint(ip, "player_1/a")
        assert hb._stream_mode(endpoint) == EndpointStreamMode(StreamLease(0xB1, 4))
        with hb._devices_lock:
            hb._devices[ip].caps = DeviceCaps(haptic_channels=1)
        assert hb._stream_mode(endpoint) == EndpointStreamMode(StreamLease(0xB1, 4), mono=True)
    finally:
        hb.close()


# ── 13-14: ERROR ────────────────────────────────────────────────────
def _streaming(hb, ip="192.0.2.10", address="player_1/chest", lease=(0xB1, 4), pcm=LOOP_PCM):
    reply(hb, ip, address, lease=lease)
    playback = hb.stream_pcm(pcm, loop=pcm is LOOP_PCM)
    _wait_until(lambda: of(stream_log(hb, ip), protocol.CMD_STREAM_DATA))
    return playback


def test_error_is_accepted_only_from_the_device_port_and_tracked_session(fake_client):  # noqa: F811
    hb = make_hb()
    ip = "192.0.2.10"
    try:
        playback = _streaming(hb)
        good = envelope(current_identity(hb, ip))
        wrong = envelope(current_identity(hb, ip)[:2] + (99,))
        send_error(hb, ip, protocol.ERR_STREAM_SUPERSEDED, good, port=7701)
        send_error(hb, "192.0.2.99", protocol.ERR_STREAM_SUPERSEDED, good)
        send_error(hb, ip, protocol.ERR_STREAM_SUPERSEDED, wrong)
        send_error(hb, ip, protocol.ERR_STREAM_SUPERSEDED, b"")
        assert playback.status is PlaybackStatus.ACTIVE
        assert not hb._stream_states[ip].lease_superseded
    finally:
        hb.close()


def test_lease_invalid_renews_and_continues_from_the_cursor(fake_client):  # noqa: F811
    hb = make_hb()
    ip, address = "192.0.2.10", "player_1/chest"
    try:
        playback = _streaming(hb, pcm=RAMP_PCM)
        _wait_until(lambda: len(_session_data(stream_log(hb, ip), (0xB1, 4, 1))) >= 3)
        pings = len(lease_pings(hb, ip))
        send_error(hb, ip, protocol.ERR_STREAM_LEASE_INVALID, envelope((0xB1, 4, 1)))
        assert playback.deferred_reason is DeferredReason.LEASE_UNAVAILABLE
        assert len(lease_pings(hb, ip)) == pings + 1        # renewed at once
        log = stream_log(hb, ip)
        assert log[-1]["cmd"] == protocol.CMD_STREAM_END and log[-1]["identity"] == (0xB1, 4, 1)
        last_old = samples(_session_data(log, (0xB1, 4, 1))[-1])
        reply(hb, ip, address, lease=(0xB1, 5))
        assert playback.status is PlaybackStatus.ACTIVE
        _wait_until(lambda: _session_data(stream_log(hb, ip), (0xB1, 5, 1)))
        first_new = _session_data(stream_log(hb, ip), (0xB1, 5, 1))[0]
        assert data_offset(first_new) == 0
        assert samples(first_new)[0] == last_old[-2] + 1
    finally:
        hb.close()


def test_superseded_error_defers_without_taking_back(fake_client):  # noqa: F811
    hb = make_hb()
    ip = "192.0.2.10"
    try:
        playback = _streaming(hb)
        send_error(hb, ip, protocol.ERR_STREAM_SUPERSEDED, envelope(current_identity(hb, ip)))
        assert playback.deferred_reason is DeferredReason.SUPERSEDED
        mark = len(stream_log(hb, ip))
        time.sleep(0.1)
        assert of(stream_log(hb, ip)[mark:], protocol.CMD_STREAM_BEGIN) == []
        assert of(stream_log(hb, ip)[mark:], protocol.CMD_STREAM_DATA) == []
    finally:
        hb.close()


def test_not_started_resends_the_same_begin_at_once(fake_client):  # noqa: F811
    hb = make_hb()
    ip = "192.0.2.10"
    try:
        _streaming(hb)
        identity = current_identity(hb, ip)
        session = hb._stream_hub._sessions[StreamEndpoint(ip, "player_1/chest")]
        session.last_begin_at = 0.0
        before = time.perf_counter()
        send_error(hb, ip, protocol.ERR_STREAM_NOT_STARTED, envelope(identity))
        assert session.last_begin_at >= before
        assert current_identity(hb, ip) == identity
    finally:
        hb.close()


def test_timed_out_begins_the_next_generation_once(fake_client):  # noqa: F811
    hb = make_hb()
    ip = "192.0.2.10"
    try:
        _streaming(hb)
        old = current_identity(hb, ip)
        send_error(hb, ip, protocol.ERR_STREAM_TIMED_OUT, envelope(old))
        new = current_identity(hb, ip)
        assert new == old[:2] + (old[2] + 1,)
        assert (protocol.CMD_STREAM_END, old) in [(e["cmd"], e["identity"]) for e in stream_log(hb, ip)]
        # the device repeats the error for the old session (1 per second): no second restart
        send_error(hb, ip, protocol.ERR_STREAM_TIMED_OUT, envelope(old))
        assert current_identity(hb, ip) == new
    finally:
        hb.close()


def test_stopped_takes_sources_off_that_endpoint_for_good(fake_client):  # noqa: F811
    hb = make_hb()
    a, b = "192.0.2.10", "192.0.2.11"
    try:
        reply(hb, a, "player_1/a", lease=(0xA1, 1))
        reply(hb, b, "player_1/b", lease=(0xB1, 1))
        playback = hb.stream_pcm(LOOP_PCM, loop=True, target="player_1")
        _wait_until(lambda: of(stream_log(hb, a), protocol.CMD_STREAM_DATA)
                    and of(stream_log(hb, b), protocol.CMD_STREAM_DATA))
        send_error(hb, a, protocol.ERR_STREAM_STOPPED, envelope(current_identity(hb, a)))
        _wait_until(lambda: of(stream_log(hb, a), protocol.CMD_STREAM_END),
                    timeout=SESSION_END_LINGER + 1.0)
        mark_a, mark_b = len(stream_log(hb, a)), len(stream_log(hb, b))
        time.sleep(0.1)
        assert playback.status is PlaybackStatus.ACTIVE          # still playing on b
        assert stream_log(hb, a)[mark_a:] == []                   # never rejoins a
        assert of(stream_log(hb, b)[mark_b:], protocol.CMD_STREAM_DATA)
        fresh = hb.stream_pcm(LOOP_PCM, loop=True, target="player_1")
        assert of(stream_log(hb, a)[mark_a:], protocol.CMD_STREAM_BEGIN)  # a new Playback joins
        fresh.stop()
        playback.stop()
    finally:
        hb.close()


def test_stopped_on_its_only_endpoint_stops_a_loop_and_completes_a_one_shot(fake_client):  # noqa: F811
    for pcm in (LOOP_PCM, RAMP_PCM):
        hb = make_hb()
        ip = "192.0.2.10"
        try:
            playback = _streaming(hb, pcm=pcm)
            send_error(hb, ip, protocol.ERR_STREAM_STOPPED, envelope(current_identity(hb, ip)))
            assert playback.status is PlaybackStatus.STOPPED
        finally:
            hb.close()


def test_gen_unsupported_reclassifies_a_gen1_endpoint(fake_client):  # noqa: F811
    hb = make_hb()
    ip, address = "192.0.2.10", "player_1/chest"
    try:
        reply(hb, ip, address)                                   # Stream Gen 1
        playback = hb.stream_pcm(LOOP_PCM, loop=True)
        _wait_until(lambda: of(stream_log(hb, ip), protocol.CMD_STREAM_DATA))
        pings = len(lease_pings(hb, ip))
        send_error(hb, ip, protocol.ERR_STREAM_GEN_UNSUPPORTED)
        assert hb.devices[0].stream_class is EndpointClass.UNKNOWN
        assert playback.deferred_reason is DeferredReason.UNKNOWN_CLASS
        assert len(lease_pings(hb, ip)) == pings + 1
        end = stream_log(hb, ip)[-1]
        assert end["cmd"] == protocol.CMD_STREAM_END and end["version"] == protocol.VERSION
        reply(hb, ip, address, lease=(0xB1, 1))                  # updated firmware
        assert playback.status is PlaybackStatus.ACTIVE
    finally:
        hb.close()


def test_gen_unsupported_without_a_gen1_session_is_ignored(fake_client):  # noqa: F811
    hb = make_hb()
    ip = "192.0.2.10"
    try:
        playback = _streaming(hb)
        send_error(hb, ip, protocol.ERR_STREAM_GEN_UNSUPPORTED)
        assert hb.devices[0].stream_class is EndpointClass.GEN2
        assert playback.status is PlaybackStatus.ACTIVE
    finally:
        hb.close()


def test_target_mismatch_ends_the_session_and_re_resolves(fake_client):  # noqa: F811
    hb = make_hb()
    ip, address = "192.0.2.10", "player_1/chest"
    try:
        playback = _streaming(hb)
        identity = current_identity(hb, ip)
        pings = len(lease_pings(hb, ip))
        send_error(hb, ip, protocol.ERR_STREAM_TARGET_MISMATCH, envelope(identity))
        assert hb.devices[0].address == ""
        assert playback.deferred_reason is DeferredReason.NO_RESOLVED_ENDPOINT
        assert len(lease_pings(hb, ip)) == pings + 1
        assert (protocol.CMD_STREAM_END, identity) in [(e["cmd"], e["identity"]) for e in stream_log(hb, ip)]
        reply(hb, ip, address, lease=(0xB1, 4))
        assert playback.status is PlaybackStatus.ACTIVE
        assert current_identity(hb, ip) == (0xB1, 4, identity[2] + 1)
    finally:
        hb.close()


def test_unknown_command_for_caps_req_means_no_capabilities(fake_client):  # noqa: F811
    hb = make_hb()
    ip = "192.0.2.10"
    try:
        discover(hb, ip, "player_1/a")
        send_error(hb, ip, protocol.ERR_UNKNOWN_COMMAND, ref_command=protocol.CMD_CAPS_REQ)
        assert hb.devices[0].caps is DeviceCaps.NONE
        assert ip not in hb._caps_requests
    finally:
        hb.close()


# ── 15: Deferred reasons ────────────────────────────────────────────
def test_deferred_reasons(fake_client):  # noqa: F811
    hb = make_hb()
    try:
        playback = hb.stream_pcm(LOOP_PCM, loop=True, target="player_1")
        assert playback.deferred_reason is DeferredReason.NO_RESOLVED_ENDPOINT
        discover(hb, "192.0.2.10", "player_1/a")
        assert playback.deferred_reason is DeferredReason.UNKNOWN_CLASS
        reply(hb, "192.0.2.10", "player_1/a", lease=(0xB1, 1), flags=0)
        assert playback.deferred_reason is DeferredReason.LEASE_UNAVAILABLE
        discover(hb, "192.0.2.11", "player_1/b")                 # unknown, lower priority
        assert playback.deferred_reason is DeferredReason.LEASE_UNAVAILABLE
        reply(hb, "192.0.2.12", "player_1/c", lease=(0xC1, 1), flags=VALID | SUPERSEDED)
        assert playback.deferred_reason is DeferredReason.SUPERSEDED
        reply(hb, "192.0.2.11", "player_1/b")                     # one endpoint plays
        assert playback.status is PlaybackStatus.ACTIVE
        assert playback.deferred_reason is None
    finally:
        hb.close()


def test_stream_endpoints_and_change_listener(fake_client):  # noqa: F811
    hb = make_hb()
    ip, address = "192.0.2.10", "player_1/a"
    seen = []
    hb.add_stream_endpoint_listener(seen.append)
    try:
        discover(hb, ip, address)
        reply(hb, ip, address, lease=(0xB1, 1))
        reply(hb, ip, address, lease=(0xB1, 1), flags=VALID | SUPERSEDED)
        info = hb.stream_endpoints()[0]
        assert (info.ip, info.address, info.stream_class, info.lease_state) == (
            ip, address, EndpointClass.GEN2, LeaseState.SUPERSEDED)
        assert [(i.stream_class, i.lease_state) for i in seen] == [
            (EndpointClass.UNKNOWN, LeaseState.NONE),
            (EndpointClass.GEN2, LeaseState.VALID),
            (EndpointClass.GEN2, LeaseState.SUPERSEDED),
        ]
    finally:
        hb.close()


# ── 16-17: CAPS client ──────────────────────────────────────────────
def caps_payload(generation: int, haptic_channels: int = 2) -> bytes:
    return bytes([1, 0, 0x01, 1, generation, 0x05, 4, 15, 32, 5, 0,
                  0x06, 1, haptic_channels])


def test_caps_request_retries_then_gives_up(fake_client):  # noqa: F811
    hb = make_hb()
    ip = "192.0.2.10"
    try:
        discover(hb, ip, "player_1/a")
        requests = sent_to(hb, ip, protocol.CMD_CAPS_REQ)
        assert len(requests) == 1 and requests[0]["payload"] == bytes([protocol.CAPS_VERSION])
        assert not [p for p in hb._client.discovery_sent if decode(p)["cmd"] == protocol.CMD_CAPS_REQ]
        start = hb._caps_requests[ip].sent_at
        hb._keepalive_tick(now=start + 0.49)
        assert len(sent_to(hb, ip, protocol.CMD_CAPS_REQ)) == 1
        hb._keepalive_tick(now=start + 0.5)
        hb._keepalive_tick(now=start + 1.0)
        assert len(sent_to(hb, ip, protocol.CMD_CAPS_REQ)) == 3
        assert hb.devices[0].caps is None
        hb._keepalive_tick(now=start + 1.5)
        assert hb.devices[0].caps is DeviceCaps.NONE
        discover(hb, ip, "player_1/a")                           # same boot: not again
        hb._keepalive_tick(now=start + 3.0)
        assert len(sent_to(hb, ip, protocol.CMD_CAPS_REQ)) == 3
        discover(hb, ip, "player_1/a", firmware="v2")            # firmware changed
        assert len(sent_to(hb, ip, protocol.CMD_CAPS_REQ)) == 4
    finally:
        hb.close()


def test_caps_answer_and_newer_generation_warns_once(fake_client, caplog):  # noqa: F811
    hb = make_hb()
    try:
        with caplog.at_level(logging.WARNING, logger="hapbeat"):
            for ip in ("192.0.2.10", "192.0.2.11"):
                discover(hb, ip, f"player_1/{ip[-2:]}")
                seq = hb._caps_requests[ip].seq
                hb._on_packet(protocol.CMD_CAPS, (seq + 1) & 0xFFFF, caps_payload(2), (ip, 7700))
                assert next(d for d in hb.devices if d.ip == ip).caps is None   # wrong seq
                hb._on_packet(protocol.CMD_CAPS, seq, caps_payload(2), (ip, 7700))
                caps = next(d for d in hb.devices if d.ip == ip).caps
                assert (caps.generation, caps.lease_ttl_s, caps.haptic_channels) == (2, 15, 2)
        warnings = [r for r in caplog.records if "newer than this SDK" in r.getMessage()]
        assert len(warnings) == 1
    finally:
        hb.close()


def test_sdk_major_matches_the_package_version():
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    version = re.search(r'^version = "([^"]+)"', text, re.M).group(1)
    assert int(version.split(".")[0]) == protocol.SDK_MAJOR


# ── 18: generation exhaustion ───────────────────────────────────────
def test_generation_exhaustion_requests_a_new_incarnation(fake_client):  # noqa: F811
    hb = make_hb()
    ip, address = "192.0.2.10", "player_1/chest"
    try:
        reply(hb, ip, address, lease=(0xB1, 4))
        hb._stream_hub._last_generation[StreamLease(0xB1, 4)] = MAX_GENERATION - 1
        hb.stream_pcm(SHORT_PCM)
        assert current_identity(hb, ip) == (0xB1, 4, MAX_GENERATION)
        _wait_until(lambda: of(stream_log(hb, ip), protocol.CMD_STREAM_END),
                    timeout=SESSION_END_LINGER + 1.0)
        old_incarnation = hb._client_incarnation
        playback = hb.stream_pcm(LOOP_PCM, loop=True)
        assert hb._client_incarnation not in (0, old_incarnation)
        ping = lease_pings(hb, ip)[-1]
        assert struct.unpack("<qQ", ping["payload"])[1] == hb._client_incarnation
        assert playback.status is PlaybackStatus.DEFERRED
        reply(hb, ip, address, lease=(0xB1, 9))                  # a new ticket
        assert playback.status is PlaybackStatus.ACTIVE
        assert current_identity(hb, ip) == (0xB1, 9, 1)
    finally:
        hb.close()
