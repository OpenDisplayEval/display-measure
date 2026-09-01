"""Matching exposure to the light there is (§spec:sessions).

The bench CR-300 read this panel's black at 0.0161 cd/m² against its own
blocked-aperture zero of ~0.0149, while the CR-120 read 0.000222 of the
same patch in the same room. The instrument was reporting its own floor,
because the session asked for one sample at NORMAL speed and never
looked at what came back.
"""

import pytest

from display_measure.exposure import (
    DEFAULT_LADDER,
    ExposureRung,
    instrument_floor,
    rung_for,
)


class TestTheLadder:
    def test_it_climbs_from_cheap_to_deep(self) -> None:
        thresholds = [rung.trustworthy_above for rung in DEFAULT_LADDER]
        seconds = [rung.seconds for rung in DEFAULT_LADDER]

        assert thresholds == sorted(thresholds, reverse=True)
        assert seconds == sorted(seconds)

    def test_bright_light_takes_the_cheapest_rung(self) -> None:
        """Seventy seconds on 1500 cd/m² buys nothing."""
        assert rung_for(1488.0) is DEFAULT_LADDER[0]

    def test_a_reading_near_black_takes_the_deepest(self) -> None:
        assert rung_for(0.000222) is DEFAULT_LADDER[-1]

    def test_nothing_read_yet_takes_the_cheapest(self) -> None:
        """The first read is what tells a session where it is;
        escalation is on what came back, not on a guess."""
        assert rung_for(None) is DEFAULT_LADDER[0]

    def test_the_floor_is_the_deepest_claim_any_rung_makes(self) -> None:
        assert instrument_floor() == min(r.trustworthy_above for r in DEFAULT_LADDER)


class Stepped:
    """An instrument that reads deeper the more exposure it is given.

    Stands in for the real behaviour without the bench: below its floor
    it returns the floor, and its floor falls as exposure rises.
    """

    manufacturer = "test"
    model = "Stepped"
    serial_number = "0"

    class MeasurementSpeed(str):
        pass

    def __init__(self, true_luminance: float) -> None:
        self.true_luminance = true_luminance
        self.average_samples = 1
        self.measurement_speed = "normal"
        self.reads = 0

    @property
    def _floor(self) -> float:
        deep = self.measurement_speed == "slow"
        return 0.0001 if deep else 0.02 / self.average_samples

    def measure(self):
        import numpy as np

        from display_measure.instrument import XYZReading

        self.reads += 1
        seen = max(self.true_luminance, self._floor)
        return XYZReading(XYZ=np.array([seen, seen, seen]))


class TestEscalationInASession:
    def test_a_dark_patch_climbs_until_the_reading_is_trustworthy(self) -> None:
        from display_measure.protocol import Patch
        from display_measure.session import _read_at_depth

        instrument = Stepped(true_luminance=0.000222)
        events: list = []

        reading = _read_at_depth(
            instrument,
            Patch("black", (0, 0, 0), "black_level"),
            attempts=1,
            ladder=DEFAULT_LADDER,
            emit=events.append,
            index=1,
        )

        assert float(reading.XYZ[1]) == pytest.approx(0.000222)
        assert instrument.reads > 1, "it settled for the first rung"
        assert events, "it climbed without saying so"

    def test_a_bright_patch_is_read_once(self) -> None:
        from display_measure.protocol import Patch
        from display_measure.session import _read_at_depth

        instrument = Stepped(true_luminance=1488.0)

        _read_at_depth(
            instrument,
            Patch("white", (4095, 4095, 4095), "white_point"),
            attempts=1,
            ladder=DEFAULT_LADDER,
            emit=lambda e: None,
            index=1,
        )

        assert instrument.reads == 1

    def test_an_instrument_with_no_knobs_is_not_climbed(self) -> None:
        """The doubles have neither setting, and should not be read four
        times to discover that."""
        from display_measure.protocol import Patch
        from display_measure.session import _read_at_depth

        class Fixed:
            manufacturer = model = serial_number = "x"

            def __init__(self) -> None:
                self.reads = 0

            def measure(self):
                import numpy as np

                from display_measure.instrument import XYZReading

                self.reads += 1
                return XYZReading(XYZ=np.array([1e-9, 1e-9, 1e-9]))

        instrument = Fixed()
        _read_at_depth(
            instrument,
            Patch("black", (0, 0, 0), "black_level"),
            attempts=1,
            ladder=DEFAULT_LADDER,
            emit=lambda e: None,
            index=1,
        )

        assert instrument.reads == 1


def test_a_custom_ladder_is_honoured() -> None:
    one = ExposureRung(speed=None, average_samples=1, trustworthy_above=0.0, seconds=1)

    assert rung_for(0.0, (one,)) is one
