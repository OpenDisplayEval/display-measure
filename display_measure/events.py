"""The session event stream: one seam, N consumers (§spec:session-events).

A session reports its lifecycle here and nowhere else. The CLI's
session log is a consumer (:mod:`display_measure.session_log`); so is
color-wrangler's operator UI, in another repository. That is the whole
point of the seam — the core stays presentation-agnostic and a
hardware session stays scriptable headless.

Rejected: frontends parsing the log stream (unstructured, no counts, no
machine contract), and the core calling frontend callbacks directly
(couples the measurement loop to a frontend's lifecycle).

**This module is the package's public contract, and it is deliberately
cheap.** It imports nothing but the standard library: no numpy, no
specio, no colour. A consumer — or `display-measure --help` — can
import `display_measure` and pattern-match the whole lifecycle without
paying for the measurement stack. Every event is a frozen dataclass of
plain values, so `dataclasses.asdict` renders one for a wire protocol
and the class name is its discriminator. Nothing here holds a live
object: an event outlives the session that emitted it.

Consumers should ignore event types they do not recognize. New events
are added by subclassing `SessionEvent`; that is not a breaking change,
and a consumer written against today's set keeps working.

Ordering is fixed. `SessionStarted` is first and `SessionEnded` is
last, exactly once each, on every path — completed, refused or
failed. One exception, named here so no consumer discovers it the
hard way: the hardware path audits the processor *before* the session
opens (`display_measure.session.hardware_session`), so a refusal there
raises without any event at all. Nothing has started to end.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

__all__ = [
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
    "SessionEnded",
    "SessionEvent",
    "SessionMode",
    "SessionStarted",
]


class SessionMode(StrEnum):
    """What a session drives and hands off (§spec:sessions).

    `verify` registers here when its workstream lands; the modes share
    one core and differ only in that.
    """

    CHARACTERIZE = "characterize"


class Gate(StrEnum):
    """The gates a session can report an outcome for (§spec:session-gates).

    Only the gates the session core itself runs are listed. The wire
    format and output scaling gates run in the hardware path's
    pre-session processor audit, ahead of `SessionStarted`, and refuse
    the command rather than the session.
    """

    CONTRACT_AUDIT = "contract-audit"
    PANEL_STATE = "panel-state"
    AMBIENT = "ambient-gate"
    OUTPUT_LEVEL = "output-level"


class GateVerdict(StrEnum):
    """How a gate resolved.

    `ATTESTED` is not a pass: the panel-state gate carries what the
    operator confirmed rather than comparing it, because no instrument
    here can read it (§spec:session-gates). `STUB` marks a gate whose
    seam exists but whose refusal §road:session-gates has yet to fill
    in — a consumer that renders it as a pass would be claiming a check
    nobody ran.
    """

    PASS = "pass"
    REFUSED = "refused"
    ATTESTED = "attested"
    STUB = "stub"


class Outcome(StrEnum):
    """How a session ended.

    `REFUSED` is a gate stopping the session on purpose; `FAILED` is
    anything else going wrong. The distinction is the operator's next
    move — fix the rig, or file a bug.
    """

    COMPLETED = "completed"
    REFUSED = "refused"
    FAILED = "failed"


@dataclass(frozen=True)
class SessionEvent:
    """Base of every session event.

    Carries no fields: the concrete types carry different payloads and
    a lowest common denominator would be a field every consumer has to
    ignore. It exists so a sink can be typed once
    (`Callable[[SessionEvent], None]`) and so adding an event type does
    not widen a union every consumer has to re-match.
    """


@dataclass(frozen=True)
class SessionStarted(SessionEvent):
    """A session opens, before any gate runs.

    `patch_count` is exact because the patch protocol is fixed and
    versioned (§spec:patch-protocol): a consumer renders progress as
    measured-of-total from the first event, with no heuristic and no
    waiting to see how many patches show up.
    """

    mode: SessionMode
    protocol_name: str
    patch_count: int
    at: datetime


@dataclass(frozen=True)
class GateEvaluated(SessionEvent):
    """A gate resolved (§spec:session-gates).

    `detail` says what the gate read and what it expected — the same
    sentence a refusal raises, so a UI surfaces a refusal with the gate
    that produced it rather than a bare exit code.
    """

    gate: Gate
    verdict: GateVerdict
    detail: str


@dataclass(frozen=True)
class PlaybackStarted(SessionEvent):
    """The DeckLink is up and signaling, before the first patch.

    Both fields are the enum *names* rather than the enums: an event
    crossing a wire should not drag bmd-signal-gen in behind it.
    """

    pixel_format: str
    eotf: str


@dataclass(frozen=True)
class PatchStarted(SessionEvent):
    """A patch is on the wall (the drive stage). `index` is 1-based."""

    index: int
    patch: str
    rgb: tuple[int, int, int]


@dataclass(frozen=True)
class PatchSettling(SessionEvent):
    """The settle stage begins — emitted before the wait, not after.

    A settle and an instrument read are the two places a session sits
    still for a while. Announcing them as they start is what makes a
    run auditable while it happens rather than only after it ends
    (§spec:sessions).
    """

    index: int
    seconds: float


@dataclass(frozen=True)
class PatchCompleted(SessionEvent):
    """One patch measured: the read landed and the step is done.

    `xyz` is the reading summary in absolute cd/m² and `seconds` covers
    the whole step — drive, settle and read. Instrument reads dominate
    a session's wall clock and vary by instrument and patch level, so a
    consumer estimating remaining time has to measure the pace rather
    than assume a constant (§spec:session-events).
    """

    index: int
    patch: str
    xyz: tuple[float, float, float]
    seconds: float


@dataclass(frozen=True)
class HandoffCompleted(SessionEvent):
    """The immutable artifact is on disk (§spec:artifact-chain).

    The hash is the artifact's identity: promotion records it, so a
    consumer showing the operator what was produced shows this.
    """

    path: str
    sha256: str


@dataclass(frozen=True)
class SessionEnded(SessionEvent):
    """The last event on every path. `detail` is empty when it completed."""

    outcome: Outcome
    at: datetime
    detail: str = ""


# What a session emits into. Synchronous and in-order: a consumer that
# wants a queue owns the queue, because the core has no opinion about
# how a frontend schedules itself. A sink that raises fails the session.
EventSink = Callable[[SessionEvent], None]
