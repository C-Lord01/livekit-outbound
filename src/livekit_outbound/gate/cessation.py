"""Durable record of an in-call cessation: the suppression write and its audit row.

When the person on a live call asks not to be contacted again, or states
that they are represented by an attorney, two rows are written here in two
separate commits:

1. A :class:`SuppressionFlag` on the engagement. This is the same state the
   pre-dial ``suppression`` rule reads, so the next gate evaluation for any
   of the contact's numbers denies with ``SUPPRESSED``. This commit is the
   instant the cessation becomes durable, and it is the instant the
   latency measurement stops.
2. A :class:`CessationEvent`, the audit record: when during the call the
   system became aware, what was said, which detection path fired, and how
   long the suppression took to become durable.

Same rules as the rest of the gate package: nothing here reads a clock. The
caller passes the instant, and it must be timezone-aware. Neither row has a
simulated marker because an in-call cessation is always a live event; there
is no clock injection on this path by construction.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from livekit_outbound.gate.clock import require_aware_instant
from livekit_outbound.gate.models import (
    CessationEvent,
    DetectionPath,
    PhoneNumber,
    SuppressionFlag,
    SuppressionKind,
)

# ``SuppressionFlag.source`` for flags raised during a call.
IN_CALL_SOURCE = "in_call_request"

SUPPRESSION_REASONS: dict[SuppressionKind, str] = {
    SuppressionKind.STOP_CONTACT: "Stop-contact request made during a call",
    SuppressionKind.ATTORNEY_REPRESENTED: "Attorney representation stated during a call",
}


def write_suppression(
    session: Session,
    *,
    engagement_id: int,
    kind: SuppressionKind,
    utterance: str,
    now: datetime,
) -> SuppressionFlag:
    """Raise a suppression flag on ``engagement_id`` and confirm it is durable.

    Commits, then re-reads the row through a fresh SELECT (not the identity
    map) so that a return from this function means the flag is readable by
    any other connection, including the next pre-dial evaluation. Raises on
    any failure; it never returns a flag that is not on disk.
    """
    now = require_aware_instant(now)
    flag = SuppressionFlag(
        engagement_id=engagement_id,
        kind=kind,
        flagged_at=now,
        reason=f"{SUPPRESSION_REASONS[kind]}: {utterance!r}",
        source=IN_CALL_SOURCE,
    )
    session.add(flag)
    session.commit()

    confirmed = session.execute(
        select(SuppressionFlag.id).where(SuppressionFlag.id == flag.id)
    ).scalar_one_or_none()
    if confirmed is None:
        raise RuntimeError(
            f"suppression flag #{flag.id} for engagement #{engagement_id} was committed "
            "but could not be read back"
        )
    return flag


def write_cessation_event(
    session: Session,
    *,
    engagement_id: int,
    phone_number: str | None,
    external_call_id: str | None,
    suppression_flag_id: int | None,
    detected_at: datetime,
    utterance: str,
    kind: SuppressionKind,
    detection_path: DetectionPath,
    detection_detail: str,
    transcript_delay_ms: float | None,
    latency_to_durable_ms: float | None,
) -> CessationEvent:
    """Write the audit record for one in-call cessation and commit it.

    ``suppression_flag_id`` and ``latency_to_durable_ms`` are None only when
    the suppression write failed; the row then carries
    ``suppression_durable=False`` so the failure is visible in the audit
    trail rather than only in a log.
    """
    detected_at = require_aware_instant(detected_at, name="detected_at")
    phone_number_id = None
    if phone_number is not None:
        phone_number_id = session.scalar(
            select(PhoneNumber.id).where(PhoneNumber.number == phone_number)
        )
    event = CessationEvent(
        engagement_id=engagement_id,
        phone_number_id=phone_number_id,
        suppression_flag_id=suppression_flag_id,
        external_call_id=external_call_id,
        detected_at=detected_at,
        utterance=utterance,
        kind=kind,
        detection_path=detection_path,
        detection_detail=detection_detail,
        transcript_delay_ms=transcript_delay_ms,
        latency_to_durable_ms=latency_to_durable_ms,
        suppression_durable=suppression_flag_id is not None,
    )
    session.add(event)
    session.commit()
    return event
