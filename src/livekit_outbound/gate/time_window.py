"""Calling-hours rule: no calls before 8am or after 9pm local time.

Both the TCPA rules (47 CFR 64.1200(c)(1)) and Reg F (12 CFR
1006.6(b)(1)(i)) treat a time before 8:00 AM or after 9:00 PM, local time
at the called party's location, as presumptively inconvenient. This module
answers "is the clock at the dialed number's location inside the
presumptively-convenient window right now." Resolving *which* clock that
is -- from the dialed number's area code -- is the composite gate's job
(see gate/area_codes.py and gate/decide.py).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from livekit_outbound.gate.clock import require_aware_instant

WINDOW_START_LOCAL = time(8, 0, 0)
WINDOW_END_LOCAL = time(21, 0, 0)


@dataclass(frozen=True, slots=True)
class TimeWindowResult:
    allowed: bool
    local_time: datetime
    reason: str | None


def check_time_window(
    local_timezone: str,
    *,
    now: datetime,
) -> TimeWindowResult:
    """Evaluate the 8am-9pm local-time window for ``local_timezone``.

    Unlike the frequency and cooldown rules this takes no database
    session: the decision depends only on an IANA timezone name and the
    clock.

    The window is half-open -- allowed iff 08:00:00 <= local time < 21:00:00.
    8:00:00 AM exactly is allowed (the regulation permits calling *at* 8am),
    9:00:00 PM exactly is denied. This is the third boundary convention in
    this package, and each is deliberate: frequency.py counts attempts over
    a closed window (an attempt exactly 7 days old still counts),
    cooldown.py expires its cooldown exclusively (a conversation exactly
    7 days old no longer blocks), and here the two edges differ from each
    other because the regulation phrases the limits as "before 8am" and
    "after 9pm" acting on a clock that includes the boundary instants.

    ``now`` is the instant being judged. It is required and must be
    timezone-aware: this rule never reads the wall clock, so the caller
    decides (and a test or demo can inject) the instant. It is converted to
    the target local time via zoneinfo before comparison, so
    daylight-saving transitions are honored automatically.

    Raises :class:`ValueError` naming the offending string if
    ``local_timezone`` is not a recognized IANA timezone.
    """
    now = require_aware_instant(now)

    try:
        tz = ZoneInfo(local_timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(
            f"unknown timezone {local_timezone!r}: not a valid IANA timezone name"
        ) from exc

    local_time = now.astimezone(tz)
    local_clock = local_time.time()

    if local_clock < WINDOW_START_LOCAL:
        reason = (
            f"local time {local_time.isoformat()} is before the "
            f"{WINDOW_START_LOCAL.isoformat()} window start"
        )
        return TimeWindowResult(allowed=False, local_time=local_time, reason=reason)

    if local_clock >= WINDOW_END_LOCAL:
        reason = (
            f"local time {local_time.isoformat()} is at or after the "
            f"{WINDOW_END_LOCAL.isoformat()} window end"
        )
        return TimeWindowResult(allowed=False, local_time=local_time, reason=reason)

    return TimeWindowResult(allowed=True, local_time=local_time, reason=None)
