"""The lowest value a display and an instrument can reproducibly separate
from black, and the shape of the climb out of it (§spec:measure-sessions).

A characterization is only as good as its darkest rung. The patch
protocol's floor is a fixed code, chosen once; whether that code is
*measurable* depends on the display's shadow response, the instrument's
repeatability at a hundredth of a nit, and the ambient the room is
holding. Those are properties of a pairing on a night, not of the
protocol, so they are measured rather than assumed.

The walk is per axis because the answer differs per axis. A saturated
primary emits a fraction of the light a neutral does at the same code —
blue around a twelfth — so the first code that clears black on the grey
axis is well below the first that clears it on blue. Reporting one
number for the display would describe none of its channels.

Two outcomes are distinguished, because they call for different actions.
A rung the instrument *refused* — the CR-300 answers "light intensity
too low or unmeasurable" — says the pairing cannot see that stimulus at
all. A rung it measured but that does not separate from black says the
reading exists and carries no information. The first wants a more
sensitive instrument; the second wants more repeats, or a higher floor.
"""

from __future__ import annotations

import math
import statistics
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

    from display_measure.artifact import WireEncoding
    from display_measure.session import Instrument, PatchDrive

__all__ = [
    "WALK_CODES",
    "AxisFloor",
    "FloorReport",
    "Rung",
    "measure_floor",
    "render_report",
]

# Codes walked upward from black. Dense where the answer lives — the
# bench display's first measurable rung sat between 16 and 32 — and
# thinning out above it, since a walk that has already separated is only
# confirming itself.
WALK_CODES: tuple[int, ...] = (0, 2, 4, 6, 8, 12, 16, 20, 24, 32, 40, 48, 64, 96, 128)

# Separation in combined standard deviations. Three keeps a rung whose
# mean sits inside the noise of black from being called visible; it is
# the same bar the eye of a reviewer would apply to the table.
SEPARATION_SIGMA = 3.0

# Consecutive separable rungs that end the walk. One can be luck at three
# sigma; two in a row that are both above it is the climb, not the noise.
CONFIRMING_RUNGS = 2

# The axes walked, as the multiplier each applies to the walked code.
AXES: dict[str, tuple[int, int, int]] = {
    "neutral": (1, 1, 1),
    "red": (1, 0, 0),
    "green": (0, 1, 0),
    "blue": (0, 0, 1),
}


@dataclass(frozen=True)
class Rung:
    """One code, measured `repeats` times.

    `refused` carries the instrument's own words when it declined the
    stimulus. `readings` is then empty: there is no number to average,
    and recording one would invent a measurement.
    """

    code: int
    readings: tuple[float, ...]
    refused: str = ""

    @property
    def mean(self) -> float:
        return statistics.fmean(self.readings) if self.readings else math.nan

    @property
    def sigma(self) -> float:
        """Zero for a single reading: one measurement has no spread, which
        is a fact about the sample rather than about the instrument."""
        return statistics.stdev(self.readings) if len(self.readings) > 1 else 0.0


@dataclass(frozen=True)
class AxisFloor:
    """The climb out of black along one axis."""

    axis: str
    black: Rung
    rungs: tuple[Rung, ...]
    lowest_separable: int | None

    @property
    def first_step(self) -> float:
        """Light the lowest separable rung adds over black, in cd/m².

        The shape's first number: how big a jump the display makes on
        its way out of black, which is what says whether the protocol's
        floor lands on a step or inside one.
        """
        if self.lowest_separable is None:
            return math.nan
        rung = next(r for r in self.rungs if r.code == self.lowest_separable)
        return rung.mean - self.black.mean


@dataclass(frozen=True)
class FloorReport:
    """Every axis walked, and what the room was holding while they were."""

    axes: tuple[AxisFloor, ...]
    ambient: float
    separation_sigma: float = SEPARATION_SIGMA


def separates(rung: Rung, black: Rung, *, sigma: float = SEPARATION_SIGMA) -> bool:
    """Whether `rung` reads reproducibly brighter than `black`.

    Two conditions, and both are the phrase taken literally.

    *Reproducibly*: every reading of the rung came in above every reading
    of black. This is the rank test, and it is what a reviewer means by
    reproducible — no averaging can rescue a rung whose readings
    interleave with black's.

    *Brighter*: the rise clears the combined spread of both readings,
    which add in quadrature. The mean alone is not enough when the two
    spreads differ.

    A refused rung never separates: no reading, no claim. A single repeat
    reports no spread, so the rank test carries the verdict alone — which
    is why `--repeats` has a floor of two.
    """
    if rung.refused or not rung.readings or not black.readings:
        return False
    if min(rung.readings) <= max(black.readings):
        return False
    combined = math.hypot(rung.sigma, black.sigma)
    return (rung.mean - black.mean) > sigma * combined


def _read_rung(
    drive: PatchDrive,
    instrument: Instrument,
    encoding: WireEncoding,
    code: int,
    axis: tuple[int, int, int],
    *,
    repeats: int,
    settle_seconds: float,
) -> Rung:
    from display_measure.instrument import luminance
    from display_measure.protocol import Patch
    from display_measure.session import _frame

    rgb = (code * axis[0], code * axis[1], code * axis[2])
    drive.display_frame(_frame(encoding, Patch(f"floor_{code}", rgb, role="floor")))
    time.sleep(settle_seconds)
    readings: list[float] = []
    for _ in range(repeats):
        try:
            readings.append(luminance(instrument.measure()))
        except Exception as refusal:
            # Only `measure()` is inside the guard, so this is the
            # instrument declining rather than a fault in the walk. The
            # refusal is the measurement here: it says the pairing cannot
            # see this stimulus.
            return Rung(code, (), f"{type(refusal).__name__}: {refusal}")
    return Rung(code, tuple(readings))


def measure_axis(
    drive: PatchDrive,
    instrument: Instrument,
    encoding: WireEncoding,
    axis: str,
    *,
    codes: Sequence[int] = WALK_CODES,
    repeats: int = 3,
    settle_seconds: float = 1.0,
    sigma: float = SEPARATION_SIGMA,
) -> AxisFloor:
    """Walk one axis upward from black until the climb is confirmed."""
    multiplier = AXES[axis]
    walked = [c for c in codes if c > 0]
    black = _read_rung(
        drive,
        instrument,
        encoding,
        0,
        multiplier,
        repeats=repeats,
        settle_seconds=settle_seconds,
    )
    rungs: list[Rung] = []
    lowest: int | None = None
    confirming = 0
    for code in walked:
        rung = _read_rung(
            drive,
            instrument,
            encoding,
            code,
            multiplier,
            repeats=repeats,
            settle_seconds=settle_seconds,
        )
        rungs.append(rung)
        if separates(rung, black, sigma=sigma):
            if lowest is None:
                lowest = code
            confirming += 1
            if confirming >= CONFIRMING_RUNGS:
                break
        else:
            # A rung that falls back into the noise unmakes the claim: the
            # first one was luck, and the climb has not started.
            lowest, confirming = None, 0
    return AxisFloor(
        axis=axis, black=black, rungs=tuple(rungs), lowest_separable=lowest
    )


def measure_floor(
    drive: PatchDrive,
    instrument: Instrument,
    encoding: WireEncoding,
    *,
    axes: Sequence[str] = tuple(AXES),
    codes: Sequence[int] = WALK_CODES,
    repeats: int = 3,
    settle_seconds: float = 1.0,
    sigma: float = SEPARATION_SIGMA,
) -> FloorReport:
    """Walk every axis and report where each one leaves black.

    Black is read per axis rather than once: the axes are walked minutes
    apart, and the drift between them is exactly the noise the separation
    test is measuring against.
    """
    walked = tuple(
        measure_axis(
            drive,
            instrument,
            encoding,
            axis,
            codes=codes,
            repeats=repeats,
            settle_seconds=settle_seconds,
            sigma=sigma,
        )
        for axis in axes
    )
    ambient = min((a.black.mean for a in walked if a.black.readings), default=math.nan)
    return FloorReport(axes=walked, ambient=ambient, separation_sigma=sigma)


def _step(value: float) -> str:
    """A step smaller than the table's own precision, said so rather than
    rounded to zero — a reported step of 0.00000 is not a step."""
    return f"{value:.5f}" if abs(value) >= 5e-5 else f"{value:.2e}"


def render_report(report: FloorReport) -> str:
    """The walk as a table: the shape, not just the answer.

    The per-rung numbers are the point. A reviewer deciding where a
    protocol's floor belongs needs to see how steeply the display leaves
    black, not only the first code that cleared the noise.
    """
    lines = [
        f"black floor (darkest axis): {report.ambient:.5f} cd/m²",
        f"separation: {report.separation_sigma:g} sd over the combined spread",
        "",
    ]
    for axis in report.axes:
        if axis.lowest_separable is None:
            verdict = "no rung separated from black"
        else:
            verdict = (
                f"lowest reproducible code {axis.lowest_separable}, "
                f"first step {_step(axis.first_step)} cd/m²"
            )
        lines.append(f"{axis.axis}: {verdict}")
        lines.append(f"  {'code':>5} {'mean cd/m²':>12} {'sd':>10} {'over black':>11}")
        lines.append(
            f"  {0:>5} {axis.black.mean:12.5f} {axis.black.sigma:10.5f} {'—':>11}"
        )
        for rung in axis.rungs:
            if rung.refused:
                lines.append(f"  {rung.code:>5} {rung.refused:>34}")
                continue
            mark = "*" if rung.code == axis.lowest_separable else " "
            lines.append(
                f"  {rung.code:>5} {rung.mean:12.5f} {rung.sigma:10.5f} "
                f"{rung.mean - axis.black.mean:11.5f}{mark}"
            )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
