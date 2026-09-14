"""The gate takes its evaluation instant as input and never reads a clock.

These tests pin the invariant that makes clock injection safe: every rule
evaluates the one instant it is handed, nothing in the gate package
consults wall-clock time, and an injected instant is stamped on the audit
row without changing how any rule decides.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import select

from livekit_outbound.gate import clock, cooldown, decide, frequency, time_window
from livekit_outbound.gate.cooldown import check_conversation_cooldown
from livekit_outbound.gate.decide import evaluate_gate
from livekit_outbound.gate.frequency import check_seven_in_seven
from livekit_outbound.gate.models import AuditLogEntry, Contact, Engagement
from livekit_outbound.gate.seed import CONTACT_C_NUMBER, seed
from livekit_outbound.gate.time_window import check_time_window

GATE_PACKAGE = Path(decide.__file__).resolve().parent
RULE_MODULES = (decide, frequency, cooldown, time_window, clock)

NOON_LA = datetime(2026, 7, 15, 19, 0, tzinfo=timezone.utc)
NAIVE = datetime(2026, 7, 15, 12, 0)


class _ForbiddenClock(datetime):
    """A datetime whose clock-reading constructors blow up."""

    @classmethod
    def now(cls, tz=None):
        raise AssertionError("a gate rule read the wall clock")

    @classmethod
    def utcnow(cls):
        raise AssertionError("a gate rule read the wall clock")

    @classmethod
    def today(cls):
        raise AssertionError("a gate rule read the wall clock")


def _engagement_c(session) -> Engagement:
    contact = session.scalar(select(Contact).where(Contact.name == "Test Contact C"))
    (engagement,) = contact.engagements
    return engagement


# --- the instant is required ---------------------------------------------------


def test_every_rule_requires_the_instant_as_a_keyword(db_session):
    seed(db_session)
    engagement = _engagement_c(db_session)

    with pytest.raises(TypeError, match="now"):
        check_seven_in_seven(db_session, engagement.id)
    with pytest.raises(TypeError, match="now"):
        check_conversation_cooldown(db_session, engagement.id)
    with pytest.raises(TypeError, match="now"):
        check_time_window("America/Los_Angeles")
    with pytest.raises(TypeError, match="now"):
        evaluate_gate(db_session, engagement.contact_id, engagement.id, CONTACT_C_NUMBER)


def test_naive_instant_is_rejected_by_every_rule_and_the_gate(db_session):
    seed(db_session)
    engagement = _engagement_c(db_session)

    with pytest.raises(ValueError, match="timezone-aware"):
        check_seven_in_seven(db_session, engagement.id, now=NAIVE)
    with pytest.raises(ValueError, match="timezone-aware"):
        check_conversation_cooldown(db_session, engagement.id, now=NAIVE)
    with pytest.raises(ValueError, match="timezone-aware"):
        check_time_window("America/Los_Angeles", now=NAIVE)
    audit_before = db_session.scalar(select(AuditLogEntry.id).order_by(AuditLogEntry.id.desc()))
    with pytest.raises(ValueError, match="timezone-aware"):
        evaluate_gate(db_session, engagement.contact_id, engagement.id, CONTACT_C_NUMBER, now=NAIVE)
    # Rejected before any evaluation, so no audit row either way.
    audit_after = db_session.scalar(select(AuditLogEntry.id).order_by(AuditLogEntry.id.desc()))
    assert audit_before == audit_after


# --- no rule reads the wall clock ---------------------------------------------


def test_no_rule_reads_the_wall_clock_when_an_instant_is_supplied(db_session, monkeypatch):
    seed(db_session)
    engagement = _engagement_c(db_session)
    for module in RULE_MODULES:
        monkeypatch.setattr(module, "datetime", _ForbiddenClock)

    result = evaluate_gate(
        db_session, engagement.contact_id, engagement.id, CONTACT_C_NUMBER, now=NOON_LA
    )

    assert result.decision == "ALLOW"
    assert result.checked_at == NOON_LA
    # Sanity check that the patch would have caught a read.
    with pytest.raises(AssertionError, match="wall clock"):
        frequency.datetime.now(timezone.utc)


def test_gate_package_source_contains_no_clock_reads():
    # A static guard alongside the runtime one: no module in the gate
    # package (the synthetic seed script aside, which is data) may call a
    # clock-reading constructor.
    forbidden = re.compile(r"\b(datetime\.(now|utcnow|today)|time\.time|time\.monotonic)\s*\(")
    offenders = []
    for path in sorted(GATE_PACKAGE.glob("*.py")):
        if path.name == "seed.py":
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if forbidden.search(line):
                offenders.append(f"{path.name}:{lineno}: {line.strip()}")
    assert offenders == []


def test_every_rule_sees_the_same_instant(db_session):
    # The decision's checked_at, the audit row's decided_at, and the
    # frequency window's end all come from the single supplied instant.
    seed(db_session)
    engagement = _engagement_c(db_session)

    result = evaluate_gate(
        db_session, engagement.contact_id, engagement.id, CONTACT_C_NUMBER, now=NOON_LA
    )
    frequency_result = check_seven_in_seven(db_session, engagement.id, now=NOON_LA)
    cooldown_result = check_conversation_cooldown(db_session, engagement.id, now=NOON_LA)
    window_result = check_time_window("America/Los_Angeles", now=NOON_LA)

    assert result.checked_at == NOON_LA
    assert frequency_result.window_end == NOON_LA
    assert frequency_result.window_start == NOON_LA - timedelta(days=7)
    assert cooldown_result.allowed is True
    assert window_result.local_time == NOON_LA
    audit = db_session.scalar(select(AuditLogEntry).order_by(AuditLogEntry.id.desc()))
    assert audit.decided_at == NOON_LA


# --- the simulated marker -------------------------------------------------------


def test_simulated_flag_stamps_the_audit_row_and_changes_nothing_else(db_session):
    seed(db_session)
    engagement = _engagement_c(db_session)

    live = evaluate_gate(
        db_session, engagement.contact_id, engagement.id, CONTACT_C_NUMBER, now=NOON_LA
    )
    live_audit = db_session.scalar(select(AuditLogEntry).order_by(AuditLogEntry.id.desc()))
    simulated = evaluate_gate(
        db_session,
        engagement.contact_id,
        engagement.id,
        CONTACT_C_NUMBER,
        now=NOON_LA,
        simulated=True,
    )
    simulated_audit = db_session.scalar(select(AuditLogEntry).order_by(AuditLogEntry.id.desc()))

    # Same verdict, same reason, same count, same instant.
    assert simulated.decision == live.decision == "ALLOW"
    assert simulated.reason == live.reason
    assert simulated.attempt_count == live.attempt_count
    assert simulated.checked_at == live.checked_at == NOON_LA
    assert live.simulated is False
    assert simulated.simulated is True

    # Only the dedicated marker differs.
    assert live_audit.simulated_now is None
    assert simulated_audit.simulated_now == NOON_LA
    assert simulated_audit.decided_at == NOON_LA


def test_recorded_at_is_the_wall_clock_and_distinct_from_the_injected_instant(db_session):
    seed(db_session)
    engagement = _engagement_c(db_session)
    before = datetime.now(timezone.utc).replace(microsecond=0)

    evaluate_gate(
        db_session,
        engagement.contact_id,
        engagement.id,
        CONTACT_C_NUMBER,
        now=NOON_LA,
        simulated=True,
    )
    audit = db_session.scalar(select(AuditLogEntry).order_by(AuditLogEntry.id.desc()))

    after = datetime.now(timezone.utc)
    assert audit.recorded_at is not None
    assert audit.recorded_at.tzinfo == timezone.utc
    assert before - timedelta(seconds=1) <= audit.recorded_at <= after + timedelta(seconds=1)
    assert audit.recorded_at != audit.decided_at
    assert audit.simulated_now == audit.decided_at == NOON_LA


def test_simulated_deny_is_audited_as_deny_not_softened(db_session):
    # A DENY under an injected instant is still a DENY: the marker adds
    # information, it never changes the verdict.
    seed(db_session)
    engagement = _engagement_c(db_session)
    three_thirty_am_pacific = datetime(2026, 7, 15, 10, 30, tzinfo=timezone.utc)

    result = evaluate_gate(
        db_session,
        engagement.contact_id,
        engagement.id,
        CONTACT_C_NUMBER,
        now=three_thirty_am_pacific,
        simulated=True,
    )
    audit = db_session.scalar(select(AuditLogEntry).order_by(AuditLogEntry.id.desc()))

    assert result.decision == "DENY"
    assert result.reason_code == "OUTSIDE_CALLING_WINDOW"
    assert audit.decision.value == "DENY"
    assert audit.simulated_now == three_thirty_am_pacific
