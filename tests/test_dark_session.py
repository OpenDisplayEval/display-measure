"""A session on a display darker than its instrument finishes with bounds.

The doubles stand in for the instrument: a display double given a floor
refuses below it as a driver refuses below the floor it states
(§spec:below-floor).
"""

from pathlib import Path
from typing import Any

import pytest
from conftest import FIXED_TIME, projection, rows

from display_measure import session
from display_measure.artifact import Bound, verify
from display_measure.events import (
    GateEvaluated,
    GateVerdict,
    PatchBounded,
    PatchCompleted,
    SessionEvent,
    UnreadablePatch,
)
from display_measure.plausible_display import DOUBLE_FLOOR_SOURCE
from display_measure.processor import AmbientTooHigh, ContractViolation
from display_measure.protocol import (
    ANCHORS,
    BLACK_PATCH,
    CONFIG_SUITE,
    MeasurementSuite,
    compose,
)

# Above the display double's black (0.005 cd/m²), far below its primaries.
FLOOR = 0.5


def run(
    out: Path, suite: MeasurementSuite = CONFIG_SUITE, floor: float = FLOOR
) -> list[SessionEvent]:
    events: list[SessionEvent] = []
    session.doubles_session(
        out,
        clock=lambda: FIXED_TIME,
        settle_seconds=0.0,
        suite=suite,
        floor=floor,
        emit=events.append,
    )
    return events


class TestADarkDisplayFinishes:
    def test_black_is_a_bound_naming_the_floor(self, tmp_path: Path) -> None:
        out = tmp_path / "m.csmf"
        run(out)

        doc = projection(out)

        bound = {"below": pytest.approx(FLOOR), "floor_source": DOUBLE_FLOOR_SOURCE}
        assert doc["luminance"]["black_level"] == bound
        assert doc["ambient_floor"] == bound
        black = doc["protocol"]["presentation_order"].index(BLACK_PATCH)
        assert doc["readings"][black]["below"] == pytest.approx(FLOOR)
        assert doc["readings"][black]["instrument"].startswith("display-measure")

    def test_the_other_anchors_are_readings(self, tmp_path: Path) -> None:
        out = tmp_path / "m.csmf"
        run(out)

        doc = projection(out)

        assert len(rows(out)) == 4, "five patches driven, black a bound"
        assert doc["luminance"]["peak_luminance"] > 100
        assert verify(out)

    def test_the_stream_names_the_bound(self, tmp_path: Path) -> None:
        events = run(tmp_path / "m.csmf")

        bounded = [e for e in events if isinstance(e, PatchBounded)]
        completed = [e for e in events if isinstance(e, PatchCompleted)]
        assert [e.patch for e in bounded] == [BLACK_PATCH]
        assert bounded[0].floor == pytest.approx(FLOOR)
        assert bounded[0].floor_source == DOUBLE_FLOOR_SOURCE
        assert len(bounded) + len(completed) == len(CONFIG_SUITE.patches)

    def test_without_a_floor_black_is_a_reading(self, tmp_path: Path) -> None:
        out = tmp_path / "m.csmf"
        session.doubles_session(
            out, clock=lambda: FIXED_TIME, settle_seconds=0.0, suite=CONFIG_SUITE
        )

        assert isinstance(projection(out)["luminance"]["black_level"], float)


class TestTheAmbientGateOnABound:
    def test_a_bound_above_the_budget_refuses_at_black(self, tmp_path: Path) -> None:
        events: list[SessionEvent] = []
        suite = compose("anchors", "noise-floor")
        assert suite.max_ambient is not None and suite.max_ambient < FLOOR

        with pytest.raises(AmbientTooHigh, match="cannot confirm"):
            session.doubles_session(
                tmp_path / "m.csmf",
                clock=lambda: FIXED_TIME,
                settle_seconds=0.0,
                suite=suite,
                floor=FLOOR,
                emit=events.append,
            )

        refused = [
            e
            for e in events
            if isinstance(e, GateEvaluated) and e.verdict == GateVerdict.REFUSED
        ]
        assert refused and "0.5" in refused[0].detail
        assert not (tmp_path / "m.csmf").exists()

    def test_a_bound_within_the_budget_passes(self) -> None:
        events: list[SessionEvent] = []
        bound = Bound(floor=0.004, floor_source="nominal")

        recorded = session._gate_black(bound, 0.005, events.append)

        assert recorded is bound
        assert isinstance(events[-1], GateEvaluated)
        assert events[-1].verdict == GateVerdict.PASS


class TestWhatIsNotABound:
    def test_a_white_refused_as_too_dark_refuses_the_session(
        self, tmp_path: Path
    ) -> None:
        with pytest.raises(ContractViolation, match="white was refused"):
            run(tmp_path / "m.csmf", floor=1.0e6)

    def test_probes_refuse_a_bounded_black(self, tmp_path: Path) -> None:
        from display_measure.probes import FIRST_LIGHT

        suite = MeasurementSuite(
            name="probed", blocks=(ANCHORS,), probes=(FIRST_LIGHT,)
        )

        with pytest.raises(UnreadablePatch, match="probes"):
            run(tmp_path / "m.csmf", suite=suite)

    @pytest.mark.parametrize(
        "attributes",
        [
            {"too_dark": False, "floor": 3.4},
            {"too_dark": True, "floor": None},
        ],
        ids=["overload", "no-stated-floor"],
    )
    def test_a_refusal_that_states_no_bound_is_none(
        self, attributes: dict[str, Any]
    ) -> None:
        class MeasurementOutOfRange(Exception):
            pass

        failure = MeasurementOutOfRange()
        for name, value in attributes.items():
            setattr(failure, name, value)

        assert session._bound_from(failure) is None

    def test_an_unrelated_failure_is_no_bound(self) -> None:
        assert session._bound_from(RuntimeError("link")) is None
