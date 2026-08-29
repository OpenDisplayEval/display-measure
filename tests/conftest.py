"""Shared fixtures: one doubles session reused by assertion tests.

Session-scoped so the CLI tests can compare bytes against the same run
the session tests assert on. Separate runs happen only where the test
needs one: the second determinism run, the random-instrument divergence
run, the overwrite refusal, and the event-stream run.

`display_run` deliberately takes the *default* sink, so what it captures is
the log a library caller gets for free; `display_events` collects the same
session as events with no log attached. Two runs, not one, so the
log-renders-the-stream test compares two independent paths rather than
comparing a stream against itself (§spec:session-events).
"""

import io
import logging
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pytest
from bmd_sg.decklink import MockBMDDeckLink

from display_measure.events import SessionEvent
from display_measure.protocol import PATCH_PIXEL_FORMAT
from display_measure.session import Clock, doubles_session

FIXED_TIME = datetime(2026, 8, 10, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def device() -> Iterator[MockBMDDeckLink]:
    """A mock DeckLink declaring the session's wire format."""
    with MockBMDDeckLink(0) as mock:
        mock.pixel_format = PATCH_PIXEL_FORMAT
        yield mock


def drive(device: MockBMDDeckLink, rgb: tuple[int, int, int]) -> None:
    """Drive a solid patch; the display doubles read the frame back."""
    device.display_frame(np.full((4, 4, 3), rgb, dtype=np.uint16))


@pytest.fixture(scope="session")
def fixed_clock() -> Clock:
    return lambda: FIXED_TIME


@pytest.fixture(scope="session")
def display_run(
    tmp_path_factory: pytest.TempPathFactory,
    fixed_clock: Clock,
) -> tuple[Path, str, MockBMDDeckLink]:
    """One plausible-display session: artifact path, captured log, closed device."""
    out = tmp_path_factory.mktemp("display") / "measurements.yaml"
    # The package logger, not just the session module: the contract audit
    # narrates from display_measure.processor and is a session stage.
    logger = logging.getLogger("display_measure")
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    previous_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        device = doubles_session(out, clock=fixed_clock, settle_seconds=0.0)
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)
    return (out, stream.getvalue(), device)


@pytest.fixture(scope="session")
def display_artifact(display_run: tuple[Path, str, MockBMDDeckLink]) -> Path:
    return display_run[0]


@pytest.fixture(scope="session")
def display_log(display_run: tuple[Path, str, MockBMDDeckLink]) -> str:
    return display_run[1]


@pytest.fixture(scope="session")
def display_device(display_run: tuple[Path, str, MockBMDDeckLink]) -> MockBMDDeckLink:
    return display_run[2]


@pytest.fixture(scope="session")
def display_events(
    tmp_path_factory: pytest.TempPathFactory,
    fixed_clock: Clock,
) -> tuple[tuple[SessionEvent, ...], Path]:
    """One plausible-display session collected as events: the stream and its artifact."""
    out = tmp_path_factory.mktemp("events") / "measurements.yaml"
    collected: list[SessionEvent] = []
    doubles_session(out, clock=fixed_clock, settle_seconds=0.0, emit=collected.append)
    return tuple(collected), out


@pytest.fixture(scope="session")
def display_stream(
    display_events: tuple[tuple[SessionEvent, ...], Path],
) -> tuple[SessionEvent, ...]:
    return display_events[0]


@pytest.fixture(scope="session")
def full_ramp_threshold() -> float:
    """Above anything the display double emits, so every patch outside the
    derivation set routes to the disciplined colorimeter — the
    threshold-covers-the-ramp case §spec:sessions names."""
    return 1e6


@pytest.fixture(scope="session")
def hybrid_artifact(
    tmp_path_factory: pytest.TempPathFactory,
    fixed_clock: Clock,
    full_ramp_threshold: float,
) -> Path:
    """One disciplined-colorimeter session over the same display double."""
    out = tmp_path_factory.mktemp("hybrid") / "measurements.yaml"
    doubles_session(
        out,
        clock=fixed_clock,
        settle_seconds=0.0,
        hybrid=True,
        luminance_threshold=full_ramp_threshold,
    )
    return out
