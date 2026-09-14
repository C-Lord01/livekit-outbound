"""Integration tests for the composite gate (livekit_outbound/gate/decide.py)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from livekit_outbound.gate.decide import RULES, GateDecision, evaluate_gate
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

# Contact C's number is a 213 (Los Angeles) number: UTC-7 on this July date.
NOON_LOCAL_FOR_C = datetime(2026, 7, 15, 19, 0, tzinfo=timezone.utc)
THREE_AM_LOCAL_FOR_C = datetime(2026, 7, 15, 10, 0, tzinfo=timezone.utc)


def _engagement_for_contact(session, contact_name: str) -> Engagement:
    contact = session.scalar(select(Contact).where(Contact.name == contact_name))
    assert contact is not None, f"expected seeded contact {contact_name!r}"
    assert len(contact.engagements) == 1
    return contact.engagements[0]


def _latest_audit(session, engagement_id: int) -> AuditLogEntry:
    """The newest audit entry for the engagement (Contact D is seeded with one)."""
    entry = session.scalar(
        select(AuditLogEntry)
        .where(AuditLogEntry.engagement_id == engagement_id)
        .order_by(AuditLogEntry.id.desc())
        .limit(1)
    )
    assert entry is not None, "expected an AuditLogEntry to have been written"
    return entry


def _assert_audit_matches(session, engagement_id: int, result) -> AuditLogEntry:
    entry = _latest_audit(session, engagement_id)
    assert entry.decision == AuditDecision(result.decision)
    assert entry.attempt_count_at_decision == result.attempt_count
    assert entry.decided_at == result.checked_at
    if result.reason is not None:
        assert entry.reason == result.reason
    return entry


def test_clean_contact_is_allowed_and_audited(db_session):
    seed(db_session)
    engagement_c = _engagement_for_contact(db_session, "Test Contact C")

    result = evaluate_gate(
        db_session, engagement_c.contact_id, engagement_c.id, CONTACT_C_NUMBER, now=NOON_LOCAL_FOR_C
    )

    assert result.decision == "ALLOW"
    assert result.allowed is True
    assert result.reason is None
    assert result.reason_code == "ALLOWED"
    assert result.rule is None
    assert result.attempt_count == 0
    assert result.failed_check is None
    assert result.checked_at == NOON_LOCAL_FOR_C

    entry = _assert_audit_matches(db_session, engagement_c.id, result)
    assert entry.reason == "all checks passed"
    assert entry.phone_number_id == engagement_c.contact.phone_numbers[0].id


def test_suppression_flag_denies_before_any_other_check_runs(db_session):
    seed(db_session)
    engagement_d = _engagement_for_contact(db_session, "Test Contact D")

    result = evaluate_gate(
        db_session,
        engagement_d.contact_id,
        engagement_d.id,
        CONTACT_D_NUMBERS[0],
        now=datetime.now(timezone.utc),
    )

    assert result.decision == "DENY"
    assert result.allowed is False
    assert result.failed_check == "suppression"
    assert result.reason_code == "SUPPRESSED"
    assert result.reason == "suppression flag active: Contact sent a written stop-contact request"
    # attempt_count is None because the gate short-circuited on suppression --
    # the 7-in-7 check (which would have counted D's in-window pending
    # attempt) never ran.
    assert result.attempt_count is None

    _assert_audit_matches(db_session, engagement_d.id, result)


def test_recent_live_conversation_denies_on_cooldown(db_session):
    seed(db_session)
    engagement_b = _engagement_for_contact(db_session, "Test Contact B")

    result = evaluate_gate(
        db_session,
        engagement_b.contact_id,
        engagement_b.id,
        CONTACT_B_NUMBER,
        now=datetime.now(timezone.utc),
    )

    assert result.decision == "DENY"
    assert result.failed_check == "conversation_cooldown"
    assert result.reason_code == "COOLDOWN_ACTIVE"
    assert result.attempt_count is None
    assert "live conversation" in result.reason

    _assert_audit_matches(db_session, engagement_b.id, result)


def test_seventh_attempt_denies_on_frequency_with_count_populated(db_session):
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

    result = evaluate_gate(
        db_session,
        engagement_a.contact_id,
        engagement_a.id,
        CONTACT_A_NUMBERS[0],
        now=datetime.now(timezone.utc),
    )

    assert result.decision == "DENY"
    assert result.failed_check == "seven_in_seven"
    assert result.reason_code == "FREQUENCY_CAP_REACHED"
    assert result.attempt_count == 7
    assert "7 attempts already made" in result.reason

    _assert_audit_matches(db_session, engagement_a.id, result)


def test_outside_local_calling_hours_denies_on_time_window(db_session):
    seed(db_session)
    engagement_c = _engagement_for_contact(db_session, "Test Contact C")

    result = evaluate_gate(
        db_session,
        engagement_c.contact_id,
        engagement_c.id,
        CONTACT_C_NUMBER,
        now=THREE_AM_LOCAL_FOR_C,
    )

    assert result.decision == "DENY"
    assert result.failed_check == "time_window"
    assert result.reason_code == "OUTSIDE_CALLING_WINDOW"
    assert "before the 08:00:00 window start" in result.reason
    # The reason names the zone and how it was derived, so the audit row is
    # self-describing.
    assert "America/Los_Angeles" in result.reason
    assert "area code 213" in result.reason
    # The frequency check ran (and passed) before the time window denied,
    # so the count is populated even on this DENY.
    assert result.attempt_count == 0

    _assert_audit_matches(db_session, engagement_c.id, result)


def test_number_not_on_file_for_contact_is_denied_not_raised(db_session):
    # Documented choice in decide.py: an unknown number is an auditable
    # DENY (fail-closed, handled uniformly by the dialer), not an exception.
    seed(db_session)
    engagement_c = _engagement_for_contact(db_session, "Test Contact C")

    result = evaluate_gate(
        db_session, engagement_c.contact_id, engagement_c.id, "+13125550177", now=NOON_LOCAL_FOR_C
    )

    assert result.decision == "DENY"
    assert result.failed_check == "phone_number"
    assert result.reason_code == "NUMBER_NOT_ON_FILE"
    assert "'+13125550177'" in result.reason
    assert result.attempt_count is None

    entry = _assert_audit_matches(db_session, engagement_c.id, result)
    assert entry.phone_number_id is None


def test_every_failed_check_value_has_a_rule_and_reason_code():
    # The dialer branches on reason_code, so every check name the gate can
    # emit must map to a rule, and codes must be unique.
    checks = [rule.check for rule in RULES]
    assert checks == [
        "phone_number",
        "suppression",
        "conversation_cooldown",
        "seven_in_seven",
        "time_window",
    ]
    codes = [rule.reason_code for rule in RULES]
    assert len(set(codes)) == len(codes)
    for rule in RULES:
        decision = GateDecision(
            decision="DENY",
            reason="x",
            attempt_count=None,
            checked_at=datetime.now(timezone.utc),
            failed_check=rule.check,
        )
        assert decision.reason_code == rule.reason_code
        assert decision.rule is rule
