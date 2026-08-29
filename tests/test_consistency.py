"""Session self-consistency (§road:session-consistency).

The contract audit catches a wrong processor. These catch a wrong
*session* — a measurement that contradicts itself, which no amount of
correct external state prevents.

The numbers are the 2026-08-28 bench run's, which produced exactly this
failure and shipped an artifact anyway: a gray ramp that falls 12.8x
across the routing boundary, code 256 reading 6.997 cd/m2 on the
colorimeter against code 384's 0.547 on the spectroradiometer.
"""

import pytest

from display_measure.artifact import SOURCE_COLORIMETER, SOURCE_SPECTRORADIOMETER
from display_measure.consistency import (
    InconsistentSession,
    audit_ramp_monotonicity,
    audit_routing_boundary,
)

# A healthy ramp: the dark-room capture's gray response, low end first.
GOOD = [
    (16, 0.0067),
    (24, 0.0192),
    (32, 0.0407),
    (48, 0.1072),
    (64, 0.2345),
    (96, 0.4258),
    (128, 0.6270),
    (192, 1.6902),
    (256, 3.1503),
]


class TestMonotonicity:
    def test_a_rising_ramp_passes(self) -> None:
        audit_ramp_monotonicity({"gray": GOOD})

    def test_a_ramp_that_falls_is_refused(self) -> None:
        """More drive, less light, is not a measurement."""
        broken = [*GOOD[:5], (96, 0.20), *GOOD[6:]]
        with pytest.raises(InconsistentSession, match="gray"):
            audit_ramp_monotonicity({"gray": broken})

    def test_the_refusal_names_the_codes_that_fall(self) -> None:
        broken = [*GOOD[:5], (96, 0.20), *GOOD[6:]]
        with pytest.raises(InconsistentSession) as e:
            audit_ramp_monotonicity({"gray": broken})
        assert "64" in str(e.value) and "96" in str(e.value)

    def test_every_ramp_is_checked_not_just_the_first(self) -> None:
        """0.02 sits well above the floor and well below code 32's
        0.0407, so this is a real inversion rather than noise."""
        with pytest.raises(InconsistentSession, match="blue"):
            audit_ramp_monotonicity(
                {"gray": GOOD, "blue": [*GOOD[:3], (48, 0.02), *GOOD[4:]]}
            )

    def test_readings_at_the_floor_do_not_trip_it(self) -> None:
        """Two rungs under the instrument's floor read as noise, and noise
        is not evidence the wall got darker. The gate is for real
        inversions, not for the dark end being hard to measure."""
        noisy = [(16, 0.00012), (24, 0.00009), (32, 0.00014), *GOOD[3:]]
        audit_ramp_monotonicity({"gray": noisy}, floor=0.001)


class TestRoutingBoundary:
    """A hybrid session reads the dark end with one instrument and the
    bright end with another. Where they meet, they have to agree."""

    def test_a_continuous_boundary_passes(self) -> None:
        rows = [
            (192, 1.6902, SOURCE_COLORIMETER),
            (256, 3.1503, SOURCE_COLORIMETER),
            (384, 8.0, SOURCE_SPECTRORADIOMETER),
            (512, 15.3, SOURCE_SPECTRORADIOMETER),
        ]
        audit_routing_boundary({"gray": rows})

    def test_the_2026_08_28_discontinuity_is_refused(self) -> None:
        """The run this gate exists for: the ramp falls 12.8x where the
        instruments change hands."""
        rows = [
            (192, 5.16, SOURCE_COLORIMETER),
            (256, 6.997, SOURCE_COLORIMETER),
            (384, 0.547, SOURCE_SPECTRORADIOMETER),
            (512, 0.953, SOURCE_SPECTRORADIOMETER),
        ]
        with pytest.raises(InconsistentSession, match="256"):
            audit_routing_boundary({"gray": rows})

    def test_a_single_instrument_ramp_has_no_boundary(self) -> None:
        rows = [(c, y, SOURCE_SPECTRORADIOMETER) for c, y in GOOD]
        audit_routing_boundary({"gray": rows})
