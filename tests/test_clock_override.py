"""Deterministic clock injection on the pre-dial path (predial + dial.py --now).

The override moves the instant every rule evaluates against and nothing
else. These tests show it can flip a verdict only by moving the clock, that
naive timestamps are refused, that injected records are marked and live
ones are not, and that the override is reachable from the command line
only.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from livekit_outbound import predial
from livekit_outbound.gate.models import AuditLogEntry, CallAttempt
from livekit_outbound.gate.seed import CONTACT_C_NUMBER, CONTACT_D_NUMBERS, seed
from tests.conftest import rows

# Contact C's number is a 213 (Los Angeles) number, UTC-7 in July.
BEFORE_WINDOW_OPENS = datetime(2026, 7, 15, 14, 30, tzinfo=timezone.utc)  # 07:30 PDT
INSIDE_WINDOW = datetime(2026, 7, 15, 19, 0, tzinfo=timezone.utc)  # 12:00 PDT


# --- parsing -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2026-07-15T12:30:00Z", datetime(2026, 7, 15, 12, 30, tzinfo=timezone.utc)),
        ("2026-07-15T12:30:00+00:00", datetime(2026, 7, 15, 12, 30, tzinfo=timezone.utc)),
        ("2026-07-15T08:30:00-04:00", datetime(2026, 7, 15, 12, 30, tzinfo=timezone.utc)),
        ("2026-07-15T21:30:00+09:00", datetime(2026, 7, 15, 12, 30, tzinfo=timezone.utc)),
        ("  2026-07-15T12:30Z ", datetime(2026, 7, 15, 12, 30, tzinfo=timezone.utc)),
    ],
)
def test_parse_instant_accepts_z_and_explicit_offsets_and_normalizes_to_utc(text, expected):
    parsed = predial.parse_instant(text)
    assert parsed == expected
    assert parsed.tzinfo == timezone.utc


@pytest.mark.parametrize(
    "text",
    [
        "2026-07-15T12:30:00",  # naive: no offset, no Z
        "2026-07-15 12:30",
        "2026-07-15",  # date only is naive too
    ],
)
def test_parse_instant_rejects_naive_timestamps_with_a_clear_message(text):
    with pytest.raises(ValueError, match="has no timezone") as exc_info:
        predial.parse_instant(text)
    assert "will not assume a zone" in str(exc_info.value)
    assert "Z" in str(exc_info.value) and "-04:00" in str(exc_info.value)


@pytest.mark.parametrize("text", ["yesterday", "12:30Z", "2026-13-40T00:00:00Z", ""])
def test_parse_instant_rejects_non_iso_input(text):
    with pytest.raises(ValueError, match="not an ISO-8601 timestamp"):
        predial.parse_instant(text)


# --- the override moves the clock and only the clock -----------------------------------


def test_same_number_denied_before_the_window_and_allowed_inside_it(db_session):
    seed(db_session)

    early = predial.authorize(db_session, CONTACT_C_NUMBER, clock_override=BEFORE_WINDOW_OPENS)
    inside = predial.authorize(db_session, CONTACT_C_NUMBER, clock_override=INSIDE_WINDOW)

    assert early.allowed is False
    assert early.reason_code == "OUTSIDE_CALLING_WINDOW"
    assert "07:30" in early.decision.reason
    assert early.decision.checked_at == BEFORE_WINDOW_OPENS
    assert inside.allowed is True
    assert inside.reason_code == "ALLOWED"
    assert inside.decision.checked_at == INSIDE_WINDOW


def test_override_cannot_get_past_a_rule_that_is_not_about_time(db_session):
    # A suppressed number stays denied no matter what instant is injected.
    seed(db_session)
    for instant in (BEFORE_WINDOW_OPENS, INSIDE_WINDOW, INSIDE_WINDOW + timedelta(days=400)):
        auth = predial.authorize(db_session, CONTACT_D_NUMBERS[0], clock_override=instant)
        assert auth.reason_code == "SUPPRESSED"


def test_override_rejects_a_naive_instant(db_session):
    seed(db_session)
    with pytest.raises(ValueError, match="clock_override must be timezone-aware"):
        predial.authorize(db_session, CONTACT_C_NUMBER, clock_override=datetime(2026, 7, 15, 12))


# --- audit marking -----------------------------------------------------------------------


def test_injected_authorization_marks_the_audit_row_and_live_one_does_not(db_session, monkeypatch):
    seed(db_session)
    monkeypatch.setattr(predial, "utcnow", lambda: INSIDE_WINDOW)

    live = predial.authorize(db_session, CONTACT_C_NUMBER)
    injected = predial.authorize(db_session, CONTACT_C_NUMBER, clock_override=INSIDE_WINDOW)

    live_row = db_session.get(AuditLogEntry, live.audit_entry_id)
    injected_row = db_session.get(AuditLogEntry, injected.audit_entry_id)
    assert live.simulated is False and live.clock_override is None
    assert live_row.simulated_now is None
    assert live_row.decided_at == INSIDE_WINDOW
    assert injected.simulated is True and injected.clock_override == INSIDE_WINDOW
    assert injected_row.simulated_now == INSIDE_WINDOW
    assert injected_row.decided_at == INSIDE_WINDOW
    assert injected_row.recorded_at != INSIDE_WINDOW


def test_recorded_attempt_carries_the_marker_but_a_real_wall_clock_time(db_session):
    seed(db_session)
    auth = predial.authorize(db_session, CONTACT_C_NUMBER, clock_override=INSIDE_WINDOW)
    before = datetime.now(timezone.utc)

    attempt = predial.record_attempt(db_session, auth, external_call_id="call-sim")

    stored = db_session.get(CallAttempt, attempt.id)
    assert stored.simulated_now == INSIDE_WINDOW
    assert before - timedelta(seconds=1) <= stored.attempted_at <= datetime.now(timezone.utc) + timedelta(seconds=1)
    assert stored.attempted_at != INSIDE_WINDOW


def test_format_prints_a_clock_override_banner_only_when_injected(db_session, monkeypatch):
    seed(db_session)
    monkeypatch.setattr(predial, "utcnow", lambda: INSIDE_WINDOW)

    live_text = predial.format_authorization(predial.authorize(db_session, CONTACT_C_NUMBER))
    injected_text = predial.format_authorization(
        predial.authorize(db_session, CONTACT_C_NUMBER, clock_override=BEFORE_WINDOW_OPENS)
    )

    assert "CLOCK OVERRIDDEN" not in live_text
    assert "simulated" not in live_text
    assert "!! CLOCK OVERRIDDEN (--now): every rule evaluated at 2026-07-15T14:30:00+00:00" in injected_text
    assert "No rule was skipped or relaxed" in injected_text
    assert "evaluated at  2026-07-15T14:30:00+00:00 (injected)" in injected_text
    assert "written (DENY, marked simulated)" in injected_text


# --- dial.py --now -----------------------------------------------------------------------


def test_dial_now_rejects_naive_timestamp_before_touching_anything(dial_module, capsys):
    with pytest.raises(SystemExit) as exit_info:
        dial_module.parse_args([CONTACT_C_NUMBER, "--now", "2026-07-15T12:30:00"])

    assert exit_info.value.code == dial_module.EXIT_USAGE
    err = capsys.readouterr().err
    assert "--now:" in err
    assert "has no timezone" in err
    assert "will not assume a zone" in err


def test_dial_now_parses_to_a_utc_clock_override(dial_module):
    args = dial_module.parse_args([CONTACT_C_NUMBER, "--now", "2026-07-15T08:30:00-04:00"])
    assert args.clock_override == datetime(2026, 7, 15, 12, 30, tzinfo=timezone.utc)


def test_dial_with_now_denies_before_window_and_dials_inside_it(
    dial_module, gate_db, dial_env, capsys
):
    with pytest.raises(SystemExit) as exit_info:
        asyncio.run(dial_module.dial(CONTACT_C_NUMBER, clock_override=BEFORE_WINDOW_OPENS))
    assert exit_info.value.code == dial_module.EXIT_GATE_DENIED
    early = capsys.readouterr()
    assert "CLOCK OVERRIDDEN" in early.out
    assert "reason code   OUTSIDE_CALLING_WINDOW" in early.out
    assert dial_env.instances == []

    asyncio.run(dial_module.dial(CONTACT_C_NUMBER, clock_override=INSIDE_WINDOW))
    inside = capsys.readouterr()
    assert "CLOCK OVERRIDDEN" in inside.out
    assert "reason code   ALLOWED" in inside.out
    assert "attempt marked simulated" in inside.out
    (lk,) = dial_env.instances
    assert lk.sip.requests[0].sip_call_to == CONTACT_C_NUMBER

    deny_row, allow_row = rows(gate_db, AuditLogEntry)[-2:]
    assert deny_row.decision.value == "DENY" and deny_row.simulated_now == BEFORE_WINDOW_OPENS
    assert allow_row.decision.value == "ALLOW" and allow_row.simulated_now == INSIDE_WINDOW
    attempt = rows(gate_db, CallAttempt)[-1]
    assert attempt.simulated_now == INSIDE_WINDOW
    assert attempt.attempted_at != INSIDE_WINDOW


def test_dial_without_now_leaves_no_simulated_marker(
    dial_module, gate_db, dial_env, fixed_wall_clock, capsys
):
    asyncio.run(dial_module.dial(CONTACT_C_NUMBER))

    assert "CLOCK OVERRIDDEN" not in capsys.readouterr().out
    assert rows(gate_db, AuditLogEntry)[-1].simulated_now is None
    assert rows(gate_db, CallAttempt)[-1].simulated_now is None


# --- CLI only: nothing else can inject the clock -----------------------------------------


def test_environment_cannot_inject_the_clock(
    dial_module, gate_db, dial_env, fixed_wall_clock, monkeypatch, capsys
):
    # Every plausible spelling set in the environment, none of them honored:
    # the run evaluates at the (pinned) wall clock and is not marked.
    for name in ("NOW", "DIAL_NOW", "GATE_NOW", "CLOCK_OVERRIDE", "GATE_CLOCK_OVERRIDE", "DIAL_CLOCK_OVERRIDE"):
        monkeypatch.setenv(name, BEFORE_WINDOW_OPENS.isoformat())

    asyncio.run(dial_module.dial(CONTACT_C_NUMBER))

    out = capsys.readouterr().out
    assert "CLOCK OVERRIDDEN" not in out
    assert "reason code   ALLOWED" in out  # BEFORE_WINDOW_OPENS would have denied
    assert rows(gate_db, AuditLogEntry)[-1].simulated_now is None


def test_no_source_reads_a_clock_override_from_env_or_files(dial_module):
    # dial.py reads exactly these settings from the environment, none of
    # which is a clock; predial reads none.
    import re
    from pathlib import Path

    dial_source = Path(dial_module.__file__).read_text(encoding="utf-8")
    env_reads = set(re.findall(r'os\.environ(?:\.get)?\(?\[?"([A-Z_]+)"', dial_source))
    assert env_reads == {
        "LIVEKIT_URL",
        "LIVEKIT_API_KEY",
        "LIVEKIT_API_SECRET",
        "SIP_OUTBOUND_TRUNK_ID",
        "LIVEKIT_SIP_TRUNK_NAME",
        "AGENT_NAME",
    }
    predial_source = Path(predial.__file__).read_text(encoding="utf-8")
    assert "os.environ" not in predial_source
    assert "getenv" not in predial_source
    assert "dotenv" not in predial_source


def test_cli_exposes_no_rule_bypass_flags(dial_module):
    # The full option surface. Anything that skips, disables, or overrides a
    # rule outcome would have to appear here, and must not.
    args = dial_module.parse_args([CONTACT_C_NUMBER])
    assert sorted(vars(args)) == ["clock_override", "engagement_id", "now", "phone_number"]
