"""Policy gate for regulated outbound voice.

Before a number is dialed, :func:`evaluate_gate` checks it against the
rules that TCPA, FDCPA, and Regulation F apply to outbound calling --
stop-contact suppression, the 7-day post-conversation cooldown, the
7-in-7 frequency cap, and the 8am-9pm calling window at the dialed
number's location -- and writes an audit record of the decision.
"""

from livekit_outbound.gate.decide import (
    ALLOW_REASON_CODE,
    RULES,
    RULES_BY_CHECK,
    GateDecision,
    Rule,
    evaluate_gate,
    resolve_timezones,
)

__all__ = [
    "ALLOW_REASON_CODE",
    "RULES",
    "RULES_BY_CHECK",
    "GateDecision",
    "Rule",
    "evaluate_gate",
    "resolve_timezones",
]
