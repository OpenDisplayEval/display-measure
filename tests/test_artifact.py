"""Measurements-artifact tests: shape, determinism, immutability."""

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from display_measure.artifact import (
    SOURCE_COLORIMETER,
    SOURCE_SPECTRORADIOMETER,
    AdditivityTriad,
    InstrumentIdentity,
    InstrumentRouting,
    MeasurementsArtifact,
    PerChannelResponse,
    ProcessorStateSnapshot,
    ResponsePoint,
    render,
    write,
)


def sample_artifact() -> MeasurementsArtifact:
    return MeasurementsArtifact(
        red_xy=(0.680, 0.320),
        green_xy=(0.265, 0.690),
        blue_xy=(0.150, 0.060),
        white_xy=(0.3127, 0.3290),
        black_level=0.005,
        peak_luminance=1000.0,
        ambient_floor=5.0,
        instrument=InstrumentIdentity(
            manufacturer="specio",
            model="Virtual Random Spectrometer",
            serial_number="0000-0000",
            firmware="1.0.0",
        ),
        processor_state=ProcessorStateSnapshot(
            eotf_type="GAMMA",
            gamma_value=2.4,
            intensity="100%",
            processing_enabled=frozenset(),
        ),
        session_start=datetime(2026, 8, 10, 12, 0, 0, tzinfo=UTC),
        session_end=datetime(2026, 8, 10, 12, 5, 0, tzinfo=UTC),
    )


def test_render_parses_into_the_shape_ocio_display_gen_loads() -> None:
    doc = yaml.safe_load(render(sample_artifact()))
    primaries = doc["colorimetry"]["primaries"]
    assert primaries["red"] == pytest.approx([0.680, 0.320])
    assert primaries["green"] == pytest.approx([0.265, 0.690])
    assert primaries["blue"] == pytest.approx([0.150, 0.060])
    assert doc["colorimetry"]["white_point"] == pytest.approx([0.3127, 0.3290])
    assert doc["luminance"]["black_level"] == pytest.approx(0.005)
    assert doc["luminance"]["peak_luminance"] == pytest.approx(1000.0)
    assert doc["ambient_floor"] == pytest.approx(5.0)
    assert doc["instrument"]["model"] == "Virtual Random Spectrometer"
    assert doc["instrument"]["firmware"] == "1.0.0"
    assert doc["processor_state"]["eotf"]["type"] == "GAMMA"
    assert doc["processor_state"]["eotf"]["gamma_value"] == pytest.approx(2.4)
    assert doc["processor_state"]["intensity"] == "100%"
    assert doc["processor_state"]["processing_disabled"] is True
    assert doc["session"]["start"] == "2026-08-10T12:00:00+00:00"
    assert doc["session"]["end"] == "2026-08-10T12:05:00+00:00"
    assert doc["measurement_date"] == "2026-08-10"


def test_gamma_value_omitted_for_non_gamma_eotf() -> None:
    a = sample_artifact()
    pq_state = ProcessorStateSnapshot(
        eotf_type="PQ",
        gamma_value=None,
        intensity="100%",
        processing_enabled=frozenset(),
    )
    doc = yaml.safe_load(render(replace(a, processor_state=pq_state)))
    assert doc["processor_state"]["eotf"]["type"] == "PQ"
    assert "gamma_value" not in doc["processor_state"]["eotf"]


def test_firmware_omitted_when_instrument_reports_none() -> None:
    a = sample_artifact()
    no_firmware = replace(a, instrument=replace(a.instrument, firmware=None))
    doc = yaml.safe_load(render(no_firmware))
    assert "firmware" not in doc["instrument"]


def test_naive_timestamps_are_refused() -> None:
    with pytest.raises(ValueError, match="timezone"):
        MeasurementsArtifact(
            red_xy=(0.6, 0.3),
            green_xy=(0.3, 0.6),
            blue_xy=(0.15, 0.06),
            white_xy=(0.31, 0.33),
            black_level=0.01,
            peak_luminance=1000.0,
            ambient_floor=1.0,
            instrument=sample_artifact().instrument,
            processor_state=sample_artifact().processor_state,
            session_start=datetime(2026, 8, 10, 12, 0, 0),
            session_end=datetime(2026, 8, 10, 12, 5, 0, tzinfo=UTC),
        )


def test_write_refuses_to_overwrite(tmp_path: Path) -> None:
    a = sample_artifact()
    out = tmp_path / "measurements.yaml"
    write(a, out)
    with pytest.raises(FileExistsError):
        write(a, out)


def test_write_emits_rendered_bytes(tmp_path: Path) -> None:
    a = sample_artifact()
    out = tmp_path / "measurements.yaml"
    write(a, out)
    assert out.read_bytes() == render(a).encode("utf-8")


def full_protocol_artifact() -> MeasurementsArtifact:
    point = ResponsePoint(code=16, xyz=(0.1, 0.2, 0.3))
    ramp = (point, ResponsePoint(code=3072, xyz=(900.0, 1000.0, 1100.0)))
    return replace(
        sample_artifact(),
        protocol_name="color-wrangler/characterize/1",
        presentation_order=("black", "gray_0016", "white"),
        per_channel_response=PerChannelResponse(red=ramp, green=ramp, blue=ramp),
        gray_response=ramp,
        additivity=AdditivityTriad(
            yellow_xyz=(1.0, 2.0, 3.0),
            cyan_xyz=(4.0, 5.0, 6.0),
            magenta_xyz=(7.0, 8.0, 9.0),
        ),
    )


def test_render_carries_protocol_and_response_sections() -> None:
    doc = yaml.safe_load(render(full_protocol_artifact()))
    assert doc["protocol"]["name"] == "color-wrangler/characterize/1"
    assert doc["protocol"]["presentation_order"] == ["black", "gray_0016", "white"]
    red = doc["per_channel_response"]["red"]
    assert red[0]["code"] == 16
    assert red[0]["xyz"] == pytest.approx([0.1, 0.2, 0.3])
    assert red[1]["code"] == 3072
    assert doc["gray_response"][0]["code"] == 16
    assert doc["additivity"]["yellow"] == pytest.approx([1.0, 2.0, 3.0])
    assert doc["additivity"]["cyan"] == pytest.approx([4.0, 5.0, 6.0])
    assert doc["additivity"]["magenta"] == pytest.approx([7.0, 8.0, 9.0])


def test_protocol_sections_are_omitted_from_skeleton_artifacts() -> None:
    doc = yaml.safe_load(render(sample_artifact()))
    for key in ("protocol", "per_channel_response", "gray_response", "additivity"):
        assert key not in doc


def test_presentation_order_requires_protocol_name() -> None:
    with pytest.raises(ValueError, match="protocol_name"):
        replace(sample_artifact(), presentation_order=("black",))


def test_full_protocol_artifact_render_is_deterministic() -> None:
    assert render(full_protocol_artifact()) == render(full_protocol_artifact())


def routed_artifact() -> MeasurementsArtifact:
    return replace(
        full_protocol_artifact(),
        instrument_routing=InstrumentRouting(
            method="four-color-matrix",
            spectroradiometer=InstrumentIdentity(
                manufacturer="Colorimetry Research",
                model="CR-300",
                serial_number="A00322",
                firmware=None,
            ),
            colorimeter=InstrumentIdentity(
                manufacturer="Colorimetry Research",
                model="CR-120",
                serial_number="A00140",
                firmware=None,
            ),
            correction_matrix=(
                (1.0, 0.02, -0.01),
                (0.01, 0.98, 0.02),
                (-0.02, 0.03, 1.05),
            ),
            luminance_threshold=10.0,
            sources=(SOURCE_SPECTRORADIOMETER, SOURCE_COLORIMETER, SOURCE_COLORIMETER),
        ),
    )


def test_render_carries_the_routing_section() -> None:
    doc = yaml.safe_load(render(routed_artifact()))
    routing = doc["instrument_routing"]
    assert routing["method"] == "four-color-matrix"
    assert routing["spectroradiometer"]["serial_number"] == "A00322"
    assert routing["colorimeter"]["model"] == "CR-120"
    expected = [[1.0, 0.02, -0.01], [0.01, 0.98, 0.02], [-0.02, 0.03, 1.05]]
    for row, want in zip(routing["correction_matrix"], expected, strict=True):
        assert row == pytest.approx(want)
    assert routing["luminance_threshold"] == pytest.approx(10.0)
    assert routing["sources"] == ["spectroradiometer", "colorimeter", "colorimeter"]
    assert render(routed_artifact()) == render(routed_artifact())


def test_routing_is_omitted_from_single_instrument_artifacts() -> None:
    assert "instrument_routing" not in yaml.safe_load(render(sample_artifact()))
    assert "instrument_routing" not in yaml.safe_load(render(full_protocol_artifact()))


def test_routing_sources_parallel_the_presentation_order() -> None:
    routed = routed_artifact()
    assert routed.instrument_routing is not None
    short = replace(routed.instrument_routing, sources=(SOURCE_COLORIMETER,))
    with pytest.raises(ValueError, match="sources"):
        replace(routed, instrument_routing=short)
    with pytest.raises(ValueError, match="sources"):
        replace(sample_artifact(), instrument_routing=routed.instrument_routing)


def test_routing_refuses_an_unnamed_source() -> None:
    routed = routed_artifact()
    assert routed.instrument_routing is not None
    with pytest.raises(ValueError, match="thermometer"):
        replace(
            routed.instrument_routing,
            sources=("thermometer", SOURCE_COLORIMETER, SOURCE_COLORIMETER),
        )


def test_patch_seconds_render_and_length_validation() -> None:
    timed = replace(
        full_protocol_artifact(),
        patch_seconds=(0.5, 1.25, 2.0),
    )
    doc = yaml.safe_load(render(timed))
    assert doc["protocol"]["patch_seconds"] == pytest.approx([0.5, 1.25, 2.0])
    with pytest.raises(ValueError, match="patch_seconds"):
        replace(timed, patch_seconds=(0.5,))
    with pytest.raises(ValueError, match="patch_seconds"):
        replace(sample_artifact(), patch_seconds=(0.5,))
