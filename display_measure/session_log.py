"""The session log: a consumer of the event stream, not a second path.

Every line a session narrates is rendered here, from the events
`display_measure.events` defines. The core emits; this formats. That
ordering is the point of §spec:session-events — when the log was
written from inside the measurement loop it was a second reporting
path, and a frontend that wanted the same facts had to parse it.

The session core defaults its sink to `log_events`, so a library
caller and the CLI both get the narration for free and neither is
privileged. A frontend that wants no log passes its own sink.

Stdlib only, like the events themselves: rendering a session must not
drag numpy or specio in behind it.
"""

import logging

from display_measure.events import (
    GateEvaluated,
    HandoffCompleted,
    PatchCompleted,
    PatchSettling,
    PatchStarted,
    PlaybackStarted,
    SessionEnded,
    SessionEvent,
    SessionStarted,
)

__all__ = ["log_events"]

log = logging.getLogger("display_measure.session")


def _gate_name(value: str) -> str:
    """The gate's slug as prose: `output-level` reads `output level`.

    The enum values are slugs because they are a wire contract a UI
    matches on; a log line is for a human reading it at the rig.
    """
    return value.replace("-", " ")


def log_events(event: SessionEvent) -> None:
    """Render one session event to the session log.

    Unrecognized events fall through to DEBUG rather than raising: a
    sink that fails the session it is only narrating would trade a
    measurement for a formatting bug.
    """
    match event:
        case SessionStarted(mode, protocol_name, patch_count):
            log.info(
                "session: %s, protocol %s, %d patches",
                mode,
                protocol_name,
                patch_count,
            )
        case GateEvaluated(gate, verdict, detail):
            log.info("%s: %s — %s", _gate_name(gate), verdict.upper(), detail)
        case PlaybackStarted(pixel_format, eotf):
            log.info("playback: %s with explicit %s signaling", pixel_format, eotf)
        case PatchStarted(_, patch, rgb):
            log.info("patch drive: %s %r", patch, rgb)
        case PatchSettling(_, seconds):
            log.info("settle: %.3f s", seconds)
        case PatchCompleted(_, patch, xyz, _):
            log.info(
                "instrument read: %s XYZ=[%.4f %.4f %.4f]",
                patch,
                xyz[0],
                xyz[1],
                xyz[2],
            )
        case HandoffCompleted(path, sha256):
            log.info("handoff: wrote %s (sha256 %s)", path, sha256)
        case SessionEnded(outcome, _, detail):
            log.info("session ended: %s%s", outcome, f" — {detail}" if detail else "")
        case _:
            log.debug("unrendered session event: %r", event)
