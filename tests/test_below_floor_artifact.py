"""A patch refused as too dark is a bound in the file, never a reading."""

from dataclasses import replace
from pathlib import Path

import pytest
import yaml
from specio.serialization.csmf import load_csmf_file
from test_artifact import spectral_artifact

from display_measure.artifact import (
    ABSENT_SPECTRUM,
    SCHEMA,
    SPECTRUM_MEASURED,
    Bound,
    MeasurementsArtifact,
    carried_projection,
    render,
    verify,
    write,
)

FLOOR = Bound(floor=3.4, floor_source="nominal: PR-655/670 User Manual, Table 12")


def bounded_artifact() -> MeasurementsArtifact:
    """The three-row artifact with black — row 0 — refused as too dark."""
    artifact = spectral_artifact()
    assert artifact.readings is not None
    assert artifact.spectra is not None
    return replace(
        artifact,
        black_level=FLOOR,
        ambient_floor=FLOOR,
        readings=(FLOOR, *artifact.readings[1:]),
        spectra=(ABSENT_SPECTRUM, *artifact.spectra[1:]),
    )


class TestTheProjection:
    def test_black_and_ambient_render_as_the_bound(self) -> None:
        doc = yaml.safe_load(render(bounded_artifact()))

        expected = {"below": pytest.approx(3.4), "floor_source": FLOOR.floor_source}
        assert doc["schema"] == SCHEMA
        assert doc["luminance"]["black_level"] == expected
        assert doc["ambient_floor"] == expected

    def test_a_bounded_row_names_its_instrument_and_floor(self) -> None:
        rows = yaml.safe_load(render(bounded_artifact()))["readings"]

        assert rows[0] == {
            "below": pytest.approx(3.4),
            "floor_source": FLOOR.floor_source,
            "instrument": "specio Virtual Random Spectrometer 0000-0000",
        }
        assert isinstance(rows[1], list), "a reading stays a tristimulus value"

    def test_a_reading_renders_as_before(self) -> None:
        doc = yaml.safe_load(render(spectral_artifact()))

        assert isinstance(doc["luminance"]["black_level"], float)


class TestTheSeamFile:
    def test_a_bound_carries_no_measurement_row(self, tmp_path: Path) -> None:
        out = tmp_path / "m.csmf"
        write(bounded_artifact(), out)

        loaded = load_csmf_file(out)

        assert len(loaded.measurements) == 2
        artifact = bounded_artifact()
        assert artifact.driven_codes is not None
        assert [tuple(c) for c in loaded.test_colors] == list(artifact.driven_codes[1:])

    def test_the_file_verifies_with_its_bounds(self, tmp_path: Path) -> None:
        out = tmp_path / "m.csmf"
        recorded = write(bounded_artifact(), out)

        assert verify(out) == recorded

    def test_the_projection_keeps_the_full_driven_order(self, tmp_path: Path) -> None:
        out = tmp_path / "m.csmf"
        write(bounded_artifact(), out)

        doc = yaml.safe_load(carried_projection(load_csmf_file(out).ancillary)[1])

        assert doc["protocol"]["presentation_order"] == [
            "black",
            "gray_0016",
            "white",
        ]
        assert "below" in doc["readings"][0]


class TestABoundIsNotAReading:
    def test_a_bound_cannot_carry_a_spectrum(self) -> None:
        artifact = bounded_artifact()
        source = spectral_artifact().spectra
        assert artifact.spectra is not None and source is not None
        measured = source[0]
        assert measured.provenance == SPECTRUM_MEASURED

        with pytest.raises(ValueError, match="bound"):
            replace(artifact, spectra=(measured, *artifact.spectra[1:]))

    @pytest.mark.parametrize("floor", [0.0, -1.0, float("nan"), float("inf")])
    def test_a_floor_is_a_positive_luminance(self, floor: float) -> None:
        with pytest.raises(ValueError, match="positive"):
            Bound(floor=floor, floor_source="nominal")

    def test_a_floor_source_is_renderable(self) -> None:
        with pytest.raises(ValueError):
            Bound(floor=1.0, floor_source='a "quoted" source')
