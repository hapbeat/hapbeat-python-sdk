"""Per-device stream class and lease (contracts stream-session.md).

Pure decision logic, no sockets: which wire format a device endpoint gets and
under which device-issued lease. A device is classified only from a *matched*
direct reply to this SDK's own extended (16-byte, unicast) PING:

- valid HBS2 tail        -> Stream Gen 2, streamed under the lease it reports;
- nothing after volume_wiper -> Stream Gen 1 (firmware before Stream Gen 2):
  Stream Gen 1 packets + 300 ms guard;
- anything else          -> ignored (no class or lease change).

Unsolicited, unmatched and late PONGs, and replies to the 8-byte discovery
PING, never change the class. The rules mirror Unity
``HapbeatClient.EvaluateStreamLeasePong`` and Unreal
``HapbeatStreamSessionContract::EvaluatePongLease``.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Mapping, Optional

from . import protocol

# A broadcast PING is answered by every device, so a pending request is not
# consumed by its first reply; it ages out instead (same window as Unity/Unreal).
PENDING_PING_MAX_AGE = 15.0
# A non-active device lease expires after LEASE_TTL without an extended PING;
# a held lease is refreshed at least every TTL / 3 (CAPS lease_ttl_s / 3 when
# the device reports it, LEASE_REFRESH_INTERVAL otherwise).
LEASE_TTL = 15.0
LEASE_REFRESH_INTERVAL = 5.0
# While an endpoint has no usable lease yet (unknown class, lease invalid), the
# extended PING is retried this often.
LEASE_RETRY_INTERVAL = 0.5


@dataclass(frozen=True)
class StreamLease:
    """A device-issued lease: the device boot plus the ticket it issued us."""

    boot_id: int
    ticket: int

    @property
    def valid(self) -> bool:
        return self.boot_id != 0 and self.ticket != 0


@dataclass(frozen=True)
class StreamSessionIdentity:
    """The Stream Gen 2 envelope of one endpoint session: lease + generation."""

    lease: StreamLease
    generation: int

    @property
    def envelope(self) -> bytes:
        return protocol.build_stream_envelope(
            self.lease.boot_id, self.lease.ticket, self.generation
        )


class EndpointClass(enum.Enum):
    UNKNOWN = "Unknown"  # no matched reply yet: streams defer
    GEN2 = "Gen2"        # matched reply carried a valid HBS2 tail
    GEN1 = "Gen1"        # matched reply ended at volume_wiper (older firmware)


@dataclass(frozen=True)
class PendingPing:
    """One outstanding PING, kept until aged out (see PENDING_PING_MAX_AGE)."""

    timestamp_us: int
    client_incarnation: int
    sent_at: float  # time.monotonic()
    # 16-byte lease request; an 8-byte discovery PING never classifies
    extended: bool = False
    # unicast destination; None for a broadcast PING
    ip: Optional[str] = None


@dataclass(frozen=True)
class EndpointLeaseState:
    endpoint_class: EndpointClass = EndpointClass.UNKNOWN
    # GEN2 only: the lease the device issued for ``client_incarnation``
    lease: StreamLease = StreamLease(0, 0)
    lease_valid: bool = False
    lease_superseded: bool = False
    # the last accepted reply carried lease_reissued (diagnostics only)
    lease_reissued: bool = False
    # incarnation of the PING whose matched reply produced this state
    client_incarnation: int = 0
    # echoed timestamp of the last accepted matched reply, across both classes:
    # a reply to an older-or-equal request never changes class or lease again
    last_accepted_timestamp_us: int = 0

    def usable_gen2_lease(self, current_incarnation: int) -> Optional[StreamLease]:
        """The lease Stream Gen 2 BEGIN/DATA/END may carry now, or None."""
        if (
            self.endpoint_class is EndpointClass.GEN2
            and self.lease_valid
            and not self.lease_superseded
            and self.client_incarnation == current_incarnation
            and self.lease.valid
        ):
            return self.lease
        return None


class PongLeaseDecision(enum.Enum):
    # ordinary discovery only: liveness / address update, class and lease kept.
    # Unsolicited (timestamp 0), unmatched, late and ignored-tail replies.
    NO_CHANGE = "NoChange"
    # drop the whole PONG: a structurally valid HBS2 reply whose correlation
    # fails (retired request / previous incarnation). It must not roll the
    # device's address or liveness back either.
    DISCARD = "Discard"
    # matched direct reply: the returned state replaces the endpoint's
    ACCEPT = "Accept"


def matched_pending(
    pong: Mapping, pending_pings: Mapping[int, PendingPing], source_ip: str
) -> Optional[PendingPing]:
    """The outstanding PING ``pong`` directly answers (same seq and echoed
    timestamp, from the unicast destination), or None. Unsolicited PONGs carry
    timestamp 0 and never match."""
    timestamp = int(pong.get("timestamp", 0))
    if timestamp == 0:
        return None
    pending = pending_pings.get(pong.get("seq", -1))
    if pending is None or pending.timestamp_us != timestamp:
        return None
    if pending.ip is not None and pending.ip != source_ip:
        return None
    return pending


def evaluate_pong_lease(
    pong: Mapping,
    pending_pings: Mapping[int, PendingPing],
    current_incarnation: int,
    existing: Optional[EndpointLeaseState],
    source_ip: str,
) -> tuple[PongLeaseDecision, Optional[EndpointLeaseState]]:
    """Decide what a received PONG from ``source_ip`` may do to its endpoint's
    class / lease.

    ``pong`` is a :func:`hapbeat.protocol.parse_pong` dict; missing keys are
    treated as an unsolicited identity PONG.
    """
    tail = pong.get("lease_tail", protocol.LEASE_TAIL_ABSENT)
    if tail == protocol.LEASE_TAIL_IGNORED:
        # not a lease reply this SDK can read, and not proof of Stream Gen 1
        return PongLeaseDecision.NO_CHANGE, None

    timestamp = int(pong.get("timestamp", 0))
    pending = pending_pings.get(pong.get("seq", -1))
    if pending is not None and pending.ip is not None and pending.ip != source_ip:
        # a unicast request answered from another address: not its reply
        return PongLeaseDecision.NO_CHANGE, None

    if tail == protocol.LEASE_TAIL_VALID:
        echoed = pong["echoed_client_incarnation"]
        # HBS2 is only valid on a direct reply to the active incarnation
        if current_incarnation == 0 or echoed != current_incarnation:
            return PongLeaseDecision.DISCARD, None
        if (
            timestamp == 0
            or pending is None
            or not pending.extended
            or pending.timestamp_us != timestamp
            or pending.client_incarnation != echoed
        ):
            return PongLeaseDecision.DISCARD, None
        if existing is not None and timestamp <= existing.last_accepted_timestamp_us:
            # older than an accepted reply: never roll back to an older boot/ticket
            return PongLeaseDecision.DISCARD, None

        valid = bool(pong["lease_valid"])
        superseded = bool(pong["lease_superseded"])
        reissued = bool(pong.get("lease_reissued", False))
        # A superseded writer must not turn a later periodic reply into an
        # implicit takeover. Only a fresh incarnation, or a ticket the device
        # re-issued to us (lease_reissued: the other writer is gone or it is
        # our own earlier ticket), clears the latch.
        keep_superseded = (
            existing is not None
            and existing.endpoint_class is EndpointClass.GEN2
            and existing.lease_superseded
            and existing.client_incarnation == pending.client_incarnation
            and not (valid and not superseded and reissued)
        )
        if keep_superseded:
            lease = existing.lease
            valid = False
            superseded = True
        else:
            lease = StreamLease(pong["device_boot_id"], pong["lease_ticket"])
        return PongLeaseDecision.ACCEPT, EndpointLeaseState(
            endpoint_class=EndpointClass.GEN2,
            lease=lease,
            lease_valid=valid,
            lease_superseded=superseded,
            lease_reissued=reissued,
            client_incarnation=pending.client_incarnation,
            last_accepted_timestamp_us=timestamp,
        )

    # Nothing after volume_wiper. Older firmware cannot echo the incarnation,
    # so the request itself must be an extended PING of the current incarnation
    # (a reply to the 8-byte discovery PING never has a tail).
    if timestamp == 0:
        return PongLeaseDecision.NO_CHANGE, None  # unsolicited identity PONG
    if (
        current_incarnation == 0
        or pending is None
        or not pending.extended
        or pending.timestamp_us != timestamp
        or pending.client_incarnation != current_incarnation
    ):
        return PongLeaseDecision.NO_CHANGE, None  # reply to some other request
    if existing is not None and timestamp <= existing.last_accepted_timestamp_us:
        return PongLeaseDecision.NO_CHANGE, None  # late reply
    return PongLeaseDecision.ACCEPT, EndpointLeaseState(
        endpoint_class=EndpointClass.GEN1,
        client_incarnation=pending.client_incarnation,
        last_accepted_timestamp_us=timestamp,
    )
