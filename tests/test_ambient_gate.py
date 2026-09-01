"""Ambient as a block condition (§spec:session-gates, MEASUREMENT.md).

Some measurements describe the display and some describe the display in
its environment, and room light is a contaminant for the first and an
operating condition for the second. Which one a session is doing is
decided by the blocks it composes, not by a flag.
"""

from pathlib import Path

import pytest
from bmd_sg.decklink import MockBMDDeckLink

from display_measure.processor import AmbientTooHigh, audit_ambient
from display_measure.protocol import (
    ANCHORS,
    CONFIG_SUITE,
    NOISE_FLOOR,
    REPORT_SUITE,
    compose,
)


class TestWhichBlocksCareAboutTheRoom:
    def test_a_block_describing_the_display_demands_darkness(self) -> None:
        """`noise-floor` measures the spread of the display's black. At
        the level it is trying to resolve, room light is the thing it
        would otherwise measure."""
        assert NOISE_FLOOR.max_ambient is not None
        assert REPORT_SUITE.max_ambient == NOISE_FLOOR.max_ambient

    def test_a_block_describing_the_display_in_its_room_does_not(self) -> None:
        """An OCIO config renders into whatever ambient a venue has. A
        black level measured in the dark would tell it the shadows go
        somewhere they do not."""
        assert ANCHORS.max_ambient is None
        assert CONFIG_SUITE.max_ambient is None

    def test_a_composition_takes_the_strictest_ceiling(self) -> None:
        mixed = compose("anchors", "noise-floor")

        assert mixed.max_ambient == NOISE_FLOOR.max_ambient


class TestTheAudit:
    def test_a_floor_within_the_ceiling_passes(self) -> None:
        audit_ambient(0.001, 0.005)

    def test_a_floor_above_the_ceiling_is_refused_with_both_numbers(self) -> None:
        with pytest.raises(AmbientTooHigh) as raised:
            audit_ambient(1.0104, 0.005)

        message = str(raised.value)
        assert "1.01" in message
        assert "0.005" in message
        assert "--suite config" in message

    def test_the_refusal_says_what_to_do_instead(self) -> None:
        """Two actions, and the operator picks: darken the room, or
        measure a composition that takes the room as it is."""
        with pytest.raises(AmbientTooHigh, match="darken the room"):
            audit_ambient(1.0, 0.005)


class TestInASession:
    def test_a_report_session_refuses_a_lit_room(self, tmp_path: Path) -> None:
        """The double reads an ambient the report blocks do not allow,
        and the session stops at the opening black rather than spending
        two hours measuring the room."""
        from display_measure.plausible_display import PlausibleDisplay
        from display_measure.session import (
            DECKLINK_INDEX,
            characterize,
            normalized_reading,
        )

        with MockBMDDeckLink(DECKLINK_INDEX) as device:
            with pytest.raises(AmbientTooHigh):
                characterize(
                    device=device,
                    instrument=PlausibleDisplay(device, ambient=1.0),
                    out_path=tmp_path / "lit.csmf",
                    clock=lambda: __import__("datetime").datetime(
                        2026, 8, 10, tzinfo=__import__("datetime").UTC
                    ),
                    settle_seconds=0.0,
                    suite=REPORT_SUITE,
                    warmup_seconds=0.0,
                    conditioning_seconds=0.0,
                    read_attempts=1,
                    reading=normalized_reading(),
                )
            assert not (tmp_path / "lit.csmf").exists()

    def test_a_config_session_measures_the_same_room_happily(
        self, tmp_path: Path
    ) -> None:
        """Same room, different question, no refusal — and the ambient
        is recorded as the operating condition it is."""
        from display_measure.plausible_display import PlausibleDisplay
        from display_measure.session import (
            DECKLINK_INDEX,
            characterize,
            normalized_reading,
        )

        with MockBMDDeckLink(DECKLINK_INDEX) as device:
            characterize(
                device=device,
                instrument=PlausibleDisplay(device, ambient=1.0),
                out_path=tmp_path / "config.csmf",
                clock=lambda: __import__("datetime").datetime(
                    2026, 8, 10, tzinfo=__import__("datetime").UTC
                ),
                settle_seconds=0.0,
                suite=CONFIG_SUITE,
                warmup_seconds=0.0,
                conditioning_seconds=0.0,
                read_attempts=1,
                reading=normalized_reading(),
            )

            assert (tmp_path / "config.csmf").is_file()


def test_the_cli_restates_the_session_s_read_attempts() -> None:
    """`--help` loads no measurement stack, so the CLI carries its own
    copy. Two copies of a number is one too many unless checked."""
    from display_measure import cli
    from display_measure.session import DEFAULT_READ_ATTEMPTS

    assert cli.DEFAULT_READ_ATTEMPTS == DEFAULT_READ_ATTEMPTS


def test_read_attempts_is_not_a_block_condition() -> None:
    """A truncated serial reply is a stumbling link whatever is being
    measured. Treating it as a measurement condition cost a bench
    session its first run: the config suite inherited a single attempt
    from `anchors`, and one truncated reply ended it."""
    from display_measure.protocol import BLOCKS

    for block in BLOCKS.values():
        assert not hasattr(block, "read_attempts"), block.id
