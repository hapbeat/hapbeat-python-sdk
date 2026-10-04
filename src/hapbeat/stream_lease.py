"""Per-device stream class and lease (contracts stream-session-v2.md).

Pure decision logic, no sockets: which wire format a device endpoint gets and
under which device-issued lease. A device is classified only from a *matched*
direct reply to this SDK's own extended PING:

- valid HBS2 tail       -> v2, streamed under the lease it reports;
- no HBS2 marker        -> legacy (pre-v2 firmware): v1 stream + 300 ms guard;
- malformed HBS2 marker -> ignored (no class or lease change).

Unsolicited, unmatched and late PONGs never change the class. The rules mirror
Unity ``HapbeatClient.EvaluateStreamLeasePong`` and Unreal
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
# While any device streams v2, keep-alive PINGs refresh its lease at least this
# often: a non-active device lease expires after 15 s without PING.
V2_LEASE_REFRESH_INTERVAL = 5.0


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
    """The v2 envelope of one endpoint session: lease + sender generation."""

    lease: StreamLease
    generation: int


class EndpointClass(enum.Enum):
    UNKNOWN = "Unknown"  # no matched reply yet: streams defer
    V2 = "V2"            # matched reply carried a valid HBS2 tail
    LEGACY = "Legacy"    # matched reply carried no HBS2 marker (pre-v2 firmware)


@dataclass(frozen=True)
class PendingPing:
    """One outstanding PING, kept until aged out (see PENDING_PING_MAX_AGE)."""

    timestamp_us: int
    client_incarnation: int
    sent_at: float  # time.monotonic()


@dataclass(frozen=True)
class EndpointLeaseState:
    endpoint_class: EndpointClass = EndpointClass.UNKNOWN
    # V2 only: the lease the device issued for ``client_incarnation``
    lease: StreamLease = StreamLease(0, 0)
    lease_valid: bool = False
    lease_superseded: bool = False
    # incarnation of the PING whose matched reply produced this state
    client_incarnation: int = 0
    # echoed timestamp of the last accepted matched reply, across both classes:
    # a reply to an older-or-equal request never changes class or lease again
    last_accepted_timestamp_us: int = 0

    def usable_v2_lease(self, current_incarnation: int) -> Optional[StreamLease]:
        """The lease v2 BEGIN/DATA/END may carry now, or None."""
        if (
            self.endpoint_class is EndpointClass.V2
            and self.lease_valid
            and not self.lease_superseded
            and self.client_incarnation == current_incarnation
            and self.lease.valid
        ):
            return self.lease
        return None


class PongLeaseDecision(enum.Enum):
    # ordinary discovery only: liveness / address update, class and lease kept.
    # Unsolicited (timestamp 0), unmatched, late and malformed-HBS2 replies.
    NO_CHANGE = "NoChange"
    # drop the whole PONG: a structurally valid HBS2 reply whose correlation
    # fails (retired request / previous incarnation). It must not roll the
    # device's address or liveness back either.
    DISCARD = "Discard"
    # matched direct reply: the returned state replaces the endpoint's
    ACCEPT = "Accept"


def evaluate_pong_lease(
    pong: Mapping,
    pending_pings: Mapping[int, PendingPing],
    current_incarnation: int,
    existing: Optional[EndpointLeaseState],
) -> tuple[PongLeaseDecision, Optional[EndpointLeaseState]]:
    """Decide what a received PONG may do to its endpoint's class / lease.

    ``pong`` is a :func:`hapbeat.protocol.parse_pong` dict; missing keys are
    treated as an unsolicited identity PONG.
    """
    tail = pong.get("lease_tail", protocol.LEASE_TAIL_ABSENT)
    if tail == protocol.LEASE_TAIL_MALFORMED:
        # not a lease reply, and not proof of a pre-v2 device either
        return PongLeaseDecision.NO_CHANGE, None

    timestamp = int(pong.get("timestamp", 0))
    pending = pending_pings.get(pong.get("seq", -1))

    if tail == protocol.LEASE_TAIL_VALID:
        echoed = pong["echoed_client_incarnation"]
        # HBS2 is only valid on a direct reply to the active incarnation
        if current_incarnation == 0 or echoed != current_incarnation:
            return PongLeaseDecision.DISCARD, None
        if (
            timestamp == 0
            or pending is None
            or pending.timestamp_us != timestamp
            or pending.client_incarnation != echoed
        ):
            return PongLeaseDecision.DISCARD, None
        if existing is not None and timestamp <= existing.last_accepted_timestamp_us:
            # older than an accepted reply: never roll back to an older boot/ticket
            return PongLeaseDecision.DISCARD, None

        # A superseded writer must not turn a later periodic discovery reply
        # into an implicit takeover; only a fresh incarnation clears the latch.
        keep_superseded = (
            existing is not None
            and existing.endpoint_class is EndpointClass.V2
            and existing.lease_superseded
            and existing.client_incarnation == pending.client_incarnation
        )
        if keep_superseded:
            lease = existing.lease
            lease_valid = False
            superseded = True
        else:
            lease = StreamLease(pong["device_boot_id"], pong["lease_ticket"])
            lease_valid = bool(pong["lease_valid"])
            superseded = bool(pong["lease_superseded"])
        return PongLeaseDecision.ACCEPT, EndpointLeaseState(
            endpoint_class=EndpointClass.V2,
            lease=lease,
            lease_valid=lease_valid,
            lease_superseded=superseded,
            client_incarnation=pending.client_incarnation,
            last_accepted_timestamp_us=timestamp,
        )

    # No HBS2 marker. Pre-v2 firmware cannot echo the incarnation, so the
    # request itself must belong to the current incarnation.
    if timestamp == 0:
        return PongLeaseDecision.NO_CHANGE, None  # unsolicited identity PONG
    if (
        current_incarnation == 0
        or pending is None
        or pending.timestamp_us != timestamp
        or pending.client_incarnation != current_incarnation
    ):
        return PongLeaseDecision.NO_CHANGE, None  # reply to some other request
    if existing is not None and timestamp <= existing.last_accepted_timestamp_us:
        return PongLeaseDecision.NO_CHANGE, None  # late reply
    return PongLeaseDecision.ACCEPT, EndpointLeaseState(
        endpoint_class=EndpointClass.LEGACY,
        client_incarnation=pending.client_incarnation,
        last_accepted_timestamp_us=timestamp,
    )
