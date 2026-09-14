"""Tests for the dialer-side gate adapter (livekit_outbound/predial.py)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from livekit_outbound import predial
from livekit_outbound.gate.models import (
    AuditDecision,
    AuditLogEntry,
    CallAttempt,
    CallOutcome,
    Contact,
    Engagement,
)
from livekit_outbound.gate.seed import (
    CONTACT_A_NUMBERS,
    CONTACT_B_NUMBER,
    CONTACT_C_NUMBER,
    CONTACT_D_NUMBERS,
    seed,
)

# Inside the calling window everywhere in the continental US and Canada.
MIDDAY_UTC = datetime(2026, 7, 15, 19, 0, tzinfo=timezone.utc)


def _audit_entries(session, engagement_id: int) -> list[AuditLogEntry]:
    return list(
        session.scalars(
            select(AuditLogEntry)
            .where(AuditLogEntry.engagement_id == engagement_id)
            .order_by(AuditLogEntry.id)
        ).all()
    )


def test_suppressed_number_is_denied_with_structured_reason_and_audit(db_session):
    seed(db_session)

    auth = predial.authorize(db_session, CONTACT_D_NUMBERS[0], clock_override=MIDDAY_UTC)

    assert auth.allowed is False
    assert auth.reason_code == "SUPPRESSED"
    assert auth.rule_name == "suppression"
    assert auth.enrolled is False
    assert auth.contact.name == "Test Contact D"
    assert auth.timezones == ("America/Denver",)
    assert auth.timezone_source == "area code 303"

    entries = _audit_entries(db_session, auth.engagement.id)
    newest = entries[-1]
    assert newest.id == auth.audit_entry_id
    assert newest.decision is AuditDecision.DENY
    assert newest.decided_at == MIDDAY_UTC
    assert "suppression flag active" in newest.reason


def test_clean_number_is_allowed_and_audited(db_session):
    seed(db_session)

    auth = predial.authorize(db_session, CONTACT_C_NUMBER, clock_override=MIDDAY_UTC)

    assert auth.allowed is True
    assert auth.reason_code == "ALLOWED"
    assert auth.rule_name is None
    assert auth.decision.attempt_count == 0

    newest = _audit_entries(db_session, auth.engagement.id)[-1]
    assert newest.id == auth.audit_entry_id
    assert newest.decision is AuditDecision.ALLOW
    assert newest.reason == "all checks passed"


def test_recent_conversation_and_frequency_reason_codes_surface(db_session):
    seed(db_session)

    cooldown = predial.authorize(db_session, CONTACT_B_NUMBER, clock_override=datetime.now(timezone.utc))
    assert cooldown.reason_code == "COOLDOWN_ACTIVE"

    # Contact A sits one shy of the cap; one recorded dial tips it over.
    near_cap = predial.authorize(db_session, CONTACT_A_NUMBERS[0], clock_override=_midday_today())
    assert near_cap.allowed is True
    assert near_cap.decision.attempt_count == 6
    predial.record_attempt(
        db_session, near_cap, external_call_id="call-test", attempted_at=_midday_today()
    )

    capped = predial.authorize(db_session, CONTACT_A_NUMBERS[1], clock_override=_midday_today())
    assert capped.reason_code == "FREQUENCY_CAP_REACHED"
    assert capped.decision.attempt_count == 7


def test_unknown_number_with_known_area_code_is_enrolled_with_derived_timezone(db_session):
    seed(db_session)
    number = "+14705550199"  # 470: Atlanta, Eastern

    auth = predial.authorize(db_session, number, clock_override=MIDDAY_UTC)

    assert auth.enrolled is True
    assert auth.allowed is True
    assert auth.contact.timezone == "America/New_York"
    assert auth.contact.phone_numbers[0].number == number
    assert auth.engagement.description == predial.AUTO_ENROLLED_ENGAGEMENT
    assert auth.timezones == ("America/New_York",)

    # A second call to the same number reuses the enrolled record.
    again = predial.authorize(db_session, number, clock_override=MIDDAY_UTC)
    assert again.enrolled is False
    assert again.contact.id == auth.contact.id
    assert again.engagement.id == auth.engagement.id


def test_enrolled_number_is_still_subject_to_the_calling_window(db_session):
    # Enrollment must not be a way around the rules: a fresh Los Angeles
    # number at 03:30 local is denied like any other.
    three_thirty_am_pacific = datetime(2026, 7, 15, 10, 30, tzinfo=timezone.utc)

    auth = predial.authorize(db_session, "+13105550199", clock_override=three_thirty_am_pacific)

    assert auth.enrolled is True
    assert auth.reason_code == "OUTSIDE_CALLING_WINDOW"
    assert "America/Los_Angeles" in auth.decision.reason


def test_unknown_number_without_derivable_timezone_is_refused_before_the_gate(db_session):
    seed(db_session)
    before = db_session.scalar(select(AuditLogEntry.id).order_by(AuditLogEntry.id.desc()))

    with pytest.raises(predial.UnresolvableNumberError, match="area code"):
        predial.authorize(db_session, "+18005550100", clock_override=MIDDAY_UTC)

    # Nothing was enrolled and no audit row was written: there is no subject
    # to write it against.
    assert db_session.scalar(select(Contact).where(Contact.name.contains("800"))) is None
    after = db_session.scalar(select(AuditLogEntry.id).order_by(AuditLogEntry.id.desc()))
    assert before == after


def test_auto_enroll_can_be_disabled(db_session):
    with pytest.raises(predial.UnresolvableNumberError, match="auto-enrollment is disabled"):
        predial.authorize(db_session, "+14705550199", clock_override=MIDDAY_UTC, auto_enroll=False)


def test_contact_with_several_engagements_requires_an_explicit_choice(db_session):
    seed(db_session)
    contact_c = db_session.scalar(select(Contact).where(Contact.name == "Test Contact C"))
    second = Engagement(contact=contact_c, description="Synthetic outreach: second matter", status="active")
    db_session.add(second)
    db_session.commit()

    with pytest.raises(predial.AmbiguousEngagementError, match="--engagement-id"):
        predial.authorize(db_session, CONTACT_C_NUMBER, clock_override=MIDDAY_UTC)

    chosen = predial.authorize(db_session, CONTACT_C_NUMBER, engagement_id=second.id, clock_override=MIDDAY_UTC)
    assert chosen.engagement.id == second.id
    assert chosen.allowed is True

    contact_d = db_session.scalar(select(Contact).where(Contact.name == "Test Contact D"))
    foreign_engagement = contact_d.engagements[0]
    with pytest.raises(predial.PredialError, match="does not belong"):
        predial.authorize(
            db_session, CONTACT_C_NUMBER, engagement_id=foreign_engagement.id, clock_override=MIDDAY_UTC
        )


def test_record_attempt_is_pending_and_refuses_denied_authorizations(db_session):
    seed(db_session)
    allowed = predial.authorize(db_session, CONTACT_C_NUMBER, clock_override=MIDDAY_UTC)

    attempt = predial.record_attempt(
        db_session, allowed, external_call_id="call-abc123", attempted_at=MIDDAY_UTC
    )

    stored = db_session.get(CallAttempt, attempt.id)
    assert stored.outcome is CallOutcome.PENDING
    assert stored.external_call_id == "call-abc123"
    assert stored.attempted_at == MIDDAY_UTC
    assert stored.engagement_id == allowed.engagement.id
    assert stored.phone_number.number == CONTACT_C_NUMBER

    predial.mark_attempt(db_session, attempt, CallOutcome.FAILED)
    assert db_session.get(CallAttempt, attempt.id).outcome is CallOutcome.FAILED

    denied = predial.authorize(db_session, CONTACT_D_NUMBERS[0], clock_override=MIDDAY_UTC)
    with pytest.raises(ValueError, match="denied"):
        predial.record_attempt(db_session, denied, external_call_id="call-nope")


def test_format_shows_verdict_code_rule_citation_and_inputs_on_deny(db_session):
    seed(db_session)
    auth = predial.authorize(db_session, CONTACT_D_NUMBERS[0], clock_override=MIDDAY_UTC)

    text = predial.format_authorization(auth)

    assert "PRE-DIAL POLICY GATE: DENY" in text
    assert "reason code   SUPPRESSED" in text
    assert "rule          suppression:" in text
    assert "47 CFR 64.1200(d)" in text
    assert "1692c(c)" in text
    assert CONTACT_D_NUMBERS[0] in text
    assert "Test Contact D" in text
    assert "America/Denver (from area code 303)" in text
    assert "13:00:00 MDT in America/Denver" in text
    assert "window        08:00-21:00 local" in text
    assert "attempts      not counted" in text
    assert "detail        suppression flag active" in text
    # An injected instant was used, so the entry is marked as such.
    assert f"audit         entry #{auth.audit_entry_id} written (DENY, marked simulated)" in text


def test_format_lists_rules_passed_and_attempt_count_on_allow(db_session):
    seed(db_session)
    auth = predial.authorize(db_session, CONTACT_A_NUMBERS[0], clock_override=_midday_today())

    text = predial.format_authorization(auth)

    assert "PRE-DIAL POLICY GATE: ALLOW" in text
    assert "reason code   ALLOWED" in text
    assert "rules passed  phone_number, suppression, conversation_cooldown, seven_in_seven, time_window" in text
    assert "attempts      6 in the last 7 days" in text
    assert "detail" not in text
    assert "written (ALLOW, marked simulated)" in text


def test_format_prints_every_candidate_zone_for_a_split_area_code(db_session):
    auth = predial.authorize(db_session, "+18505550199", clock_override=MIDDAY_UTC)

    text = predial.format_authorization(auth)

    assert "America/New_York, America/Chicago (from area code 850)" in text
    assert "in America/New_York" in text
    assert "in America/Chicago" in text


def _midday_today() -> datetime:
    """Today at 18:00 UTC: inside the window for every seeded contact, and
    inside the seed's rolling 7-day history (which is anchored to real now)."""
    today = datetime.now(timezone.utc)
    midday = today.replace(hour=18, minute=0, second=0, microsecond=0)
    if midday > today:
        midday -= timedelta(days=1)
    return midday
