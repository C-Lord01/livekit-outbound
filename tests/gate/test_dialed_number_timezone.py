"""The calling window is judged at the dialed number's location.

These tests drive the composite gate end to end and pin down three
properties of the calling-window check:

1. The clock used is the one at the dialed number's area code, not the
   server's clock and not the contact's home timezone on file.
2. The decision is a function of the instant only -- the same instant
   expressed in any tzinfo produces the same verdict, so the server's local
   timezone can never leak in.
3. Daylight-saving transitions at the number's location are honored.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from livekit_outbound.gate.decide import evaluate_gate
from livekit_outbound.gate.models import Contact, Engagement, PhoneNumber


def _enroll(session, number: str, recorded_timezone: str) -> tuple[Contact, Engagement]:
    """A test-local contact with one number and one engagement.

    ``recorded_timezone`` is the contact's home timezone on file; the tests
    deliberately make it disagree with the number's area code.
    """
    contact = Contact(name=f"Test Contact {number}", timezone=recorded_timezone)
    engagement = Engagement(contact=contact, description="Synthetic timezone test", status="active")
    phone = PhoneNumber(contact=contact, number=number, is_active=True)
    session.add_all([contact, engagement, phone])
    session.commit()
    return contact, engagement


def _decide(session, number: str, now: datetime, recorded_timezone: str = "UTC"):
    contact, engagement = _enroll(session, number, recorded_timezone)
    return evaluate_gate(session, contact.id, engagement.id, number, now=now)


# --- 1. Dialed number's location, not the server or the contact record -----


def test_same_instant_two_numbers_in_different_zones_get_different_verdicts(db_session):
    # 2026-07-15 03:30 UTC is 23:30 in New York (denied) and 20:30 in Los
    # Angeles (allowed). Only the area code distinguishes the two calls.
    instant = datetime(2026, 7, 15, 3, 30, tzinfo=timezone.utc)

    new_york = _decide(db_session, "+12125550100", instant)
    los_angeles = _decide(db_session, "+12135550100", instant)

    assert new_york.decision == "DENY"
    assert new_york.failed_check == "time_window"
    assert "America/New_York" in new_york.reason
    assert "area code 212" in new_york.reason
    assert los_angeles.decision == "ALLOW"


def test_area_code_overrides_the_contacts_recorded_timezone(db_session):
    # The contact record claims New York, but the number being dialed is a
    # Los Angeles number. At 03:30 UTC the record's clock says 23:30
    # (deny) while the number's clock says 20:30 (allow). The number wins.
    instant = datetime(2026, 7, 15, 3, 30, tzinfo=timezone.utc)

    result = _decide(db_session, "+12135550100", instant, recorded_timezone="America/New_York")

    assert result.decision == "ALLOW"


def test_area_code_overrides_the_contacts_recorded_timezone_in_the_denying_direction(db_session):
    # Mirror image: record says New York (10:30, allow) but the Los Angeles
    # number's clock reads 07:30 (deny). Overriding must not only ever
    # loosen the verdict.
    instant = datetime(2026, 7, 15, 14, 30, tzinfo=timezone.utc)

    result = _decide(db_session, "+12135550100", instant, recorded_timezone="America/New_York")

    assert result.decision == "DENY"
    assert result.failed_check == "time_window"
    assert "07:30" in result.reason
    assert "America/Los_Angeles" in result.reason


def test_recorded_timezone_is_only_used_when_the_area_code_is_unknown(db_session):
    # A toll-free number has no location; the gate falls back to the
    # contact's recorded timezone and says so in the reason.
    instant = datetime(2026, 7, 15, 3, 30, tzinfo=timezone.utc)  # 23:30 in New York

    result = _decide(db_session, "+18005550100", instant, recorded_timezone="America/New_York")

    assert result.decision == "DENY"
    assert "America/New_York" in result.reason
    assert "contact record" in result.reason


@pytest.mark.parametrize(
    "server_tz",
    ["UTC", "Asia/Tokyo", "Europe/London", "America/Los_Angeles", "Pacific/Auckland"],
)
def test_verdict_is_independent_of_the_tzinfo_the_clock_is_expressed_in(db_session, server_tz):
    # One instant, expressed the way a server in ``server_tz`` would hand it
    # over. The verdict must be identical every time: the gate reasons about
    # the instant, converted to the *number's* zone, never about the wall
    # clock it was given.
    instant_utc = datetime(2026, 7, 15, 3, 30, tzinfo=timezone.utc)
    instant_as_seen_by_server = instant_utc.astimezone(ZoneInfo(server_tz))
    assert instant_as_seen_by_server == instant_utc  # same instant, different clock face

    new_york = _decide(db_session, "+12125550100", instant_as_seen_by_server)
    los_angeles = _decide(db_session, "+12135550100", instant_as_seen_by_server)

    assert new_york.decision == "DENY"
    assert los_angeles.decision == "ALLOW"
    # The audit timestamp is the instant, not the server's wall clock.
    assert new_york.checked_at == instant_utc


def test_split_area_code_requires_the_window_to_hold_in_every_candidate_zone(db_session):
    # 850 (Florida panhandle) spans Eastern and Central. At 12:30 UTC it is
    # 08:30 Eastern (allowed) but 07:30 Central (denied): the gate cannot
    # know which side of the line the number is on, so it must deny.
    early = datetime(2026, 7, 15, 12, 30, tzinfo=timezone.utc)
    midday = datetime(2026, 7, 15, 16, 0, tzinfo=timezone.utc)  # 12:00 / 11:00
    late = datetime(2026, 7, 16, 1, 30, tzinfo=timezone.utc)  # 21:30 / 20:30

    assert _decide(db_session, "+18505550101", early).decision == "DENY"
    assert _decide(db_session, "+18505550102", midday).decision == "ALLOW"
    late_result = _decide(db_session, "+18505550103", late)
    assert late_result.decision == "DENY"
    assert "America/New_York" in late_result.reason


# --- 2. Daylight-saving boundaries at the number's location ------------------


def test_spring_forward_shifts_the_window_start_by_an_hour_in_utc(db_session):
    # US daylight saving time began 2026-03-08 at 02:00 local. 12:30 UTC is
    # 07:30 EST the day before (denied) and 08:30 EDT on the day itself
    # (allowed). A fixed-offset implementation would give the same verdict
    # on both days.
    before = datetime(2026, 3, 7, 12, 30, tzinfo=timezone.utc)
    after = datetime(2026, 3, 8, 12, 30, tzinfo=timezone.utc)

    day_before = _decide(db_session, "+12125550101", before)
    day_of = _decide(db_session, "+12125550102", after)

    assert day_before.decision == "DENY"
    assert "07:30" in day_before.reason
    assert "-05:00" in day_before.reason  # EST
    assert day_of.decision == "ALLOW"


def test_fall_back_shifts_the_window_end_by_an_hour_in_utc(db_session):
    # US daylight saving time ended 2026-11-01 at 02:00 local. 01:30 UTC is
    # 21:30 EDT on the evening of Oct 31 (denied) and 20:30 EST on the
    # evening of Nov 1 (allowed).
    before = datetime(2026, 11, 1, 1, 30, tzinfo=timezone.utc)
    after = datetime(2026, 11, 2, 1, 30, tzinfo=timezone.utc)

    evening_before = _decide(db_session, "+12125550101", before)
    evening_after = _decide(db_session, "+12125550102", after)

    assert evening_before.decision == "DENY"
    assert "21:30" in evening_before.reason
    assert "-04:00" in evening_before.reason  # EDT
    assert evening_after.decision == "ALLOW"


def test_exact_transition_instant_is_evaluated_on_the_new_offset(db_session):
    # At 2026-03-08 07:00:00 UTC the New York clock jumps from 01:59:59 EST
    # straight to 03:00:00 EDT. One second before is 01:59:59 (denied,
    # before 8am); the transition instant itself is 03:00 (still denied).
    # Then 12:00 UTC that day is 08:00:00 EDT exactly: the inclusive window
    # start, allowed. Same UTC clock the previous day was 07:00 EST: denied.
    transition = datetime(2026, 3, 8, 7, 0, 0, tzinfo=timezone.utc)

    just_before = _decide(db_session, "+12125550101", transition - timedelta(seconds=1))
    at_transition = _decide(db_session, "+12125550102", transition)
    eight_am_edt = _decide(db_session, "+12125550103", datetime(2026, 3, 8, 12, 0, tzinfo=timezone.utc))
    seven_am_est = _decide(db_session, "+12125550104", datetime(2026, 3, 7, 12, 0, tzinfo=timezone.utc))

    assert "01:59:59" in just_before.reason
    assert at_transition.decision == "DENY"
    assert "03:00:00" in at_transition.reason
    assert eight_am_edt.decision == "ALLOW"
    assert seven_am_est.decision == "DENY"


def test_zone_without_daylight_saving_is_not_shifted(db_session):
    # Arizona (602) stays on MST year-round; Colorado (303) is on MDT in
    # July. Same instant, 14:30 UTC: 07:30 in Phoenix (denied) but 08:30 in
    # Denver (allowed). Deriving the zone from the area code, rather than a
    # nominal "Mountain" offset, is what gets this right.
    instant = datetime(2026, 7, 15, 14, 30, tzinfo=timezone.utc)

    phoenix = _decide(db_session, "+16025550100", instant)
    denver = _decide(db_session, "+13035550100", instant)

    assert phoenix.decision == "DENY"
    assert "America/Phoenix" in phoenix.reason
    assert denver.decision == "ALLOW"
