"""Enforce a cessation during a live call.

The monitor listens to the session's final transcripts and, off the audio
path, runs :class:`CessationDetector` on each one. When a detection comes
back it enforces in this order, which is mandatory:

1. Detect (already done by the time enforcement starts).
2. Write the suppression to the gate's state store and confirm the commit.
3. Have the agent deliver a brief acknowledgement.
4. Call ``OutboundCaller.hangup("cessation: <kind>")``.

The suppression write must complete before the hang-up. Session teardown
removes the participant and closes the session within a few hundred
milliseconds, and a write that is still in flight at that point can be lost;
so the write happens first and the hang-up waits for it. If the write
fails, the call is still terminated and the failure is logged at ERROR and
recorded in the audit row.

Measurement: the compliance-relevant latency is from the final transcript
event (``UserInputTranscribedEvent.created_at``) to the confirmed
suppression commit, not to the acknowledgement or the hang-up. The SDK does
not expose the transcript delay on the transcript event in this version; it
is derived here from the ``user_state_changed`` transition to ``listening``,
which is the SDK's end-of-speech signal. Their sum is the end-to-end figure
from the person finishing speaking to durable suppression.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from livekit_outbound.cessation.detect import CessationDetector, Detection
from livekit_outbound.cessation.store import SuppressionStore
from livekit_outbound.gate.models import SuppressionKind
from livekit_outbound.predial import utcnow

logger = logging.getLogger("outbound-caller.cessation")

# Spoken once the suppression is durable, just before the hang-up. Not
# interruptible: the person needs to hear that the request took effect.
ACKNOWLEDGEMENTS: dict[SuppressionKind, str] = {
    SuppressionKind.STOP_CONTACT: (
        "Understood. I've recorded your request, and we will not contact you at "
        "this number again. Goodbye."
    ),
    SuppressionKind.ATTORNEY_REPRESENTED: (
        "Understood. I've recorded that you're represented by an attorney, and we "
        "will not contact you directly again. Goodbye."
    ),
}


@dataclass(frozen=True, slots=True)
class CallContext:
    """The gate subject this call is about, from the dispatch metadata."""

    room_name: str
    phone_number: str | None
    contact_id: int | None
    engagement_id: int


class CessationMonitor:
    """Subscribe to a session's transcripts and enforce cessation.

    ``session`` needs ``on``, ``say``, and ``interrupt``; ``agent`` needs
    ``hangup``. Both are duck-typed so tests can pass recording fakes.
    """

    def __init__(
        self,
        *,
        session: Any,
        agent: Any,
        detector: CessationDetector,
        store: SuppressionStore,
        call: CallContext,
        clock: Callable[[], datetime] = utcnow,
        acknowledgements: dict[SuppressionKind, str] = ACKNOWLEDGEMENTS,
    ) -> None:
        self._session = session
        self._agent = agent
        self._detector = detector
        self._store = store
        self._call = call
        self._clock = clock
        self._acknowledgements = acknowledgements
        self._detection: Detection | None = None
        self._speech_ended_at: float | None = None
        self._tasks: set[asyncio.Task[None]] = set()
        self._enforce_lock = asyncio.Lock()

    @property
    def detection(self) -> Detection | None:
        """The cessation that was enforced on this call, if any."""
        return self._detection

    def attach(self) -> None:
        self._session.on("user_input_transcribed", self.handle_transcript)
        self._session.on("user_state_changed", self.handle_user_state)

    async def drain(self, timeout: float = 10.0) -> None:
        """Wait for any evaluation or enforcement still in flight.

        Called from the job's shutdown path so that a suppression write that
        started just before teardown is committed before the process exits.
        A task that outlives ``timeout`` is cancelled and reported at ERROR.
        """
        pending = [task for task in self._tasks if not task.done()]
        if not pending:
            return
        _, still_pending = await asyncio.wait(pending, timeout=timeout)
        for task in still_pending:
            task.cancel()
            logger.error(
                "cessation task %s did not finish within %.1fs of shutdown; cancelled",
                task.get_name(),
                timeout,
                extra=self._log_fields(),
            )

    # --- event handlers (synchronous, called inside the session's emit) ---

    def handle_user_state(self, ev: Any) -> None:
        # The speaking -> listening transition is the SDK's end-of-speech
        # signal; its timestamp anchors transcript_delay.
        if getattr(ev, "old_state", None) == "speaking" and ev.new_state == "listening":
            self._speech_ended_at = ev.created_at

    def handle_transcript(self, ev: Any) -> asyncio.Task[None] | None:
        """Schedule detection for a final transcript. Returns the task, for tests."""
        if not ev.is_final:
            return None  # interim transcripts change under us; never evaluate them
        transcript = (ev.transcript or "").strip()
        if not transcript:
            return None
        if self._detection is not None:
            logger.info(
                "cessation already enforced on this call; not evaluating: %s",
                transcript,
                extra=self._log_fields(),
            )
            return None

        transcript_delay_ms: float | None = None
        if self._speech_ended_at is not None and ev.created_at >= self._speech_ended_at:
            transcript_delay_ms = (ev.created_at - self._speech_ended_at) * 1000.0

        task = asyncio.create_task(
            self._evaluate(transcript, event_at=ev.created_at, transcript_delay_ms=transcript_delay_ms),
            name="cessation_evaluate",
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    # --- detection and enforcement (off the audio path) ---

    async def _evaluate(
        self, transcript: str, *, event_at: float, transcript_delay_ms: float | None
    ) -> None:
        try:
            detection = await self._detector.detect(transcript)
        except asyncio.CancelledError:
            raise
        except Exception:
            # The detector fails closed internally; reaching here means a bug
            # in the detector itself. Still fail closed.
            logger.exception("cessation detector raised; failing closed")
            detection = CessationDetector._fail_closed("detector raised")
        if detection is None:
            return
        await self.enforce(
            detection, transcript, event_at=event_at, transcript_delay_ms=transcript_delay_ms
        )

    async def enforce(
        self,
        detection: Detection,
        utterance: str,
        *,
        event_at: float,
        transcript_delay_ms: float | None,
    ) -> None:
        """Steps 2 to 4. Public so a caller with its own detection can use it."""
        async with self._enforce_lock:
            if self._detection is not None:
                logger.info(
                    "cessation already enforced (%s via %s); ignoring further detection: "
                    "%s via %s for %r",
                    self._detection.kind.value,
                    self._detection.path.value,
                    detection.kind.value,
                    detection.path.value,
                    utterance,
                    extra=self._log_fields(),
                )
                return
            self._detection = detection
            detected_at = self._clock()
            fields = self._log_fields(
                kind=detection.kind.value,
                detection_path=detection.path.value,
                detection_detail=detection.detail,
                utterance=utterance,
            )
            logger.info(
                "cessation detected: %s via %s (%s): %r",
                detection.kind.value,
                detection.path.value,
                detection.detail,
                utterance,
                extra=fields,
            )

            # 2. Durable suppression, confirmed, before anything else happens.
            flag_id: int | None = None
            latency_ms: float | None = None
            try:
                flag_id = await asyncio.to_thread(
                    self._store.write_suppression,
                    engagement_id=self._call.engagement_id,
                    kind=detection.kind,
                    utterance=utterance,
                    now=detected_at,
                )
                latency_ms = (time.time() - event_at) * 1000.0
                end_to_end_ms = (
                    latency_ms + transcript_delay_ms if transcript_delay_ms is not None else None
                )
                logger.info(
                    "cessation durable: suppression #%s written; path=%s "
                    "latency_to_durable_ms=%.1f transcript_delay_ms=%s end_to_end_ms=%s",
                    flag_id,
                    detection.path.value,
                    latency_ms,
                    _fmt_ms(transcript_delay_ms),
                    _fmt_ms(end_to_end_ms),
                    extra={
                        **fields,
                        "suppression_flag_id": flag_id,
                        "latency_to_durable_ms": latency_ms,
                        "transcript_delay_ms": transcript_delay_ms,
                        "end_to_end_ms": end_to_end_ms,
                    },
                )
            except Exception:
                # A cessation that did not become durable is a serious
                # condition: the next dial could go out. Terminate anyway,
                # and make sure this is impossible to miss.
                logger.error(
                    "CESSATION SUPPRESSION WRITE FAILED for engagement #%s (%s via %s): %r. "
                    "The call is being terminated but the contact is NOT suppressed; "
                    "record the suppression manually before any further contact.",
                    self._call.engagement_id,
                    detection.kind.value,
                    detection.path.value,
                    utterance,
                    exc_info=True,
                    extra=fields,
                )

            # Audit row. Written after the measurement so it carries the number.
            try:
                await asyncio.to_thread(
                    self._store.write_event,
                    engagement_id=self._call.engagement_id,
                    phone_number=self._call.phone_number,
                    external_call_id=self._call.room_name,
                    suppression_flag_id=flag_id,
                    detected_at=detected_at,
                    utterance=utterance,
                    kind=detection.kind,
                    detection_path=detection.path,
                    detection_detail=detection.detail,
                    transcript_delay_ms=transcript_delay_ms,
                    latency_to_durable_ms=latency_ms,
                )
            except Exception:
                logger.error(
                    "cessation audit write failed for engagement #%s", self._call.engagement_id,
                    exc_info=True,
                    extra=fields,
                )

            # 3. Acknowledge. Cut off whatever the agent was about to say in
            #    reply to the utterance, then speak the acknowledgement
            #    uninterruptibly. A failure here must not stop the hang-up.
            try:
                try:
                    self._session.interrupt(force=True)
                except RuntimeError:
                    pass  # nothing playing, or the session is already closing
                handle = self._session.say(
                    self._acknowledgements[detection.kind], allow_interruptions=False
                )
                await handle.wait_for_playout()
            except Exception:
                logger.exception("cessation acknowledgement failed; hanging up regardless")

            # 4. Hang up. Not inside a function tool, so no run_ctx.
            await self._agent.hangup(f"cessation: {detection.kind.value}")

    def _log_fields(self, **extra: Any) -> dict[str, Any]:
        return {
            "room": self._call.room_name,
            "phone_number": self._call.phone_number,
            "contact_id": self._call.contact_id,
            "engagement_id": self._call.engagement_id,
            **extra,
        }


def _fmt_ms(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.1f}"
