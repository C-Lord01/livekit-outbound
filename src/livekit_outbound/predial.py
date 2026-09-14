"""Caller-side adapter between the dialer and the policy gate.

The gate (:mod:`livekit_outbound.gate`) decides in terms of a contact, an
engagement, a number that is on file for that contact, and an instant to
evaluate at. A dialer only has a phone number. This module does the
translation, without touching the rules:

1. Resolve the number to a contact and an engagement in the gate database.
   A number the gate has never seen is enrolled as a new contact with a
   fresh engagement, using the timezone derived from its area code, so a
   number with no history is a *clean* number rather than an error. A
   number whose location cannot be derived (non-NANP, toll-free) cannot be
   enrolled safely and is refused before the gate runs.
2. Pick the evaluation instant. This is the only place in the pre-dial
   path that reads the wall clock (:func:`utcnow`). A caller may instead
   inject an instant (``clock_override``); the gate then evaluates every
   rule at that instant and stamps the audit row as simulated. Injection
   moves the clock the rules see and nothing else: no rule is skipped,
   relaxed, or overridden.
3. Run :func:`evaluate_gate`, which writes the audit record itself.
4. Package the decision with the inputs that produced it, for display and
   for the dialer to branch on.

It also records the dial as a :class:`CallAttempt` once the dialer commits
to placing the call, so subsequent gate evaluations count it toward the
frequency cap.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from livekit_outbound.gate.area_codes import timezones_for_number
from livekit_outbound.gate.clock import require_aware_instant
from livekit_outbound.gate.decide import (
    RULES,
    GateDecision,
    evaluate_gate,
    resolve_timezones,
)
from livekit_outbound.gate.models import (
    AuditDecision,
    AuditLogEntry,
    CallAttempt,
    CallOutcome,
    Contact,
    Engagement,
    PhoneNumber,
)
from livekit_outbound.gate.time_window import WINDOW_END_LOCAL, WINDOW_START_LOCAL

AUTO_ENROLLED_ENGAGEMENT = "Auto-enrolled by the dialer"


class PredialError(Exception):
    """The number could not be resolved to a gate subject; nothing was dialed."""


class UnresolvableNumberError(PredialError):
    """Unknown number whose location cannot be derived from its area code."""


class AmbiguousEngagementError(PredialError):
    """The contact has several engagements and none was specified."""

    def __init__(self, contact: Contact, engagements: list[Engagement]) -> None:
        self.contact = contact
        self.engagements = engagements
        listing = ", ".join(f"#{e.id} ({e.description})" for e in engagements)
        super().__init__(
            f"contact #{contact.id} has {len(engagements)} engagements; "
            f"pass --engagement-id with one of: {listing}"
        )


def utcnow() -> datetime:
    """The one wall-clock read in the pre-dial path. Tests may patch it."""
    return datetime.now(timezone.utc)


def parse_instant(text: str) -> datetime:
    """Parse an ISO-8601 timestamp for clock injection, in UTC.

    The timestamp must carry an explicit UTC offset or a trailing ``Z``. A
    naive timestamp is rejected rather than interpreted in some assumed
    zone, because the whole point of the calling-window rule is *which*
    clock a time is read on.
    """
    try:
        parsed = datetime.fromisoformat(text.strip())
    except ValueError as exc:
        raise ValueError(
            f"{text!r} is not an ISO-8601 timestamp "
            "(expected e.g. 2026-07-15T12:30:00Z or 2026-07-15T08:30:00-04:00)"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(
            f"{text!r} has no timezone: append Z for UTC or an explicit offset "
            "such as -04:00. The gate will not assume a zone."
        )
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True, slots=True)
class DialAuthorization:
    """A gate decision together with the inputs that produced it."""

    decision: GateDecision
    number: str
    contact: Contact
    engagement: Engagement
    timezones: tuple[str, ...]
    timezone_source: str
    audit_entry_id: int
    enrolled: bool
    # The injected evaluation instant, or None when the wall clock was used.
    clock_override: datetime | None

    @property
    def allowed(self) -> bool:
        return self.decision.allowed

    @property
    def reason_code(self) -> str:
        return self.decision.reason_code

    @property
    def rule_name(self) -> str | None:
        return self.decision.failed_check

    @property
    def simulated(self) -> bool:
        return self.clock_override is not None


def authorize(
    session: Session,
    number: str,
    *,
    engagement_id: int | None = None,
    clock_override: datetime | None = None,
    auto_enroll: bool = True,
) -> DialAuthorization:
    """Ask the gate whether ``number`` may be dialed.

    With ``clock_override`` unset the gate evaluates at the current wall
    clock. With it set, every rule evaluates at that instant instead and
    the audit row is marked simulated. Either way the same rules run with
    the same inputs; the override cannot change a rule's outcome except by
    changing the instant it is asked about.

    Raises :class:`PredialError` if the number cannot be tied to a gate
    subject at all; in that case no gate evaluation and no audit record
    happens, because there is no engagement to write it against. Any
    exception from inside the gate propagates after the gate has audited
    it as a DENY.
    """
    if clock_override is not None:
        now = require_aware_instant(clock_override, name="clock_override")
        simulated = True
    else:
        now = utcnow()
        simulated = False

    phone = session.scalar(select(PhoneNumber).where(PhoneNumber.number == number))
    enrolled = False
    if phone is None:
        if not auto_enroll:
            raise UnresolvableNumberError(
                f"{number} is not on file and auto-enrollment is disabled"
            )
        contact = _enroll_contact(session, number)
        enrolled = True
    else:
        contact = phone.contact

    engagement = _select_engagement(session, contact, engagement_id)

    decision = evaluate_gate(
        session, contact.id, engagement.id, number, now=now, simulated=simulated
    )
    zones, source = resolve_timezones(number, contact.timezone)
    audit_entry = _audit_entry_for(session, engagement.id, decision)
    return DialAuthorization(
        decision=decision,
        number=number,
        contact=contact,
        engagement=engagement,
        timezones=zones,
        timezone_source=source,
        audit_entry_id=audit_entry.id,
        enrolled=enrolled,
        clock_override=now if simulated else None,
    )


def record_attempt(
    session: Session,
    authorization: DialAuthorization,
    *,
    external_call_id: str | None,
    attempted_at: datetime | None = None,
) -> CallAttempt:
    """Record that the dialer is placing the call the gate just allowed.

    Must be called only after an ALLOW. The attempt starts as ``pending``;
    the dialer updates it with :func:`mark_attempt` when it learns the
    outcome. A pending attempt already counts toward the 7-in-7 cap, which
    is exactly the point: the next gate evaluation sees this dial.

    ``attempted_at`` is the wall clock even when the authorization was
    evaluated at an injected instant, because the dial really is happening
    now; the injected instant is carried on ``simulated_now`` instead so
    the attempt is visibly marked.
    """
    if not authorization.allowed:
        raise ValueError("refusing to record a call attempt for a denied authorization")
    phone = session.scalar(
        select(PhoneNumber)
        .where(PhoneNumber.contact_id == authorization.contact.id)
        .where(PhoneNumber.number == authorization.number)
    )
    assert phone is not None, "an ALLOW decision implies the number is on file"
    attempt = CallAttempt(
        engagement_id=authorization.engagement.id,
        phone_number_id=phone.id,
        external_call_id=external_call_id,
        attempted_at=attempted_at if attempted_at is not None else utcnow(),
        outcome=CallOutcome.PENDING,
        simulated_now=authorization.clock_override,
    )
    session.add(attempt)
    session.commit()
    return attempt


def mark_attempt(session: Session, attempt: CallAttempt, outcome: CallOutcome) -> None:
    attempt.outcome = outcome
    session.commit()


def format_authorization(authorization: DialAuthorization) -> str:
    """A demo-legible rendering: verdict, reason code, rule, and inputs."""
    decision = authorization.decision
    now = decision.checked_at
    lines: list[str] = []
    verdict = "DENY" if not decision.allowed else "ALLOW"
    lines.append("=" * 64)
    lines.append(f"PRE-DIAL POLICY GATE: {verdict}")
    lines.append("=" * 64)
    if authorization.clock_override is not None:
        lines.append(f"  !! CLOCK OVERRIDDEN (--now): every rule evaluated at {now.isoformat()},")
        lines.append("  !! not the wall clock. No rule was skipped or relaxed. The audit entry")
        lines.append("  !! and any call attempt are permanently marked simulated.")
    lines.append(f"  reason code   {decision.reason_code}")

    if decision.rule is not None:
        rule = decision.rule
        lines.append(f"  rule          {rule.check}: {rule.title}")
        lines.append(f"  authority     {rule.citation}")
    else:
        lines.append("  rules passed  " + ", ".join(rule.check for rule in RULES))

    lines.append("  inputs")
    lines.append(f"    number        {authorization.number}")
    contact_note = " (enrolled just now)" if authorization.enrolled else ""
    lines.append(
        f"    contact       #{authorization.contact.id} {authorization.contact.name}{contact_note}"
    )
    lines.append(
        f"    engagement    #{authorization.engagement.id} {authorization.engagement.description}"
    )
    clock_note = " (injected)" if authorization.clock_override is not None else ""
    lines.append(f"    evaluated at  {now.isoformat()}{clock_note}")
    lines.append(
        f"    timezone      {', '.join(authorization.timezones)} "
        f"(from {authorization.timezone_source})"
    )
    for zone in authorization.timezones:
        local = now.astimezone(ZoneInfo(zone))
        lines.append(f"    local time    {local.strftime('%Y-%m-%d %H:%M:%S %Z')} in {zone}")
    lines.append(
        f"    window        {WINDOW_START_LOCAL.strftime('%H:%M')}-"
        f"{WINDOW_END_LOCAL.strftime('%H:%M')} local"
    )
    attempts = (
        f"{decision.attempt_count} in the last 7 days"
        if decision.attempt_count is not None
        else "not counted (an earlier rule decided first)"
    )
    lines.append(f"    attempts      {attempts}")

    if decision.reason is not None:
        lines.append(f"  detail        {decision.reason}")
    simulated_note = ", marked simulated" if authorization.clock_override is not None else ""
    lines.append(f"  audit         entry #{authorization.audit_entry_id} written ({verdict}{simulated_note})")
    lines.append("=" * 64)
    return "\n".join(lines)


def _enroll_contact(session: Session, number: str) -> Contact:
    zones = timezones_for_number(number)
    if not zones:
        raise UnresolvableNumberError(
            f"{number} is not on file and its timezone cannot be derived from its "
            "area code; register the contact with a timezone before dialing"
        )
    contact = Contact(name=f"Contact {number}", timezone=zones[0])
    phone = PhoneNumber(contact=contact, number=number, is_active=True)
    session.add_all([contact, phone])
    session.commit()
    return contact


def _select_engagement(
    session: Session, contact: Contact, engagement_id: int | None
) -> Engagement:
    engagements = list(
        session.scalars(
            select(Engagement).where(Engagement.contact_id == contact.id).order_by(Engagement.id)
        ).all()
    )
    if engagement_id is not None:
        for engagement in engagements:
            if engagement.id == engagement_id:
                return engagement
        raise PredialError(
            f"engagement #{engagement_id} does not belong to contact #{contact.id}"
        )
    if len(engagements) == 1:
        return engagements[0]
    if not engagements:
        engagement = Engagement(
            contact_id=contact.id, description=AUTO_ENROLLED_ENGAGEMENT, status="active"
        )
        session.add(engagement)
        session.commit()
        return engagement
    raise AmbiguousEngagementError(contact, engagements)


def _audit_entry_for(session: Session, engagement_id: int, decision: GateDecision) -> AuditLogEntry:
    entry = session.scalar(
        select(AuditLogEntry)
        .where(AuditLogEntry.engagement_id == engagement_id)
        .where(AuditLogEntry.decided_at == decision.checked_at)
        .where(AuditLogEntry.decision == AuditDecision(decision.decision))
        .order_by(AuditLogEntry.id.desc())
        .limit(1)
    )
    assert entry is not None, "the gate writes an audit entry on every evaluation"
    return entry
