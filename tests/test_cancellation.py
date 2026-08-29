"""Cancelling a session between patch steps (§spec:session-events)."""

from pathlib import Path
from typing import Any, cast

import pytest
from bmd_sg.decklink import MockBMDDeckLink

from display_measure.events import (
    Outcome,
    PatchCompleted,
    PatchStarted,
    SessionCancelled,
    SessionEnded,
    SessionEvent,
)
from display_measure.plausible_display import PlausibleDisplay
from display_measure.protocol import protocol_patches
from display_measure.session import (
    Clock,
    characterize,
    doubles_session,
    normalized_reading,
)


class CancelAfter:
    """A cancel source that answers true once `count` patches are in.

    Counts `PatchCompleted` rather than a display clock, so the test says
    exactly where the operator pressed the button and does not race the
    session it is cancelling.
    """

    def __init__(self, count: int) -> None:
        self.count = count
        self.measured = 0
        self.stream: list[SessionEvent] = []

    def emit(self, event: SessionEvent) -> None:
        self.stream.append(event)
        if isinstance(event, PatchCompleted):
            self.measured += 1

    def __call__(self) -> bool:
        return self.measured >= self.count


def run_cancelled(out: Path, clock: Clock, after: int) -> CancelAfter:
    """One doubles session cancelled after `after` patches are measured."""
    cancel = CancelAfter(after)
    with pytest.raises(SessionCancelled) as raised:
        doubles_session(
            out,
            clock=clock,
            settle_seconds=0.0,
            emit=cancel.emit,
            cancelled=cancel,
        )
    assert "no artifact written" in str(raised.value)
    return cancel


def test_a_cancelled_session_writes_no_artifact(
    fixed_clock: Clock, tmp_path: Path
) -> None:
    """The guarantee that matters. A measurements artifact is immutable
    and complete (§spec:artifact-chain), so a partial one does not
    exist — a cancelled session's output is nothing at all."""
    out = tmp_path / "cancelled.yaml"
    run_cancelled(out, fixed_clock, after=3)
    assert not out.exists()
    assert list(tmp_path.iterdir()) == []


def test_cancellation_lands_between_patches_not_inside_one(
    fixed_clock: Clock, tmp_path: Path
) -> None:
    """Stopping mid-patch would leave a frame on the display with no
    reading behind it, and there is nothing to salvage by doing so."""
    cancel = run_cancelled(tmp_path / "cancelled.yaml", fixed_clock, after=3)
    driven = [e for e in cancel.stream if isinstance(e, PatchStarted)]
    measured = [e for e in cancel.stream if isinstance(e, PatchCompleted)]
    assert len(driven) == len(measured) == 3
    assert [e.patch for e in driven] == [e.patch for e in measured]


def test_the_stream_ends_cancelled(fixed_clock: Clock, tmp_path: Path) -> None:
    cancel = run_cancelled(tmp_path / "cancelled.yaml", fixed_clock, after=2)
    ended = [e for e in cancel.stream if isinstance(e, SessionEnded)]
    assert len(ended) == 1
    assert ended[0] is cancel.stream[-1]
    assert ended[0].outcome == Outcome.CANCELLED
    assert "2 of 72" in ended[0].detail


def test_cancelling_before_the_first_patch_drives_nothing(
    fixed_clock: Clock, tmp_path: Path
) -> None:
    out = tmp_path / "cancelled.yaml"
    cancel = run_cancelled(out, fixed_clock, after=0)
    assert not [e for e in cancel.stream if isinstance(e, PatchStarted)]
    assert not out.exists()


def test_cancellation_stops_playback(fixed_clock: Clock, tmp_path: Path) -> None:
    """The display goes back to nothing. A rig left holding the last patch
    after the session ended is a rig in an unknown state."""
    device = MockBMDDeckLink(0)
    cancel = CancelAfter(2)
    with device, pytest.raises(SessionCancelled):
        device._max_frame_history = len(protocol_patches())
        characterize(
            device=device,
            instrument=PlausibleDisplay(device),
            out_path=tmp_path / "cancelled.yaml",
            clock=fixed_clock,
            settle_seconds=0.0,
            reading=normalized_reading(),
            emit=cancel.emit,
            cancelled=cancel,
        )
    calls = cast("list[dict[str, Any]]", device.get_method_calls("stop_playback"))
    assert len(calls) == 1
