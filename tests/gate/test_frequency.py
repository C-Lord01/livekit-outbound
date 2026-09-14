"""Tests for the Reg F 7-in-7 frequency rule (livekit_outbound/gate/frequency.py)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from livekit_outbound.gate.frequency import check_seven_in_seven
from livekit_outbound.gate.models import (
    CallAttempt,
    CallOutcome,
    Contact,
    Engagement,
    PhoneNumber,
)
from livekit_outbound.gate.seed import seed


def _engagement_for_contact(session, contact_name: str) -> Engagement:
    contact = session.scalar(select(Contact).where(Contact.name == contact_name))
    assert contact is not None, f"expected seeded contact {contact_name!r}"
    assert len(contact.engagements) == 1
    return contact.engagements[0]


def _make_boundary_engagement(session) -> tuple[Engagement, PhoneNumber]:
    """A minimal, test-local contact/engagement/phone, independent of the seed script."""
    contact = Contact(name="Test Contact Boundary", timezone="America/Chicago")
    engagement = Engagement(
        contact=contact, description="Synthetic boundary-test engagement", status="active"
    )
    phone = PhoneNumber(contact=contact, number="+13125550199", is_active=True)
    session.add_all([contact, engagement, phone])
    session.flush()
    return engagement, phone


def test_six_attempts_two_numbers_is_allowed(db_session):
    seed(db_session)
    engagement_a = _engagement_for_contact(db_session, "Test Contact A")

    result = check_seven_in_seven(db_session, engagement_a.id, now=datetime.now(timezone.utc))

    assert result.allowed is True
    assert result.attempt_count == 6
    assert result.reason is None


def test_seventh_attempt_triggers_denial_with_count_and_next_window_in_reason(db_session):
    seed(db_session)
    engagement_a = _engagement_for_contact(db_session, "Test Contact A")
    existing_phone = engagement_a.contact.phone_numbers[0]

    seventh = CallAttempt(
        engagement=engagement_a,
        phone_number=existing_phone,
        attempted_at=datetime.now(timezone.utc) - timedelta(hours=1),
        outcome=CallOutcome.NO_ANSWER,
    )
    db_session.add(seventh)
    db_session.commit()

    result = check_seven_in_seven(db_session, engagement_a.id, now=datetime.now(timezone.utc))

    assert result.allowed is False
    assert result.attempt_count == 7
    assert result.reason is not None
    assert "7 attempts already made in the last 7 days across 2 numbers" in result.reason

    oldest_attempted_at = min(a.attempted_at for a in engagement_a.call_attempts)
    expected_next_window = oldest_attempted_at + timedelta(days=7)
    assert expected_next_window.isoformat() in result.reason


def test_attempts_across_three_numbers_all_count_toward_same_total(db_session):
    seed(db_session)
    engagement_a = _engagement_for_contact(db_session, "Test Contact A")
    contact_a = engagement_a.contact

    third_number = PhoneNumber(contact=contact_a, number="+13125550103", is_active=True)
    db_session.add(third_number)
    db_session.flush()

    extra_attempt = CallAttempt(
        engagement=engagement_a,
        phone_number=third_number,
        attempted_at=datetime.now(timezone.utc) - timedelta(hours=1),
        outcome=CallOutcome.NO_ANSWER,
    )
    db_session.add(extra_attempt)
    db_session.commit()

    result = check_seven_in_seven(db_session, engagement_a.id, now=datetime.now(timezone.utc))

    assert result.attempt_count == 7
    assert result.allowed is False
    assert "across 3 numbers" in result.reason


def test_attempt_just_outside_seven_day_window_is_excluded(db_session):
    engagement, phone = _make_boundary_engagement(db_session)
    now = datetime.now(timezone.utc)
    stale_attempt = CallAttempt(
        engagement=engagement,
        phone_number=phone,
        attempted_at=now - timedelta(days=7, minutes=1),
        outcome=CallOutcome.NO_ANSWER,
    )
    db_session.add(stale_attempt)
    db_session.commit()

    result = check_seven_in_seven(db_session, engagement.id, now=now)

    assert result.attempt_count == 0
    assert result.allowed is True


def test_attempt_just_inside_seven_day_window_is_included(db_session):
    engagement, phone = _make_boundary_engagement(db_session)
    now = datetime.now(timezone.utc)
    recent_attempt = CallAttempt(
        engagement=engagement,
        phone_number=phone,
        attempted_at=now - timedelta(days=6, hours=23),
        outcome=CallOutcome.NO_ANSWER,
    )
    db_session.add(recent_attempt)
    db_session.commit()

    result = check_seven_in_seven(db_session, engagement.id, now=now)

    assert result.attempt_count == 1
    assert result.allowed is True


def test_contact_with_zero_attempts_is_allowed(db_session):
    seed(db_session)
    engagement_c = _engagement_for_contact(db_session, "Test Contact C")

    result = check_seven_in_seven(db_session, engagement_c.id, now=datetime.now(timezone.utc))

    assert result.allowed is True
    assert result.attempt_count == 0
    assert result.reason is None


def test_pending_outcome_counts_toward_the_total(db_session):
    # Contact D's only in-window attempt (the other two are 9-10 days old)
    # has outcome=pending -- confirms a not-yet-resolved dial still counts.
    seed(db_session)
    engagement_d = _engagement_for_contact(db_session, "Test Contact D")

    result = check_seven_in_seven(db_session, engagement_d.id, now=datetime.now(timezone.utc))

    assert result.attempt_count == 1
    assert result.allowed is True
