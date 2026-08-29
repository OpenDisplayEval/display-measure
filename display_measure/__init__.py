"""Gated instrument sessions for LED-wall characterization (§spec:sessions).

The session event stream is re-exported here because it is the package's
public contract: a consumer — color-wrangler's operator UI, an RPC
surface later — imports the lifecycle from `display_measure` and never
reaches into a private module for it (§spec:session-events).

Importing this package costs nothing but the standard library. The
measurement stack (numpy, specio, colour) loads when a session runs,
not when a frontend imports the events it renders, and not for
`display-measure --help`.
"""

from display_measure.events import (
    Cancelled,
    EventSink,
    Gate,
    GateEvaluated,
    GateVerdict,
    HandoffCompleted,
    Outcome,
    PatchCompleted,
    PatchSettling,
    PatchStarted,
    PlaybackStarted,
    SessionCancelled,
    SessionEnded,
    SessionEvent,
    SessionMode,
    SessionStarted,
)

__all__ = [
    "Cancelled",
    "EventSink",
    "Gate",
    "GateEvaluated",
    "GateVerdict",
    "HandoffCompleted",
    "Outcome",
    "PatchCompleted",
    "PatchSettling",
    "PatchStarted",
    "PlaybackStarted",
    "SessionCancelled",
    "SessionEnded",
    "SessionEvent",
    "SessionMode",
    "SessionStarted",
]
