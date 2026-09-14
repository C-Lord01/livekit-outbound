"""agent.py ends a call through one reusable path, OutboundCaller.hangup().

The LiveKit session and job context are replaced with recording fakes, so
these tests prove the ordering of the hang-up (finish speech, remove the SIP
participant, close the session), the tool-vs-direct wait strategy, the
tolerance for a callee who already hung up, the room-deletion fallback, the
idempotency guard, and that the end_call tool is registered and delegates to
hangup() with a logged reason.
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from livekit import api
from livekit.agents import StopResponse

AGENT_SCRIPT = Path(__file__).resolve().parent.parent / "agent.py"

ROOM = "call-test"
IDENTITY = "sip-test-callee"


@pytest.fixture(scope="session")
def agent_module():
    """agent.py imported as a module (it is a script, not a package)."""
    spec = importlib.util.spec_from_file_location("agent_script", AGENT_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class Recorder:
    """Shared ordered log of what the fakes were asked to do."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []


class FakeSpeech:
    def __init__(self, rec: Recorder) -> None:
        self._rec = rec

    async def wait_for_playout(self) -> None:
        self._rec.calls.append(("speech.wait_for_playout",))


class FakeRunContext:
    def __init__(self, rec: Recorder) -> None:
        self._rec = rec

    async def wait_for_playout(self) -> None:
        self._rec.calls.append(("run_ctx.wait_for_playout",))


class FakeSession:
    def __init__(self, rec: Recorder, *, speech: FakeSpeech | None) -> None:
        self._rec = rec
        self.current_speech = speech

    def shutdown(self, *, drain: bool = True) -> None:
        self._rec.calls.append(("session.shutdown", drain))


class FakeRoomService:
    def __init__(self, rec: Recorder, *, error: Exception | None = None) -> None:
        self._rec = rec
        self._error = error

    async def remove_participant(self, request) -> None:
        self._rec.calls.append(("remove_participant", request.room, request.identity))
        if self._error is not None:
            raise self._error


class FakeJobContext:
    def __init__(self, rec: Recorder, *, remove_error: Exception | None = None) -> None:
        self._rec = rec
        self.room = SimpleNamespace(name=ROOM)
        self.api = SimpleNamespace(room=FakeRoomService(rec, error=remove_error))

    def delete_room(self, room_name: str | None = None):
        self._rec.calls.append(("delete_room", room_name or self.room.name))
        fut: asyncio.Future[None] = asyncio.get_event_loop().create_future()
        fut.set_result(None)
        return fut


@pytest.fixture
def rec() -> Recorder:
    return Recorder()


def make_agent(agent_module, monkeypatch, rec: Recorder, *, speech=None, remove_error=None):
    """An OutboundCaller wired to fakes. ``Agent.session`` normally requires a
    running activity, so the property is replaced for the test."""
    session = FakeSession(rec, speech=speech)
    monkeypatch.setattr(agent_module.OutboundCaller, "session", property(lambda self: session))
    job_ctx = FakeJobContext(rec, remove_error=remove_error)
    agent = agent_module.OutboundCaller(job_ctx=job_ctx, participant_identity=IDENTITY)
    return agent, session, job_ctx


def test_direct_hangup_finishes_speech_then_removes_sip_then_closes(agent_module, monkeypatch, rec):
    """The Block 4 path: no RunContext, so the current speech handle is awaited."""
    agent, _, _ = make_agent(agent_module, monkeypatch, rec, speech=FakeSpeech(rec))

    asyncio.run(agent.hangup("cessation: stop-contact request"))

    assert rec.calls == [
        ("speech.wait_for_playout",),
        ("remove_participant", ROOM, IDENTITY),
        ("session.shutdown", True),
    ]
    assert agent.hangup_reason == "cessation: stop-contact request"


def test_hangup_from_tool_waits_on_run_context_not_own_speech(agent_module, monkeypatch, rec):
    """Inside a tool the tool's own handle cannot be awaited; the RunContext wait is used."""
    agent, _, _ = make_agent(agent_module, monkeypatch, rec, speech=FakeSpeech(rec))

    asyncio.run(agent.hangup("caller requested: goodbye", run_ctx=FakeRunContext(rec)))

    assert rec.calls == [
        ("run_ctx.wait_for_playout",),
        ("remove_participant", ROOM, IDENTITY),
        ("session.shutdown", True),
    ]


def test_hangup_with_nothing_playing(agent_module, monkeypatch, rec):
    agent, _, _ = make_agent(agent_module, monkeypatch, rec, speech=None)

    asyncio.run(agent.hangup("silence"))

    assert rec.calls == [
        ("remove_participant", ROOM, IDENTITY),
        ("session.shutdown", True),
    ]


def test_callee_already_gone_still_closes_session(agent_module, monkeypatch, rec, caplog):
    gone = api.TwirpError(api.TwirpErrorCode.NOT_FOUND, "participant not found", status=404)
    agent, _, _ = make_agent(agent_module, monkeypatch, rec, remove_error=gone)

    with caplog.at_level(logging.INFO, logger="outbound-caller"):
        asyncio.run(agent.hangup("caller requested: bye"))

    assert rec.calls == [
        ("remove_participant", ROOM, IDENTITY),
        ("session.shutdown", True),
    ]
    assert "sip participant already gone" in caplog.text


def test_remove_failure_falls_back_to_deleting_room(agent_module, monkeypatch, rec, caplog):
    boom = api.TwirpError(api.TwirpErrorCode.INTERNAL, "server hiccup", status=500)
    agent, _, _ = make_agent(agent_module, monkeypatch, rec, remove_error=boom)

    with caplog.at_level(logging.WARNING, logger="outbound-caller"):
        asyncio.run(agent.hangup("caller requested: bye"))

    assert rec.calls == [
        ("remove_participant", ROOM, IDENTITY),
        ("delete_room", ROOM),
        ("session.shutdown", True),
    ]
    assert "deleting room instead" in caplog.text


def test_second_hangup_is_a_no_op(agent_module, monkeypatch, rec, caplog):
    agent, _, _ = make_agent(agent_module, monkeypatch, rec)

    async def twice() -> None:
        await agent.hangup("first")
        await agent.hangup("second")

    with caplog.at_level(logging.INFO, logger="outbound-caller"):
        asyncio.run(twice())

    assert rec.calls == [
        ("remove_participant", ROOM, IDENTITY),
        ("session.shutdown", True),
    ]
    assert agent.hangup_reason == "first"
    assert "hangup already in progress (first); ignoring: second" in caplog.text


def test_reason_is_logged(agent_module, monkeypatch, rec, caplog):
    agent, _, _ = make_agent(agent_module, monkeypatch, rec)

    with caplog.at_level(logging.INFO, logger="outbound-caller"):
        asyncio.run(agent.hangup("cessation: suppression flag raised mid-call"))

    messages = [r.getMessage() for r in caplog.records if r.name == "outbound-caller"]
    assert "ending call: cessation: suppression flag raised mid-call" in messages
    assert "call ended: cessation: suppression flag raised mid-call" in messages
    ending = next(r for r in caplog.records if r.getMessage().startswith("ending call"))
    assert ending.reason == "cessation: suppression flag raised mid-call"
    assert ending.room == ROOM
    assert ending.participant == IDENTITY


def test_end_call_tool_is_registered_and_delegates_to_hangup(agent_module, monkeypatch, rec):
    agent, _, _ = make_agent(agent_module, monkeypatch, rec)

    tool_names = {tool.info.name for tool in agent.tools}
    assert "end_call" in tool_names
    tool = next(t for t in agent.tools if t.info.name == "end_call")
    assert "hang up" in tool.info.description.lower()

    with pytest.raises(StopResponse):
        asyncio.run(agent.end_call(FakeRunContext(rec), reason="the person said goodbye"))

    assert rec.calls == [
        ("run_ctx.wait_for_playout",),
        ("remove_participant", ROOM, IDENTITY),
        ("session.shutdown", True),
    ]
    assert agent.hangup_reason == "caller requested: the person said goodbye"


def test_instructions_tell_the_model_when_to_end_the_call(agent_module):
    text = agent_module.INSTRUCTIONS
    assert "end_call" in text
    assert "goodbye" in text
    assert "hang up" in text
    # Stop requests are the monitor's job; the model must not race it to hang up.
    assert "do not call end_call" in text


def test_call_context_requires_an_engagement(agent_module):
    full = agent_module.call_context(
        "call-1",
        {"phone_number": "+12135550120", "contact_id": 3, "engagement_id": 3},
    )
    assert full is not None
    assert (full.room_name, full.phone_number, full.contact_id, full.engagement_id) == (
        "call-1", "+12135550120", 3, 3
    )

    assert agent_module.call_context("call-1", {}) is None
    assert agent_module.call_context("call-1", {"engagement_id": "3"}) is None
    partial = agent_module.call_context("call-1", {"engagement_id": 9})
    assert partial is not None
    assert partial.contact_id is None and partial.phone_number is None
