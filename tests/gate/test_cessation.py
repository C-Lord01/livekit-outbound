"""In-call cessation writes: durable suppression, the audit row, and the gate's view.

The point of the suppression write is that the very next pre-dial
evaluation for that contact denies with ``SUPPRESSED``. These tests prove
it against the real gate, both through the gate functions directly and
through the agent-side store on a file-backed database.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from livekit_outbound import predial
from livekit_outbound.cessation import GateSuppressionStore
from livekit_outbound.gate.cessation import (
    IN_CALL_SOURCE,
    write_cessation_event,
    write_suppression,
)
from livekit_outbound.gate.decide import evaluate_gate
from livekit_outbound.gate.models import (
    CessationEvent,
    Contact,
    DetectionPath,
    Engagement,
    PhoneNumber,
    SuppressionFlag,
    SuppressionKind,
)
from livekit_outbound.gate.seed import CONTACT_C_NUMBER
from tests.conftest import MIDDAY_UTC, engagement_id_for, rows

NUMBER = "+13125550177"


@pytest.fixture
def subject(db_session):
    contact = Contact(name="Test Contact E", timezone="America/Chicago")
    engagement = Engagement(contact=contact, description="Synthetic outreach", status="active")
    phone = PhoneNumber(contact=contact, number=NUMBER, is_active=True)
    db_session.add_all([contact, engagement, phone])
    db_session.commit()
    return contact, engagement, phone


def test_clean_subject_is_allowed_before_cessation(db_session, subject):
    contact, engagement, _ = subject
    decision = evaluate_gate(db_session, contact.id, engagement.id, NUMBER, now=MIDDAY_UTC)
    assert decision.allowed


@pytest.mark.parametrize(
    "kind, expected_reason",
    [
        (SuppressionKind.STOP_CONTACT, "Stop-contact request made during a call"),
        (SuppressionKind.ATTORNEY_REPRESENTED, "Attorney representation stated during a call"),
    ],
)
def test_suppression_write_is_durable_and_then_the_gate_denies(db_session, subject, kind, expected_reason):
    contact, engagement, _ = subject

    flag = write_suppression(
        db_session, engagement_id=engagement.id, kind=kind, utterance="stop", now=MIDDAY_UTC
    )

    stored = db_session.execute(
        select(SuppressionFlag).where(SuppressionFlag.id == flag.id)
    ).scalar_one()
    assert stored.kind is kind
    assert stored.source == IN_CALL_SOURCE
    assert stored.flagged_at == MIDDAY_UTC
    assert stored.reason == f"{expected_reason}: 'stop'"

    decision = evaluate_gate(
        db_session, contact.id, engagement.id, NUMBER, now=MIDDAY_UTC + timedelta(minutes=1)
    )
    assert not decision.allowed
    assert decision.failed_check == "suppression"
    assert decision.reason_code == "SUPPRESSED"
    assert expected_reason in decision.reason


def test_suppression_write_rejects_naive_instant(db_session, subject):
    _, engagement, _ = subject
    with pytest.raises(ValueError, match="timezone-aware"):
        write_suppression(
            db_session,
            engagement_id=engagement.id,
            kind=SuppressionKind.STOP_CONTACT,
            utterance="stop",
            now=datetime(2026, 7, 15, 12, 0),
        )
    assert rows_of(db_session, SuppressionFlag) == []


def test_cessation_event_records_everything_an_auditor_needs(db_session, subject):
    _, engagement, phone = subject
    flag = write_suppression(
        db_session,
        engagement_id=engagement.id,
        kind=SuppressionKind.ATTORNEY_REPRESENTED,
        utterance="talk to my lawyer",
        now=MIDDAY_UTC,
    )

    event = write_cessation_event(
        db_session,
        engagement_id=engagement.id,
        phone_number=NUMBER,
        external_call_id="call-abc123",
        suppression_flag_id=flag.id,
        detected_at=MIDDAY_UTC,
        utterance="talk to my lawyer",
        kind=SuppressionKind.ATTORNEY_REPRESENTED,
        detection_path=DetectionPath.FAST_PATH,
        detection_detail="matched 'talk to my lawyer'",
        transcript_delay_ms=180.0,
        latency_to_durable_ms=12.5,
    )

    stored = db_session.execute(select(CessationEvent).where(CessationEvent.id == event.id)).scalar_one()
    assert stored.engagement_id == engagement.id
    assert stored.phone_number_id == phone.id
    assert stored.suppression_flag_id == flag.id
    assert stored.external_call_id == "call-abc123"
    assert stored.detected_at == MIDDAY_UTC
    assert stored.utterance == "talk to my lawyer"
    assert stored.kind is SuppressionKind.ATTORNEY_REPRESENTED
    assert stored.detection_path is DetectionPath.FAST_PATH
    assert stored.detection_detail == "matched 'talk to my lawyer'"
    assert stored.transcript_delay_ms == 180.0
    assert stored.latency_to_durable_ms == 12.5
    assert stored.suppression_durable is True
    assert stored.recorded_at is not None
    # No simulated marker exists on this path.
    assert not hasattr(stored, "simulated_now")


def test_cessation_event_after_failed_write_is_marked_not_durable(db_session, subject):
    _, engagement, _ = subject
    event = write_cessation_event(
        db_session,
        engagement_id=engagement.id,
        phone_number="+19995550100",  # not on file: phone_number_id stays NULL
        external_call_id="call-abc123",
        suppression_flag_id=None,
        detected_at=MIDDAY_UTC,
        utterance="stop calling me",
        kind=SuppressionKind.STOP_CONTACT,
        detection_path=DetectionPath.FAIL_CLOSED,
        detection_detail="classifier timed out after 1.5s",
        transcript_delay_ms=None,
        latency_to_durable_ms=None,
    )
    assert event.suppression_durable is False
    assert event.suppression_flag_id is None
    assert event.phone_number_id is None


def test_store_end_to_end_then_predial_denies(gate_db, fixed_wall_clock):
    """The agent-side store on the seeded file database, then the dialer's own gate call."""
    engagement_id = engagement_id_for(gate_db, "Test Contact C")
    store = GateSuppressionStore(gate_db)

    flag_id = store.write_suppression(
        engagement_id=engagement_id,
        kind=SuppressionKind.STOP_CONTACT,
        utterance="please stop calling me",
        now=MIDDAY_UTC,
    )
    event_id = store.write_event(
        engagement_id=engagement_id,
        phone_number=CONTACT_C_NUMBER,
        external_call_id="call-abc123",
        suppression_flag_id=flag_id,
        detected_at=MIDDAY_UTC,
        utterance="please stop calling me",
        kind=SuppressionKind.STOP_CONTACT,
        detection_path=DetectionPath.FAST_PATH,
        detection_detail="matched 'stop calling me'",
        transcript_delay_ms=150.0,
        latency_to_durable_ms=9.0,
    )
    assert isinstance(flag_id, int) and isinstance(event_id, int)

    from livekit_outbound.gate.db import open_session

    session = open_session(gate_db, create_schema=False)
    try:
        authorization = predial.authorize(session, CONTACT_C_NUMBER)
    finally:
        session.close()

    assert not authorization.allowed
    assert authorization.reason_code == "SUPPRESSED"
    assert authorization.rule_name == "suppression"
    assert "Stop-contact request made during a call" in authorization.decision.reason
    assert not authorization.simulated

    (event,) = [e for e in rows(gate_db, CessationEvent)]
    assert event.suppression_flag_id == flag_id
    assert event.suppression_durable is True


def rows_of(session, model):
    return list(session.scalars(select(model)).all())
