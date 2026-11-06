"""stop_all() then a new stream, and a new session under an old lease (1.0.1).

- STOP_ALL ends every matching device's session, so the SDK ends its own
  there at once instead of letting it linger: a source started right after
  must begin a new generation, not send DATA the device rejects with
  STREAM_STOPPED (sdk-multi-stream.md §6.13).
- The device lets an unused lease expire one TTL after its last extended PING:
  a new session waits for a refreshed lease instead of a BEGIN the device would
  answer with STREAM_LEASE_INVALID. A running session is not held.

Fake transport only: no sockets, no live haptics."""

from __future__ import annotations

import time

import pytest

from hapbeat import DeferredReason, PlaybackStatus, protocol
from hapbeat.stream_lease import LEASE_TTL

from .pong_helpers import reply
from .test_hapbeat import fake_client  # noqa: F401 (pytest fixture)
from .test_stream_session_v2 import LOOP_PCM, _wait_until, decode, make_hb, of, stream_log
from .test_wire_1x import (
    RAMP_PCM,
    current_identity,
    discover,
    envelope,
    lease_pings,
    samples,
    send_error,
)

IP = "192.0.2.10"
ADDRESS = "player_1/chest"


def _streaming(hb, pcm=LOOP_PCM, target=""):
    playback = hb.stream_pcm(pcm, loop=pcm is LOOP_PCM, target=target)
    _wait_until(lambda: of(stream_log(hb, IP), protocol.CMD_STREAM_DATA))
    return playback


@pytest.fixture
def later(monkeypatch):
    """Move the SDK's device / lease clock (time.monotonic) forward by hand."""
    real = time.monotonic
    offset = [0.0]
    monkeypatch.setattr(time, "monotonic", lambda: real() + offset[0])

    def advance(seconds: float) -> None:
        offset[0] += seconds

    return advance


# ── stop_all() then a new stream ────────────────────────────────────
def test_stop_all_then_a_new_stream_begins_a_new_generation(fake_client):  # noqa: F811
    hb = make_hb()
    try:
        reply(hb, IP, ADDRESS, lease=(0xB1, 4))
        first = _streaming(hb)
        old = current_identity(hb, IP)
        hb.stop_all()
        assert first.status is PlaybackStatus.STOPPED
        sent = [decode(packet) for packet, addr in hb._client.sent if addr == IP]
        cmds = [(entry["cmd"], entry["identity"]) for entry in sent]
        # END at once (no linger), before the STOP_ALL
        end_at = cmds.index((protocol.CMD_STREAM_END, old))
        stop_at = max(i for i, (cmd, _) in enumerate(cmds) if cmd == protocol.CMD_STOP_ALL)
        assert end_at < stop_at

        mark = len(stream_log(hb, IP))
        second = hb.stream_pcm(RAMP_PCM)
        assert second.status is PlaybackStatus.ACTIVE
        begins = of(stream_log(hb, IP)[mark:], protocol.CMD_STREAM_BEGIN)
        assert begins and begins[0]["identity"] == old[:2] + (old[2] + 1,)
        _wait_until(lambda: of(stream_log(hb, IP)[mark:], protocol.CMD_STREAM_DATA))
        data = of(stream_log(hb, IP)[mark:], protocol.CMD_STREAM_DATA)
        assert {entry["identity"] for entry in data} == {begins[0]["identity"]}
        assert samples(data[0])[0] == 0  # from frame 0

        # the device rejects a late DATA of the stopped generation: that is not
        # about the new Playback, which keeps playing
        send_error(hb, IP, protocol.ERR_STREAM_STOPPED, envelope(old))
        assert second.status is PlaybackStatus.ACTIVE
    finally:
        hb.close()


def test_stop_all_with_a_target_takes_other_sources_off_that_device(fake_client):  # noqa: F811
    hb = make_hb()
    other = "192.0.2.11"
    try:
        reply(hb, IP, ADDRESS, lease=(0xB1, 4))
        reply(hb, other, "player_2/chest", lease=(0xC1, 1))
        everywhere = _streaming(hb)                       # target "" matches both
        _wait_until(lambda: of(stream_log(hb, other), protocol.CMD_STREAM_DATA))
        old = current_identity(hb, IP)
        hb.stop_all(target="player_1")
        # the device stopped it, so it does not rejoin there; it plays on
        assert (protocol.CMD_STREAM_END, old) in [
            (entry["cmd"], entry["identity"]) for entry in stream_log(hb, IP)
        ]
        mark_a, mark_b = len(stream_log(hb, IP)), len(stream_log(hb, other))
        time.sleep(0.1)
        assert everywhere.status is PlaybackStatus.ACTIVE
        assert stream_log(hb, IP)[mark_a:] == []
        assert of(stream_log(hb, other)[mark_b:], protocol.CMD_STREAM_DATA)
        # a new Playback joins player_1 under the next generation
        fresh = hb.stream_pcm(LOOP_PCM, loop=True, target="player_1")
        begins = of(stream_log(hb, IP)[mark_a:], protocol.CMD_STREAM_BEGIN)
        assert begins and begins[0]["identity"] == old[:2] + (old[2] + 1,)
        fresh.stop()
        everywhere.stop()
    finally:
        hb.close()


# ── a new session under an old lease ────────────────────────────────
def test_new_session_waits_for_a_lease_refresh_after_the_ttl(fake_client, later):  # noqa: F811
    hb = make_hb()
    try:
        reply(hb, IP, ADDRESS, lease=(0xB1, 4))
        later(20.0)                              # idle 20 s: no lease PING
        discover(hb, IP, ADDRESS)                # discovery keeps the device alive
        pings = len(lease_pings(hb, IP))
        playback = hb.stream_pcm(RAMP_PCM)
        assert playback.status is PlaybackStatus.DEFERRED
        assert playback.deferred_reason is DeferredReason.LEASE_UNAVAILABLE
        assert of(stream_log(hb, IP), protocol.CMD_STREAM_BEGIN) == []
        assert len(lease_pings(hb, IP)) == pings + 1        # asked at once
        reply(hb, IP, ADDRESS, lease=(0xB1, 5))             # the refreshed lease
        assert playback.status is PlaybackStatus.ACTIVE
        begins = of(stream_log(hb, IP), protocol.CMD_STREAM_BEGIN)
        assert {entry["identity"] for entry in begins} == {(0xB1, 5, 1)}
        _wait_until(lambda: of(stream_log(hb, IP), protocol.CMD_STREAM_DATA))
        first = of(stream_log(hb, IP), protocol.CMD_STREAM_DATA)[0]
        assert samples(first)[0] == 0                       # the start is not skipped
    finally:
        hb.close()


def test_lease_confirmed_within_the_ttl_begins_at_once(fake_client, later):  # noqa: F811
    hb = make_hb()
    try:
        reply(hb, IP, ADDRESS, lease=(0xB1, 4))
        later(LEASE_TTL - 2.0)
        discover(hb, IP, ADDRESS)
        playback = hb.stream_pcm(LOOP_PCM, loop=True)
        assert playback.status is PlaybackStatus.ACTIVE
        assert current_identity(hb, IP) == (0xB1, 4, 1)
        playback.stop()
    finally:
        hb.close()


def test_running_session_is_not_held_by_an_old_lease(fake_client, later):  # noqa: F811
    hb = make_hb()
    try:
        reply(hb, IP, ADDRESS, lease=(0xB1, 4))
        playback = _streaming(hb)
        identity = current_identity(hb, IP)
        later(20.0)                              # the lease replies were lost
        discover(hb, IP, ADDRESS)
        mark = len(stream_log(hb, IP))
        hb._stream_hub.reconcile()
        _wait_until(lambda: of(stream_log(hb, IP)[mark:], protocol.CMD_STREAM_DATA))
        later_packets = stream_log(hb, IP)[mark:]
        assert of(later_packets, protocol.CMD_STREAM_END) == []
        assert {entry["identity"] for entry in later_packets} == {identity}
        assert playback.status is PlaybackStatus.ACTIVE
        playback.stop()
    finally:
        hb.close()


def test_old_lease_is_asked_again_every_half_second(fake_client, later):  # noqa: F811
    hb = make_hb()
    try:
        reply(hb, IP, ADDRESS, lease=(0xB1, 4))
        hb._on_endpoint_wanted(hb._stream_endpoints()[0])   # held for one TTL
        last = hb._last_lease_ping_at[IP]
        count = len(lease_pings(hb, IP))
        hb._keepalive_tick(now=last + 0.5)                  # fresh: every TTL / 3
        assert len(lease_pings(hb, IP)) == count
        later(20.0)                                         # the replies were lost
        discover(hb, IP, ADDRESS)
        hb._on_endpoint_wanted(hb._stream_endpoints()[0])   # a new source wants it
        last = hb._last_lease_ping_at[IP]
        count = len(lease_pings(hb, IP))
        hb._keepalive_tick(now=last + 0.5)
        assert len(lease_pings(hb, IP)) == count + 1
    finally:
        hb.close()
