"""The active port has to pass colour through (§spec:signal-contract).

A characterize session drives raw code values and records what returns
as the display's own primaries. That holds only while the processor maps
the input gamut onto the panel's one-for-one.

The bench found this live: HDMI was set to a custom gamut matching the
panel exactly, and SDI to Rec.2020. A v210 session would have measured
Rec.2020 red mapped into the panel and recorded it as the panel's red,
passing every other gate on the way.
"""

import pytest

from display_measure.processor import (
    GAMUT_IDENTITY_TOLERANCE,
    GamutTransformInPath,
    audit_input_gamut,
)

# The bench panel, as the processor reports it.
PANEL = {"red": (0.6950, 0.3046), "green": (0.2045, 0.7419), "blue": (0.1349, 0.0518)}
# The HDMI port, set to a custom gamut matching it.
HDMI = {"red": (0.6950, 0.3046), "green": (0.2045, 0.7419), "blue": (0.1348, 0.0518)}
# The SDI port, set to Rec.2020.
SDI = {"red": (0.7080, 0.2920), "green": (0.1700, 0.7970), "blue": (0.1310, 0.0460)}


class TestPassthroughPasses:
    def test_a_port_matching_the_panel_is_accepted(self) -> None:
        audit_input_gamut(HDMI, PANEL)

    def test_fourth_decimal_rounding_is_not_a_transform(self) -> None:
        """The processor states both to four places and they round
        differently — 0.1348 against 0.1349. That is rounding, not a
        colour transform, and refusing it would refuse every rig."""
        assert abs(HDMI["blue"][0] - PANEL["blue"][0]) < GAMUT_IDENTITY_TOLERANCE
        audit_input_gamut(HDMI, PANEL)


class TestATransformIsRefused:
    def test_rec2020_input_is_caught(self) -> None:
        with pytest.raises(GamutTransformInPath) as raised:
            audit_input_gamut(SDI, PANEL)

        message = str(raised.value)
        assert "red" in message and "green" in message and "blue" in message

    def test_the_refusal_shows_both_sets_of_coordinates(self) -> None:
        """An operator fixing this in the Tessera UI needs to know what
        to type, and what it currently says."""
        with pytest.raises(GamutTransformInPath, match=r"0\.7080"):
            audit_input_gamut(SDI, PANEL)

    def test_it_says_what_to_do(self) -> None:
        with pytest.raises(GamutTransformInPath, match="achievable gamut"):
            audit_input_gamut(SDI, PANEL)

    def test_one_drifted_primary_is_enough(self) -> None:
        """A gamut is three corners; two matching does not make it
        passthrough."""
        nearly = dict(HDMI, green=(0.1700, 0.7970))

        with pytest.raises(GamutTransformInPath, match="green"):
            audit_input_gamut(nearly, PANEL)


class TestTheGateRunsInTheSession:
    def test_the_audit_is_wired_into_the_processor_gate(self) -> None:
        """It gates before anything is driven, with the other contract
        checks — a refusal costs a round trip, not a rig."""
        import inspect

        from display_measure import session

        assert "audit_input_gamut" in inspect.getsource(session._audit_processor)
