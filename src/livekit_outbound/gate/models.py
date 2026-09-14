"""SQLAlchemy models for the outbound-voice policy gate.

Schema only: no rule logic lives here. The vocabulary is deliberately
generic so the gate applies to any regulated outbound calling program:

- A :class:`Contact` is the person being called. They may have several
  :class:`PhoneNumber` records.
- An :class:`Engagement` is one matter the organization is calling the
  contact about. Frequency limits, cooldowns, suppression flags, and
  calling-window overrides are all scoped to an engagement, because that is
  the unit the underlying regulations count against.
- A :class:`CallAttempt` is one dial. Attempts on any of a contact's
  numbers all accrue against the same engagement.
- A :class:`SuppressionFlag` records that contact must stop (a do-not-call
  request, a request to communicate only in writing, a referral to counsel,
  and so on).
- A :class:`CallingWindowOverride` narrows the default calling hours for an
  engagement when the contact has stated a preference.
- An :class:`AuditLogEntry` is the immutable record of every ALLOW/DENY
  decision the gate makes.

All timestamp columns use :class:`livekit_outbound.gate.db.UTCDateTime`,
which requires timezone-aware UTC datetimes on write and always returns
timezone-aware UTC datetimes on read (SQLite has no native tz-aware
timestamp type, so this is enforced at the application layer).
``window_start_local``/``window_end_local`` on
:class:`CallingWindowOverride` are plain times-of-day, interpreted in the
local timezone of the number being dialed.
"""

from __future__ import annotations

import enum
from datetime import datetime, time
from typing import Optional

from sqlalchemy import CheckConstraint, ForeignKey, Index, String, Text, func
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship

from livekit_outbound.gate.db import Base, UTCDateTime


class CallOutcome(str, enum.Enum):
    VOICEMAIL = "voicemail"
    NO_ANSWER = "no_answer"
    LIVE_CONVERSATION = "live_conversation"
    FAILED = "failed"
    PENDING = "pending"


class AuditDecision(str, enum.Enum):
    ALLOW = "ALLOW"
    DENY = "DENY"


def _enum_type(py_enum: type[enum.Enum]) -> SAEnum:
    """Store enum ``.value`` strings rather than SQLAlchemy's default ``.name``."""
    return SAEnum(py_enum, values_callable=lambda e: [member.value for member in e], native_enum=False)


class Contact(Base):
    """The person being called."""

    __tablename__ = "contacts"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    # IANA timezone name, e.g. "America/Chicago". This is the contact's
    # home timezone and is only a fallback: the calling-window check
    # resolves the timezone of the specific number being dialed from its
    # area code first (see gate/area_codes.py).
    timezone: Mapped[str] = mapped_column(String(64))

    engagements: Mapped[list["Engagement"]] = relationship(back_populates="contact")
    phone_numbers: Mapped[list["PhoneNumber"]] = relationship(back_populates="contact")


class Engagement(Base):
    """One matter the organization is contacting a contact about.

    Every per-engagement rule (frequency, cooldown, suppression, window
    override, audit) hangs off this row.
    """

    __tablename__ = "engagements"

    id: Mapped[int] = mapped_column(primary_key=True)
    contact_id: Mapped[int] = mapped_column(ForeignKey("contacts.id"), index=True)
    description: Mapped[str] = mapped_column(String(500))
    status: Mapped[str] = mapped_column(String(50), index=True)

    contact: Mapped["Contact"] = relationship(back_populates="engagements")
    call_attempts: Mapped[list["CallAttempt"]] = relationship(back_populates="engagement")
    suppression_flags: Mapped[list["SuppressionFlag"]] = relationship(back_populates="engagement")
    calling_window_overrides: Mapped[list["CallingWindowOverride"]] = relationship(
        back_populates="engagement"
    )
    audit_log_entries: Mapped[list["AuditLogEntry"]] = relationship(back_populates="engagement")


class PhoneNumber(Base):
    __tablename__ = "phone_numbers"

    id: Mapped[int] = mapped_column(primary_key=True)
    contact_id: Mapped[int] = mapped_column(ForeignKey("contacts.id"), index=True)
    # E.164, e.g. "+13125550101".
    number: Mapped[str] = mapped_column(String(32))
    is_active: Mapped[bool] = mapped_column(default=True)

    contact: Mapped["Contact"] = relationship(back_populates="phone_numbers")
    call_attempts: Mapped[list["CallAttempt"]] = relationship(back_populates="phone_number")
    audit_log_entries: Mapped[list["AuditLogEntry"]] = relationship(back_populates="phone_number")


class CallAttempt(Base):
    """A single dial attempt. Multiple phone numbers on one contact all
    accrue against the same engagement's attempt total via ``engagement_id``."""

    __tablename__ = "call_attempts"
    __table_args__ = (
        Index("ix_call_attempts_engagement_id_attempted_at", "engagement_id", "attempted_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    engagement_id: Mapped[int] = mapped_column(ForeignKey("engagements.id"), index=True)
    phone_number_id: Mapped[int] = mapped_column(ForeignKey("phone_numbers.id"), index=True)
    # Correlation id from the dialer or call platform (e.g. a room name), if any.
    external_call_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    # Wall-clock time the dial was placed. Always real, even when the gate
    # that allowed it evaluated an injected instant: the call happened now.
    attempted_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    outcome: Mapped[CallOutcome] = mapped_column(_enum_type(CallOutcome))
    # The injected instant the authorizing gate decision was evaluated at,
    # when the clock was overridden (e.g. dial.py --now). NULL for a dial
    # authorized against the wall clock. Marks the attempt as simulated.
    simulated_now: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, nullable=True)

    engagement: Mapped["Engagement"] = relationship(back_populates="call_attempts")
    phone_number: Mapped["PhoneNumber"] = relationship(back_populates="call_attempts")


class SuppressionFlag(Base):
    """Marks that contact on an engagement must stop (e.g. a do-not-call or
    stop-contact request)."""

    __tablename__ = "suppression_flags"

    id: Mapped[int] = mapped_column(primary_key=True)
    engagement_id: Mapped[int] = mapped_column(ForeignKey("engagements.id"), index=True)
    flagged_at: Mapped[datetime] = mapped_column(UTCDateTime)
    reason: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(100))

    engagement: Mapped["Engagement"] = relationship(back_populates="suppression_flags")


class CallingWindowOverride(Base):
    """An engagement-specific narrowing of the default calling window,
    expressed as local time-of-day at the dialed number's location."""

    __tablename__ = "calling_window_overrides"
    __table_args__ = (
        CheckConstraint(
            "day_of_week IS NULL OR (day_of_week BETWEEN 0 AND 6)",
            name="ck_calling_window_override_day_of_week_range",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    engagement_id: Mapped[int] = mapped_column(ForeignKey("engagements.id"), index=True)
    window_start_local: Mapped[time] = mapped_column()
    window_end_local: Mapped[time] = mapped_column()
    # 0=Monday .. 6=Sunday; NULL means the override applies every day.
    day_of_week: Mapped[Optional[int]] = mapped_column(nullable=True)

    engagement: Mapped["Engagement"] = relationship(back_populates="calling_window_overrides")


class AuditLogEntry(Base):
    """Immutable record of an ALLOW/DENY gate decision, for audit trail."""

    __tablename__ = "audit_log_entries"

    id: Mapped[int] = mapped_column(primary_key=True)
    engagement_id: Mapped[int] = mapped_column(ForeignKey("engagements.id"), index=True)
    phone_number_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("phone_numbers.id"), nullable=True, index=True
    )
    decision: Mapped[AuditDecision] = mapped_column(_enum_type(AuditDecision))
    reason: Mapped[str] = mapped_column(Text)
    # NULL when the gate denied before the frequency check ran, so no count exists.
    attempt_count_at_decision: Mapped[Optional[int]] = mapped_column(nullable=True)
    # The instant every rule evaluated against.
    decided_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    # Wall-clock time the row was written, assigned by the database rather
    # than by Python, so no code in the gate package reads a clock. It
    # differs from decided_at only when the evaluation instant was injected.
    recorded_at: Mapped[datetime] = mapped_column(UTCDateTime, server_default=func.now())
    # The injected instant, set only when the evaluation instant was
    # supplied rather than read from the wall clock (e.g. dial.py --now).
    # Equal to decided_at in that case; NULL for a live evaluation. A row
    # with this set is permanently and visibly marked as simulated.
    simulated_now: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, nullable=True)

    # Fetch recorded_at (a server default) as part of the INSERT so the
    # in-memory row matches the stored one without a separate refresh.
    __mapper_args__ = {"eager_defaults": True}

    engagement: Mapped["Engagement"] = relationship(back_populates="audit_log_entries")
    phone_number: Mapped[Optional["PhoneNumber"]] = relationship(back_populates="audit_log_entries")
