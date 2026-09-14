"""The cessation monitor enforces in order: detect, durable write, acknowledge, hang up.

Session, agent, and store are recording fakes sharing one ordered call log,
so ordering is asserted explicitly rather than inferred.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from livekit_outbound.cessation import (
    ACKNOWLEDGEMENTS,
    CallContext,
    CessationDetector,
    CessationMonitor,
    Verdict,
)
from livekit_outbound.gate.models import DetectionPath, SuppressionKind
from tests.test_cessation_detect import ScriptedClassifier

CALL = CallContext(
    room_name="call-abc123", phone_number="+12135550120", contact_id=3, engagement_id=3
)
DETECTED_AT = datetime(2026, 7, 15, 19, 0, 5, tzinfo=timezone.utc)


class Log:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def index(self, name: str) -> int:
        return next(i for i, call in enumerate(self.calls) if call[0] == name)

    def names(self) -> list[str]:
        return [call[0] for call in self.calls]


class FakeSpeechHandle:
    def __init__(self, log: Log) -> None:
        self._log = log

    async def wait_for_playout(self) -> None:
        self._log.calls.append(("ack.wait_for_playout",))


class FakeSession:
    def __init__(self, log: Log, *, say_error: Exception | None = None) -> None:
        self._log = log
        self._say_error = say_error
        self.handlers: dict[str, list] = {}

    def on(self, event: str, callback):
        self.handlers.setdefault(event, []).append(callback)
        return callback

    def emit(self, event: str, ev) -> None:
        for callback in self.handlers.get(event, []):
            callback(ev)

    def interrupt(self, *, force: bool = False):
        self._log.calls.append(("session.interrupt", force))
        fut: asyncio.Future[None] = asyncio.get_event_loop().create_future()
        fut.set_result(None)
        return fut

    def say(self, text: str, *, allow_interruptions: bool = True):
        self._log.calls.append(("session.say", text, allow_interruptions))
        if self._say_error is not None:
            raise self._say_error
        return FakeSpeechHandle(self._log)


class FakeAgent:
    def __init__(self, log: Log) -> None:
        self._log = log

    async def hangup(self, reason: str, *, run_ctx=None) -> None:
        self._log.calls.append(("agent.hangup", reason, run_ctx))


class FakeStore:
    def __init__(self, log: Log, *, suppression_error: Exception | None = None) -> None:
        self._log = log
        self._suppression_error = suppression_error
        self.events: list[dict] = []

    def write_suppression(self, *, engagement_id, kind, utterance, now) -> int:
        self._log.calls.append(("store.write_suppression", engagement_id, kind, utterance, now))
        if self._suppression_error is not None:
            raise self._suppression_error
        return 42

    def write_event(self, **fields) -> int:
        self._log.calls.append(("store.write_event", fields["kind"], fields["detection_path"]))
        self.events.append(fields)
        return 7


def transcript(text: str, *, is_final: bool = True, created_at: float | None = None):
    return SimpleNamespace(
        transcript=text, is_final=is_final, created_at=created_at or time.time()
    )


def user_state(old: str, new: str, at: float):
    return SimpleNamespace(old_state=old, new_state=new, created_at=at)


def make_monitor(
    log: Log,
    *,
    classifier=None,
    session=None,
    store=None,
    timeout: float = 1.0,
) -> tuple[CessationMonitor, FakeSession, FakeStore]:
    session = session or FakeSession(log)
    store = store or FakeStore(log)
    classifier = classifier or ScriptedClassifier(Verdict(category="none", rationale="chat"))
    monitor = CessationMonitor(
        session=session,
        agent=FakeAgent(log),
        detector=CessationDetector(classifier, timeout=timeout),
        store=store,
        call=CALL,
        clock=lambda: DETECTED_AT,
    )
    monitor.attach()
    return monitor, session, store


async def feed(monitor: CessationMonitor, session: FakeSession, *events) -> None:
    """Emit events through the session and wait for the monitor's tasks."""
    for ev in events:
        session.emit("user_input_transcribed", ev)
    await monitor.drain(timeout=5.0)


def test_write_completes_before_acknowledgement_and_hangup(caplog):
    log = Log()
    monitor, session, store = make_monitor(log)

    with caplog.at_level(logging.INFO, logger="outbound-caller.cessation"):
        asyncio.run(feed(monitor, session, transcript("Stop calling me.")))

    assert log.names() == [
        "store.write_suppression",
        "store.write_event",
        "session.interrupt",
        "session.say",
        "ack.wait_for_playout",
        "agent.hangup",
    ]
    assert log.index("store.write_suppression") < log.index("agent.hangup")
    assert log.index("store.write_suppression") < log.index("session.say")

    _, engagement_id, kind, utterance, now = log.calls[0]
    assert (engagement_id, kind, utterance, now) == (
        3, SuppressionKind.STOP_CONTACT, "Stop calling me.", DETECTED_AT
    )
    assert log.calls[log.index("session.say")] == (
        "session.say", ACKNOWLEDGEMENTS[SuppressionKind.STOP_CONTACT], False
    )
    assert log.calls[-1] == ("agent.hangup", "cessation: stop_contact", None)

    assert monitor.detection is not None
    assert monitor.detection.path is DetectionPath.FAST_PATH
    assert "cessation detected: stop_contact via fast_path" in caplog.text


def test_latency_is_measured_to_the_durable_write_and_logged_with_path(caplog):
    log = Log()
    monitor, session, store = make_monitor(log)
    speech_ended = time.time() - 0.250
    ev = transcript("Take me off your list.", created_at=speech_ended + 0.200)

    with caplog.at_level(logging.INFO, logger="outbound-caller.cessation"):
        asyncio.run(
            feed_with_state(monitor, session, user_state("speaking", "listening", speech_ended), ev)
        )

    durable = next(r for r in caplog.records if r.getMessage().startswith("cessation durable"))
    assert durable.levelno == logging.INFO
    assert "path=fast_path" in durable.getMessage()
    assert durable.suppression_flag_id == 42
    assert durable.latency_to_durable_ms >= 0
    assert durable.transcript_delay_ms == pytest.approx(200.0, abs=1.0)
    assert durable.end_to_end_ms == pytest.approx(
        durable.latency_to_durable_ms + durable.transcript_delay_ms
    )

    (event,) = store.events
    assert event["latency_to_durable_ms"] == durable.latency_to_durable_ms
    assert event["transcript_delay_ms"] == durable.transcript_delay_ms
    assert event["suppression_flag_id"] == 42
    assert event["external_call_id"] == "call-abc123"
    assert event["phone_number"] == "+12135550120"
    assert event["detected_at"] == DETECTED_AT
    assert event["utterance"] == "Take me off your list."


async def feed_with_state(monitor, session, state_ev, transcript_ev):
    session.emit("user_state_changed", state_ev)
    session.emit("user_input_transcribed", transcript_ev)
    await monitor.drain(timeout=5.0)


def test_classifier_path_is_logged_as_such(caplog):
    log = Log()
    classifier = ScriptedClassifier(Verdict(category="stop_contact", rationale="no more calls"))
    monitor, session, store = make_monitor(log, classifier=classifier)

    with caplog.at_level(logging.INFO, logger="outbound-caller.cessation"):
        asyncio.run(feed(monitor, session, transcript("I'd rather you didn't ring again.")))

    durable = next(r for r in caplog.records if r.getMessage().startswith("cessation durable"))
    assert "path=classifier" in durable.getMessage()
    assert store.events[0]["detection_path"] is DetectionPath.CLASSIFIER
    assert store.events[0]["detection_detail"] == "no more calls"


def test_ordinary_conversation_writes_nothing_and_keeps_talking():
    log = Log()
    monitor, session, store = make_monitor(log)

    asyncio.run(feed(monitor, session, transcript("Doing well, what's this about?")))

    assert log.calls == []
    assert store.events == []
    assert monitor.detection is None


def test_interim_transcripts_are_ignored_even_when_they_match():
    log = Log()
    classifier = ScriptedClassifier(Verdict(category="none", rationale=""))
    monitor, session, store = make_monitor(log, classifier=classifier)

    asyncio.run(
        feed(
            monitor,
            session,
            transcript("stop calling", is_final=False),
            transcript("stop calling me", is_final=False),
            transcript("   ", is_final=True),
        )
    )

    assert log.calls == []
    assert classifier.calls == []
    assert monitor.detection is None


def test_write_failure_still_terminates_and_logs_error(caplog):
    log = Log()
    store = FakeStore(log, suppression_error=RuntimeError("disk full"))
    monitor, session, _ = make_monitor(log, store=store)

    with caplog.at_level(logging.INFO, logger="outbound-caller.cessation"):
        asyncio.run(feed(monitor, session, transcript("Do not call me again.")))

    assert log.names() == [
        "store.write_suppression",
        "store.write_event",
        "session.interrupt",
        "session.say",
        "ack.wait_for_playout",
        "agent.hangup",
    ]
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1
    assert "SUPPRESSION WRITE FAILED" in errors[0].getMessage()
    assert "NOT suppressed" in errors[0].getMessage()
    assert errors[0].exc_info is not None
    assert not any(r.getMessage().startswith("cessation durable") for r in caplog.records)

    (event,) = store.events
    assert event["suppression_flag_id"] is None
    assert event["latency_to_durable_ms"] is None


def test_acknowledgement_failure_does_not_block_hangup(caplog):
    log = Log()
    session = FakeSession(log, say_error=RuntimeError("AgentSession is closing"))
    monitor, session, _ = make_monitor(log, session=session)

    with caplog.at_level(logging.INFO, logger="outbound-caller.cessation"):
        asyncio.run(feed(monitor, session, transcript("Stop calling me.")))

    assert log.names()[-1] == "agent.hangup"
    assert "acknowledgement failed" in caplog.text


def test_second_detection_is_logged_and_ignored(caplog):
    log = Log()
    monitor, session, store = make_monitor(log)

    with caplog.at_level(logging.INFO, logger="outbound-caller.cessation"):
        asyncio.run(
            feed(
                monitor,
                session,
                transcript("Stop calling me."),
                transcript("I have an attorney."),
            )
        )

    assert log.names().count("store.write_suppression") == 1
    assert log.names().count("agent.hangup") == 1
    assert monitor.detection.kind is SuppressionKind.STOP_CONTACT
    assert "cessation already enforced" in caplog.text


def test_concurrent_detections_only_enforce_once():
    """Two finals already in flight before either is enforced: the lock lets one through."""
    log = Log()
    monitor, session, store = make_monitor(log)

    async def run() -> None:
        first = monitor.handle_transcript(transcript("Stop calling me."))
        second = monitor.handle_transcript(transcript("Never call me again."))
        await asyncio.gather(first, second)

    asyncio.run(run())

    assert log.names().count("store.write_suppression") == 1
    assert log.names().count("agent.hangup") == 1
    assert len(store.events) == 1


@pytest.mark.parametrize(
    "utterance, kind",
    [
        ("Don't call me again.", SuppressionKind.STOP_CONTACT),
        ("You'll have to talk to my attorney.", SuppressionKind.ATTORNEY_REPRESENTED),
    ],
)
def test_each_kind_is_recorded_and_acknowledged_distinctly(utterance, kind):
    log = Log()
    monitor, session, store = make_monitor(log)

    asyncio.run(feed(monitor, session, transcript(utterance)))

    assert log.calls[0][2] is kind
    assert store.events[0]["kind"] is kind
    assert log.calls[log.index("session.say")][1] == ACKNOWLEDGEMENTS[kind]
    assert log.calls[-1] == ("agent.hangup", f"cessation: {kind.value}", None)


def test_fail_closed_detection_is_enforced_like_any_other(caplog):
    log = Log()
    monitor, session, store = make_monitor(log, classifier=ScriptedClassifier(hang=True), timeout=0.05)

    with caplog.at_level(logging.INFO, logger="outbound-caller.cessation"):
        asyncio.run(feed(monitor, session, transcript("Well, that depends.")))

    assert log.names()[-1] == "agent.hangup"
    assert store.events[0]["detection_path"] is DetectionPath.FAIL_CLOSED
    assert "path=fail_closed" in caplog.text


def test_drain_waits_for_in_flight_enforcement():
    """A write that starts just before shutdown finishes before drain returns."""
    log = Log()

    class SlowStore(FakeStore):
        def write_suppression(self, **kw) -> int:
            time.sleep(0.2)
            return super().write_suppression(**kw)

    store = SlowStore(log)
    monitor, session, _ = make_monitor(log, store=store)

    async def run() -> None:
        session.emit("user_input_transcribed", transcript("Stop calling me."))
        await asyncio.sleep(0)  # let the task start
        await monitor.drain(timeout=5.0)

    asyncio.run(run())
    assert "store.write_suppression" in log.names()
    assert log.names()[-1] == "agent.hangup"
