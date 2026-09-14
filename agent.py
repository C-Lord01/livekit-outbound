"""Minimal outbound voice agent: greets the callee, then holds a conversation.

Run in dev mode:   uv run agent.py dev
Run in prod mode:  uv run agent.py start

Registered under AGENT_NAME for explicit dispatch, so it only joins rooms that
scripts/dial.py dispatches it to. The dial script attaches JSON metadata
({"phone_number": ..., "participant_identity": ...}) which is read back here
from ctx.job.metadata (docs/sdk-facts.md, section 4).

Ending the call: OutboundCaller.hangup() is the single hang-up path. The
end_call function tool calls it when the person says goodbye or asks to hang
up, and the cessation monitor calls the same method directly. It lets
in-flight speech finish, removes the SIP participant, and closes the session
(docs/sdk-facts.md, section 7). The session's close handler then shuts the
job down.

Mid-call cessation: a CessationMonitor (livekit_outbound.cessation) watches
final transcripts. When the person asks not to be contacted again or states
they are represented by an attorney, it writes a suppression flag to the
gate database and confirms it, acknowledges, and hangs up, in that order
(docs/sdk-facts.md, section 8). It needs the gate subject, which the dial
script passes as contact_id and engagement_id in the job metadata; a job
without an engagement is refused rather than run without enforcement.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from dotenv import load_dotenv
from livekit import api, rtc
from livekit.agents import (
    Agent,
    AgentServer,
    AgentSession,
    JobContext,
    JobProcess,
    RunContext,
    StopResponse,
    cli,
    function_tool,
)
from livekit.agents.voice.events import CloseEvent, UserInputTranscribedEvent
from livekit.plugins import cartesia, deepgram, openai, silero

from livekit_outbound.cessation import (
    CallContext,
    CessationDetector,
    CessationMonitor,
    GateSuppressionStore,
    LLMClassifier,
)

load_dotenv()

logger = logging.getLogger("outbound-caller")

AGENT_NAME = os.environ.get("AGENT_NAME", "outbound-caller")

# Small, fast model for the cessation classifier, and the budget it gets per
# utterance. On timeout the detector fails closed (treats it as a cessation),
# so a slow model shows up as over-suppression, never as a missed request.
CESSATION_CLASSIFIER_MODEL = "gpt-4.1-nano"
CESSATION_CLASSIFIER_TIMEOUT_S = 1.5

GREETING = (
    "Hi, this is Ada calling from the LiveKit test line. "
    "This is a quick automated check of our outbound calling. How are you doing today?"
)

INSTRUCTIONS = (
    "You are Ada, a friendly voice assistant on a phone call. "
    "Keep replies to one or two short sentences, speak naturally, and do not use "
    "markdown or emoji. "
    "When the person indicates the conversation is over, for example they say "
    "goodbye, say they have to go, say they are all set, or ask you to hang up, "
    "say a brief goodbye and then call the end_call tool. Do not keep the person "
    "on the line after they have said goodbye, and do not ask if there is anything "
    "else once they have asked you to hang up. "
    "If the person asks not to be called or contacted again, or says they have an "
    "attorney or lawyer, do not call end_call and do not argue or ask why: say only "
    "'Understood.' and stop. The system records the request and ends the call."
)


def _check_provider_keys() -> None:
    """Fail fast if a provider key is unset or still the .env.example placeholder."""
    from dotenv import dotenv_values

    example = Path(__file__).resolve().parent / ".env.example"
    placeholders = dotenv_values(example) if example.exists() else {}
    bad = [
        name
        for name in ("DEEPGRAM_API_KEY", "OPENAI_API_KEY", "CARTESIA_API_KEY")
        if not os.environ.get(name) or os.environ.get(name) == placeholders.get(name)
    ]
    if bad:
        raise SystemExit(
            "Missing or still-placeholder environment variables: " + ", ".join(bad)
        )


class OutboundCaller(Agent):
    """The voice agent on the call.

    Owns the one hang-up path, ``hangup()``. The ``end_call`` tool is a thin
    wrapper over it for the LLM; other code (the Block 4 cessation path) calls
    ``hangup()`` directly with its own reason.
    """

    def __init__(self, *, job_ctx: JobContext, participant_identity: str) -> None:
        super().__init__(instructions=INSTRUCTIONS)
        self._job_ctx = job_ctx
        self._participant_identity = participant_identity
        self._hangup_reason: str | None = None

    @property
    def hangup_reason(self) -> str | None:
        """Why the agent ended the call, or None if it has not (yet)."""
        return self._hangup_reason

    @function_tool
    async def end_call(self, ctx: RunContext, reason: str) -> None:
        """End the phone call and hang up.

        Call this when the person indicates the conversation is over: they say
        goodbye, say they have to go, say they are all set, or ask you to hang
        up. Say your goodbye before calling this tool; nothing said after it
        will be heard.

        Args:
            reason: One short sentence on why the call is ending, in your own
                words, for example "the person said goodbye" or "the person
                asked me to hang up".
        """
        await self.hangup(f"caller requested: {reason}", run_ctx=ctx)
        # The line is down; do not generate a spoken reply to the tool result.
        raise StopResponse()

    async def hangup(self, reason: str, *, run_ctx: RunContext | None = None) -> None:
        """End the call: finish in-flight speech, drop the SIP leg, close the session.

        Args:
            reason: Why the call is being ended. Logged, and kept on
                ``hangup_reason`` for the session close handler.
            run_ctx: Pass the tool's ``RunContext`` when calling from inside a
                function tool. From there the tool's own speech handle cannot be
                awaited (the SDK raises), so the wait covers the words spoken
                before the tool call instead. Leave unset when calling from
                anywhere else, such as the cessation path.

        Safe to call more than once: only the first call acts.

        The session close is scheduled, not awaited, because from inside a tool
        the close waits for that very tool to return. Subscribe to the
        session's ``close`` event to observe completion.
        """
        room_name = self._job_ctx.room.name
        log_fields = {
            "room": room_name,
            "participant": self._participant_identity,
            "reason": reason,
        }
        if self._hangup_reason is not None:
            logger.info(
                "hangup already in progress (%s); ignoring: %s",
                self._hangup_reason,
                reason,
                extra=log_fields,
            )
            return
        self._hangup_reason = reason
        logger.info("ending call: %s", reason, extra=log_fields)

        # 1. Let whatever is being said finish playing before the line drops.
        if run_ctx is not None:
            await run_ctx.wait_for_playout()
        elif (speech := self.session.current_speech) is not None:
            await speech.wait_for_playout()

        # 2. Disconnect the phone leg. The callee may already have hung up, in
        #    which case the participant is gone and that is fine. Any other
        #    failure falls back to deleting the room, which drops everyone.
        try:
            await self._job_ctx.api.room.remove_participant(
                api.RoomParticipantIdentity(room=room_name, identity=self._participant_identity)
            )
            logger.info("sip participant removed", extra=log_fields)
        except api.TwirpError as e:
            if e.code == api.TwirpErrorCode.NOT_FOUND:
                logger.info("sip participant already gone", extra=log_fields)
            else:
                logger.warning(
                    "could not remove sip participant (%s: %s); deleting room instead",
                    e.code,
                    e.message,
                    extra=log_fields,
                )
                await self._job_ctx.delete_room()

        # 3. Close the session. Removing a participant through the API does not
        #    trigger RoomInputOptions.close_on_disconnect (only client-initiated,
        #    room-deleted, and rejected disconnects do), so close it here.
        #    drain=True: no forced interruption of anything still queued.
        self.session.shutdown(drain=True)
        logger.info("call ended: %s", reason, extra=log_fields)


def prewarm(proc: JobProcess) -> None:
    # Load the Silero VAD once per process so each job starts quickly.
    proc.userdata["vad"] = silero.VAD.load()


server = AgentServer()
server.setup_fnc = prewarm


@server.rtc_session(agent_name=AGENT_NAME)
async def entrypoint(ctx: JobContext) -> None:
    metadata: dict = {}
    if ctx.job.metadata:
        try:
            metadata = json.loads(ctx.job.metadata)
        except json.JSONDecodeError:
            logger.warning("job metadata is not JSON: %r", ctx.job.metadata)

    participant_identity = metadata.get("participant_identity") or None
    logger.info(
        "starting outbound job",
        extra={"room": ctx.room.name, "phone_number": metadata.get("phone_number")},
    )

    call = call_context(ctx.room.name, metadata)
    if call is None:
        # Without an engagement there is nothing to write a stop request
        # against, so a cessation could not be honored. Refuse the call rather
        # than run it without enforcement.
        logger.error(
            "job metadata has no engagement_id; refusing to run the call without "
            "cessation enforcement (dispatch through scripts/dial.py)",
            extra={"room": ctx.room.name, "metadata": metadata},
        )
        await ctx.delete_room()
        ctx.shutdown(reason="no engagement in job metadata")
        return

    await ctx.connect()

    session = AgentSession(
        vad=ctx.proc.userdata["vad"],
        stt=deepgram.STT(model="nova-3", language="en-US"),
        llm=openai.LLM(model="gpt-4.1-mini"),
        tts=cartesia.TTS(),
    )

    @session.on("user_input_transcribed")
    def _on_user_transcript(ev: UserInputTranscribedEvent) -> None:
        if ev.is_final and ev.transcript:
            logger.info("user said: %s", ev.transcript)

    # Wait for the SIP participant to land in the room before starting the
    # session, so the pipeline binds to the phone caller rather than to nothing.
    participant = await ctx.wait_for_participant(
        identity=participant_identity,
        kind=rtc.ParticipantKind.PARTICIPANT_KIND_SIP,
    )
    logger.info("sip participant joined: %s", participant.identity)

    agent = OutboundCaller(job_ctx=ctx, participant_identity=participant.identity)

    classifier_llm = cessation_classifier_llm()
    monitor = CessationMonitor(
        session=session,
        agent=agent,
        detector=CessationDetector(
            LLMClassifier(classifier_llm), timeout=CESSATION_CLASSIFIER_TIMEOUT_S
        ),
        store=GateSuppressionStore(),
        call=call,
    )
    monitor.attach()

    # If the job is shutting down while a suppression write is still in
    # flight (the callee hung up mid-request, or the LLM ended the call), the
    # worker waits for this before exiting, so the write is not lost.
    async def _drain_cessation() -> None:
        await monitor.drain()
        await classifier_llm.aclose()

    ctx.add_shutdown_callback(_drain_cessation)

    @session.on("close")
    def _on_session_close(ev: CloseEvent) -> None:
        # Fires for every close: our own hangup, the callee hanging up (via
        # close_on_disconnect), or an error. Log which, then end the job so the
        # worker does not hold the process until the room times out.
        detection = monitor.detection
        logger.info(
            "session closed: %s",
            ev.reason.value,
            extra={
                "room": ctx.room.name,
                "close_reason": ev.reason.value,
                "hangup_reason": agent.hangup_reason,
                "cessation": detection.kind.value if detection else None,
                "error": ev.error,
            },
        )
        ctx.shutdown(reason=f"session closed: {ev.reason.value}")

    await session.start(
        agent,
        room=ctx.room,
        room_input_options=rtc_input_options(participant.identity),
    )

    # Fixed greeting, then the LLM takes over for the rest of the conversation.
    await session.say(GREETING, allow_interruptions=True)


def cessation_classifier_llm() -> openai.LLM:
    """The classifier's model, tuned for the 1.5s budget.

    Deterministic, a hard cap on output tokens (the verdict is ~20 tokens;
    output is what costs time), and the TLS connection opened now rather than
    on the first utterance, where a cold handshake alone was measured at 3s.
    """
    llm = openai.LLM(model=CESSATION_CLASSIFIER_MODEL, temperature=0, max_completion_tokens=40)
    llm.prewarm()
    return llm


def call_context(room_name: str, metadata: dict) -> CallContext | None:
    """The gate subject from the dispatch metadata, or None if it has no engagement."""
    engagement_id = metadata.get("engagement_id")
    if not isinstance(engagement_id, int):
        return None
    contact_id = metadata.get("contact_id")
    return CallContext(
        room_name=room_name,
        phone_number=metadata.get("phone_number") or None,
        contact_id=contact_id if isinstance(contact_id, int) else None,
        engagement_id=engagement_id,
    )


def rtc_input_options(identity: str):
    from livekit.agents.voice import room_io

    return room_io.RoomInputOptions(participant_identity=identity, close_on_disconnect=True)


if __name__ == "__main__":
    _check_provider_keys()
    cli.run_app(server)
