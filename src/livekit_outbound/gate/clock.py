"""The gate never reads a clock.

Every rule takes the instant it evaluates against as a required argument,
and the composite gate threads one instant through all of them. The only
place wall-clock time enters the pre-dial path is the caller
(:mod:`livekit_outbound.predial`), which either reads the clock once or
accepts an injected instant. That makes time-dependent rules exercisable
outside the live calling window without any rule being able to tell the
difference, and without any rule being skippable.
"""

from __future__ import annotations

from datetime import datetime


def require_aware_instant(now: datetime, *, name: str = "now") -> datetime:
    """Return ``now`` if it is timezone-aware; raise ``ValueError`` otherwise.

    A naive datetime has no defined instant, so it can neither be compared
    with the UTC timestamps in the database nor converted to the dialed
    number's local clock. Refusing it here keeps the failure loud and early.
    """
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError(
            f"{name} must be timezone-aware; got naive {now.isoformat()!r} "
            "(append Z for UTC or an explicit offset such as -04:00)"
        )
    return now
