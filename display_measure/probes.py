"""Adaptive measurements (§spec:patch-protocol, MEASUREMENT.md).

A probe searches for something whose location is a property of the
display in front of it. `display_measure.protocol` carries the static
blocks, whose codes are fixed and shuffled; this module carries the
measurements whose codes cannot be known before the session reads
something.

The session owns drive, settle, instrument and retry; a probe owns only
the decision about what to ask for next.
"""

from collections.abc import Callable
from dataclasses import dataclass, field, replace
from functools import partial
from typing import Protocol, runtime_checkable

from display_measure.codes import FULL_DRIVE


@dataclass(frozen=True)
class ProbeResult:
    """What a probe found, and the patches it drove finding it.

    The codes are not bookkeeping. A probe's answer is only auditable
    against the readings that produced it, and unlike a block those
    readings are not implied by the probe's id.
    """

    probe_id: str
    findings: dict[str, float | None]
    driven: tuple[tuple[tuple[int, int, int], float], ...]

    @property
    def patch_count(self) -> int:
        return len(self.driven)


@runtime_checkable
class Probe(Protocol):
    """An adaptive measurement: it decides its next patch from what it read.

    A block's patches are known before the session starts, so they can
    be shuffled into the presentation and counted for progress. A probe's
    are not: it is searching for something whose location is a property
    of the display in front of it, and a fixed code list either misses
    that or spends its patches bracketing where it is not. First light is
    the example — on this bench panel red lights at code 6 and green and
    blue at 8; on another panel it could be 40.

    Three consequences follow, and none of them should be buried
    (§spec:patch-protocol):

    Its readings are thermally correlated. Each patch depends on the one
    before, so a probe cannot join the shuffle that decorrelates panel
    drift from signal level. Probes therefore run after the shuffled
    blocks, in a block of session time of their own.

    Its cost is a bound, not a count. `max_patches` is what a session can
    promise; the patches actually driven are known only afterwards.

    Its patch list is a result. What a probe converged on is itself a
    measurement, so the artifact records the codes it drove alongside
    what it read — a static block's codes are implied by its id, and a
    probe's are not.
    """

    name: str
    version: int
    measures: str
    max_patches: int

    @property
    def id(self) -> str: ...

    def with_floor(self, floor: float) -> "Probe":
        """This probe, searching against a floor the session measured.

        A probe is defined without one — the floor is a measurement, not
        a constant — and the session fills it in from the anchors it
        read. Asking the probe rather than rebuilding it from the
        outside keeps the session from assuming how a probe is put
        together.
        """
        ...

    def run(self, read: Callable[[tuple[int, int, int]], float]) -> ProbeResult:
        """Drive and read patches through `read`, returning what was found.

        `read` drives one RGB code triplet and returns its luminance in
        cd/m². The probe owns the search; the session owns the drive,
        the settle, the instrument and the retry.
        """
        ...


# How far above the noise floor a reading has to sit before it counts as
# light rather than as the floor. The bench toe map is why this is not 1:
# neutral code 6 read back at u' = 1.1987, a physically impossible
# chromaticity, because the reading was noise wearing a luminance. Three
# times the floor is the same discrimination the fidelity report's patch
# filter applies at 3 dB.
LIGHT_OVER_FLOOR = 3.0

# Where the doubling search gives up looking for a bracket. Well above
# any threshold worth calling "first light": a panel dark to code 512
# has no usable shadow detail at all, and the finding is that it is
# dark, not where it starts.
DEFAULT_SEARCH_CEILING = 512


@dataclass
class FirstLight:
    """The lowest code on each channel that emits light the instrument sees.

    Binary search, not a code list. Where the threshold sits is a
    property of the panel: on the bench display red lights at code 6 and
    green and blue at 8, while another panel's could be 40. A static
    block over codes 1-14 asserts the answer is in 1-14 — it either
    misses it or spends its patches bracketing where it is not.

    The search runs per channel because the channels differ, and that
    difference is itself the finding: a panel whose blue lights four
    codes above its red has a colour shift out of black that no neutral
    ramp reveals.
    """

    floor: float | None
    name: str = "first-light"
    version: int = 1
    ceiling: int = DEFAULT_SEARCH_CEILING
    # Three channels, each a binary search over `ceiling` codes, plus a
    # doubling probe to establish the bracket. Twelve steps covers 4096.
    max_patches: int = 3 * 2 * 13
    measures: str = field(
        default=(
            "The lowest drive code on each channel whose reading clears "
            "the display's noise floor — where the panel starts emitting "
            "at all, per channel. The spread between the three is a "
            "colour shift out of black that no neutral ramp shows."
        )
    )

    @property
    def id(self) -> str:
        return f"{self.name}/{self.version}"

    def with_floor(self, floor: float) -> "FirstLight":
        return replace(self, floor=floor)

    def run(self, read: Callable[[tuple[int, int, int]], float]) -> ProbeResult:
        """Search each channel for its threshold, reading through `read`."""
        if self.floor is None:
            raise ValueError(
                "first-light compares readings against the display's noise "
                "floor, and this session measured none. Compose the "
                "`anchors` block (and `noise-floor` for a floor with a "
                "spread) ahead of the probe."
            )
        threshold = self.floor * LIGHT_OVER_FLOOR
        driven: list[tuple[tuple[int, int, int], float]] = []

        def lit(code: int, *, axis: int) -> bool:
            channels = [0, 0, 0]
            channels[axis] = code
            rgb = (channels[0], channels[1], channels[2])
            luminance = read(rgb)
            driven.append((rgb, luminance))
            return bool(luminance > threshold)

        findings: dict[str, float | None] = {}
        for axis, channel in enumerate(("red", "green", "blue")):
            findings[channel] = self._search(partial(lit, axis=axis))
        return ProbeResult(probe_id=self.id, findings=findings, driven=tuple(driven))

    def _search(self, lit: Callable[[int], bool]) -> int | None:
        """The lowest code where `lit` holds, or None if none does.

        Doubling first, then bisecting: the threshold sits near the
        bottom of the range on every display worth measuring, so a
        search that starts by halving 4095 spends its first reads far
        above the answer.
        """
        high = 1
        while not lit(high):
            if high >= FULL_DRIVE:
                return None
            high = min(high * 2, FULL_DRIVE)
        low = high // 2  # The last code known dark, or 0 at the bottom.
        while low + 1 < high:
            middle = (low + high) // 2
            if lit(middle):
                high = middle
            else:
                low = middle
        return high


# A template, carrying no floor: the floor is a measurement, and the
# session fills it in from the anchors it read before the probe runs
# (`dataclasses.replace`). A probe handed no floor refuses rather than
# comparing readings against zero, which would call the black reading
# itself the first light.
FIRST_LIGHT = FirstLight(floor=None)

PROBES = {FIRST_LIGHT.name: FIRST_LIGHT}
