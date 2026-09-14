"""Tests for the post-conversation 7-day cooldown rule (livekit_outbound/gate/cooldown.py)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from livekit_outbound.gate.cooldown import check_conversation_cooldown
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


def _make_cooldown_engagement(session) -> tuple[Engagement, PhoneNumber]:
    """A minimal, test-local contact/engagement/phone, independent of the seed script."""
    contact = Contact(name="Test Contact Cooldown", timezone="America/Chicago")
    engagement = Engagement(
        contact=contact, description="Synthetic cooldown-test engagement", status="active"
    )
    phone = PhoneNumber(contact=contact, number="+13125550198", is_active=True)
    session.add_all([contact, engagement, phone])
    session.flush()
    return engagement, phone


def test_live_conversation_three_days_ago_is_denied_with_four_day_cooldown_left(db_session):
    seed(db_session)
    engagement_b = _engagement_for_contact(db_session, "Test Contact B")
    live_attempt = engagement_b.call_attempts[0]

    result = check_conversation_cooldown(db_session, engagement_b.id, now=datetime.now(timezone.utc))

    assert result.allowed is False
    assert result.last_live_conversation_at == live_attempt.attempted_at
    assert result.cooldown_expires_at == live_attempt.attempted_at + timedelta(days=7)
    remaining = result.cooldown_expires_at - datetime.now(timezone.utc)
    assert abs(remaining - timedelta(days=4)) < timedelta(minutes=5)
    assert result.reason is not None
    assert live_attempt.attempted_at.isoformat() in result.reason
    assert result.cooldown_expires_at.isoformat() in result.reason


def test_live_conversation_eight_days_ago_is_allowed(db_session):
    engagement, phone = _make_cooldown_engagement(db_session)
    now = datetime.now(timezone.utc)
    stale_live = CallAttempt(
        engagement=engagement,
        phone_number=phone,
        attempted_at=now - timedelta(days=8),
        outcome=CallOutcome.LIVE_CONVERSATION,
    )
    db_session.add(stale_live)
    db_session.commit()

    result = check_conversation_cooldown(db_session, engagement.id, now=now)

    assert result.allowed is True
    assert result.last_live_conversation_at == stale_live.attempted_at
    assert result.reason is None


def test_recent_voicemail_without_any_live_conversation_is_allowed(db_session):
    # The outcome filter is what's under test: a voicemail 1 hour ago is well
    # inside the 7-day window, so a denial here would mean non-conversation
    # outcomes are wrongly starting the cooldown.
    engagement, phone = _make_cooldown_engagement(db_session)
    now = datetime.now(timezone.utc)
    voicemail = CallAttempt(
        engagement=engagement,
        phone_number=phone,
        attempted_at=now - timedelta(hours=1),
        outcome=CallOutcome.VOICEMAIL,
    )
    db_session.add(voicemail)
    db_session.commit()

    result = check_conversation_cooldown(db_session, engagement.id, now=now)

    assert result.allowed is True
    assert result.last_live_conversation_at is None
    assert result.cooldown_expires_at is None
    assert result.reason is None


def test_cooldown_runs_from_most_recent_of_multiple_live_conversations(db_session):
    engagement, phone = _make_cooldown_engagement(db_session)
    now = datetime.now(timezone.utc)
    older_live = CallAttempt(
        engagement=engagement,
        phone_number=phone,
        attempted_at=now - timedelta(days=20),
        outcome=CallOutcome.LIVE_CONVERSATION,
    )
    recent_live = CallAttempt(
        engagement=engagement,
        phone_number=phone,
        attempted_at=now - timedelta(days=6),
        outcome=CallOutcome.LIVE_CONVERSATION,
    )
    db_session.add_all([older_live, recent_live])
    db_session.commit()

    result = check_conversation_cooldown(db_session, engagement.id, now=now)

    assert result.allowed is False
    assert result.last_live_conversation_at == recent_live.attempted_at
    assert result.cooldown_expires_at == recent_live.attempted_at + timedelta(days=7)


def test_live_conversation_exactly_seven_days_ago_is_allowed(db_session):
    engagement, phone = _make_cooldown_engagement(db_session)
    now = datetime.now(timezone.utc)
    boundary_live = CallAttempt(
        engagement=engagement,
        phone_number=phone,
        attempted_at=now - timedelta(days=7),
        outcome=CallOutcome.LIVE_CONVERSATION,
    )
    db_session.add(boundary_live)
    db_session.commit()

    result = check_conversation_cooldown(db_session, engagement.id, now=now)

    assert result.allowed is True
    assert result.cooldown_expires_at == now
    assert result.reason is None
