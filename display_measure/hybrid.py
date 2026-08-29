"""The disciplined colorimeter (§spec:sessions).

A spectroradiometer's integration time explodes at low light — the
bench baseline is 18 s per patch, dominated by the dark rungs — while a
colorimeter stays fast there and pays instead in filter mismatch,
worst on the narrow-band primaries an LED wall emits. The four-color
matrix method (Ohno & Hardis 1997; ASTM E1455) takes both wins: derive
a 3x3 from paired readings of one rung per primary, and a
three-primary additive display makes that one matrix valid for every
mixture the wall can emit.

Three properties make the correction trustworthy:

- **Derived in session.** The matrix absorbs the two instruments' spot
  geometry, so it belongs to this mount, not to the instrument pair. A
  re-aim re-derives it, from patches the protocol already drives.
- **Applied in software.** Never through the colorimeter's onboard
  calibration slots: an in-instrument matrix is exactly the
  unauditable correction this system exists to avoid. The artifact
  carries the matrix, the threshold, and the instrument behind every
  row (`display_measure.artifact.InstrumentRouting`).
- **Wrapping, not replacing, the session.** `HybridInstrument`
  satisfies the session's `Instrument` protocol, so drive, settle, and
  read are untouched (§spec:sessions). The session reads it by patch
  name, so the hybrid keeps no copy of the session's iteration.

Routing costs one colorimeter read per patch: the threshold is stated
in measured luminance, and the fast instrument is the one that can
measure it cheaply. Bright patches then pay the spectroradiometer's
exposure on top; dark patches — the expensive ones — never do.

The reference relation is not symmetric in luminance. A
spectroradiometer is the reference for *spectral* accuracy, which is
what filter mismatch spoils, and that is why the correction points this
way. It is not automatically the better instrument at the bottom of the
range: dispersing light across hundreds of bins costs sensitivity a
colorimeter keeps, and the published floors say so — 0.02 cd/m² for the
bench CR-300 against 0.0007 for the CR-100 its colorimeter derives
from. The bench CR-120 is reportedly a sensitivity-tuned CR-100 variant
built for Brompton PureTone work (vendor context, unconfirmed), which
fits what it measures: it refuses above roughly 450 cd/m² where the
CR-100 is specified to 5140, and gain bought at the bottom is paid for
in headroom. Do not answer that refusal by fitting one of its ND
filters — attenuation raises the floor by the same factor it lowers the
ceiling, discarding the property the instrument exists for, and the
patches it refuses are ones the spectroradiometer takes anyway. Below
roughly the spectroradiometer's floor this correction is being
extrapolated far past the luminances it was derived at, and a
four-color matrix corrects a multiplicative error while an additive
offset would survive it. The opening black read stays on the reference
pending instrument-floor measurements (§road:instrument-floors).
"""

import logging
import math
from typing import Protocol, runtime_checkable

import numpy as np
import numpy.typing as npt

from display_measure.artifact import (
    SOURCE_COLORIMETER,
    SOURCE_SPECTRORADIOMETER,
    InstrumentRouting,
)
from display_measure.instrument import (
    Instrument,
    InstrumentReading,
    XYZReading,
    identity,
    luminance,
    triple,
)

__all__ = [
    "DEFAULT_LUMINANCE_THRESHOLD",
    "DERIVATION_PATCHES",
    "DerivationRefused",
    "DisciplinedInstrument",
    "HybridInstrument",
    "audit_derivation",
    "four_color_matrix",
]

log = logging.getLogger("display_measure.session")

METHOD = "four-color-matrix"

# A filter-mismatch correction is a percent-scale error on narrow-band
# LED emitters, so its matrix sits near identity — the wall double models
# it at 0.97-1.06 on the diagonal and ±0.04 off it. These bounds are
# generous against that: they exist to catch a derivation that is not
# filter mismatch at all, not to police a real instrument pair.
#
# The 2026-08-28 bench run derived a matrix with 128 on the leading term
# and condition number 189, applied it three decades below the rungs it
# came from, and inflated the dark end 13-20x. Nothing refused, and the
# session spent twenty minutes measuring through it
# (§road:session-consistency).
MAX_IDENTITY_DEVIATION = 0.5
MAX_CONDITION_NUMBER = 10.0

# How far the two instruments may disagree before correction. Filter
# mismatch is tens of percent; an order of magnitude is an instrument
# fault, a units error, or a patch neither one actually saw, and fitting
# a matrix to it launders the fault into every corrected row.
MAX_RAW_DISAGREEMENT = 4.0

# The anchors the correction is derived from: one patch per primary,
# each a pure sample of that emitter.
#
# Half-drive rungs, not the full-drive anchors. A colorimeter's ceiling
# is far below a show wall's peak — the CR-120 on the bench rig
# saturates around 400-500 cd/m² against a 1900 cd/m² wall, so
# full-drive red and green cannot be read at all, and a derivation that
# cannot be measured is no derivation. An LED primary's spectrum barely
# moves with drive level, which is what filter mismatch responds to, so
# a dimmer rung samples the same emitter and both instruments stay in
# range. Override for a wall whose full drive the colorimeter can take.
DERIVATION_PATCHES = ("red_2048", "green_2048", "blue_2048")

# Patches measuring at or above this luminance (cd/m²) go to the
# spectroradiometer. 1% of a 1000 cd/m² wall's peak — above it the
# spectroradiometer integrates quickly, below it the exposure is what
# makes a session take twenty minutes.
DEFAULT_LUMINANCE_THRESHOLD = 10.0


class DerivationRefused(RuntimeError):
    """The in-session correction is not fit to measure through."""


def audit_derivation(
    matrix: npt.NDArray[np.float64],
    reference: npt.NDArray[np.float64],
    colorimeter: npt.NDArray[np.float64],
) -> None:
    """Refuse a correction that cannot be trusted below its rungs.

    Runs where the matrix is derived — three patches into the protocol —
    so a broken correction costs three reads rather than a full session
    (§road:session-gates). Both arrays hold the paired anchor readings as
    columns.

    The correction is derived at half drive and applied as far down as the
    instrument can read, so the checks are about extrapolation fitness,
    not fit: an exact solve on three rungs always reproduces those three
    rungs, and a residual there would prove nothing.
    """
    problems: list[str] = []

    # Compared as whole-tristimulus magnitudes: a primary's Y alone goes
    # near zero on deep blue, and a ratio against it says more about the
    # luminous efficiency function than about the two instruments.
    ref_scale = np.linalg.norm(reference, axis=0)
    col_scale = np.linalg.norm(colorimeter, axis=0)
    with np.errstate(divide="ignore", invalid="ignore"):
        ratios = ref_scale / col_scale
    worst = float(np.max(np.maximum(ratios, 1.0 / ratios)))
    if not np.isfinite(worst) or worst > MAX_RAW_DISAGREEMENT:
        problems.append(
            f"the instruments disagree by up to {worst:.3g}x on the derivation "
            f"rungs, past the {MAX_RAW_DISAGREEMENT:g}x a filter mismatch "
            "explains — the correction would be fitting an instrument fault"
        )

    deviation = float(np.max(np.abs(matrix - np.eye(3))))
    if deviation > MAX_IDENTITY_DEVIATION:
        problems.append(
            f"the correction is {deviation:.3g} from identity, past "
            f"{MAX_IDENTITY_DEVIATION:g}; a filter-mismatch correction on "
            "narrow-band primaries is a percent-scale adjustment"
        )

    condition = float(np.linalg.cond(matrix))
    if condition > MAX_CONDITION_NUMBER:
        problems.append(
            f"the correction is ill-conditioned ({condition:.4g}, past "
            f"{MAX_CONDITION_NUMBER:g}); it amplifies instrument noise into "
            "every row it corrects"
        )

    if problems:
        rung_luminance = reference[1]
        span = (
            f"{float(np.min(rung_luminance)):.4g}-"
            f"{float(np.max(rung_luminance)):.4g} cd/m²"
        )
        raise DerivationRefused(
            f"the in-session colorimeter correction, derived across {span}, "
            "is not fit to measure through; refusing before the protocol "
            "runs:\n  - " + "\n  - ".join(problems)
        )


@runtime_checkable
class DisciplinedInstrument(Protocol):
    """An `Instrument` that corrects one sensor against another.

    The session recognizes it structurally: it drives the patches the
    derivation needs first, reads by patch name, and records the routing
    reported at handoff. An instrument that does not implement this runs
    as a single instrument, unchanged.
    """

    def derivation_patches(self) -> tuple[str, ...]:
        """Patches to drive ahead of the shuffle, in the order wanted."""
        ...

    def measure_patch(self, patch: str) -> InstrumentReading:
        """Read the named patch, routing or deriving as that patch needs."""
        ...

    def routing(self) -> InstrumentRouting:
        """The derivation and per-row attribution, for the artifact."""
        ...


def four_color_matrix(
    reference: npt.NDArray[np.float64],
    colorimeter: npt.NDArray[np.float64],
) -> npt.NDArray[np.float64]:
    """The 3x3 mapping colorimeter XYZ onto the reference instrument's.

    Both arguments hold the paired anchor readings as columns, in the
    same order. Solving rather than inverting keeps the conditioning
    the anchors deserve.

    Raises ValueError when the anchors do not span three dimensions —
    the correction is only defined on a basis.
    """
    try:
        return np.linalg.solve(colorimeter.T, reference.T).T
    except np.linalg.LinAlgError as e:
        raise ValueError(
            "the derivation anchors are not linearly independent; a "
            "four-color matrix needs three primaries that span the display's "
            "gamut"
        ) from e


class HybridInstrument:
    """Two instruments behind one `Instrument` surface.

    Reads the derivation anchors with both, derives the colorimeter's
    correction from the pairs, then routes every later patch by its
    measured luminance: at or above `luminance_threshold` the
    spectroradiometer's reading, below it the disciplined colorimeter's.
    """

    manufacturer = "display-measure"
    model = "disciplined-hybrid"

    def __init__(
        self,
        spectroradiometer: Instrument,
        colorimeter: Instrument,
        *,
        luminance_threshold: float = DEFAULT_LUMINANCE_THRESHOLD,
        derivation_patches: tuple[str, ...] = DERIVATION_PATCHES,
    ) -> None:
        if not math.isfinite(luminance_threshold) or luminance_threshold < 0:
            raise ValueError(
                f"the routing threshold is a luminance in cd/m²; got "
                f"{luminance_threshold!r}"
            )
        self._spectroradiometer = spectroradiometer
        self._colorimeter = colorimeter
        self._threshold = luminance_threshold
        self._derivation = derivation_patches
        self._pairs: list[tuple[npt.NDArray[np.float64], npt.NDArray[np.float64]]] = []
        self._matrix: npt.NDArray[np.float64] | None = None
        self._sources: list[str] = []

    @property
    def serial_number(self) -> str:
        """Both serials: the correction belongs to the pair, not either."""
        return (
            f"{self._spectroradiometer.serial_number}+{self._colorimeter.serial_number}"
        )

    def derivation_patches(self) -> tuple[str, ...]:
        return self._derivation

    def measure_patch(self, patch: str) -> InstrumentReading:
        if patch in self._derivation:
            return self._pair(patch)
        if self._matrix is None:
            # Nothing can route before the correction exists. Only
            # patches driven ahead of the derivation anchors reach here
            # — the pinned opening black, whose reading the ambient gate
            # consumes and which deserves the reference instrument.
            return self._from_spectroradiometer(patch)
        if self._threshold <= 0.0:
            # Every patch is at or above the threshold, so probing the
            # colorimeter could only confirm what the threshold already
            # decided. Costs one wasted read per patch otherwise.
            return self._from_spectroradiometer(patch)
        probe = self._probe()
        if probe is None:
            return self._from_spectroradiometer(patch, note=" (colorimeter over range)")
        corrected = XYZReading(XYZ=self._matrix @ probe)
        if luminance(corrected) >= self._threshold:
            return self._from_spectroradiometer(patch)
        self._sources.append(SOURCE_COLORIMETER)
        log.info(
            "instrument route: %s -> disciplined colorimeter (%.4f cd/m², "
            "under the %.4f threshold)",
            patch,
            luminance(corrected),
            self._threshold,
        )
        return corrected

    def measure(self) -> InstrumentReading:
        """Satisfy `Instrument` for callers that drive no named patch.

        A session reaches `measure_patch` instead; this keeps the hybrid
        usable as a plain instrument, reading the reference directly
        rather than guessing a routing it has no patch to route.
        """
        return self._spectroradiometer.measure()

    def routing(self) -> InstrumentRouting:
        if self._matrix is None:
            raise RuntimeError(
                "no four-color matrix was derived: the session drove no paired "
                f"readings of {', '.join(self._derivation)}"
            )
        return InstrumentRouting(
            method=METHOD,
            spectroradiometer=identity(self._spectroradiometer),
            colorimeter=identity(self._colorimeter),
            correction_matrix=(
                triple(self._matrix[0]),
                triple(self._matrix[1]),
                triple(self._matrix[2]),
            ),
            luminance_threshold=self._threshold,
            sources=tuple(self._sources),
        )

    def _probe(self) -> npt.NDArray[np.float64] | None:
        """The colorimeter's reading, or None when it cannot take the patch.

        A colorimeter's ceiling sits below a show wall's peak, and it
        refuses an over-range patch rather than returning a saturated
        number — the CR-120 raises on anything past roughly 450 cd/m²
        against a 1900 cd/m² wall. That refusal is the routing signal it
        sounds like: too bright for this instrument, so the
        spectroradiometer takes it, which is where a patch that bright
        was always going. Any other failure routes the same way and says
        so in the log; the artifact records the instrument behind every
        row either way.
        """
        try:
            return np.asarray(self._colorimeter.measure().XYZ, dtype=np.float64)
        except Exception as e:
            log.info("colorimeter declined the patch (%s)", e)
            return None

    def _from_spectroradiometer(self, patch: str, note: str = "") -> InstrumentReading:
        self._sources.append(SOURCE_SPECTRORADIOMETER)
        log.info("instrument route: %s -> spectroradiometer%s", patch, note)
        return self._spectroradiometer.measure()

    def _pair(self, patch: str) -> InstrumentReading:
        """Read the anchor with both instruments; derive once all are in.

        The spectroradiometer's reading is what the artifact carries:
        an anchor is full drive on a primary, the brightest patch of its
        channel and the one the reference instrument reads fastest.
        """
        colorimeter = np.asarray(self._colorimeter.measure().XYZ, dtype=np.float64)
        reading = self._from_spectroradiometer(patch)
        self._pairs.append(
            (np.asarray(reading.XYZ, dtype=np.float64), colorimeter),
        )
        if len(self._pairs) == len(self._derivation):
            self._derive()
        return reading

    def _derive(self) -> None:
        reference = np.column_stack([pair[0] for pair in self._pairs])
        colorimeter = np.column_stack([pair[1] for pair in self._pairs])
        self._matrix = four_color_matrix(reference, colorimeter)
        audit_derivation(self._matrix, reference, colorimeter)
        log.info(
            "disciplined colorimeter: %s derived from %s; %s reads below %.4f cd/m²",
            METHOD,
            ", ".join(self._derivation),
            self._colorimeter.model,
            self._threshold,
        )
