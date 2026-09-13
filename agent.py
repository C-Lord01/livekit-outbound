"""Minimal outbound voice agent: greets the callee, then holds a conversation.

Run in dev mode:   uv run agent.py dev
Run in prod mode:  uv run agent.py start

Registered under AGENT_NAME for explicit dispatch, so it only joins rooms that
scripts/dial.py dispatches it to. The dial script attaches JSON metadata
({"phone_number": ..., "participant_identity": ...}) which is read back here
from ctx.job.metadata (docs/sdk-facts.md, section 4).
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from dotenv import load_dotenv
from livekit import rtc
from livekit.agents import (
    Agent,
    AgentServer,
    AgentSession,
    JobContext,
    JobProcess,
    cli,
)
from livekit.agents.voice.events import UserInputTranscribedEvent
from livekit.plugins import cartesia, deepgram, openai, silero

load_dotenv()

logger = logging.getLogger("outbound-caller")

AGENT_NAME = os.environ.get("AGENT_NAME", "outbound-caller")

GREETING = (
    "Hi, this is Ada calling from the LiveKit test line. "
    "This is a quick automated check of our outbound calling. How are you doing today?"
)

INSTRUCTIONS = (
    "You are Ada, a friendly voice assistant on a phone call. "
    "Keep replies to one or two short sentences, speak naturally, and do not use "
    "markdown or emoji. If the person says goodbye, say goodbye and stop talking."
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

    await session.start(
        Agent(instructions=INSTRUCTIONS),
        room=ctx.room,
        room_input_options=rtc_input_options(participant.identity),
    )

    # Fixed greeting, then the LLM takes over for the rest of the conversation.
    await session.say(GREETING, allow_interruptions=True)


def rtc_input_options(identity: str):
    from livekit.agents.voice import room_io

    return room_io.RoomInputOptions(participant_identity=identity, close_on_disconnect=True)


if __name__ == "__main__":
    _check_provider_keys()
    cli.run_app(server)
