"""Confirms the synthetic seed data loads and relationships resolve.

No rule logic is exercised here -- just schema, FKs, and the shape of the
seeded dataset.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

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
from livekit_outbound.gate.seed import seed

# E.164, a real NANP area code, then the reserved fictional 555-01XX block.
FICTIONAL_NUMBER = re.compile(r"^\+1[2-9]\d{2}55501\d{2}$")


def _contact(session, name: str) -> Contact:
    contact = session.scalar(select(Contact).where(Contact.name == name))
    assert contact is not None, f"expected seeded contact {name!r}"
    return contact


def test_seed_creates_four_contacts(db_session):
    seed(db_session)
    names = {c.name for c in db_session.scalars(select(Contact)).all()}
    assert names == {
        "Test Contact A",
        "Test Contact B",
        "Test Contact C",
        "Test Contact D",
    }


def test_seed_data_is_obviously_synthetic(db_session):
    seed(db_session)
    for contact in db_session.scalars(select(Contact)).all():
        assert contact.name.startswith("Test Contact")
    for phone in db_session.scalars(select(PhoneNumber)).all():
        assert FICTIONAL_NUMBER.match(phone.number), phone.number
    for engagement in db_session.scalars(select(Engagement)).all():
        assert engagement.description.startswith("Synthetic")


def test_contact_engagement_relationship_resolves_both_directions(db_session):
    seed(db_session)
    contact_a = _contact(db_session, "Test Contact A")
    assert len(contact_a.engagements) == 1
    engagement_a = contact_a.engagements[0]
    assert engagement_a.contact is contact_a
    assert engagement_a.description == "Synthetic outreach: service reminder"
    assert engagement_a.status == "active"


def test_multiple_phone_numbers_share_one_engagements_attempt_total(db_session):
    seed(db_session)
    contact_a = _contact(db_session, "Test Contact A")
    engagement_a = contact_a.engagements[0]

    assert len(contact_a.phone_numbers) == 2
    assert len(engagement_a.call_attempts) == 6

    phone_ids_on_engagement = {attempt.phone_number_id for attempt in engagement_a.call_attempts}
    assert phone_ids_on_engagement == {p.id for p in contact_a.phone_numbers}
    for attempt in engagement_a.call_attempts:
        assert attempt.engagement is engagement_a
        assert attempt.phone_number.contact is contact_a


def test_near_limit_contact_all_attempts_within_seven_days(db_session):
    seed(db_session)
    contact_a = _contact(db_session, "Test Contact A")
    engagement_a = contact_a.engagements[0]

    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    for attempt in engagement_a.call_attempts:
        assert attempt.attempted_at.tzinfo is not None
        assert attempt.attempted_at >= cutoff
        assert attempt.outcome in {CallOutcome.NO_ANSWER, CallOutcome.VOICEMAIL}


def test_recently_reached_contact_has_live_conversation_three_days_ago(db_session):
    seed(db_session)
    contact_b = _contact(db_session, "Test Contact B")
    engagement_b = contact_b.engagements[0]

    assert len(engagement_b.call_attempts) == 1
    attempt = engagement_b.call_attempts[0]
    assert attempt.outcome is CallOutcome.LIVE_CONVERSATION

    expected = datetime.now(timezone.utc) - timedelta(days=3)
    assert abs((attempt.attempted_at - expected).total_seconds()) < 60


def test_clean_contact_has_no_call_attempts(db_session):
    seed(db_session)
    contact_c = _contact(db_session, "Test Contact C")
    engagement_c = contact_c.engagements[0]

    assert engagement_c.call_attempts == []
    assert len(contact_c.phone_numbers) == 1


def test_suppression_flag_and_override_resolve_to_the_flagged_engagement(db_session):
    seed(db_session)
    contact_d = _contact(db_session, "Test Contact D")
    engagement_d = contact_d.engagements[0]

    assert len(engagement_d.suppression_flags) == 1
    flag = engagement_d.suppression_flags[0]
    assert isinstance(flag, SuppressionFlag)
    assert flag.engagement is engagement_d
    assert flag.source == "written_request"

    assert len(engagement_d.calling_window_overrides) == 1
    override = engagement_d.calling_window_overrides[0]
    assert isinstance(override, CallingWindowOverride)
    assert override.engagement is engagement_d
    assert override.day_of_week is None
    assert override.window_start_local < override.window_end_local


def test_audit_log_entry_resolves_engagement_and_phone_number(db_session):
    seed(db_session)
    contact_d = _contact(db_session, "Test Contact D")
    engagement_d = contact_d.engagements[0]

    assert len(engagement_d.audit_log_entries) == 1
    entry = engagement_d.audit_log_entries[0]
    assert isinstance(entry, AuditLogEntry)
    assert entry.decision is AuditDecision.DENY
    assert entry.engagement is engagement_d
    assert entry.phone_number is not None
    assert entry.phone_number.contact is contact_d
    assert entry.attempt_count_at_decision == len(engagement_d.call_attempts)


def test_inactive_phone_number_is_preserved_but_flagged(db_session):
    seed(db_session)
    contact_d = _contact(db_session, "Test Contact D")
    numbers_by_active = {p.is_active for p in contact_d.phone_numbers}
    assert numbers_by_active == {True, False}


def test_call_attempt_optional_external_call_id(db_session):
    seed(db_session)
    contact_d = _contact(db_session, "Test Contact D")
    engagement_d = contact_d.engagements[0]

    with_id = [a for a in engagement_d.call_attempts if a.external_call_id is not None]
    without_id = [a for a in engagement_d.call_attempts if a.external_call_id is None]
    assert len(with_id) == 1
    assert with_id[0].external_call_id == "synthetic-call-001"
    assert len(without_id) == 2


def test_all_timestamps_round_trip_as_utc_aware(db_session):
    seed(db_session)
    attempts = db_session.scalars(select(CallAttempt)).all()
    assert attempts, "expected seeded call attempts"
    for attempt in attempts:
        assert attempt.attempted_at.tzinfo == timezone.utc


def test_seeded_numbers_sit_in_the_contacts_recorded_timezone(db_session):
    # The seed pairs each contact's area code with their recorded home
    # timezone, so the area-code path and the fallback path agree for demo
    # data; a mismatch here would make the demo output confusing.
    from livekit_outbound.gate.area_codes import timezones_for_number

    seed(db_session)
    for phone in db_session.scalars(select(PhoneNumber)).all():
        assert timezones_for_number(phone.number) == (phone.contact.timezone,), phone.number
