"""Agent-side adapter onto the gate database for in-call cessation writes.

The gate functions (:mod:`livekit_outbound.gate.cessation`) are synchronous
SQLAlchemy and take a session. The monitor runs inside the agent's event
loop, so it calls this store through ``asyncio.to_thread``; the engine is
created with ``check_same_thread=False`` for exactly that reason (see
:mod:`livekit_outbound.gate.db`). Each write opens its own session and
closes it, so a failure in one write cannot poison the next.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from livekit_outbound.gate.cessation import write_cessation_event, write_suppression
from livekit_outbound.gate.db import open_session
from livekit_outbound.gate.models import DetectionPath, SuppressionKind


class SuppressionStore(Protocol):
    """What the monitor needs from persistence. Both calls are synchronous
    and must not return until the row is committed."""

    def write_suppression(
        self, *, engagement_id: int, kind: SuppressionKind, utterance: str, now: datetime
    ) -> int:
        """Raise the flag and return its id once it is durable."""
        ...

    def write_event(
        self,
        *,
        engagement_id: int,
        phone_number: str | None,
        external_call_id: str | None,
        suppression_flag_id: int | None,
        detected_at: datetime,
        utterance: str,
        kind: SuppressionKind,
        detection_path: DetectionPath,
        detection_detail: str,
        transcript_delay_ms: float | None,
        latency_to_durable_ms: float | None,
    ) -> int:
        """Write the audit row and return its id."""
        ...


class GateSuppressionStore:
    """The real store: one short-lived session per write on the gate database.

    ``url`` defaults to the configured gate database (``GATE_DATABASE_URL``
    or ``./data/gate.db``), the same one the dial script consulted before
    placing the call.
    """

    def __init__(self, url: str | None = None) -> None:
        self._url = url

    def write_suppression(
        self, *, engagement_id: int, kind: SuppressionKind, utterance: str, now: datetime
    ) -> int:
        session = open_session(self._url)
        try:
            flag = write_suppression(
                session, engagement_id=engagement_id, kind=kind, utterance=utterance, now=now
            )
            return flag.id
        finally:
            session.close()

    def write_event(self, **fields) -> int:
        session = open_session(self._url)
        try:
            return write_cessation_event(session, **fields).id
        finally:
            session.close()
