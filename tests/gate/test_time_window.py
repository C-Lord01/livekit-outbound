"""Tests for the 8am-9pm local-time window rule (livekit_outbound/gate/time_window.py)."""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from livekit_outbound.gate.time_window import check_time_window

CHICAGO = "America/Chicago"


def _utc_at_local(tz_name: str, hour: int, minute: int = 0, second: int = 0) -> datetime:
    """The UTC instant at which a clock in ``tz_name`` shows the given local time."""
    local = datetime(2026, 7, 15, hour, minute, second, tzinfo=ZoneInfo(tz_name))
    return local.astimezone(timezone.utc)


def test_noon_local_is_allowed():
    result = check_time_window(CHICAGO, now=_utc_at_local(CHICAGO, 12))

    assert result.allowed is True
    assert result.local_time.hour == 12
    assert result.reason is None


def test_one_second_before_eight_am_local_is_denied():
    result = check_time_window(CHICAGO, now=_utc_at_local(CHICAGO, 7, 59, 59))

    assert result.allowed is False
    assert result.reason is not None
    assert "before the 08:00:00 window start" in result.reason
    assert result.local_time.isoformat() in result.reason


def test_exactly_eight_am_local_is_allowed():
    result = check_time_window(CHICAGO, now=_utc_at_local(CHICAGO, 8, 0, 0))

    assert result.allowed is True
    assert result.reason is None


def test_exactly_nine_pm_local_is_denied():
    result = check_time_window(CHICAGO, now=_utc_at_local(CHICAGO, 21, 0, 0))

    assert result.allowed is False
    assert result.reason is not None
    assert "at or after the 21:00:00 window end" in result.reason
    assert result.local_time.isoformat() in result.reason


def test_one_second_before_nine_pm_local_is_allowed():
    result = check_time_window(CHICAGO, now=_utc_at_local(CHICAGO, 20, 59, 59))

    assert result.allowed is True
    assert result.reason is None


def test_conversion_uses_target_local_time_not_utc():
    # 09:30 UTC is mid-morning in UTC (would be allowed), but 21:30 in
    # Auckland (UTC+12 in July) -- after the window end. A denial here proves
    # the rule really converts to the target clock instead of comparing
    # the UTC time directly.
    utc_morning = datetime(2026, 7, 15, 9, 30, tzinfo=timezone.utc)

    result = check_time_window("Pacific/Auckland", now=utc_morning)

    assert result.allowed is False
    assert result.local_time.hour == 21
    assert result.local_time.minute == 30


def test_unknown_timezone_raises_value_error_naming_the_string():
    with pytest.raises(ValueError, match="Not/AZone"):
        check_time_window("Not/AZone", now=datetime(2026, 7, 15, 12, tzinfo=timezone.utc))
