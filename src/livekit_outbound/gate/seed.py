"""Seed obviously-synthetic demo data for local development and testing.

Creates four fake contacts covering distinct call-history scenarios:

- Test Contact A: two phone numbers, six attempts across both in the last
  7 days (one shy of the 7-in-7 cap), exercising multi-number attempt totals.
- Test Contact B: one live conversation 3 days ago, nothing since.
- Test Contact C: clean history, never contacted.
- Test Contact D: a suppression flag, a calling-window override, and a mix
  of failed/no-answer/pending attempts, plus a DENY audit log entry.

Every name is "Test Contact *" and every number sits in the reserved
fictional 555-01XX block behind a real area code chosen to match the
contact's recorded timezone (312 Chicago, 212 New York, 213 Los Angeles,
303 Denver). Running this script resets and repopulates the target
database -- do not point it at anything but a local dev/test database.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone

from sqlalchemy.orm import Session

from livekit_outbound.gate.db import Base, create_gate_engine, session_factory
from livekit_outbound.gate.models import (
    AuditDecision,
    AuditLogEntry,
    CallAttempt,
    CallingWindowOverride,
    CallOutcome,
    Contact,
    Engagement,
    PhoneNumber,
    SuppressionFlag,
)

NOW = datetime.now(timezone.utc)

# Numbers callers can use to exercise each scenario from the command line.
CONTACT_A_NUMBERS = ("+13125550101", "+13125550102")
CONTACT_B_NUMBER = "+12125550110"
CONTACT_C_NUMBER = "+12135550120"
CONTACT_D_NUMBERS = ("+13035550130", "+13035550131")


def _days_ago(days: float) -> datetime:
    return NOW - timedelta(days=days)


def _hours_ago(hours: float) -> datetime:
    return NOW - timedelta(hours=hours)


def seed(session: Session) -> None:
    """Insert the synthetic dataset into ``session`` and commit it."""

    # --- Contact A: two numbers, 6 attempts in 7 days (near the 7-in-7 cap) ---
    contact_a = Contact(name="Test Contact A", timezone="America/Chicago")
    engagement_a = Engagement(
        contact=contact_a,
        description="Synthetic outreach: service reminder",
        status="active",
    )
    phone_a1 = PhoneNumber(contact=contact_a, number=CONTACT_A_NUMBERS[0], is_active=True)
    phone_a2 = PhoneNumber(contact=contact_a, number=CONTACT_A_NUMBERS[1], is_active=True)
    attempts_a = [
        CallAttempt(engagement=engagement_a, phone_number=phone_a1, attempted_at=_days_ago(6.5), outcome=CallOutcome.NO_ANSWER),
        CallAttempt(engagement=engagement_a, phone_number=phone_a2, attempted_at=_days_ago(5.5), outcome=CallOutcome.VOICEMAIL),
        CallAttempt(engagement=engagement_a, phone_number=phone_a1, attempted_at=_days_ago(4.5), outcome=CallOutcome.NO_ANSWER),
        CallAttempt(engagement=engagement_a, phone_number=phone_a2, attempted_at=_days_ago(3.5), outcome=CallOutcome.NO_ANSWER),
        CallAttempt(engagement=engagement_a, phone_number=phone_a1, attempted_at=_days_ago(2.5), outcome=CallOutcome.VOICEMAIL),
        CallAttempt(engagement=engagement_a, phone_number=phone_a2, attempted_at=_days_ago(1.5), outcome=CallOutcome.NO_ANSWER),
    ]

    # --- Contact B: live conversation 3 days ago, nothing since ---
    contact_b = Contact(name="Test Contact B", timezone="America/New_York")
    engagement_b = Engagement(
        contact=contact_b,
        description="Synthetic outreach: appointment follow-up",
        status="active",
    )
    phone_b1 = PhoneNumber(contact=contact_b, number=CONTACT_B_NUMBER, is_active=True)
    attempts_b = [
        CallAttempt(
            engagement=engagement_b,
            phone_number=phone_b1,
            attempted_at=_days_ago(3),
            outcome=CallOutcome.LIVE_CONVERSATION,
        ),
    ]

    # --- Contact C: clean history, never contacted ---
    contact_c = Contact(name="Test Contact C", timezone="America/Los_Angeles")
    engagement_c = Engagement(
        contact=contact_c,
        description="Synthetic outreach: survey invitation",
        status="active",
    )
    phone_c1 = PhoneNumber(contact=contact_c, number=CONTACT_C_NUMBER, is_active=True)

    # --- Contact D: suppression flag, calling-window override, mixed outcomes ---
    contact_d = Contact(name="Test Contact D", timezone="America/Denver")
    engagement_d = Engagement(
        contact=contact_d,
        description="Synthetic outreach: renewal notice",
        status="on_hold",
    )
    phone_d1 = PhoneNumber(contact=contact_d, number=CONTACT_D_NUMBERS[0], is_active=True)
    phone_d2 = PhoneNumber(contact=contact_d, number=CONTACT_D_NUMBERS[1], is_active=False)
    attempts_d = [
        CallAttempt(engagement=engagement_d, phone_number=phone_d1, attempted_at=_days_ago(10), outcome=CallOutcome.FAILED),
        CallAttempt(engagement=engagement_d, phone_number=phone_d1, attempted_at=_days_ago(9), outcome=CallOutcome.NO_ANSWER),
        CallAttempt(
            engagement=engagement_d,
            phone_number=phone_d1,
            attempted_at=_hours_ago(1),
            outcome=CallOutcome.PENDING,
            external_call_id="synthetic-call-001",
        ),
    ]
    suppression_d = SuppressionFlag(
        engagement=engagement_d,
        flagged_at=_days_ago(2),
        reason="Contact sent a written stop-contact request",
        source="written_request",
    )
    override_d = CallingWindowOverride(
        engagement=engagement_d,
        window_start_local=time(9, 0),
        window_end_local=time(12, 0),
        day_of_week=None,  # applies every day
    )
    audit_d = AuditLogEntry(
        engagement=engagement_d,
        phone_number=phone_d1,
        decision=AuditDecision.DENY,
        reason="Suppression flag on file; contact suppressed",
        attempt_count_at_decision=len(attempts_d),
        decided_at=_hours_ago(1),
    )

    session.add_all(
        [
            contact_a, engagement_a, phone_a1, phone_a2, *attempts_a,
            contact_b, engagement_b, phone_b1, *attempts_b,
            contact_c, engagement_c, phone_c1,
            contact_d, engagement_d, phone_d1, phone_d2, *attempts_d,
            suppression_d, override_d, audit_d,
        ]
    )
    session.commit()


def main() -> None:
    engine = create_gate_engine()
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    session = session_factory(engine)()
    try:
        seed(session)
        contact_count = session.query(Contact).count()
    finally:
        session.close()
    print(f"Seeded {contact_count} synthetic contacts into {engine.url}")
    print("Try:")
    print(f"  suppressed (DENY):        uv run scripts/dial.py {CONTACT_D_NUMBERS[0]}")
    print(f"  recent conversation:      uv run scripts/dial.py {CONTACT_B_NUMBER}")
    print(f"  one shy of frequency cap: uv run scripts/dial.py {CONTACT_A_NUMBERS[0]}")
    print(f"  clean history (ALLOW):    uv run scripts/dial.py {CONTACT_C_NUMBER}")


if __name__ == "__main__":
    main()
