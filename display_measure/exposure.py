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


# The serial timeout the CR driver applies, in seconds, per speed. A
# read cannot be interrupted — the instrument has no abort command, and
# a host that stops listening leaves it integrating regardless — so this
# is not an estimate of how long a read takes. It is how long the
# instrument is unreachable if the read finds no light.
SPEED_CEILING = {"normal": 21.0, "slow": 70.0, "fast": 14.0, None: 21.0}

# The darkest luminance worth resolving, cd/m².
#
# Not the darkest measurable — the darkest with a consumer. These
# displays are measured for human viewers and cinematic cameras, and
# both stop caring well above the physics:
#
#   HDR mastering / the PQ design floor    ~0.005 cd/m²
#   a 17-stop camera under a 1500 nit peak ~0.011 cd/m²
#   an eye adapted in a viewing room       ~0.001-0.005 cd/m²
#
# An order of magnitude under the content floor is generous. Below it a
# reading answers no question anyone asked, and chasing it is what
# turned a ladder into a 75-minute measurement nobody would ever run.
#
# The bench CR-120 reads this panel's black at 0.000222 — four times
# under this floor at its default settings. The depth was never the
# problem; the instrument choice was.
USEFUL_FLOOR = 0.001

# The longest any rung may hold the instrument. An in-flight read cannot
# be cancelled — the instrument has no abort, and a host that stops
# listening leaves it integrating regardless — so this is time no
# operator can take back.
#
# Sized so a whole climb stays inside a couple of minutes: a session
# that spends longer than that on one patch is a session nobody runs.
MAX_READ_SECONDS = 90.0


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

    @property
    def label(self) -> str:
        speed = self.speed or "n/a"
        return f"{speed} x{self.average_samples}"

    @property
    def ceiling_seconds(self) -> float:
        """How long this rung can hold the instrument, from the driver's
        own formula rather than a guess about it."""
        return SPEED_CEILING.get(self.speed, 21.0) * self.average_samples

    def __post_init__(self) -> None:
        if self.ceiling_seconds > MAX_READ_SECONDS:
            raise ValueError(
                f"rung {self.label} can hold the instrument for "
                f"{self.ceiling_seconds:.0f}s, over the {MAX_READ_SECONDS:.0f}s "
                "a read may cost. An in-flight read cannot be cancelled, so "
                "this is time no operator can take back."
            )


# Cheapest first. The trust thresholds are this bench's CR-300 measured
# against a CR-120 on the same patches, so they are a starting ladder
# and not a datasheet: a different instrument, or the same one after a
# recalibration, wants its own. They belong upstream in colour-specio
# eventually, as nominal ranges per device — a session should not have
# to rediscover where its instrument taps out on every run.
DEFAULT_LADDER: tuple[ExposureRung, ...] = (
    ExposureRung(speed="normal", average_samples=1, trustworthy_above=1.0),
    ExposureRung(speed="normal", average_samples=4, trustworthy_above=0.05),
    ExposureRung(speed="slow", average_samples=1, trustworthy_above=USEFUL_FLOOR),
)

# Two rungs of climb, 175 s if a patch needs all of it, and the ladder
# stops at the useful floor rather than at the instrument's. A reading
# still under it after the last rung is not a number to chase with more
# exposure — it is a patch this instrument cannot usefully resolve, and
# the answer is the colorimeter (`--instrument hybrid`), not another
# forty minutes of integration.

# No averaging rung on `slow`, and that is a deliberate omission rather
# than an oversight. Averaging reduces *random* noise by the square root
# of the count and cannot move a systematic dark-current pedestal, so a
# `slow x32` rung would multiply the wedge by 32 for a benefit nobody
# has measured. The capped sweep that would settle it has not run. Add
# the rung when there is a measurement saying it helps, not before.


def instrument_floor(ladder: tuple[ExposureRung, ...] = DEFAULT_LADDER) -> float:
    """The lowest luminance this ladder resolves.

    A reading under it is not the display's — the session has run out of
    exposure worth spending, and the honest next step is a more
    sensitive instrument rather than a longer integration.
    """
    return min(rung.trustworthy_above for rung in ladder)


def worth_resolving(luminance: float) -> bool:
    """Whether a reading is above the level anything downstream reads."""
    return luminance > USEFUL_FLOOR


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
