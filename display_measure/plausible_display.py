"""A deterministic, physically plausible display double (§spec:sessions).

Couples the two doubles seams: reads the frame most recently driven to
the mock DeckLink and synthesizes the reading a real display would produce
for that drive. The model is a simple additive display:

    XYZ = black_XYZ + sum(channel_drive_linearized * primary_XYZ)

with a pure gamma linearization matching the declared SDR contract.
Deterministic by construction — no RNG anywhere — so byte-identical
artifacts need no seed, and the synthesized measurements pass strict
downstream validation. The double is deliberately model-shaped: it
embodies exactly the additivity + pure-power-law model the
characterize protocol exists to falsify on real displays, so it can never
grade that protocol — a double that disagrees with its device is worse
than none, and grading it is the bench rig's job (§spec:sessions).

`MismatchedColorimeter` reads the same display through a fixed filter
mismatch, giving the disciplined-colorimeter session two disagreeing
instruments to reconcile without hardware.
"""

import hashlib
from typing import Protocol

import numpy as np
import numpy.typing as npt
from bmd_sg.decklink import PixelFormatType

from display_measure.artifact import (
    DECLARED_CONTRACT,
    SPECTRUM_MEASURED,
    Spectrum,
    WireEncoding,
)
from display_measure.instrument import XYZReading
from display_measure.wire import RGB12, decode_pixel

# Display model constants copied from ocio-display-gen's shipped sample
# measurements (measurements/ftg_stage1_20240115.yaml — a ROE Black
# Pearl 2 (NS) class display behind a Brompton S8): native primaries and
# white point (CIE xy), black level and peak luminance (cd/m²).
RED_XY = (0.680, 0.320)
GREEN_XY = (0.265, 0.690)
BLUE_XY = (0.150, 0.060)
WHITE_XY = (0.3127, 0.3290)
PEAK_LUMINANCE = 1000.0
BLACK_LEVEL = 0.005

# Per-drive variation, cd/m², one standard deviation. Drive noise and
# instrument repeatability together, because the quantity the report's
# filter measures is their sum and no analysis can separate them. Its purpose is that the
# double produce a *measurable* noise floor: the fidelity report keeps a
# patch when its luminance above black clears the spread of the repeated
# black readings, so an exact double has no spread, the filter cannot be
# computed from its artifacts at all, and nothing without hardware can
# check that a report is built from the patches it should be.
#
# Sized against this model's own ramps rather than copied from the
# bench. The tightest adjacent rise the protocol asks of it is blue,
# codes 16 to 24, at 2.2e-4 cd/m²; noise that approaches it inverts the
# ramp and the session's own self-consistency gate refuses the artifact,
# correctly. An order of magnitude below that leaves the floor
# measurable and the ramps monotone. The bench CR-300 repeats a
# blocked-aperture reading to sd 6e-4 — far coarser, because its floor
# sits well above this display's black, which is the gap the
# disciplined-colorimeter path exists to close.
#
# Deterministic all the same: the perturbation is derived from the read
# index, not an RNG, so one run of the doubles is byte-identical to the
# next (§spec:artifact-chain).
READ_NOISE = 1e-5


def _contract_gamma() -> float:
    """The declared contract's decode gamma, narrowed from its
    Optional field — derived, so display and contract cannot drift
    (§spec:signal-contract)."""
    gamma = DECLARED_CONTRACT.gamma_value
    if gamma is None:
        raise RuntimeError("the declared contract names no gamma decode")
    return gamma


DECODE_GAMMA = _contract_gamma()


def _unit_xyz(xy: tuple[float, float]) -> npt.NDArray[np.float64]:
    """XYZ at unit luminance (Y = 1) for a chromaticity.

    Equivalent to ``colour.models.xy_to_XYZ``; hand-rolled so the
    default session path keeps colour-science (and its ~0.8 s import)
    off the wiring entirely."""
    x, y = xy
    return np.array([x / y, 1.0, (1.0 - x - y) / y])


# The black floor carries the white chromaticity so the black reading
# stays plausibly neutral.
_BLACK_XYZ = _unit_xyz(WHITE_XY) * BLACK_LEVEL


def _primary_columns() -> npt.NDArray[np.float64]:
    """Per-channel primary XYZ columns, scaled so full-drive white
    lands exactly on the sample's peak luminance at the sample's white
    chromaticity."""
    primaries_unit = np.column_stack(
        [_unit_xyz(RED_XY), _unit_xyz(GREEN_XY), _unit_xyz(BLUE_XY)]
    )
    white_xyz = _unit_xyz(WHITE_XY) * (PEAK_LUMINANCE - BLACK_LEVEL)
    scales = np.linalg.solve(primaries_unit, white_xyz)
    return primaries_unit * scales


_PRIMARY_XYZ = _primary_columns()


# The double's spectrum: three Gaussian emitters at LED peak wavelengths
# and widths, on the 5 nm grid a spectroradiometer reports
# (§spec:spectral-retention). Narrow-band by construction, because that
# is the shape whose filter mismatch the disciplined colorimeter exists
# to correct and whose drive-invariance a reconstruction rests on.
SPECTRAL_START = 380.0
SPECTRAL_END = 780.0
SPECTRAL_STEP = 5.0
EMITTER_PEAKS = ((625.0, 12.0), (535.0, 18.0), (462.0, 12.0))

WAVELENGTHS = np.arange(SPECTRAL_START, SPECTRAL_END + SPECTRAL_STEP / 2, SPECTRAL_STEP)

# Each basis emitter's absolute XYZ, columns in EMITTER_PEAKS order,
# under CIE 1931 with k=683 (ASTM E308). Stated rather than computed so
# the default session path keeps colour-science off the wiring, as
# `_unit_xyz` does; `test_plausible_display` recomputes it against
# colour and fails if the two drift.
BASIS_XYZ = np.array(
    [
        (15082.476283165426, 8125.4529167550845, 5156.789441538233),
        (6826.31308060386, 25762.78106545038, 1507.5629252862532),
        (3.3668960564538315, 1743.367517703671, 30205.470483608013),
    ]
)


def _spd_from_xyz() -> npt.NDArray[np.float64]:
    """The map from a reading's XYZ to the emitter spectrum behind it.

    A metamer, not a datasheet: the double models a display's
    tristimulus, and this reconstructs the three-emitter spectrum that
    integrates back to it exactly, so the spectra the double writes
    never contradict the readings beside them. Its tails dip a few parts
    in ten thousand of peak below zero where the sample's primaries are
    not exactly reachable from three Gaussians — kept rather than
    clipped, because clipping would break that identity and a real
    instrument's out-of-band bins go slightly negative too.
    """
    basis = np.column_stack(
        [np.exp(-0.5 * ((WAVELENGTHS - c) / s) ** 2) for c, s in EMITTER_PEAKS]
    )
    return np.asarray(basis @ np.linalg.inv(BASIS_XYZ), dtype=np.float64)


_SPD_FROM_XYZ = _spd_from_xyz()


def emitter_spectrum(xyz: npt.NDArray[np.float64]) -> Spectrum:
    """The measured spectrum the double reports for a reading."""
    return Spectrum(
        wavelengths=tuple(float(w) for w in WAVELENGTHS),
        values=tuple(float(v) for v in _SPD_FROM_XYZ @ xyz),
        provenance=SPECTRUM_MEASURED,
    )


class FrameSource(Protocol):
    """The slice of the mock DeckLink's public surface the display reads."""

    @property
    def pixel_format(self) -> PixelFormatType: ...

    def get_last_frame(self) -> npt.NDArray[np.uint16] | None: ...


class PlausibleDisplay:
    """Synthesizes the instrument reading for the frame being driven.

    Satisfies the session's ``Instrument`` protocol; identity strings
    name the sample the model is seeded from.
    """

    manufacturer = "display-measure"
    model = "PlausibleDisplay"
    serial_number = "ftg_stage1_20240115"

    def __init__(
        self,
        device: FrameSource,
        *,
        encoding: WireEncoding = RGB12,
        ambient: float = 0.0,
        read_noise: float = READ_NOISE,
    ) -> None:
        """`encoding` is the link the display decodes — the one the
        session declares, since the wire-format gate holds the processor
        to it. `ambient` is reflected-light luminance (cd/m²) added to
        every reading — the knob §road:session-gates' ambient budget
        refusal exercises without hardware. Default 0: a dark room."""
        self._device = device
        self._encoding = encoding
        self._ambient_xyz = _unit_xyz(WHITE_XY) * ambient
        self._read_noise = read_noise
        self._last_spot: bytes | None = None
        self._drives: dict[bytes, int] = {}
        self._offset = np.zeros(3)

    def _drive_offset(
        self, decoded: npt.NDArray[np.float64]
    ) -> npt.NDArray[np.float64]:
        """This drive event's variation, from the frame rather than an RNG.

        Keyed to *what was driven and how many times it has been driven*,
        not to a read counter. Two consequences, both load-bearing:

        A drive event has one emission. Both instruments of a hybrid
        session read the same frame at the same moment, so they see the
        same offset — the colorimeter's disagreement with the
        spectroradiometer stays the filter mismatch the session exists
        to correct, rather than the mismatch plus two independent draws.

        And a session's readings do not depend on how many instruments
        took them. A read counter made a hybrid session's noise diverge
        from a spectroradiometer-only one measuring the same patches,
        which broke the guarantee that disciplining the colorimeter
        reproduces the session without it.

        Keyed on what the display *decoded*, not on the bytes the link
        carried: the panel emits by drive level, so one patch over two
        wire encodings is one emission wherever the link can represent
        it. Keying the raw frame instead made full-drive white differ
        between an rgb12 and a v210 session, which the artifacts then
        reported as two different peak luminances.

        Neutral in chromaticity: at these levels the variation is
        dominated by drive and integration, which move all three
        channels together, and a double that walked the chromaticity of
        black would be modelling something it has no basis for.
        """
        if self._read_noise <= 0:
            return np.zeros(3)
        # Rounded, so float representation cannot split one drive level
        # into two; nine places is far below any drive the model resolves.
        key = repr(tuple(round(float(v), 9) for v in decoded)).encode()
        if key != self._last_spot:
            # A new drive event of this frame content, so a new emission.
            # Two identical patches driven back to back would share one,
            # which is what physically happens: nothing changed.
            self._last_spot = key
            self._drives[key] = self._drives.get(key, 0) + 1
            digest = hashlib.sha256(key + f":{self._drives[key]}".encode()).digest()
            # Uniform on [-1, 1), scaled so the population sd is the noise.
            unit = int.from_bytes(digest[:8], "big") / 2**63 - 1.0
            self._offset = _unit_xyz(WHITE_XY) * (unit * self._read_noise * 3**0.5)
        return self._offset

    def measure(self) -> XYZReading:
        frame = self._device.get_last_frame()
        if frame is None:
            raise RuntimeError(
                "PlausibleDisplay cannot measure: no frame has been driven"
            )
        # A spot instrument aimed at the display center.
        spot = frame[frame.shape[0] // 2, frame.shape[1] // 2]
        decoded = decode_pixel(self._encoding, spot)
        linear = decoded**DECODE_GAMMA
        xyz = self._ambient_xyz + _BLACK_XYZ + _PRIMARY_XYZ @ linear
        xyz = xyz + self._drive_offset(decoded)
        # A spectroradiometer's reading: the spectrum is measured, and
        # it integrates back to the XYZ beside it.
        return XYZReading(XYZ=xyz, spectrum=emitter_spectrum(xyz))


# The colorimeter's filter/observer mismatch, as the fixed 3x3 relating
# its XYZ to a spectroradiometer's on this display's primaries. Percent-
# scale off-diagonal terms stand in for the error a tristimulus
# colorimeter makes on narrow-band LED emitters (§spec:sessions). A
# single matrix is exactly the error the four-color method corrects, so
# this double grades the derivation's arithmetic and its plumbing, not
# the method's fitness on real filters — that needs the bench rig.
FILTER_MISMATCH = np.array(
    [
        [1.04, 0.03, -0.02],
        [0.02, 0.97, 0.03],
        [-0.03, 0.04, 1.06],
    ]
)


class MismatchedColorimeter:
    """A colorimeter double reading `PlausibleDisplay` through the mismatch.

    Satisfies the session's ``Instrument`` protocol. Deterministic, and
    it shares the display, so a hybrid session's two instruments always see
    the same driven frame.

    Its readings carry no spectrum, because a colorimeter has none: it
    is three filtered photodiodes. That absence is what a reconstruction
    fills (:mod:`display_measure.hybrid`).
    """

    manufacturer = "display-measure"
    model = "MismatchedColorimeter"
    serial_number = "filter-mismatch-1"

    def __init__(self, display: PlausibleDisplay) -> None:
        self._display = display

    def measure(self) -> XYZReading:
        return XYZReading(XYZ=FILTER_MISMATCH @ self._display.measure().XYZ)
