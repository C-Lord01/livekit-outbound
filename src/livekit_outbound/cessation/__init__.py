"""Mid-call cessation enforcement.

When the person on a live call asks not to be contacted again, or states
they are represented by an attorney, the agent must stop during the call:
make the suppression durable in the gate's state, acknowledge, and hang up.

- :mod:`detect` recognizes a cessation in a final transcript: a conservative
  phrase list first, then a small LLM classifier with a short timeout that
  fails closed.
- :mod:`monitor` subscribes to the session's transcripts, runs detection
  concurrently with the audio path, and enforces in the mandatory order:
  detect, write suppression and confirm, acknowledge, hang up. It measures
  the latency from the final transcript event to the durable write.
- :mod:`store` is the agent-side adapter onto the gate database.
"""

from livekit_outbound.cessation.detect import (
    CessationDetector,
    Classifier,
    ClassifierError,
    Detection,
    LLMClassifier,
    Verdict,
    match_fast_path,
)
from livekit_outbound.cessation.monitor import (
    ACKNOWLEDGEMENTS,
    CallContext,
    CessationMonitor,
)
from livekit_outbound.cessation.store import GateSuppressionStore, SuppressionStore

__all__ = [
    "ACKNOWLEDGEMENTS",
    "CallContext",
    "CessationDetector",
    "CessationMonitor",
    "Classifier",
    "ClassifierError",
    "Detection",
    "GateSuppressionStore",
    "LLMClassifier",
    "SuppressionStore",
    "Verdict",
    "match_fast_path",
]
