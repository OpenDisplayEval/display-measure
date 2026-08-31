"""Disciplined-colorimeter tests (§spec:sessions).

The display double and the mismatched colorimeter differ by exactly one
3x3, so a correct derivation recovers the spectroradiometer's readings
to float precision. That makes the arithmetic checkable without
hardware; whether the method holds on real filters is the bench rig's
question.
"""

import numpy as np
import numpy.typing as npt
import pytest
from bmd_sg.decklink import MockBMDDeckLink
from conftest import drive

from display_measure.artifact import (
    SOURCE_COLORIMETER,
    SOURCE_SPECTRORADIOMETER,
    SPECTRUM_ABSENT,
    SPECTRUM_MEASURED,
    SPECTRUM_RECONSTRUCTED,
)
from display_measure.hybrid import (
    DEFAULT_LUMINANCE_THRESHOLD,
    DERIVATION_PATCHES,
    DerivationRefused,
    HybridInstrument,
    audit_derivation,
    four_color_matrix,
)
from display_measure.instrument import InstrumentReading, XYZReading, spectrum
from display_measure.plausible_display import (
    FILTER_MISMATCH,
    PEAK_LUMINANCE,
    MismatchedColorimeter,
    PlausibleDisplay,
)
from display_measure.protocol import protocol_patches

# Above every patch the display can emit: forces the colorimeter branch.
ALL_COLORIMETER = 10 * PEAK_LUMINANCE
# Below the black floor: forces the spectroradiometer branch.
ALL_SPECTRO = 0.0

# The protocol owns the code values; a local copy would drift from the
# ladder silently.
PATCH_RGB = {patch.name: patch.rgb for patch in protocol_patches()}


def rig(
    device: MockBMDDeckLink, threshold: float
) -> tuple[HybridInstrument, PlausibleDisplay]:
    display = PlausibleDisplay(device)
    hybrid = HybridInstrument(
        display, MismatchedColorimeter(display), luminance_threshold=threshold
    )
    return hybrid, display


def run(
    device: MockBMDDeckLink, hybrid: HybridInstrument, order: tuple[str, ...]
) -> dict[str, npt.NDArray[np.float64]]:
    """Drive each named patch and collect the reading the hybrid returns."""
    readings = {}
    for name in order:
        drive(device, PATCH_RGB[name])
        readings[name] = hybrid.measure_patch(name).XYZ
    return readings


def test_four_color_matrix_maps_the_colorimeter_onto_the_reference() -> None:
    colorimeter = np.array([[100.0, 20.0, 15.0], [50.0, 180.0, 8.0], [3.0, 25.0, 90.0]])
    reference = FILTER_MISMATCH @ colorimeter
    derived = four_color_matrix(reference, colorimeter)
    assert derived == pytest.approx(FILTER_MISMATCH)


def test_four_color_matrix_refuses_a_degenerate_derivation_set() -> None:
    """Three anchors that do not span the display's gamut cannot key a
    correction — a colorimeter reading the same color three times."""
    same = np.column_stack([np.array([1.0, 2.0, 3.0])] * 3)
    with pytest.raises(ValueError, match="independent"):
        four_color_matrix(same, same)


def test_correction_recovers_the_spectroradiometer_on_held_out_patches(
    device: MockBMDDeckLink,
) -> None:
    """The Verify criterion, hardware-free: patches outside the
    derivation set, routed to the colorimeter, land on the
    spectroradiometer's values."""
    order = (*DERIVATION_PATCHES, "white", "gray_1024", "gray_0016")
    hybrid, display = rig(device, ALL_COLORIMETER)
    readings = run(device, hybrid, order)
    for name in ("white", "gray_1024", "gray_0016"):
        drive(device, PATCH_RGB[name])
        assert readings[name] == pytest.approx(display.measure().XYZ, rel=1e-9), name


def test_a_low_threshold_routes_every_patch_to_the_spectroradiometer(
    device: MockBMDDeckLink,
) -> None:
    order = (*DERIVATION_PATCHES, "white", "gray_0016")
    hybrid, _ = rig(device, ALL_SPECTRO)
    run(device, hybrid, order)
    assert hybrid.routing().sources == (SOURCE_SPECTRORADIOMETER,) * len(order)


def test_routing_attributes_every_read_and_names_both_instruments(
    device: MockBMDDeckLink,
) -> None:
    order = (*DERIVATION_PATCHES, "white", "gray_0016")
    hybrid, _ = rig(device, ALL_COLORIMETER)
    run(device, hybrid, order)
    routing = hybrid.routing()
    assert routing.sources == (
        SOURCE_SPECTRORADIOMETER,
        SOURCE_SPECTRORADIOMETER,
        SOURCE_SPECTRORADIOMETER,
        SOURCE_COLORIMETER,
        SOURCE_COLORIMETER,
    ), "the derivation anchors are read by both; the rest by the colorimeter"
    assert routing.method == "four-color-matrix"
    assert routing.spectroradiometer.model == "PlausibleDisplay"
    assert routing.colorimeter.model == "MismatchedColorimeter"
    assert routing.luminance_threshold == ALL_COLORIMETER
    assert np.array(routing.correction_matrix) == pytest.approx(
        np.linalg.inv(FILTER_MISMATCH)
    )


def test_the_threshold_splits_the_ramp_by_measured_luminance(
    device: MockBMDDeckLink,
) -> None:
    """Bright patches spend the spectroradiometer's exposure; dark ones
    — the expensive ones — do not."""
    order = (*DERIVATION_PATCHES, "white", "gray_0016")
    hybrid, _ = rig(device, 1.0)
    run(device, hybrid, order)
    assert hybrid.routing().sources[-2:] == (
        SOURCE_SPECTRORADIOMETER,
        SOURCE_COLORIMETER,
    )


def test_serial_number_names_both_instruments(device: MockBMDDeckLink) -> None:
    hybrid, _ = rig(device, ALL_COLORIMETER)
    assert "filter-mismatch-1" in hybrid.serial_number
    assert "ftg_stage1_20240115" in hybrid.serial_number


def test_routing_refuses_before_the_correction_is_derived(
    device: MockBMDDeckLink,
) -> None:
    order = ("red", "green")
    hybrid, _ = rig(device, ALL_COLORIMETER)
    run(device, hybrid, order)
    with pytest.raises(RuntimeError, match="derived"):
        hybrid.routing()


def test_the_threshold_must_be_a_finite_luminance(device: MockBMDDeckLink) -> None:
    display = PlausibleDisplay(device)
    with pytest.raises(ValueError, match="threshold"):
        HybridInstrument(
            display, MismatchedColorimeter(display), luminance_threshold=float("inf")
        )


class OverRangeColorimeter:
    """A colorimeter that refuses patches past a ceiling, as a real one does.

    Colorimetry Research instruments raise rather than returning a
    saturated reading, so an over-range patch is an exception at the
    seam, not a large number.
    """

    manufacturer = "test"
    model = "over-range"
    serial_number = "0000"

    def __init__(self, display: PlausibleDisplay, ceiling: float) -> None:
        self._display = display
        self._ceiling = ceiling

    def measure(self) -> XYZReading:
        reading = self._display.measure()
        if float(reading.XYZ[1]) > self._ceiling:
            raise RuntimeError("Light intensity too high for range")
        return XYZReading(XYZ=FILTER_MISMATCH @ reading.XYZ)


def test_an_over_range_colorimeter_routes_to_the_spectroradiometer(
    device: MockBMDDeckLink,
) -> None:
    """The bench case: the display outruns the colorimeter's ceiling on the
    bright patches, and the session completes on the reference
    instrument rather than dying at the first one."""
    display = PlausibleDisplay(device)
    # Below full-drive white, above the derivation rungs — exactly the
    # bind a 1900 cd/m² display puts a CR-120 in.
    hybrid = HybridInstrument(
        display,
        OverRangeColorimeter(display, ceiling=PEAK_LUMINANCE / 2),
        luminance_threshold=ALL_COLORIMETER,
    )
    order = (*DERIVATION_PATCHES, "white", "gray_0016")
    readings = run(device, hybrid, order)
    sources = hybrid.routing().sources
    assert sources[-2] == SOURCE_SPECTRORADIOMETER, "white is past the ceiling"
    assert sources[-1] == SOURCE_COLORIMETER, "the dark rung is not"
    drive(device, PATCH_RGB["white"])
    assert readings["white"] == pytest.approx(display.measure().XYZ, rel=1e-9)


# --- derivation gate: bail at patch 3, not patch 72 -----------------------


class TestDerivationAudit:
    """The correction is checked where it is derived (§road:session-gates).

    Protocol 2 leads with the three derivation rungs, so a broken
    correction is knowable after three patches. The 2026-08-28 run
    discovered it after seventy-two, in the artifact, by hand.
    """

    # Half-drive rungs with the shape real LED primaries have: every
    # column carries all three tristimulus values, and blue's Y is small
    # without being zero.
    RUNGS = np.column_stack(
        [
            [264.8, 118.0, 0.27],
            [58.3, 242.7, 26.7],
            [64.6, 25.9, 389.1],
        ]
    )

    def identity_pairs(
        self,
    ) -> tuple[npt.NDArray[np.float64], npt.NDArray[np.float64]]:
        return self.RUNGS, FILTER_MISMATCH @ self.RUNGS

    def test_a_plausible_filter_mismatch_passes(self) -> None:
        reference, colorimeter = self.identity_pairs()
        audit_derivation(
            four_color_matrix(reference, colorimeter), reference, colorimeter
        )

    def test_the_2026_08_28_matrix_is_refused(self) -> None:
        """Regression fixture: the matrix the lost run actually derived.

        Its condition number is 189 and its leading term 128 where a
        filter-mismatch correction wants ~1. Applied three decades below
        the rungs, it inflated the dark end 13-20x and put a 12.8x
        discontinuity across the routing boundary.
        """
        matrix = np.array(
            [
                [128.127954648, -37.380017758, -18.923313178],
                [56.585734573, -15.61232616, -8.437245895],
                [0.260479151, -0.080811909, 1.066095],
            ]
        )
        reference = self.RUNGS
        colorimeter = np.linalg.solve(matrix, reference)
        with pytest.raises(DerivationRefused) as e:
            audit_derivation(matrix, reference, colorimeter)
        assert "identity" in str(e.value).lower() or "conditioned" in str(e.value)

    def test_instruments_disagreeing_by_orders_of_magnitude_are_refused(self) -> None:
        """Filter mismatch is a percent-scale error on narrow-band LEDs.

        A colorimeter reading 100x off the reference is not a filter
        talking; the correction would be fitting an instrument fault.
        """
        reference = self.RUNGS
        colorimeter = reference / 100.0
        with pytest.raises(DerivationRefused) as e:
            audit_derivation(
                four_color_matrix(reference, colorimeter), reference, colorimeter
            )
        assert "disagree" in str(e.value).lower()

    def test_the_refusal_names_the_span_the_correction_was_derived_across(
        self,
    ) -> None:
        """§road:instrument-floors asks the artifact to state the span; a
        refusal is the moment it matters most."""
        reference = self.RUNGS
        colorimeter = reference / 100.0
        with pytest.raises(DerivationRefused) as e:
            audit_derivation(
                four_color_matrix(reference, colorimeter), reference, colorimeter
            )
        assert "cd/m²" in str(e.value)


class TestReconstruction:
    """Colorimeter-routed rows get a spectrum, and it is named as one.

    The scaling is legitimate to the degree an emitter's spectral shape
    is drive-invariant (§spec:spectral-retention), so what the row has to
    carry is the span it was scaled across.
    """

    ORDER = (*DERIVATION_PATCHES, "white", "gray_0016", "black")

    def readings(
        self, device: MockBMDDeckLink
    ) -> tuple[HybridInstrument, dict[str, InstrumentReading]]:
        """A session at the shipped threshold: white lands on the
        spectroradiometer, the dark rungs on the colorimeter — which is
        the split a reconstruction exists to bridge."""
        display = PlausibleDisplay(device)
        hybrid = HybridInstrument(
            display,
            MismatchedColorimeter(display),
            luminance_threshold=DEFAULT_LUMINANCE_THRESHOLD,
        )
        readings = {}
        for name in self.ORDER:
            drive(device, PATCH_RGB[name])
            readings[name] = hybrid.measure_patch(name)
        return hybrid, readings

    def test_a_spectroradiometer_row_keeps_its_measured_spectrum(
        self, device: MockBMDDeckLink
    ) -> None:
        _, readings = self.readings(device)
        assert spectrum(readings["red_2048"]).provenance == SPECTRUM_MEASURED

    def test_a_colorimeter_row_is_reconstructed_from_the_bright_reading(
        self, device: MockBMDDeckLink
    ) -> None:
        _, readings = self.readings(device)
        reconstructed = spectrum(readings["gray_0016"])
        assert reconstructed.provenance == SPECTRUM_RECONSTRUCTED
        # White is the bright reading of the same stimulus, so the shape
        # is white's and the scale is the dark row's own luminance.
        anchor = spectrum(readings["white"])
        scale = readings["gray_0016"].XYZ[1] / readings["white"].XYZ[1]
        assert reconstructed.values == pytest.approx(
            [value * scale for value in anchor.values], rel=1e-9
        )

    def test_the_reconstruction_names_the_span_it_was_scaled_across(
        self, device: MockBMDDeckLink
    ) -> None:
        _, readings = self.readings(device)
        span = spectrum(readings["gray_0016"]).derived_across
        assert span is not None
        low, high = span
        assert low == pytest.approx(float(readings["gray_0016"].XYZ[1]))
        assert high == pytest.approx(float(readings["white"].XYZ[1]))
        assert low < high, "the span reads low to high, so extrapolation is visible"

    def test_a_row_with_no_bright_reading_of_its_stimulus_stays_absent(
        self, device: MockBMDDeckLink
    ) -> None:
        """Black leaks; it is not a dimmer white, so nothing measured
        stands in for it. Absent is the honest record."""
        _, readings = self.readings(device)
        assert spectrum(readings["black"]).provenance == SPECTRUM_ABSENT

    def test_reconstruction_preserves_the_measured_tristimulus(
        self, device: MockBMDDeckLink
    ) -> None:
        """The row's XYZ is what the disciplined colorimeter measured;
        the reconstruction is a spectrum beside it, not a replacement."""
        display = PlausibleDisplay(device)
        _, readings = self.readings(device)
        drive(device, PATCH_RGB["gray_0016"])
        routed = readings["gray_0016"].XYZ
        assert routed == pytest.approx(display.measure().XYZ, rel=1e-9)
