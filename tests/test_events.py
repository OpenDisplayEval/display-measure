"""The session event stream and its log consumer (§spec:session-events)."""

import hashlib
import io
import logging
import subprocess
import sys
from dataclasses import asdict, is_dataclass, replace
from datetime import datetime
from pathlib import Path

import numpy as np
import pytest
from bmd_sg.decklink import MockBMDDeckLink

from display_measure.artifact import DECLARED_CONTRACT
from display_measure.events import (
    Gate,
    GateEvaluated,
    GateVerdict,
    HandoffCompleted,
    Outcome,
    PatchCompleted,
    PatchSettling,
    PatchStarted,
    PlaybackStarted,
    SessionEnded,
    SessionEvent,
    SessionMode,
    SessionStarted,
)
from display_measure.hybrid import DerivationRefused, HybridInstrument
from display_measure.instrument import XYZReading
from display_measure.plausible_wall import PlausibleWall
from display_measure.processor import ContractViolation
from display_measure.protocol import PROTOCOL_NAME, protocol_patches
from display_measure.session import (
    Clock,
    characterize,
    doubles_session,
    normalized_reading,
)
from display_measure.session_log import log_events

# What an event may carry: values that survive `asdict`, a wire, and
# the death of the session that emitted them.
PLAIN = (str, int, float, bool, datetime, type(None))


def of_type[T: SessionEvent](
    stream: tuple[SessionEvent, ...], kind: type[T]
) -> list[T]:
    return [event for event in stream if isinstance(event, kind)]


def test_the_stream_opens_with_the_session_and_its_patch_count(
    wall_stream: tuple[SessionEvent, ...],
) -> None:
    """The count rides the first event, so a consumer renders
    measured-of-total from the start rather than guessing
    (§spec:session-events)."""
    started = wall_stream[0]
    assert isinstance(started, SessionStarted)
    assert started.mode == SessionMode.CHARACTERIZE
    assert started.protocol_name == PROTOCOL_NAME
    assert started.patch_count == len(protocol_patches())


def test_the_stream_closes_exactly_once_with_the_outcome(
    wall_stream: tuple[SessionEvent, ...],
) -> None:
    ended = of_type(wall_stream, SessionEnded)
    assert len(ended) == 1
    assert ended[0] is wall_stream[-1]
    assert ended[0].outcome == Outcome.COMPLETED
    assert ended[0].detail == ""


def test_every_patch_reports_its_completion_with_a_reading_and_a_duration(
    wall_stream: tuple[SessionEvent, ...],
) -> None:
    started = of_type(wall_stream, SessionStarted)[0]
    completed = of_type(wall_stream, PatchCompleted)
    assert len(completed) == started.patch_count
    assert [event.index for event in completed] == list(
        range(1, started.patch_count + 1)
    )
    assert {event.patch for event in completed} == {
        patch.name for patch in protocol_patches()
    }
    for event in completed:
        assert len(event.xyz) == 3
        assert event.seconds == 0.0, "the fixed clock renders every step as zero"
    # Black opens a single-instrument session, and its reading is dark.
    assert completed[0].patch == "black"
    assert completed[0].xyz[1] < 1.0


def test_each_patch_walks_drive_then_settle_then_read(
    wall_stream: tuple[SessionEvent, ...],
) -> None:
    """The three per-patch stages §spec:sessions names, in order and
    announced before the session sits still for them."""
    steps = [
        event
        for event in wall_stream
        if isinstance(event, PatchStarted | PatchSettling | PatchCompleted)
    ]
    assert len(steps) == 3 * len(protocol_patches())
    for drive, settle, read in zip(steps[::3], steps[1::3], steps[2::3], strict=True):
        assert isinstance(drive, PatchStarted)
        assert isinstance(settle, PatchSettling)
        assert isinstance(read, PatchCompleted)
        assert drive.index == settle.index == read.index
        assert drive.patch == read.patch


def test_playback_declares_the_wire_format_before_the_first_patch(
    wall_stream: tuple[SessionEvent, ...],
) -> None:
    playback = of_type(wall_stream, PlaybackStarted)
    assert len(playback) == 1
    assert playback[0].pixel_format == "FORMAT_12BIT_RGB"
    assert playback[0].eotf == "SDR"
    assert wall_stream.index(playback[0]) < wall_stream.index(
        of_type(wall_stream, PatchStarted)[0]
    )


def test_every_gate_the_session_holds_reports_an_outcome(
    wall_stream: tuple[SessionEvent, ...],
) -> None:
    verdicts = {
        event.gate: event.verdict for event in of_type(wall_stream, GateEvaluated)
    }
    assert verdicts == {
        Gate.CONTRACT_AUDIT: GateVerdict.PASS,
        Gate.AMBIENT: GateVerdict.STUB,
        Gate.OUTPUT_LEVEL: GateVerdict.PASS,
        # The only gate that resolves after the protocol: a ramp is not a
        # ramp until it is measured (§road:session-consistency).
        Gate.SELF_CONSISTENCY: GateVerdict.PASS,
    }


def test_the_ambient_gate_reports_a_stub_rather_than_a_pass(
    wall_stream: tuple[SessionEvent, ...],
) -> None:
    """Rendering an unrun check as a pass would claim a gate nobody
    held (§road:session-gates)."""
    ambient = next(
        event
        for event in of_type(wall_stream, GateEvaluated)
        if event.gate == Gate.AMBIENT
    )
    assert ambient.verdict == GateVerdict.STUB
    assert "§road:session-gates" in ambient.detail


def test_handoff_carries_the_artifact_path_and_its_real_hash(
    wall_events: tuple[tuple[SessionEvent, ...], Path],
) -> None:
    stream, out = wall_events
    handoff = of_type(stream, HandoffCompleted)
    assert len(handoff) == 1
    assert handoff[0].path == str(out)
    assert handoff[0].sha256 == hashlib.sha256(out.read_bytes()).hexdigest()


def test_a_refusal_names_the_gate_and_ends_the_session(
    fixed_clock: Clock, tmp_path: Path
) -> None:
    """A session-end event carries the message; the gate event says
    which check produced it, which is what sends the operator to the
    right place at the rig (§spec:web-ui)."""
    stream: list[SessionEvent] = []
    with pytest.raises(ContractViolation):
        doubles_session(
            tmp_path / "refused.yaml",
            clock=fixed_clock,
            settle_seconds=0.0,
            declared=replace(DECLARED_CONTRACT, intensity="1800 nits"),
            emit=stream.append,
        )
    refusals = [
        event
        for event in of_type(tuple(stream), GateEvaluated)
        if event.verdict == GateVerdict.REFUSED
    ]
    assert [event.gate for event in refusals] == [Gate.OUTPUT_LEVEL]
    assert "1800" in refusals[0].detail
    ended = of_type(tuple(stream), SessionEnded)
    assert len(ended) == 1
    assert ended[0].outcome == Outcome.REFUSED
    assert "1800" in ended[0].detail
    assert not of_type(tuple(stream), HandoffCompleted), "a refusal hands nothing off"


def test_every_event_renders_to_plain_data(
    wall_stream: tuple[SessionEvent, ...],
) -> None:
    """The stream crosses a repository boundary and will cross a wire.

    Plain frozen dataclasses of plain values: `asdict` renders one and
    the class name discriminates it. An event holding a live object —
    a device, an instrument, a numpy array — would not survive either
    trip (§spec:session-events).
    """
    for event in wall_stream:
        assert is_dataclass(event)
        for name, value in asdict(event).items():
            values = value if isinstance(value, tuple) else (value,)
            assert all(isinstance(item, PLAIN) for item in values), (event, name)


def test_importing_the_package_costs_no_measurement_stack() -> None:
    """A frontend imports the lifecycle it renders, not numpy and specio.

    Subprocess probe: another test may already have imported the stack
    into this process, which would make an in-process check vacuous.
    """
    probe = (
        "import sys; "
        "import display_measure; "
        "assert display_measure.SessionStarted; "
        "assert not {'numpy', 'specio', 'colour'} & set(sys.modules), "
        "sorted({'numpy', 'specio', 'colour'} & set(sys.modules))"
    )
    subprocess.run([sys.executable, "-c", probe], check=True)


def render(stream: tuple[SessionEvent, ...]) -> str:
    """The stream through the log consumer, captured as text."""
    logger = logging.getLogger("display_measure")
    buffer = io.StringIO()
    handler = logging.StreamHandler(buffer)
    previous = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        for event in stream:
            log_events(event)
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous)
    return buffer.getvalue()


def test_the_session_log_is_the_event_stream_rendered(
    wall_log: str,
    wall_events: tuple[tuple[SessionEvent, ...], Path],
    wall_artifact: Path,
) -> None:
    """§road:cli-log-from-events: not a parallel path.

    Two independent runs — one with the default sink, which logs, and
    one with a collecting sink, which does not. Replaying the collected
    stream through the log consumer reproduces the first run's log
    exactly. Only the artifact path differs, because the two runs write
    to different directories.
    """
    stream, out = wall_events
    replayed = render(stream).replace(str(out), "<artifact>")
    assert replayed == wall_log.replace(str(wall_artifact), "<artifact>")


def test_the_log_still_narrates_every_stage(
    wall_stream: tuple[SessionEvent, ...],
) -> None:
    """The stages §spec:sessions names, still in the log now that the
    log renders events rather than writing its own lines."""
    rendered = render(wall_stream)
    for marker in (
        "session: characterize",
        "contract audit: PASS",
        "playback:",
        "ambient gate: STUB",
        "output level: PASS",
        "patch drive:",
        "settle:",
        "instrument read:",
        "handoff:",
        "sha256",
        "session ended: completed",
    ):
        assert marker in rendered, marker


def test_an_unknown_event_does_not_fail_the_session_it_narrates(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A consumer ignores what it does not recognize, so adding an
    event type never breaks one (§spec:session-events)."""

    class Invented(SessionEvent):
        pass

    with caplog.at_level(logging.DEBUG, logger="display_measure"):
        log_events(Invented())
    assert "unrendered session event" in caplog.text


class UnfitColorimeter:
    """A colorimeter double whose disagreement no filter mismatch explains.

    The shipped `MismatchedColorimeter` is fit by construction, so the
    derivation-fitness gate never fires against it. This one reads the
    same wall through a gross per-channel skew, which is what an
    instrument fault looks like from the session's side.
    """

    manufacturer = "display-measure"
    model = "UnfitColorimeter"
    serial_number = "unfit-1"

    def __init__(self, wall: PlausibleWall) -> None:
        self._wall = wall

    def measure(self) -> XYZReading:
        skew = np.array([2.5, 0.4, 3.0])
        return XYZReading(XYZ=skew * self._wall.measure().XYZ)


def test_an_unfit_correction_refuses_under_its_own_gate(
    fixed_clock: Clock, tmp_path: Path
) -> None:
    """The derivation-fitness gate lives inside the instrument, so the
    read is where the session can name it (§spec:session-gates). Without
    the name a consumer sees only that something refused."""
    out = tmp_path / "unfit.yaml"
    stream: list[SessionEvent] = []
    with MockBMDDeckLink(0) as device, pytest.raises(DerivationRefused):
        device._max_frame_history = len(protocol_patches())
        wall = PlausibleWall(device)
        characterize(
            device=device,
            instrument=HybridInstrument(wall, UnfitColorimeter(wall)),
            out_path=out,
            clock=fixed_clock,
            settle_seconds=0.0,
            reading=normalized_reading(),
            emit=stream.append,
        )
    refusals = [
        event
        for event in of_type(tuple(stream), GateEvaluated)
        if event.verdict == GateVerdict.REFUSED
    ]
    assert [event.gate for event in refusals] == [Gate.DERIVATION_FITNESS]
    assert of_type(tuple(stream), SessionEnded)[0].outcome == Outcome.REFUSED
    assert not out.exists(), "a refused session writes no artifact"
