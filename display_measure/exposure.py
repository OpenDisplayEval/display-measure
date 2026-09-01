"""Matching instrument exposure to the light there is (§spec:sessions).

An instrument left at its defaults reads its own floor when the display
is darker than that floor. The bench CR-300 read this panel's black at
0.0161 cd/m² against its own blocked-aperture zero of ~0.0149 — 73x what
the CR-120 read of the same patch in the same room — because the session
asked for one sample at NORMAL speed and that is what it got.

Both knobs exist on the Colorimetry Research surface and neither was
being used:

`average_samples` (1-50) averages, which reduces *random* noise by the
square root of the count and does nothing to a systematic dark-current
pedestal. `measurement_speed` (CR-300 only) lengthens the auto-exposure
integration, which lifts signal above read noise and reaches genuinely
deeper — at the cost of accumulating dark current too, and of time.

So the ladder escalates both, and a reading escalates when it lands
below what its current rung can be trusted to resolve. A patch measured
at the bottom of the range gets the long integration it needs; a patch
at full drive is read once and quickly, because spending seventy
seconds on 1500 cd/m² buys nothing.
"""

from dataclasses import dataclass

__all__ = [
    "DEFAULT_LADDER",
    "ExposureRung",
    "instrument_floor",
    "rung_for",
]


@dataclass(frozen=True)
class ExposureRung:
    """One exposure setting, and the luminance it can be trusted below.

    `trustworthy_above` is the level at which a reading stops being
    mostly instrument. Below it, this rung is reporting its own floor
    and the session should climb rather than believe the number.
    """

    speed: str | None
    average_samples: int
    trustworthy_above: float
    seconds: float

    @property
    def label(self) -> str:
        speed = self.speed or "n/a"
        return f"{speed} x{self.average_samples}"


# Cheapest first. The trust thresholds are this bench's CR-300 measured
# against a CR-120 on the same patches, so they are a starting ladder
# and not a datasheet: a different instrument, or the same one after a
# recalibration, wants its own. They belong upstream in colour-specio
# eventually, as nominal ranges per device — a session should not have
# to rediscover where its instrument taps out on every run.
DEFAULT_LADDER: tuple[ExposureRung, ...] = (
    ExposureRung(speed="normal", average_samples=1, trustworthy_above=1.0, seconds=1),
    ExposureRung(speed="normal", average_samples=8, trustworthy_above=0.1, seconds=6),
    ExposureRung(speed="slow", average_samples=8, trustworthy_above=0.02, seconds=20),
    ExposureRung(speed="slow", average_samples=32, trustworthy_above=0.0, seconds=90),
)


def instrument_floor(ladder: tuple[ExposureRung, ...] = DEFAULT_LADDER) -> float:
    """The lowest luminance any rung claims to resolve.

    A reading below this is the instrument, whatever the exposure — the
    number a session should refuse to present as the display's.
    """
    return min(rung.trustworthy_above for rung in ladder)


def rung_for(
    luminance: float | None, ladder: tuple[ExposureRung, ...] = DEFAULT_LADDER
) -> ExposureRung:
    """The cheapest rung that can be trusted at `luminance`.

    `None` — nothing read yet — takes the cheapest rung, because the
    first read is what tells the session where it is. Escalation happens
    on what comes back, not on a guess about what will.
    """
    if luminance is None:
        return ladder[0]
    for rung in ladder:
        if luminance > rung.trustworthy_above:
            return rung
    return ladder[-1]
