"""Reg F 7-in-7 call frequency rule.

12 CFR 1006.14(b)(2)(i) presumes a telephone call in connection with a
matter is harassing if the caller has already placed 7 or more calls about
that matter within the preceding 7 days. This module answers "is another
call about this engagement allowed right now."
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from livekit_outbound.gate.clock import require_aware_instant
from livekit_outbound.gate.models import CallAttempt, Contact, Engagement, PhoneNumber

SEVEN_IN_SEVEN_LIMIT = 7
WINDOW = timedelta(days=7)


@dataclass(frozen=True, slots=True)
class FrequencyResult:
    allowed: bool
    attempt_count: int
    window_start: datetime
    window_end: datetime
    reason: str | None


def check_seven_in_seven(
    db_session: Session,
    engagement_id: int,
    *,
    now: datetime,
) -> FrequencyResult:
    """Evaluate the Reg F 7-in-7 rule for ``engagement_id``.

    The window is rolling -- [now - 7 days, now] -- not a calendar week, so
    it's recomputed relative to ``now`` on every call rather than aligned to
    any fixed boundary.

    Every CallAttempt counts the moment it's dialed, including
    outcome == "pending": a call whose outcome hasn't been recorded yet
    doesn't change the fact that a call was already placed to the contact,
    and Reg F counts calls placed, not calls answered. Excluding "pending"
    would let a gate re-check made mid-flight (before the outcome lands)
    undercount and allow a call that shouldn't be allowed.

    Attempts are aggregated across every phone number belonging to the
    engagement's contact by walking the full relational path
    (CallAttempt -> PhoneNumber -> Contact -> Engagement) instead of
    trusting CallAttempt.engagement_id alone, so the count is provably
    scoped to numbers that actually belong to this engagement's contact.

    ``now`` is the instant the window is anchored to. It is required and
    must be timezone-aware: this rule never reads the wall clock, so the
    caller decides (and a test or demo can inject) the instant.
    """
    now = require_aware_instant(now)
    window_start = now - WINDOW
    window_end = now

    stmt = (
        select(CallAttempt)
        .join(PhoneNumber, CallAttempt.phone_number_id == PhoneNumber.id)
        .join(Contact, PhoneNumber.contact_id == Contact.id)
        .join(Engagement, Engagement.contact_id == Contact.id)
        .where(Engagement.id == engagement_id)
        .where(CallAttempt.engagement_id == engagement_id)
        .where(CallAttempt.attempted_at >= window_start)
        .where(CallAttempt.attempted_at <= window_end)
    )
    attempts = list(db_session.scalars(stmt).all())
    attempt_count = len(attempts)

    if attempt_count < SEVEN_IN_SEVEN_LIMIT:
        return FrequencyResult(
            allowed=True,
            attempt_count=attempt_count,
            window_start=window_start,
            window_end=window_end,
            reason=None,
        )

    number_count = len({attempt.phone_number_id for attempt in attempts})
    oldest_attempt = min(attempts, key=lambda attempt: attempt.attempted_at)
    next_window_opens = oldest_attempt.attempted_at + WINDOW
    reason = (
        f"{attempt_count} attempts already made in the last 7 days across "
        f"{number_count} numbers, next window opens at {next_window_opens.isoformat()}"
    )
    return FrequencyResult(
        allowed=False,
        attempt_count=attempt_count,
        window_start=window_start,
        window_end=window_end,
        reason=reason,
    )
