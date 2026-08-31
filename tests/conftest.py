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
from typing import Any, cast

import numpy as np
import pytest
import yaml
from bmd_sg.decklink import MockBMDDeckLink, PixelFormatType

from display_measure.events import SessionEvent
from display_measure.session import Clock, doubles_session
from display_measure.wire import V210

FIXED_TIME = datetime(2026, 8, 10, 12, 0, 0, tzinfo=UTC)


def projection(path: Path) -> dict[str, Any]:
    """The seam file's provenance block, parsed.

    The artifact's canonical projection rides in CSMF's ancillary field
    (§spec:measurements-artifact), so this is where the fields that used
    to be the YAML artifact now live. Assertions read it rather than the
    protobuf, which is the point of the block.
    """
    from specio.serialization.csmf import load_csmf_file

    from display_measure.artifact import carried_projection

    _, text = carried_projection(load_csmf_file(path).ancillary)
    return cast("dict[str, Any]", yaml.safe_load(text))


def rows(path: Path) -> list[Any]:
    """The seam file's measurement rows, in driven order."""
    from specio.serialization.csmf import load_csmf_file

    return list(load_csmf_file(path).measurements)


@pytest.fixture
def device() -> Iterator[MockBMDDeckLink]:
    """A mock DeckLink declaring the session's wire format."""
    with MockBMDDeckLink(0) as mock:
        mock.pixel_format = PixelFormatType.FORMAT_12BIT_RGB
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
    out = tmp_path_factory.mktemp("display") / "measurements.csmf"
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
def v210_run(
    tmp_path_factory: pytest.TempPathFactory, fixed_clock: Clock
) -> tuple[Path, MockBMDDeckLink]:
    """One doubles session over the v210 link: artifact path and device."""
    out = tmp_path_factory.mktemp("v210") / "measurements.csmf"
    device = doubles_session(out, clock=fixed_clock, settle_seconds=0.0, encoding=V210)
    return (out, device)


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
    out = tmp_path_factory.mktemp("events") / "measurements.csmf"
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
    out = tmp_path_factory.mktemp("hybrid") / "measurements.csmf"
    doubles_session(
        out,
        clock=fixed_clock,
        settle_seconds=0.0,
        hybrid=True,
        luminance_threshold=full_ramp_threshold,
    )
    return out


@pytest.fixture(scope="session")
def routed_hybrid_artifact(
    tmp_path_factory: pytest.TempPathFactory,
    fixed_clock: Clock,
) -> Path:
    """One hybrid session at the shipped routing threshold — the split a
    real rig runs, where the bright patches land on the
    spectroradiometer and the dark rungs on the disciplined colorimeter.

    `hybrid_artifact` forces every row to the colorimeter to check the
    correction; this one is what the CLI's default drives, so it is the
    fixture that carries all three spectral provenances at once.
    """
    out = tmp_path_factory.mktemp("routed") / "measurements.csmf"
    doubles_session(out, clock=fixed_clock, settle_seconds=0.0, hybrid=True)
    return out
