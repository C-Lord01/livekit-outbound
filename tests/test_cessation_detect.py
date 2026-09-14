"""Cessation detection: the fast-path backstop, the classifier, and failing closed.

No session, no database. The classifier is a stub with scripted behavior
so each failure mode is exercised directly.
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from livekit_outbound.cessation import (
    CessationDetector,
    ClassifierError,
    Verdict,
    match_fast_path,
)
from livekit_outbound.cessation.detect import normalize
from livekit_outbound.gate.models import DetectionPath, SuppressionKind


class ScriptedClassifier:
    """Returns a fixed verdict, raises, or hangs, and counts calls."""

    def __init__(self, verdict: Verdict | None = None, *, error: Exception | None = None, hang: bool = False):
        self.verdict = verdict
        self.error = error
        self.hang = hang
        self.calls: list[str] = []

    async def classify(self, utterance: str) -> Verdict:
        self.calls.append(utterance)
        if self.hang:
            await asyncio.sleep(3600)
        if self.error is not None:
            raise self.error
        assert self.verdict is not None
        return self.verdict


def detect(detector: CessationDetector, text: str):
    return asyncio.run(detector.detect(text))


# --- fast-path -----------------------------------------------------------


@pytest.mark.parametrize(
    "utterance, kind",
    [
        ("Stop calling me.", SuppressionKind.STOP_CONTACT),
        ("Don't call me again!", SuppressionKind.STOP_CONTACT),
        ("dont call me again", SuppressionKind.STOP_CONTACT),
        ("Please, do not call this number again.", SuppressionKind.STOP_CONTACT),
        ("Take me off your list.", SuppressionKind.STOP_CONTACT),
        ("put me on your do not call list please", SuppressionKind.STOP_CONTACT),
        ("Never call me again", SuppressionKind.STOP_CONTACT),
        ("I have an attorney, talk to them.", SuppressionKind.ATTORNEY_REPRESENTED),
        ("You need to talk to my lawyer.", SuppressionKind.ATTORNEY_REPRESENTED),
        ("I'm represented by counsel.", SuppressionKind.ATTORNEY_REPRESENTED),
        ("I’ve retained an attorney.", SuppressionKind.ATTORNEY_REPRESENTED),  # curly apostrophe
    ],
)
def test_fast_path_phrases_match_with_the_right_kind(utterance, kind):
    detection = match_fast_path(utterance)
    assert detection is not None
    assert detection.kind is kind
    assert detection.path is DetectionPath.FAST_PATH
    assert detection.detail.startswith("matched ")


@pytest.mark.parametrize(
    "utterance",
    [
        "Hi, how are you?",
        "Can you call me back tomorrow?",
        "I'm busy right now, goodbye.",
        "My lawyer friend lives in Denver.",  # bare 'my lawyer' is deliberately not listed
        "The stopwatch is calling me back to work.",  # no whole-phrase match
    ],
)
def test_fast_path_ignores_ordinary_speech(utterance):
    assert match_fast_path(utterance) is None


def test_normalize_strips_punctuation_and_apostrophes():
    assert normalize("  Don’t   CALL me, again!! ") == "dont call me again"


def test_fast_path_detects_with_the_classifier_broken():
    """The backstop: a dead classifier cannot cause a missed cessation."""
    classifier = ScriptedClassifier(error=RuntimeError("classifier is down"))
    detector = CessationDetector(classifier, timeout=0.05)

    detection = detect(detector, "Stop calling me.")

    assert detection is not None
    assert detection.path is DetectionPath.FAST_PATH
    assert detection.kind is SuppressionKind.STOP_CONTACT
    assert classifier.calls == []  # never consulted


# --- classifier ----------------------------------------------------------


def test_classifier_catches_phrasing_the_fast_path_misses():
    classifier = ScriptedClassifier(
        Verdict(category="stop_contact", rationale="asks for no further calls")
    )
    detector = CessationDetector(classifier, timeout=1.0)

    detection = detect(detector, "I'd really rather you people didn't ring me anymore.")

    assert classifier.calls == ["I'd really rather you people didn't ring me anymore."]
    assert detection is not None
    assert detection.kind is SuppressionKind.STOP_CONTACT
    assert detection.path is DetectionPath.CLASSIFIER
    assert detection.detail == "asks for no further calls"


def test_classifier_distinguishes_attorney_representation():
    classifier = ScriptedClassifier(
        Verdict(category="attorney_represented", rationale="directs caller to counsel")
    )
    detector = CessationDetector(classifier)

    detection = detect(detector, "Everything goes through the firm handling this for me.")

    assert detection is not None
    assert detection.kind is SuppressionKind.ATTORNEY_REPRESENTED
    assert detection.path is DetectionPath.CLASSIFIER


def test_ordinary_conversation_is_not_a_cessation():
    classifier = ScriptedClassifier(Verdict(category="none", rationale="small talk"))
    detector = CessationDetector(classifier)

    assert detect(detector, "Doing fine, thanks. What's this about?") is None
    assert classifier.calls == ["Doing fine, thanks. What's this about?"]


# --- failing closed -------------------------------------------------------


def test_classifier_timeout_is_a_cessation(caplog):
    detector = CessationDetector(ScriptedClassifier(hang=True), timeout=0.05)

    with caplog.at_level(logging.WARNING, logger="outbound-caller.cessation"):
        detection = detect(detector, "Well, I suppose that depends.")

    assert detection is not None
    assert detection.path is DetectionPath.FAIL_CLOSED
    assert detection.kind is SuppressionKind.STOP_CONTACT
    assert "timed out" in detection.detail
    assert "failing closed" in caplog.text


def test_classifier_error_is_a_cessation():
    detector = CessationDetector(
        ScriptedClassifier(error=ClassifierError("ValidationError: not json")), timeout=1.0
    )

    detection = detect(detector, "Hmm, let me think.")

    assert detection is not None
    assert detection.path is DetectionPath.FAIL_CLOSED
    assert "classifier error" in detection.detail
    assert "not json" in detection.detail


def test_ambiguous_verdict_is_a_cessation():
    detector = CessationDetector(
        ScriptedClassifier(Verdict(category="unclear", rationale="could be either"))
    )

    detection = detect(detector, "I don't know about all that.")

    assert detection is not None
    assert detection.path is DetectionPath.FAIL_CLOSED
    assert detection.kind is SuppressionKind.STOP_CONTACT
    assert "unsure" in detection.detail
    assert "could be either" in detection.detail


def test_verdict_rejects_categories_outside_the_schema():
    with pytest.raises(Exception):
        Verdict.model_validate_json('{"category": "maybe", "rationale": ""}')
