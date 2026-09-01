"""Panel conditioning and read retries (§spec:patch-protocol, MEASUREMENT.md).

Parity with the measure path display-report retired in `dd7425e`. Its
numbers — the ones the report's analysis was written against — came off
panels held at video-like load between patches and warmed for ten
minutes before the first reading. A session that drives solid patches
back to back measures a thermal state the display does not otherwise
occupy, so this is a condition of the measurement, not a nicety.
"""

from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pytest
from bmd_sg.decklink import MockBMDDeckLink

from display_measure.events import UnreadablePatch
from display_measure.instrument import XYZReading
from display_measure.plausible_display import PlausibleDisplay
from display_measure.protocol import REPORT_SUITE, Patch
from display_measure.session import (
    DECKLINK_INDEX,
    _condition,
    _read,
    characterize,
    normalized_reading,
)
from display_measure.wire import RGB12

FIXED_TIME = datetime(2026, 8, 10, 12, 0, 0, tzinfo=UTC)

# What `Stumbling` hands back once it stops stumbling.
READING = XYZReading(XYZ=np.array([1.0, 1.0, 1.0]))


class TestConditioningFrames:
    def test_conditioning_drives_frames_for_the_time_it_is_given(self) -> None:
        with MockBMDDeckLink(DECKLINK_INDEX) as device:
            device._max_frame_history = 1000
            _condition(device, RGB12, 0.4, seed=0, label="t")

            assert len(device.get_frame_history()) >= 2

    def test_one_seed_drives_one_sequence(self) -> None:
        """Nothing records these frames, but they reach the display, and
        two runs of one seed are one session (§spec:artifact-chain)."""
        drives = []
        for _ in range(2):
            with MockBMDDeckLink(DECKLINK_INDEX) as device:
                device._max_frame_history = 1000
                _condition(device, RGB12, 0.3, seed=7, label="t")
                drives.append([bytes(f) for f in device.get_frame_history()])

        assert drives[0][: len(drives[1])] == drives[1][: len(drives[0])]

    def test_a_different_seed_drives_a_different_sequence(self) -> None:
        frames = []
        for seed in (1, 2):
            with MockBMDDeckLink(DECKLINK_INDEX) as device:
                device._max_frame_history = 1000
                _condition(device, RGB12, 0.2, seed=seed, label="t")
                frames.append(bytes(device.get_frame_history()[0]))

        assert frames[0] != frames[1]

    def test_zero_seconds_drives_nothing(self) -> None:
        """A doubles session has no junction temperature to hold."""
        with MockBMDDeckLink(DECKLINK_INDEX) as device:
            _condition(device, RGB12, 0.0, seed=0, label="t")

            assert not device.get_frame_history()


class TestConditioningInASession:
    def test_a_session_conditions_before_every_patch(self, tmp_path: Path) -> None:
        """More frames than patches means colour went out between them."""
        patches = len(REPORT_SUITE.patches)
        with MockBMDDeckLink(DECKLINK_INDEX) as device:
            device._max_frame_history = 100_000
            characterize(
                device=device,
                instrument=PlausibleDisplay(device),
                out_path=tmp_path / "conditioned.csmf",
                clock=lambda: FIXED_TIME,
                settle_seconds=0.0,
                suite=REPORT_SUITE,
                warmup_seconds=0.01,
                conditioning_seconds=0.001,
                read_attempts=1,
                reading=normalized_reading(),
            )

            assert len(device.get_frame_history()) > patches

    def test_the_report_protocol_carries_what_display_report_drove(self) -> None:
        """`--warmup 10` minutes, `--stabilization-time 5`, ten attempts.

        On the protocol, not the session: these are part of what makes
        two report artifacts comparable, and a caller that has to
        remember them is a caller that will not."""
        assert REPORT_SUITE.warmup_seconds == 600.0
        assert REPORT_SUITE.conditioning_seconds == 5.0
        assert REPORT_SUITE.read_attempts == 10

    def test_the_ocio_protocol_measures_as_protocol_3_did(self) -> None:
        """It drove solid patches back to back and read each once, and
        every artifact promoted under that name was measured that way."""
        from display_measure.protocol import VERIFY_SUITE

        assert VERIFY_SUITE.warmup_seconds == 0.0
        assert VERIFY_SUITE.conditioning_seconds == 0.0
        assert VERIFY_SUITE.read_attempts == 1


class Stumbling:
    """An instrument that fails `failures` times, then reads.

    Satisfies the session's `Instrument` protocol structurally; the
    reading it returns stands in for one, since what is under test is
    how many attempts `_read` makes, not what it got back.
    """

    manufacturer = "test"
    model = "Stumbling"
    serial_number = "0"

    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.calls = 0

    def measure(self) -> XYZReading:
        self.calls += 1
        if self.calls <= self.failures:
            raise TimeoutError("truncated serial reply")
        return READING


class TestReadRetries:
    def test_a_transient_failure_is_retried(self) -> None:
        """The bench instrument's failures at the bottom of a panel are
        the link stumbling, not the patch being unreadable."""
        instrument = Stumbling(failures=3)

        assert _read(instrument, Patch("p", (0, 0, 0), "r"), attempts=10) is READING
        assert instrument.calls == 4

    def test_a_patch_no_attempt_can_read_fails_the_session(self) -> None:
        """The artifact is all-or-nothing: a hole in the ramps is not a
        shorter session (§spec:artifact-chain)."""
        instrument = Stumbling(failures=99)

        with pytest.raises(UnreadablePatch, match="'p'"):
            _read(instrument, Patch("p", (0, 0, 0), "r"), attempts=3)

        assert instrument.calls == 3
