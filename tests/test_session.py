"""Session-core tests against the device doubles (§spec:sessions)."""

import logging
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest
import yaml
from bmd_sg.decklink import EOTFType, MockBMDDeckLink, PixelFormatType

from display_measure.artifact import DECLARED_CONTRACT
from display_measure.consistency import InconsistentSession
from display_measure.hybrid import DERIVATION_PATCHES
from display_measure.processor import ContractViolation
from display_measure.protocol import (
    FULL_DRIVE,
    PROTOCOL_NAME,
    presentation_order,
    protocol_patches,
)
from display_measure.session import Clock, doubles_session


def method_calls(device: MockBMDDeckLink, method: str) -> list[dict[str, Any]]:
    # get_method_calls declares a union return; a named method always
    # yields the list branch.
    return cast("list[dict[str, Any]]", device.get_method_calls(method))


def test_identical_inputs_give_identical_bytes(
    display_artifact: Path, fixed_clock: Clock, tmp_path: Path
) -> None:
    second = tmp_path / "second.yaml"
    doubles_session(second, clock=fixed_clock, settle_seconds=0.0)
    assert second.read_bytes() == display_artifact.read_bytes()


def test_the_random_instrument_is_reachable_and_its_numbers_are_refused(
    fixed_clock: Clock, tmp_path: Path
) -> None:
    """The physics-free virtual spectrometer validates plumbing only.

    It ignores the driven frame, so its ramps do not rise and the
    self-consistency gate refuses to write an artifact from them
    (§road:session-consistency). Reaching that gate is what proves the
    seam: the session drove the protocol, read the instrument, assembled
    an artifact, and only then judged the numbers.

    There is deliberately no bypass. A flag that let a session skip this
    check would be used in anger the first time a rig misbehaved at
    2 a.m., which is precisely when the artifact matters most.
    """
    divergent = tmp_path / "random.yaml"
    with pytest.raises(InconsistentSession):
        doubles_session(divergent, clock=fixed_clock, settle_seconds=0.0, seed=0)
    assert not divergent.exists(), "a refused session writes no artifact"


def test_artifact_measurements_route_sensibly(display_artifact: Path) -> None:
    """The session routes each reading to its artifact field: the
    opening black feeds both the floor and the black level, white
    carries the peak, and the primaries are mutually distinct — the
    relations only the session (not the display model) can prove."""
    doc = yaml.safe_load(display_artifact.read_text())
    luminance = doc["luminance"]
    assert doc["ambient_floor"] == luminance["black_level"]
    assert luminance["peak_luminance"] / luminance["black_level"] > 100
    primaries = doc["colorimetry"]["primaries"]
    corners = {tuple(primaries[name]) for name in ("red", "green", "blue")}
    assert len(corners) == 3
    white = tuple(doc["colorimetry"]["white_point"])
    assert white not in corners


def test_patch_drive_uses_12bit_rgb_with_explicit_sdr_signaling(
    display_device: MockBMDDeckLink,
) -> None:
    formats = [
        call["format"] for call in method_calls(display_device, "set_pixel_format")
    ]
    assert formats == [PixelFormatType.FORMAT_12BIT_RGB]
    hdr_calls = method_calls(display_device, "set_hdr_metadata")
    assert len(hdr_calls) == 1
    assert EOTFType.SDR.int_value == hdr_calls[0]["metadata"].EOTF


def test_session_drives_the_full_protocol_in_presentation_order(
    display_device: MockBMDDeckLink,
) -> None:
    frames = display_device.get_frame_history()
    # The default doubles wiring shuffles with seed 0.
    expected = presentation_order(protocol_patches(), seed=0)
    assert len(frames) == len(expected)
    assert frames[0].max() == 0  # black opens the session
    for frame, patch in zip(frames, expected, strict=True):
        assert tuple(frame[0, 0]) == patch.rgb, patch.name


def test_artifact_records_the_protocol_and_unshuffle_key(
    display_artifact: Path,
) -> None:
    doc = yaml.safe_load(display_artifact.read_text())
    assert doc["protocol"]["name"] == PROTOCOL_NAME
    driven = [p.name for p in presentation_order(protocol_patches(), seed=0)]
    assert doc["protocol"]["presentation_order"] == driven
    for channel in ("red", "green", "blue"):
        ramp = doc["per_channel_response"][channel]
        assert [point["code"] for point in ramp] == sorted(
            point["code"] for point in ramp
        ), "response rows are protocol-ordered regardless of presentation"
        # The full-drive anchor reading doubles as the ramp's top row,
        # carrying the anchors' absolute XYZ into the artifact.
        assert ramp[-1]["code"] == FULL_DRIVE
    assert doc["gray_response"][-1]["code"] == FULL_DRIVE
    assert set(doc["additivity"]) == {"yellow", "cyan", "magenta"}
    assert len(doc["gray_response"]) == len(doc["per_channel_response"]["red"])


def test_every_stage_is_observable_in_the_session_log(display_log: str) -> None:
    for marker in (
        "contract audit: PASS",
        "ambient gate: STUB",
        "patch drive:",
        "settle:",
        "instrument read:",
        "handoff:",
        "sha256",
    ):
        assert marker in display_log


def test_artifact_output_path_is_immutable(
    display_artifact: Path, fixed_clock: Clock
) -> None:
    with pytest.raises(FileExistsError):
        doubles_session(display_artifact, clock=fixed_clock, settle_seconds=0.0)


def response_rows(doc: dict[str, Any]) -> dict[tuple[str, int], list[float]]:
    """Every ramp row keyed by (channel, code), for cross-session compare."""
    ramps = dict(doc["per_channel_response"], gray=doc["gray_response"])
    return {
        (channel, point["code"]): point["xyz"]
        for channel, ramp in ramps.items()
        for point in ramp
    }


def test_hybrid_session_leads_with_the_derivation_rungs(
    hybrid_artifact: Path,
) -> None:
    """The correction has to exist before any patch can be routed —
    black included — so the rungs it is derived from lead, and black
    follows them still ahead of the shuffle."""
    doc = yaml.safe_load(hybrid_artifact.read_text())
    order = doc["protocol"]["presentation_order"]
    assert order[:4] == [*DERIVATION_PATCHES, "black"]
    assert sorted(order) == sorted(p.name for p in protocol_patches())


def test_hybrid_artifact_attributes_every_row_to_an_instrument(
    hybrid_artifact: Path,
) -> None:
    doc = yaml.safe_load(hybrid_artifact.read_text())
    routing = doc["instrument_routing"]
    assert len(routing["sources"]) == len(doc["protocol"]["presentation_order"])
    assert set(routing["sources"]) == {"spectroradiometer", "colorimeter"}
    # The rungs are read by both instruments and recorded against the
    # reference; black routes like any other patch once they are in.
    assert routing["sources"][:3] == ["spectroradiometer"] * 3
    assert routing["colorimeter"]["model"] == "MismatchedColorimeter"
    assert routing["spectroradiometer"]["model"] == "PlausibleDisplay"
    assert doc["instrument"]["model"] == "disciplined-hybrid"


def test_disciplining_the_colorimeter_reproduces_the_spectro_only_session(
    hybrid_artifact: Path, display_artifact: Path
) -> None:
    """The threshold covers the whole ramp, so every held-out row comes
    from the corrected colorimeter — and lands on the values the
    spectro-only session measured (§spec:sessions)."""
    hybrid = yaml.safe_load(hybrid_artifact.read_text())
    spectro = yaml.safe_load(display_artifact.read_text())
    sources = hybrid["instrument_routing"]["sources"]
    # Everything but the three derivation rungs, black now included.
    assert sources.count("colorimeter") == len(sources) - 3
    for name in ("red", "green", "blue"):
        assert hybrid["colorimetry"]["primaries"][name] == pytest.approx(
            spectro["colorimetry"]["primaries"][name], rel=1e-8
        )
    assert hybrid["colorimetry"]["white_point"] == pytest.approx(
        spectro["colorimetry"]["white_point"], rel=1e-8
    )
    assert hybrid["luminance"] == pytest.approx(spectro["luminance"], rel=1e-8)
    hybrid_rows = response_rows(hybrid)
    spectro_rows = response_rows(spectro)
    assert set(hybrid_rows) == set(spectro_rows)
    for key, xyz in hybrid_rows.items():
        assert xyz == pytest.approx(spectro_rows[key], rel=1e-8), key


def test_single_instrument_sessions_record_no_routing(display_artifact: Path) -> None:
    assert "instrument_routing" not in yaml.safe_load(display_artifact.read_text())


def test_patch_seconds_cover_the_presentation_and_zero_under_fixed_clock(
    display_artifact: Path,
) -> None:
    doc = yaml.safe_load(display_artifact.read_text())
    seconds = doc["protocol"]["patch_seconds"]
    assert len(seconds) == len(doc["protocol"]["presentation_order"])
    # The doubles fixture injects a fixed clock; durations measured by
    # the session clock render as zeros, keeping reproduction runs
    # byte-deterministic.
    assert all(s == 0.0 for s in seconds)


def test_a_hybrid_session_reads_black_through_the_colorimeter(
    hybrid_artifact: Path,
) -> None:
    """Black is the session's most expensive read and the colorimeter is
    the better instrument there (§road:instrument-floors), so the
    derivation rungs lead and black routes like any other dark patch."""
    doc = yaml.safe_load(hybrid_artifact.read_text())
    order = doc["protocol"]["presentation_order"]
    sources = dict(zip(order, doc["instrument_routing"]["sources"], strict=True))
    assert order[:3] == list(DERIVATION_PATCHES), "the rungs lead a hybrid session"
    assert order[3] == "black", "black follows them, before the shuffle"
    assert sources["black"] == "colorimeter"


def test_a_single_instrument_session_still_opens_on_black(
    display_artifact: Path,
) -> None:
    """No derivation to pin, so the order is what protocol 1 drove."""
    doc = yaml.safe_load(display_artifact.read_text())
    assert doc["protocol"]["presentation_order"][0] == "black"


def test_the_ambient_floor_is_the_black_reading_wherever_black_lands(
    hybrid_artifact: Path,
) -> None:
    doc = yaml.safe_load(hybrid_artifact.read_text())
    assert doc["ambient_floor"] == pytest.approx(doc["luminance"]["black_level"])


def test_a_display_far_off_its_declared_intensity_refuses_before_the_protocol_runs(
    fixed_clock: Clock, tmp_path: Path
) -> None:
    """The gate the 2026-08-28 run needed, and where it needed it.

    The plausible display peaks at 1000 cd/m²; declaring 1800 nits makes it
    0.56x, the same shortfall Studio Mode produced on the bench. Protocol
    3 pins white second, so this refuses two patches in rather than at
    handoff.
    """
    declared = replace(DECLARED_CONTRACT, intensity="1800 nits")
    out = tmp_path / "refused.yaml"
    with pytest.raises(ContractViolation) as e:
        doubles_session(out, clock=fixed_clock, settle_seconds=0.0, declared=declared)
    assert "1800" in str(e.value)
    assert not out.exists(), "a refused session writes no artifact"


def test_the_level_gate_stops_the_session_at_white(
    fixed_clock: Clock, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Two patches driven, not seventy-two. Early refusal is the feature:
    the 2026-08-28 run spent twenty minutes measuring through a fault it
    could have named at the second patch."""
    declared = replace(DECLARED_CONTRACT, intensity="1800 nits")
    with (
        caplog.at_level(logging.INFO, logger="display_measure"),
        pytest.raises(ContractViolation),
    ):
        doubles_session(
            tmp_path / "refused.yaml",
            clock=fixed_clock,
            settle_seconds=0.0,
            declared=declared,
        )
    driven = [
        r.getMessage()
        for r in caplog.records
        if r.getMessage().startswith("patch drive: ")
    ]
    assert len(driven) == 2, driven
    assert "black" in driven[0] and "white" in driven[1]
