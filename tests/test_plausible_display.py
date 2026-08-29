"""Plausible-display double: synthesized readings for the driven frame.

The model constants come from ocio-display-gen's shipped sample
measurements (a ROE Black Pearl 2-class display), so the assertions below
pin the double to the numbers the downstream strict validator already
accepts.
"""

import numpy as np
import pytest
from bmd_sg.decklink import MockBMDDeckLink, PixelFormatType
from conftest import drive

from display_measure.artifact import DECLARED_CONTRACT
from display_measure.plausible_display import (
    BLACK_LEVEL,
    BLUE_XY,
    DECODE_GAMMA,
    FILTER_MISMATCH,
    GREEN_XY,
    PEAK_LUMINANCE,
    RED_XY,
    WHITE_XY,
    MismatchedColorimeter,
    PlausibleDisplay,
)
from display_measure.session import FULL_DRIVE


def xy_of(display: PlausibleDisplay) -> tuple[float, float]:
    x, y = display.measure().xy
    return (float(x), float(y))


def test_decode_gamma_derives_from_the_declared_contract() -> None:
    """DECODE_GAMMA is the contract's own gamma — derived, not copied."""
    assert DECLARED_CONTRACT.gamma_value == DECODE_GAMMA


def test_ambient_raises_the_black_floor(device: MockBMDDeckLink) -> None:
    """The ambient knob lifts every reading — the hardware-free handle
    for §road:session-gates' over-budget refusal."""
    drive(device, (0, 0, 0))
    dark = PlausibleDisplay(device).measure().XYZ[1]
    lit = PlausibleDisplay(device, ambient=5.0).measure().XYZ[1]
    assert lit == pytest.approx(dark + 5.0)


def test_white_lands_on_the_sample_peak_and_white_point(
    device: MockBMDDeckLink,
) -> None:
    display = PlausibleDisplay(device)
    drive(device, (FULL_DRIVE, FULL_DRIVE, FULL_DRIVE))
    reading = display.measure()
    assert float(reading.XYZ[1]) == pytest.approx(PEAK_LUMINANCE, rel=1e-9)
    assert xy_of(display) == pytest.approx(WHITE_XY, abs=1e-9)


def test_black_is_the_sample_floor_at_the_white_chromaticity(
    device: MockBMDDeckLink,
) -> None:
    display = PlausibleDisplay(device)
    drive(device, (0, 0, 0))
    reading = display.measure()
    assert float(reading.XYZ[1]) == pytest.approx(BLACK_LEVEL, rel=1e-9)
    assert xy_of(display) == pytest.approx(WHITE_XY, abs=1e-9)


def test_full_drive_primaries_measure_the_sample_chromaticities(
    device: MockBMDDeckLink,
) -> None:
    display = PlausibleDisplay(device)
    # Black leakage pulls each primary toward white by parts in 1e5,
    # hence the loose-but-physical tolerance.
    for rgb, expected in (
        ((FULL_DRIVE, 0, 0), RED_XY),
        ((0, FULL_DRIVE, 0), GREEN_XY),
        ((0, 0, FULL_DRIVE), BLUE_XY),
    ):
        drive(device, rgb)
        assert xy_of(display) == pytest.approx(expected, abs=1e-4)


def test_gray_follows_the_contract_gamma(device: MockBMDDeckLink) -> None:
    display = PlausibleDisplay(device)
    code = FULL_DRIVE // 2
    drive(device, (code, code, code))
    expected = BLACK_LEVEL + (code / FULL_DRIVE) ** DECODE_GAMMA * (
        PEAK_LUMINANCE - BLACK_LEVEL
    )
    assert float(display.measure().XYZ[1]) == pytest.approx(expected, rel=1e-9)


def test_full_drive_scales_with_the_device_bit_depth(
    device: MockBMDDeckLink,
) -> None:
    display = PlausibleDisplay(device)
    device.pixel_format = PixelFormatType.FORMAT_10BIT_RGB
    ten_bit_full = 2**10 - 1
    drive(device, (ten_bit_full, ten_bit_full, ten_bit_full))
    assert float(display.measure().XYZ[1]) == pytest.approx(PEAK_LUMINANCE, rel=1e-9)


def test_measure_refuses_before_any_frame_is_driven(
    device: MockBMDDeckLink,
) -> None:
    display = PlausibleDisplay(device)
    with pytest.raises(RuntimeError, match="frame"):
        display.measure()


def test_filter_mismatch_is_invertible_and_not_the_identity() -> None:
    """The mismatch is the error the session must recover, so it has to
    be a real rotation of the display's XYZ and a solvable one."""
    assert not np.allclose(FILTER_MISMATCH, np.eye(3))
    assert abs(float(np.linalg.det(FILTER_MISMATCH))) > 0.1


def test_colorimeter_reads_the_display_through_the_mismatch(
    device: MockBMDDeckLink,
) -> None:
    display = PlausibleDisplay(device)
    colorimeter = MismatchedColorimeter(display)
    drive(device, (FULL_DRIVE, FULL_DRIVE, FULL_DRIVE))
    reading = colorimeter.measure().XYZ
    assert reading == pytest.approx(FILTER_MISMATCH @ display.measure().XYZ)


def test_colorimeter_misreads_a_primary_chromaticity(
    device: MockBMDDeckLink,
) -> None:
    """Filter mismatch bites hardest on narrow-band primaries — the
    error a four-color matrix exists to correct."""
    display = PlausibleDisplay(device)
    colorimeter = MismatchedColorimeter(display)
    drive(device, (FULL_DRIVE, 0, 0))
    assert colorimeter.measure().xy != pytest.approx(display.measure().xy, abs=1e-3)
