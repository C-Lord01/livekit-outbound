"""Reg F 7-day contact cooldown after a live conversation.

12 CFR 1006.14(b)(2)(ii) presumes a telephone call in connection with a
matter is harassing if the caller already had a telephone conversation
with the person about that matter within the preceding 7 days. This
module answers "did a live conversation happen recently enough to block
another attempt right now."
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from livekit_outbound.gate.clock import require_aware_instant
from livekit_outbound.gate.models import (
    CallAttempt,
    CallOutcome,
    Contact,
    Engagement,
    PhoneNumber,
)

COOLDOWN = timedelta(days=7)


@dataclass(frozen=True, slots=True)
class CooldownResult:
    allowed: bool
    last_live_conversation_at: datetime | None
    cooldown_expires_at: datetime | None
    reason: str | None


def check_conversation_cooldown(
    db_session: Session,
    engagement_id: int,
    *,
    now: datetime,
) -> CooldownResult:
    """Evaluate the post-conversation 7-day cooldown for ``engagement_id``.

    Only outcome == "live_conversation" starts the cooldown: the rule keys
    on having actually spoken with the contact, so voicemails, no-answers,
    failed dials, and unresolved "pending" attempts never trigger it -- a
    message left is not a conversation had. (Contrast the 7-in-7 frequency
    rule, where every dial counts regardless of outcome.)

    The cooldown runs from the most recent live conversation, and the
    boundary is exclusive: a conversation exactly 7 days ago has a cooldown
    expiring exactly at ``now``, which no longer blocks -- matching the
    exclusive window edge used by the frequency rule.

    Live conversations are matched by walking the full relational path
    (CallAttempt -> PhoneNumber -> Contact -> Engagement) instead of
    trusting CallAttempt.engagement_id alone, so the match is provably
    scoped to numbers that actually belong to this engagement's contact.

    ``now`` is the instant the cooldown is judged at. It is required and
    must be timezone-aware: this rule never reads the wall clock, so the
    caller decides (and a test or demo can inject) the instant.
    """
    now = require_aware_instant(now)

    stmt = (
        select(CallAttempt)
        .join(PhoneNumber, CallAttempt.phone_number_id == PhoneNumber.id)
        .join(Contact, PhoneNumber.contact_id == Contact.id)
        .join(Engagement, Engagement.contact_id == Contact.id)
        .where(Engagement.id == engagement_id)
        .where(CallAttempt.engagement_id == engagement_id)
        .where(CallAttempt.outcome == CallOutcome.LIVE_CONVERSATION)
        .order_by(CallAttempt.attempted_at.desc())
        .limit(1)
    )
    last_live = db_session.scalars(stmt).first()

    if last_live is None:
        return CooldownResult(
            allowed=True,
            last_live_conversation_at=None,
            cooldown_expires_at=None,
            reason=None,
        )

    cooldown_expires_at = last_live.attempted_at + COOLDOWN

    if cooldown_expires_at <= now:
        return CooldownResult(
            allowed=True,
            last_live_conversation_at=last_live.attempted_at,
            cooldown_expires_at=cooldown_expires_at,
            reason=None,
        )

    reason = (
        f"live conversation on {last_live.attempted_at.isoformat()} started a "
        f"7-day cooldown that expires at {cooldown_expires_at.isoformat()}"
    )
    return CooldownResult(
        allowed=False,
        last_live_conversation_at=last_live.attempted_at,
        cooldown_expires_at=cooldown_expires_at,
        reason=reason,
    )
