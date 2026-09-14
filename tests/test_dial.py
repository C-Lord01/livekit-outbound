"""scripts/dial.py consults the gate before it touches LiveKit.

The LiveKit API client is replaced with a recording fake (tests/conftest.py),
so these tests prove two things without any network: a DENY never reaches
the SIP service, and an ALLOW dials and leaves a call attempt plus an ALLOW
audit row behind.
"""

from __future__ import annotations

import asyncio

import pytest

from livekit_outbound.gate.models import (
    AuditDecision,
    AuditLogEntry,
    CallAttempt,
    CallOutcome,
)
from livekit_outbound.gate.seed import CONTACT_C_NUMBER, CONTACT_D_NUMBERS
from tests.conftest import MIDDAY_UTC, engagement_id_for, rows


def test_suppressed_number_prints_deny_and_never_dials(
    dial_module, gate_db, dial_env, fixed_wall_clock, capsys
):
    with pytest.raises(SystemExit) as exit_info:
        asyncio.run(dial_module.dial(CONTACT_D_NUMBERS[0]))

    assert exit_info.value.code == dial_module.EXIT_GATE_DENIED
    captured = capsys.readouterr()
    assert "PRE-DIAL POLICY GATE: DENY" in captured.out
    assert "reason code   SUPPRESSED" in captured.out
    assert "rule          suppression:" in captured.out
    assert "not dialing" in captured.err
    assert "SUPPRESSED" in captured.err

    # LiveKit was never even constructed, let alone asked to dial.
    assert dial_env.instances == []

    newest = rows(gate_db, AuditLogEntry)[-1]
    assert newest.decision is AuditDecision.DENY
    assert newest.decided_at == MIDDAY_UTC
    assert "suppression flag active" in newest.reason
    # No call attempt was recorded for the denied dial.
    attempts = [
        a
        for a in rows(gate_db, CallAttempt)
        if a.external_call_id and a.external_call_id.startswith("call-")
    ]
    assert attempts == []


def test_clean_number_dials_and_records_attempt_and_allow_audit(
    dial_module, gate_db, dial_env, fixed_wall_clock, capsys
):
    asyncio.run(dial_module.dial(CONTACT_C_NUMBER))

    captured = capsys.readouterr()
    assert "PRE-DIAL POLICY GATE: ALLOW" in captured.out
    assert "reason code   ALLOWED" in captured.out
    assert "answered: participant phone-12135550120" in captured.out

    (lk,) = dial_env.instances
    (request,) = lk.sip.requests
    assert request.sip_call_to == CONTACT_C_NUMBER
    assert request.sip_trunk_id == "ST_fake"
    assert request.wait_until_answered is True
    assert lk.closed is True

    newest_audit = rows(gate_db, AuditLogEntry)[-1]
    assert newest_audit.decision is AuditDecision.ALLOW
    assert newest_audit.reason == "all checks passed"

    attempt = rows(gate_db, CallAttempt)[-1]
    assert attempt.outcome is CallOutcome.PENDING
    assert attempt.external_call_id == request.room_name
    assert attempt.attempted_at == MIDDAY_UTC
    assert attempt.engagement_id == engagement_id_for(gate_db, "Test Contact C")


def test_unknown_number_is_enrolled_then_dialed(
    dial_module, gate_db, dial_env, fixed_wall_clock, capsys
):
    asyncio.run(dial_module.dial("+14705550199"))

    captured = capsys.readouterr()
    assert "PRE-DIAL POLICY GATE: ALLOW" in captured.out
    assert "(enrolled just now)" in captured.out
    assert "America/New_York (from area code 470)" in captured.out
    (lk,) = dial_env.instances
    assert lk.sip.requests[0].sip_call_to == "+14705550199"


def test_number_with_no_derivable_location_is_refused_without_dialing(
    dial_module, gate_db, dial_env, fixed_wall_clock, capsys
):
    with pytest.raises(SystemExit) as exit_info:
        asyncio.run(dial_module.dial("+18005550100"))

    assert exit_info.value.code == dial_module.EXIT_GATE_DENIED
    assert "cannot evaluate" in capsys.readouterr().err
    assert dial_env.instances == []


def test_parse_args_rejects_non_e164(dial_module, capsys):
    with pytest.raises(SystemExit) as exit_info:
        dial_module.parse_args(["4705550123"])
    assert exit_info.value.code == dial_module.EXIT_USAGE

    args = dial_module.parse_args(["+14705550123", "--engagement-id", "7"])
    assert args.phone_number == "+14705550123"
    assert args.engagement_id == 7
    assert args.clock_override is None
