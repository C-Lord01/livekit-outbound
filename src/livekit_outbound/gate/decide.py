"""Composite pre-dial policy gate.

Combines every per-rule check into a single ALLOW/DENY decision for one
proposed call, in a fixed short-circuit order: number on file, suppression
flag, post-conversation cooldown, 7-in-7 frequency, then the 8am-9pm
local-time window at the dialed number. The ordering runs the
cheapest/most-absolute prohibitions first -- a stop-contact request forbids
the call no matter what the calling history or clock says.

Every evaluation writes exactly one :class:`AuditLogEntry`, on every code
path including crashes inside an individual check, and commits it. A
decision with no audit trail is itself a compliance failure, so the audit
write is the one thing this module is not allowed to skip.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from livekit_outbound.gate.area_codes import area_code, timezones_for_number
from livekit_outbound.gate.clock import require_aware_instant
from livekit_outbound.gate.cooldown import check_conversation_cooldown
from livekit_outbound.gate.frequency import check_seven_in_seven
from livekit_outbound.gate.models import (
    AuditDecision,
    AuditLogEntry,
    Contact,
    PhoneNumber,
    SuppressionFlag,
)
from livekit_outbound.gate.time_window import check_time_window


@dataclass(frozen=True, slots=True)
class Rule:
    """Descriptive metadata for one gate rule, keyed by its ``check`` name."""

    check: str
    reason_code: str
    title: str
    citation: str


# Evaluation order. ``check`` is the value ``GateDecision.failed_check``
# carries when that rule denies; ``reason_code`` is the stable, structured
# identifier callers should log and branch on.
RULES: tuple[Rule, ...] = (
    Rule(
        check="phone_number",
        reason_code="NUMBER_NOT_ON_FILE",
        title="Dialed number must be on file for the contact",
        citation="operational control (fails closed)",
    ),
    Rule(
        check="suppression",
        reason_code="SUPPRESSED",
        title="Stop-contact / do-not-call request on file",
        citation="TCPA 47 CFR 64.1200(d); FDCPA 15 U.S.C. 1692c(c); Reg F 12 CFR 1006.6(c)",
    ),
    Rule(
        check="conversation_cooldown",
        reason_code="COOLDOWN_ACTIVE",
        title="No call within 7 days of a live conversation",
        citation="Reg F 12 CFR 1006.14(b)(2)(ii)",
    ),
    Rule(
        check="seven_in_seven",
        reason_code="FREQUENCY_CAP_REACHED",
        title="No more than 7 attempts in any rolling 7-day window",
        citation="Reg F 12 CFR 1006.14(b)(2)(i)",
    ),
    Rule(
        check="time_window",
        reason_code="OUTSIDE_CALLING_WINDOW",
        title="8:00 AM to 9:00 PM local time at the dialed number",
        citation="TCPA 47 CFR 64.1200(c)(1); Reg F 12 CFR 1006.6(b)(1)(i)",
    ),
)
RULES_BY_CHECK: dict[str, Rule] = {rule.check: rule for rule in RULES}
ALLOW_REASON_CODE = "ALLOWED"


@dataclass(frozen=True, slots=True)
class GateDecision:
    decision: Literal["ALLOW", "DENY"]
    reason: str | None
    attempt_count: int | None
    checked_at: datetime
    failed_check: str | None
    # True when ``checked_at`` was injected rather than read from the wall
    # clock. Informational only: the rules evaluated exactly as they would
    # have at that instant.
    simulated: bool = False

    @property
    def allowed(self) -> bool:
        return self.decision == "ALLOW"

    @property
    def rule(self) -> Rule | None:
        """The rule that denied, or None on ALLOW."""
        return None if self.failed_check is None else RULES_BY_CHECK[self.failed_check]

    @property
    def reason_code(self) -> str:
        """Stable structured code: ``ALLOWED`` or the denying rule's code."""
        rule = self.rule
        return ALLOW_REASON_CODE if rule is None else rule.reason_code


def resolve_timezones(number: str, fallback_timezone: str) -> tuple[tuple[str, ...], str]:
    """The IANA zones the calling window is evaluated in, and where they came from.

    The dialed number's area code is authoritative. Only when the number's
    location cannot be derived (non-NANP, toll-free, unknown code) does the
    contact's recorded home timezone apply. The second element is a short
    human-readable provenance string for audit reasons and display.
    """
    zones = timezones_for_number(number)
    if zones:
        return zones, f"area code {area_code(number)}"
    return (fallback_timezone,), "contact record (area code not recognized)"


def evaluate_gate(
    db_session: Session,
    contact_id: int,
    engagement_id: int,
    proposed_number: str,
    *,
    now: datetime,
    simulated: bool = False,
) -> GateDecision:
    """Decide whether a call to ``proposed_number`` about ``engagement_id``
    may be placed at ``now``, and write the audit log entry for the decision.

    Checks short-circuit on first failure -- once one denies, later checks
    never run. ``attempt_count`` is therefore only populated when the
    7-in-7 check actually executed (whether it passed or failed), and is
    None when the gate denied earlier. ``failed_check`` names the denying
    check ("phone_number", "suppression", "conversation_cooldown",
    "seven_in_seven", "time_window") or is None on ALLOW; ``reason_code``
    maps it to the structured code in :data:`RULES`.

    An exception inside any check is audited as a DENY (the call did not
    get permission) and then re-raised, so callers still see the failure
    but the audit trail never has a gap.

    ``now`` is the instant every rule evaluates against. It is required and
    must be timezone-aware: nothing in the gate package reads the wall
    clock, so the caller is the single place a clock is consulted, and a
    test or a demo can hand in any instant. It is threaded through to every
    underlying check so the whole decision is evaluated at one consistent
    instant.

    ``simulated`` declares that ``now`` was injected rather than read from
    a clock. It changes nothing about how the rules evaluate; it only
    stamps the audit row's ``simulated_now`` (and the returned decision) so
    the record is permanently marked as produced under an overridden clock.
    """
    now = require_aware_instant(now)

    phone = db_session.scalar(
        select(PhoneNumber)
        .where(PhoneNumber.contact_id == contact_id)
        .where(PhoneNumber.number == proposed_number)
    )

    try:
        decision = _run_checks(db_session, contact_id, engagement_id, phone, proposed_number, now)
    except Exception as exc:
        _write_audit(
            db_session,
            engagement_id=engagement_id,
            phone_number_id=phone.id if phone is not None else None,
            decision=AuditDecision.DENY,
            reason=f"gate check raised {type(exc).__name__}: {exc}",
            attempt_count=None,
            decided_at=now,
            simulated=simulated,
        )
        raise

    _write_audit(
        db_session,
        engagement_id=engagement_id,
        phone_number_id=phone.id if phone is not None else None,
        decision=AuditDecision(decision.decision),
        reason=decision.reason if decision.reason is not None else "all checks passed",
        attempt_count=decision.attempt_count,
        decided_at=now,
        simulated=simulated,
    )
    return replace(decision, simulated=simulated)


def _run_checks(
    db_session: Session,
    contact_id: int,
    engagement_id: int,
    phone: PhoneNumber | None,
    proposed_number: str,
    now: datetime,
) -> GateDecision:
    # A number we can't tie to this contact is a DENY, not an exception:
    # the gate's contract is "may this number be dialed now", and a number
    # that isn't on file for the contact must never be dialed -- but that's
    # an operational data problem the dialer should handle like any other
    # denial, not a programming error deserving a crash. It also
    # short-circuits before every other check, because the remaining checks
    # reason about *this contact's* history, which proves nothing about a
    # number that isn't theirs.
    if phone is None:
        return GateDecision(
            decision="DENY",
            reason=(
                f"proposed number {proposed_number!r} is not on file for "
                f"contact {contact_id}"
            ),
            attempt_count=None,
            checked_at=now,
            failed_check="phone_number",
        )

    flag = db_session.scalar(
        select(SuppressionFlag).where(SuppressionFlag.engagement_id == engagement_id)
    )
    if flag is not None:
        return GateDecision(
            decision="DENY",
            reason=f"suppression flag active: {flag.reason}",
            attempt_count=None,
            checked_at=now,
            failed_check="suppression",
        )

    cooldown = check_conversation_cooldown(db_session, engagement_id, now=now)
    if not cooldown.allowed:
        return GateDecision(
            decision="DENY",
            reason=cooldown.reason,
            attempt_count=None,
            checked_at=now,
            failed_check="conversation_cooldown",
        )

    frequency = check_seven_in_seven(db_session, engagement_id, now=now)
    if not frequency.allowed:
        return GateDecision(
            decision="DENY",
            reason=frequency.reason,
            attempt_count=frequency.attempt_count,
            checked_at=now,
            failed_check="seven_in_seven",
        )

    contact = db_session.get(Contact, contact_id)
    if contact is None:
        raise ValueError(f"no contact with id {contact_id}")

    # The calling window is judged on the clock at the dialed number's
    # location, never the server's. When an area code straddles a boundary
    # every candidate zone must be inside the window.
    zones, zone_source = resolve_timezones(phone.number, contact.timezone)
    for zone in zones:
        window = check_time_window(zone, now=now)
        if not window.allowed:
            return GateDecision(
                decision="DENY",
                reason=f"{window.reason} in {zone} (timezone from {zone_source})",
                attempt_count=frequency.attempt_count,
                checked_at=now,
                failed_check="time_window",
            )

    return GateDecision(
        decision="ALLOW",
        reason=None,
        attempt_count=frequency.attempt_count,
        checked_at=now,
        failed_check=None,
    )


def _write_audit(
    db_session: Session,
    *,
    engagement_id: int,
    phone_number_id: int | None,
    decision: AuditDecision,
    reason: str,
    attempt_count: int | None,
    decided_at: datetime,
    simulated: bool,
) -> None:
    db_session.add(
        AuditLogEntry(
            engagement_id=engagement_id,
            phone_number_id=phone_number_id,
            decision=decision,
            reason=reason,
            attempt_count_at_decision=attempt_count,
            decided_at=decided_at,
            simulated_now=decided_at if simulated else None,
        )
    )
    db_session.commit()
