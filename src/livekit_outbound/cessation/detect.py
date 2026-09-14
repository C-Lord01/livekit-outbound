"""Recognize a cessation in one final transcript.

Two layers, in this order:

1. **Keyword fast-path.** A conservative list of phrases that cannot mean
   anything else. A match is a cessation, full stop; the classifier is not
   consulted. This layer exists so that a classifier outage or timeout can
   never cause a missed cessation.
2. **LLM classification.** For everything the fast-path does not catch, a
   small, fast model returns a structured verdict under a short timeout.

The classifier layer **fails closed**: a timeout, an error, an unparseable
response, or an ``unclear`` verdict is treated as a cessation. See
:meth:`CessationDetector.detect` for the reasoning.

Nothing here touches the session or the database. The detector is given a
:class:`Classifier` and returns a :class:`Detection` (or None), so it can be
exercised in tests with the classifier stubbed out entirely.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel

from livekit_outbound.gate.models import DetectionPath, SuppressionKind

logger = logging.getLogger("outbound-caller.cessation")

DEFAULT_CLASSIFIER_TIMEOUT_S = 1.5

# Phrases that unambiguously express a cessation. Matched on whole words
# after normalization (lowercase, apostrophes removed, punctuation stripped),
# so "Don't call me again!" and "dont call me again" both match. Keep this
# list conservative: a phrase belongs here only if no ordinary conversational
# use of it means something else. Anything softer is the classifier's job.
FAST_PATH_PHRASES: tuple[tuple[str, SuppressionKind], ...] = (
    # --- stop-contact ---
    ("stop calling me", SuppressionKind.STOP_CONTACT),
    ("stop calling this number", SuppressionKind.STOP_CONTACT),
    ("quit calling me", SuppressionKind.STOP_CONTACT),
    ("stop contacting me", SuppressionKind.STOP_CONTACT),
    ("dont call me again", SuppressionKind.STOP_CONTACT),
    ("do not call me again", SuppressionKind.STOP_CONTACT),
    ("dont call me anymore", SuppressionKind.STOP_CONTACT),
    ("do not call me anymore", SuppressionKind.STOP_CONTACT),
    ("dont call this number again", SuppressionKind.STOP_CONTACT),
    ("do not call this number again", SuppressionKind.STOP_CONTACT),
    ("dont call this number", SuppressionKind.STOP_CONTACT),
    ("do not call this number", SuppressionKind.STOP_CONTACT),
    ("never call me again", SuppressionKind.STOP_CONTACT),
    ("never call this number again", SuppressionKind.STOP_CONTACT),
    ("dont contact me again", SuppressionKind.STOP_CONTACT),
    ("do not contact me again", SuppressionKind.STOP_CONTACT),
    ("dont contact me anymore", SuppressionKind.STOP_CONTACT),
    ("do not contact me anymore", SuppressionKind.STOP_CONTACT),
    ("take me off your list", SuppressionKind.STOP_CONTACT),
    ("take me off your call list", SuppressionKind.STOP_CONTACT),
    ("take me off the list", SuppressionKind.STOP_CONTACT),
    ("remove me from your list", SuppressionKind.STOP_CONTACT),
    ("remove me from your call list", SuppressionKind.STOP_CONTACT),
    ("remove this number from your list", SuppressionKind.STOP_CONTACT),
    ("put me on your do not call list", SuppressionKind.STOP_CONTACT),
    ("add me to your do not call list", SuppressionKind.STOP_CONTACT),
    ("do not call list", SuppressionKind.STOP_CONTACT),
    # --- attorney representation ---
    ("i have an attorney", SuppressionKind.ATTORNEY_REPRESENTED),
    ("i have a lawyer", SuppressionKind.ATTORNEY_REPRESENTED),
    ("i have retained an attorney", SuppressionKind.ATTORNEY_REPRESENTED),
    ("i have retained a lawyer", SuppressionKind.ATTORNEY_REPRESENTED),
    ("ive retained an attorney", SuppressionKind.ATTORNEY_REPRESENTED),
    ("ive retained a lawyer", SuppressionKind.ATTORNEY_REPRESENTED),
    ("im represented by an attorney", SuppressionKind.ATTORNEY_REPRESENTED),
    ("i am represented by an attorney", SuppressionKind.ATTORNEY_REPRESENTED),
    ("im represented by a lawyer", SuppressionKind.ATTORNEY_REPRESENTED),
    ("i am represented by a lawyer", SuppressionKind.ATTORNEY_REPRESENTED),
    ("im represented by counsel", SuppressionKind.ATTORNEY_REPRESENTED),
    ("i am represented by counsel", SuppressionKind.ATTORNEY_REPRESENTED),
    ("represented by an attorney", SuppressionKind.ATTORNEY_REPRESENTED),
    ("represented by a lawyer", SuppressionKind.ATTORNEY_REPRESENTED),
    ("represented by counsel", SuppressionKind.ATTORNEY_REPRESENTED),
    ("talk to my attorney", SuppressionKind.ATTORNEY_REPRESENTED),
    ("talk to my lawyer", SuppressionKind.ATTORNEY_REPRESENTED),
    ("speak to my attorney", SuppressionKind.ATTORNEY_REPRESENTED),
    ("speak to my lawyer", SuppressionKind.ATTORNEY_REPRESENTED),
    ("speak with my attorney", SuppressionKind.ATTORNEY_REPRESENTED),
    ("speak with my lawyer", SuppressionKind.ATTORNEY_REPRESENTED),
    ("contact my attorney", SuppressionKind.ATTORNEY_REPRESENTED),
    ("contact my lawyer", SuppressionKind.ATTORNEY_REPRESENTED),
    ("call my attorney", SuppressionKind.ATTORNEY_REPRESENTED),
    ("call my lawyer", SuppressionKind.ATTORNEY_REPRESENTED),
    ("go through my attorney", SuppressionKind.ATTORNEY_REPRESENTED),
    ("go through my lawyer", SuppressionKind.ATTORNEY_REPRESENTED),
    ("deal with my attorney", SuppressionKind.ATTORNEY_REPRESENTED),
    ("deal with my lawyer", SuppressionKind.ATTORNEY_REPRESENTED),
)

_APOSTROPHES = re.compile(r"[‘’'`]")
_NON_WORD = re.compile(r"[^a-z0-9 ]+")
_SPACES = re.compile(r"\s+")


def normalize(text: str) -> str:
    """Lowercase, drop apostrophes, replace punctuation with spaces, collapse spaces."""
    text = _APOSTROPHES.sub("", text.lower())
    text = _NON_WORD.sub(" ", text)
    return _SPACES.sub(" ", text).strip()


@dataclass(frozen=True, slots=True)
class Detection:
    kind: SuppressionKind
    path: DetectionPath
    # Matched phrase, classifier rationale, or the fail-closed cause.
    detail: str


def match_fast_path(utterance: str) -> Detection | None:
    """Return a fast-path detection if ``utterance`` contains a listed phrase."""
    padded = f" {normalize(utterance)} "
    for phrase, kind in FAST_PATH_PHRASES:
        if f" {phrase} " in padded:
            return Detection(kind=kind, path=DetectionPath.FAST_PATH, detail=f"matched {phrase!r}")
    return None


class Verdict(BaseModel):
    """The classifier's structured answer. ``unclear`` is a legitimate output
    and is handled as a cessation by the detector.

    ``rationale`` is kept for the audit row but is prompted to be a few words:
    output tokens are the main cost of the round trip, and the round trip is
    on a 1.5s budget."""

    category: Literal["stop_contact", "attorney_represented", "none", "unclear"]
    rationale: str


class ClassifierError(Exception):
    """The classifier could not produce a usable verdict."""


@runtime_checkable
class Classifier(Protocol):
    async def classify(self, utterance: str) -> Verdict: ...


CLASSIFIER_PROMPT = (
    "You classify one utterance spoken by a person during a phone call they "
    "received from an organization. Decide whether the utterance is:\n"
    '- "stop_contact": the person asks not to be called or contacted again, '
    "asks to be removed from a list, or otherwise tells the caller to stop "
    "contacting them (at this number or at all).\n"
    '- "attorney_represented": the person states they have or are represented '
    "by an attorney, lawyer, or legal counsel, or directs the caller to their "
    "attorney.\n"
    '- "none": ordinary conversation, including declining this call, being busy, '
    "asking to be called later, or saying goodbye. Ending this call is not a "
    "request to stop future contact.\n"
    '- "unclear": you cannot tell.\n'
    "Judge only what the person said, not what the organization might want. "
    "Answer with the JSON object only. Keep the rationale to at most eight words; "
    "latency matters more than explanation."
)


class StructuredChatLLM(Protocol):
    """The slice of ``livekit.plugins.openai.LLM`` the classifier uses."""

    def chat(self, *, chat_ctx, response_format): ...  # returns an LLMStream


class LLMClassifier:
    """Classify with a LiveKit LLM plugin that supports a structured response format.

    The model is asked for a :class:`Verdict` JSON object. Anything that is
    not a valid verdict raises :class:`ClassifierError`, which the detector
    treats as a cessation.
    """

    def __init__(self, llm: StructuredChatLLM) -> None:
        self._llm = llm

    async def classify(self, utterance: str) -> Verdict:
        from livekit.agents.llm import ChatContext

        chat_ctx = ChatContext.empty()
        chat_ctx.add_message(role="system", content=CLASSIFIER_PROMPT)
        chat_ctx.add_message(role="user", content=utterance)
        try:
            response = await self._llm.chat(chat_ctx=chat_ctx, response_format=Verdict).collect()
            return Verdict.model_validate_json(response.text)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise ClassifierError(f"{type(exc).__name__}: {exc}") from exc


class CessationDetector:
    """Fast-path first, then the classifier under a timeout, failing closed."""

    def __init__(
        self,
        classifier: Classifier,
        *,
        timeout: float = DEFAULT_CLASSIFIER_TIMEOUT_S,
    ) -> None:
        self._classifier = classifier
        self._timeout = timeout

    @property
    def timeout(self) -> float:
        return self._timeout

    async def detect(self, utterance: str) -> Detection | None:
        """Return a :class:`Detection` if ``utterance`` is a cessation, else None.

        Only ever returns None on a definite ``none`` verdict from a healthy
        classifier. Every other failure mode is a cessation.
        """
        if fast := match_fast_path(utterance):
            return fast

        try:
            verdict = await asyncio.wait_for(
                self._classifier.classify(utterance), timeout=self._timeout
            )
        except asyncio.TimeoutError:
            return self._fail_closed(f"classifier timed out after {self._timeout:g}s")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return self._fail_closed(f"classifier error: {exc}")

        if verdict.category == "none":
            return None
        if verdict.category == "unclear":
            return self._fail_closed(f"classifier unsure: {verdict.rationale or 'no rationale'}")
        return Detection(
            kind=SuppressionKind(verdict.category),
            path=DetectionPath.CLASSIFIER,
            detail=verdict.rationale or "classifier verdict",
        )

    @staticmethod
    def _fail_closed(cause: str) -> Detection:
        # Fail closed. The two errors are not symmetric: suppressing a contact
        # who did not ask for it costs one lost conversation, which the
        # organization can recover from by other means. Missing a genuine
        # stop-contact request or attorney referral and calling again is a
        # statutory violation (TCPA 47 CFR 64.1200(d); FDCPA 15 U.S.C.
        # 1692c(c); Reg F 12 CFR 1006.6(b)(2), (c)). So when the classifier
        # cannot say, the answer is "stop". This mirrors how the pre-dial
        # gate handles a check that raises (audited as DENY) and how the
        # answering-machine detector's `uncertain` verdict is treated: doubt
        # resolves toward not proceeding. Stop-contact is the kind recorded,
        # because it is the broader suppression: it stops all contact rather
        # than redirecting it.
        logger.warning("cessation detection failing closed: %s", cause)
        return Detection(
            kind=SuppressionKind.STOP_CONTACT,
            path=DetectionPath.FAIL_CLOSED,
            detail=cause,
        )
