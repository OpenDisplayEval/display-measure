"""The lowest reproducible value, and the shape of the climb out of black."""

from __future__ import annotations

from typing import Any

import numpy as np
import pytest
from bmd_sg.decklink import MockBMDDeckLink

from display_measure.floor import (
    AxisFloor,
    Rung,
    measure_axis,
    measure_floor,
    render_report,
    separates,
)
from display_measure.instrument import InstrumentReading, XYZReading
from display_measure.plausible_display import PlausibleDisplay
from display_measure.wire import RGB12, V210_BT2020


def reading(luminance: float) -> InstrumentReading:
    """A reading carrying one luminance, which is all the walk consumes."""
    return XYZReading(XYZ=np.array([luminance, luminance, luminance]))


class _Named:
    """The identity every Instrument carries; the walk never reads it."""

    manufacturer = "test"
    model = "double"
    serial_number = "0"


class Floored(_Named):
    """An instrument that sees nothing below `floor` and is noisy near it."""

    def __init__(self, floor: float, noise: float = 0.0) -> None:
        self.floor = floor
        self.noise = noise
        self._n = 0
        self.display: Any = None

    def measure(self) -> InstrumentReading:
        self._n += 1
        lit = self.display.measure().XYZ[1] if self.display else 0.0
        if lit < self.floor:
            return reading(self.floor + self.noise * (self._n % 2))
        return reading(lit)


class Refusing(_Named):
    """An instrument that declines the stimulus, as a CR-300 does in the dark."""

    def measure(self) -> InstrumentReading:
        raise RuntimeError("Light intensity too low or unmeasurable")


def test_a_rung_inside_the_noise_of_black_does_not_separate() -> None:
    """Three sigma over the *combined* spread, not the rung's alone."""
    black = Rung(0, (0.0100, 0.0120, 0.0110))
    inside = Rung(8, (0.0115, 0.0125, 0.0105))
    assert not separates(inside, black)
    clear = Rung(32, (0.0400, 0.0410, 0.0405))
    assert separates(clear, black)


def test_a_refused_rung_never_separates() -> None:
    """No reading, no claim: the instrument declining is not a measurement
    of darkness, it is the absence of one."""
    black = Rung(0, (0.010, 0.011, 0.010))
    assert not separates(Rung(4, (), "CommandError: too low"), black)


def test_identical_repeats_still_separate_on_a_rise() -> None:
    """A perfect double reports no spread; a rise is then real to the
    precision the instrument gave, and demanding sigma of zero would
    reject every rung forever."""
    assert separates(Rung(16, (0.05, 0.05)), Rung(0, (0.01, 0.01)))


def test_the_walk_finds_where_the_display_leaves_black() -> None:
    """The plausible display climbs, so some rung clears the floor."""
    with MockBMDDeckLink(0) as device:
        device._max_frame_history = 200
        display = PlausibleDisplay(device, encoding=RGB12)
        axis = measure_axis(
            device, display, RGB12, "neutral", repeats=2, settle_seconds=0.0
        )
    assert axis.lowest_separable is not None
    assert axis.first_step > 0.0
    assert axis.rungs[-1].code == axis.rungs[-1].code  # walk terminated


def test_a_saturated_axis_leaves_black_later_than_a_neutral_one() -> None:
    """Blue emits a fraction of neutral's light at the same code.

    Reporting one floor for the display would describe none of its
    channels, which is why the walk is per axis.
    """
    with MockBMDDeckLink(0) as device:
        device._max_frame_history = 400
        display = PlausibleDisplay(device, encoding=RGB12)
        neutral = measure_axis(
            device, display, RGB12, "neutral", repeats=2, settle_seconds=0.0
        )
        blue = measure_axis(
            device, display, RGB12, "blue", repeats=2, settle_seconds=0.0
        )
    assert neutral.lowest_separable is not None
    assert blue.lowest_separable is not None
    assert blue.lowest_separable >= neutral.lowest_separable


def test_an_instrument_that_refuses_is_recorded_rather_than_raised() -> None:
    """A refusal is the pairing's answer about that stimulus.

    Crashing the walk would lose every rung already measured, and the
    refusal itself is what says a more sensitive instrument is needed.
    """
    with MockBMDDeckLink(0) as device:
        device._max_frame_history = 200
        axis = measure_axis(
            device, Refusing(), RGB12, "neutral", repeats=1, settle_seconds=0.0
        )
    assert axis.lowest_separable is None
    assert all(r.refused for r in axis.rungs)
    assert "unmeasurable" in axis.rungs[0].refused


def test_an_instrument_blind_below_its_floor_reports_no_separation_there() -> None:
    """The bench case: readings exist, and carry no information."""
    with MockBMDDeckLink(0) as device:
        device._max_frame_history = 400
        blind = Floored(floor=10.0, noise=0.002)
        blind.display = PlausibleDisplay(device, encoding=RGB12)
        axis = measure_axis(
            device, blind, RGB12, "neutral", repeats=3, settle_seconds=0.0
        )
    assert axis.lowest_separable is None


def test_the_walk_runs_over_a_ycbcr_link_too() -> None:
    """The floor is a property of the pairing, so it is asked per link."""
    with MockBMDDeckLink(0) as device:
        device._max_frame_history = 200
        display = PlausibleDisplay(device, encoding=V210_BT2020)
        axis = measure_axis(
            device, display, V210_BT2020, "neutral", repeats=2, settle_seconds=0.0
        )
    assert axis.lowest_separable is not None


def test_the_report_shows_every_rung_not_only_the_answer() -> None:
    """The shape is the deliverable: a reviewer placing a protocol floor
    needs to see how steeply the display leaves black."""
    with MockBMDDeckLink(0) as device:
        device._max_frame_history = 800
        display = PlausibleDisplay(device, encoding=RGB12)
        report = measure_floor(
            device,
            display,
            RGB12,
            axes=("neutral", "red"),
            repeats=2,
            settle_seconds=0.0,
        )
    rendered = render_report(report)
    assert "neutral:" in rendered and "red:" in rendered
    assert "lowest reproducible code" in rendered
    assert "over black" in rendered
    for axis in report.axes:
        for rung in axis.rungs:
            assert f"{rung.code:>5}" in rendered


def test_the_report_names_the_darkest_black_it_saw() -> None:
    with MockBMDDeckLink(0) as device:
        device._max_frame_history = 400
        display = PlausibleDisplay(device, encoding=RGB12)
        report = measure_floor(
            device, display, RGB12, axes=("neutral",), repeats=2, settle_seconds=0.0
        )
    assert report.ambient == pytest.approx(report.axes[0].black.mean)


def test_a_walk_that_never_separates_has_no_first_step() -> None:
    axis = AxisFloor("blue", Rung(0, (0.01,)), (Rung(4, (0.01,)),), None)
    assert axis.lowest_separable is None
    assert axis.first_step != axis.first_step  # nan


def test_readings_that_interleave_with_black_are_not_reproducible() -> None:
    """The means differ, and the readings overlap.

    Averaging cannot rescue a rung whose readings fall inside black's
    range: on any given session it might read darker than black, which is
    the opposite of reproducible.
    """
    black = Rung(0, (0.0100, 0.0150, 0.0125))
    overlapping = Rung(8, (0.0140, 0.0190, 0.0165))
    assert overlapping.mean > black.mean
    assert not separates(overlapping, black)


def test_a_rise_below_the_tables_precision_is_not_printed_as_zero() -> None:
    """A first step reported as 0.00000 cd/m² reads as no step at all."""
    from display_measure.floor import FloorReport, render_report

    axis = AxisFloor(
        "blue",
        Rung(0, (0.005000, 0.005000)),
        (Rung(2, (0.0050004, 0.0050004)),),
        2,
    )
    rendered = render_report(FloorReport((axis,), ambient=0.005))
    assert "first step 0.00000" not in rendered
    assert "e-0" in rendered
