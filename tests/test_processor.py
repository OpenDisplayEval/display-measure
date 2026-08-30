"""Contract audit gate: refuse a session the processor contradicts.

The bench lost a full 72-patch run to a processor left at 66 nits after
an unrelated test (§road:processor-state-snapshot). The session recorded
a declared contract it never checked, drove every patch, and emitted an
artifact. These tests pin the refusals that would have stopped it at
patch zero.
"""

from pathlib import Path
from typing import Any

import pytest

from display_measure.artifact import ProcessorStateSnapshot
from display_measure.processor import (
    ContractViolation,
    InputMetadata,
    WireFormat,
    audit_contract,
    audit_output_level,
    audit_output_scaling,
    audit_wire_format,
    contract_from_manifest,
    state_from_tessera,
)
from display_measure.wire import RGB12, V210

# The bench contract the show manifest declares (§spec:signal-contract).
DECLARED = ProcessorStateSnapshot(
    eotf_type="GAMMA",
    gamma_value=2.35,
    intensity="1800 nits",
    processing_enabled=frozenset(),
)

# The Tessera subtree shape `GET /api/output/global-colour` answers, cut
# to the leaves the contract reads.
NORMALIZED_TREE = {
    "brightness": 1800,
    "gamma": 2.35,
    "dark-magic": {"enabled": False},
    "puretone": {"enabled": False},
    "extended-bit-depth": {"enabled": False},
    "overdrive": {"enabled": False},
}

DECLARED_WIRE = WireFormat.for_encoding(RGB12)


def tree(**overrides: object) -> dict[str, Any]:
    return {**NORMALIZED_TREE, **overrides}


def test_normalized_processor_passes_and_records_live_state() -> None:
    live = state_from_tessera(tree())
    recorded = audit_contract(DECLARED, live)
    assert recorded == live
    assert recorded.intensity == "1800 nits"
    assert recorded.gamma_value == 2.35
    assert recorded.processing_disabled is True


def test_brightness_divergence_refuses() -> None:
    """The exact failure of 2026-08-28: 66 nits against 1800 declared."""
    live = state_from_tessera(tree(brightness=66))
    with pytest.raises(ContractViolation) as e:
        audit_contract(DECLARED, live)
    assert "intensity" in str(e.value)
    assert "66 nits" in str(e.value) and "1800 nits" in str(e.value)


def test_gamma_divergence_refuses() -> None:
    live = state_from_tessera(tree(gamma=2.4))
    with pytest.raises(ContractViolation) as e:
        audit_contract(DECLARED, live)
    assert "gamma" in str(e.value)


@pytest.mark.parametrize(
    "feature", ["dark-magic", "puretone", "extended-bit-depth", "overdrive"]
)
def test_any_enabled_processing_feature_refuses(feature: str) -> None:
    """`processing_disabled: true` is a claim about every feature, not a mood."""
    live = state_from_tessera(tree(**{feature: {"enabled": True}}))
    with pytest.raises(ContractViolation) as e:
        audit_contract(DECLARED, live)
    assert "processing" in str(e.value)
    assert feature in str(e.value)


def test_violations_are_reported_together() -> None:
    """One refusal names every divergence — not one trip to the display each."""
    live = state_from_tessera(
        tree(brightness=66, gamma=2.4, **{"dark-magic": {"enabled": True}})
    )
    with pytest.raises(ContractViolation) as e:
        audit_contract(DECLARED, live)
    message = str(e.value)
    assert "intensity" in message and "gamma" in message and "processing" in message


def test_unreachable_processor_refuses_rather_than_assuming() -> None:
    """A session that cannot read the processor has not audited it."""
    with pytest.raises(ContractViolation) as e:
        audit_contract(DECLARED, None)
    assert "unreachable" in str(e.value).lower()


def test_input_metadata_contradicting_the_wire_format_refuses() -> None:
    """The processor's own view of the link, against what the session drives."""
    live = InputMetadata(
        bit_depth=10, sampling="rgb", hdr_format="standard-dynamic-range"
    )
    with pytest.raises(ContractViolation) as e:
        audit_wire_format(DECLARED_WIRE, live)
    assert "bit" in str(e.value).lower()


def test_input_metadata_matching_the_wire_format_passes() -> None:
    live = InputMetadata(
        bit_depth=12, sampling="rgb", hdr_format="standard-dynamic-range"
    )
    audit_wire_format(DECLARED_WIRE, live)


def test_a_v210_declaration_is_refused_unless_the_processor_sees_10bit_ycbcr() -> None:
    """The bench link is 12-bit RGB; a session declaring v210 over it
    would bake the processor's own decode into the measurement."""
    declared = WireFormat.for_encoding(V210)
    bench = InputMetadata(
        bit_depth=12, sampling="rgb", hdr_format="standard-dynamic-range"
    )
    with pytest.raises(ContractViolation) as e:
        audit_wire_format(declared, bench)
    message = str(e.value)
    assert "10-bit" in message and "12-bit" in message
    assert "ycbcr" in message and "rgb" in message
    audit_wire_format(
        declared,
        InputMetadata(
            bit_depth=10, sampling="ycbcr", hdr_format="standard-dynamic-range"
        ),
    )


def test_a_link_that_publishes_nothing_is_reported_not_passed() -> None:
    """SDI answers for none of the three fields.

    The gate must neither crash on the absent keys nor read silence as
    agreement: it comes back naming what it could not check, which is
    what the session records and says out loud.
    """
    unverified = audit_wire_format(WireFormat.for_encoding(V210), InputMetadata())
    assert unverified == ("bit depth", "sampling", "hdr signalling")


def test_a_partly_published_link_audits_what_it_publishes() -> None:
    """A field the processor answers is still a gate; only the silent ones pass."""
    live = InputMetadata(bit_depth=10, sampling=None, hdr_format=None)
    assert audit_wire_format(WireFormat.for_encoding(V210), live) == (
        "sampling",
        "hdr signalling",
    )
    with pytest.raises(ContractViolation) as e:
        audit_wire_format(
            WireFormat.for_encoding(V210),
            InputMetadata(bit_depth=12, sampling=None, hdr_format=None),
        )
    assert "10-bit" in str(e.value) and "12-bit" in str(e.value)


def test_pq_signalling_under_an_sdr_contract_refuses() -> None:
    live = InputMetadata(bit_depth=12, sampling="rgb", hdr_format="pq")
    with pytest.raises(ContractViolation) as e:
        audit_wire_format(DECLARED_WIRE, live)
    assert "hdr" in str(e.value).lower() or "pq" in str(e.value).lower()


# --- the declared side: a contract nobody typed twice ---------------------

MANIFEST = """
show:
  description: "FTG Stage 1"
signal_contract:
  eotf:
    type: "GAMMA"
    gamma_value: 2.35
  intensity: "1800 nits"
  processing_disabled: true
"""


def test_declared_contract_comes_from_the_show_manifest(tmp_path: Path) -> None:
    """The manifest is the human-authored source of truth (§spec:provenance).

    A contract retyped into a CLI flag is a second source that can drift
    from the one the config was generated against.
    """
    path = tmp_path / "show_manifest.yaml"
    path.write_text(PER_FEATURE_MANIFEST)
    declared = contract_from_manifest(path)
    assert declared.eotf_type == "GAMMA"
    assert declared.gamma_value == 2.35
    assert declared.intensity == "1800 nits"
    assert declared.processing_enabled == frozenset(
        {"dark-magic", "puretone", "extended-bit-depth"}
    )


def test_bench_manifest_against_the_live_processor_isolates_the_real_faults(
    tmp_path: Path,
) -> None:
    """With the true contract declared, only the genuine divergences remain.

    Gamma matches at 2.35; what is left is the brightness the bench was
    left at and the processing features nobody turned off.
    """
    declared = bench_contract(tmp_path)
    live = state_from_tessera(
        tree(
            brightness=66,
            **{
                "dark-magic": {"enabled": True},
                "puretone": {"enabled": True},
                "extended-bit-depth": {"enabled": True},
            },
        )
    )
    with pytest.raises(ContractViolation) as e:
        audit_contract(declared, live)
    message = str(e.value)
    assert "gamma" not in message
    assert "66 nits" in message
    # The features match the manifest, so only the brightness is named.
    assert "processing/" not in message


def test_manifest_without_a_signal_contract_refuses(tmp_path: Path) -> None:
    path = tmp_path / "show_manifest.yaml"
    path.write_text("show:\n  description: 'no contract here'\n")
    with pytest.raises(ContractViolation) as e:
        contract_from_manifest(path)
    assert "signal_contract" in str(e.value)


# --- per-feature processing contract --------------------------------------

PER_FEATURE_MANIFEST = """
signal_contract:
  eotf:
    type: "GAMMA"
    gamma_value: 2.35
  intensity: "1800 nits"
  panel_state:
    operating_mode: "Default"
    selected_calibration: "Internal Colour Cal (Factory)"
  processing:
    dark-magic: true
    puretone: true
    extended-bit-depth: true
    overdrive: false
"""


def bench_contract(tmp_path: Path) -> ProcessorStateSnapshot:
    path = tmp_path / "show_manifest.yaml"
    path.write_text(PER_FEATURE_MANIFEST)
    return contract_from_manifest(path)


def test_the_shipping_config_passes(tmp_path: Path) -> None:
    """Dark Magic and PureTone on is the display the show runs, not a fault.

    They correct LED driver non-linearity and PWM resolution loss below
    the layer any OCIO config can reach; a gate that refused them would
    refuse the only configuration worth characterizing.
    """
    declared = bench_contract(tmp_path)
    live = state_from_tessera(
        tree(
            **{
                "dark-magic": {"enabled": True},
                "puretone": {"enabled": True},
                "extended-bit-depth": {"enabled": True},
                "overdrive": {"enabled": False},
            }
        )
    )
    recorded = audit_contract(declared, live)
    assert recorded.processing_enabled == frozenset(
        {"dark-magic", "puretone", "extended-bit-depth"}
    )


def test_processing_disabled_means_no_content_dependent_processing(
    tmp_path: Path,
) -> None:
    """The boolean ocio-display-gen reads now says something true.

    Dark Magic and PureTone are static per-pixel transfer — a
    characterization measures them. Overdrive is frame-adaptive and
    breaks the memoryless code-to-light assumption outright.
    """
    static_on = state_from_tessera(
        tree(**{"dark-magic": {"enabled": True}, "puretone": {"enabled": True}})
    )
    assert static_on.processing_disabled is True

    adaptive_on = state_from_tessera(tree(**{"overdrive": {"enabled": True}}))
    assert adaptive_on.processing_disabled is False


def test_a_feature_off_when_declared_on_refuses(tmp_path: Path) -> None:
    """Drift in either direction is drift; PureTone silently off is a
    different display from the one the manifest describes."""
    declared = bench_contract(tmp_path)
    live = state_from_tessera(
        tree(
            **{
                "dark-magic": {"enabled": True},
                "puretone": {"enabled": False},
                "extended-bit-depth": {"enabled": True},
            }
        )
    )
    with pytest.raises(ContractViolation) as e:
        audit_contract(declared, live)
    assert "puretone" in str(e.value)
    assert "declared on" in str(e.value) and "processor has it off" in str(e.value)


def test_content_dependent_processing_on_refuses(tmp_path: Path) -> None:
    declared = bench_contract(tmp_path)
    live = state_from_tessera(
        tree(
            **{
                "dark-magic": {"enabled": True},
                "puretone": {"enabled": True},
                "extended-bit-depth": {"enabled": True},
                "overdrive": {"enabled": True},
            }
        )
    )
    with pytest.raises(ContractViolation) as e:
        audit_contract(declared, live)
    assert "overdrive" in str(e.value)


def test_a_manifest_that_omits_a_known_feature_refuses(tmp_path: Path) -> None:
    """Silence is how the bench lost a run. A feature nobody declared is a
    feature nobody checked, so adding one to the tool forces a manifest
    update rather than silently un-gating it.
    """
    path = tmp_path / "show_manifest.yaml"
    path.write_text(PER_FEATURE_MANIFEST.replace("    puretone: true\n", ""))
    with pytest.raises(ContractViolation) as e:
        contract_from_manifest(path)
    assert "puretone" in str(e.value)
    assert "does not declare" in str(e.value)


def test_the_legacy_boolean_manifest_refuses_with_a_migration_message(
    tmp_path: Path,
) -> None:
    """`processing_disabled: true` conflated two categories; it cannot be
    translated into per-feature intent, so it is refused, not guessed."""
    path = tmp_path / "show_manifest.yaml"
    path.write_text(MANIFEST)
    with pytest.raises(ContractViolation) as e:
        contract_from_manifest(path)
    assert "processing:" in str(e.value)


# --- output scaling: one luminance knob, and it is `brightness` ----------


def scaled(**overrides: object) -> dict[str, Any]:
    """A normalized `global-colour` subtree plus the scaling leaves."""
    return {
        **NORMALIZED_TREE,
        "gains": {"intensity": 100, "red": 100, "green": 100, "blue": 100},
        "brightness-limit": {"enabled": False, "value": 10000},
        **overrides,
    }


def test_neutral_gains_and_no_limit_pass() -> None:
    audit_output_scaling(scaled())


def test_an_intensity_gain_below_full_refuses() -> None:
    """`brightness` is not the only luminance knob, and the other one is
    invisible in the declared contract: a 50% intensity gain halves the
    display while `brightness` still reads 1800."""
    with pytest.raises(ContractViolation) as e:
        audit_output_scaling(
            scaled(gains={"intensity": 50, "red": 100, "green": 100, "blue": 100})
        )
    assert "intensity" in str(e.value) and "50" in str(e.value)


@pytest.mark.parametrize("channel", ["red", "green", "blue"])
def test_an_unbalanced_channel_gain_refuses(channel: str) -> None:
    """A per-channel gain moves the white point without touching a single
    value the contract declares."""
    gains = {"intensity": 100, "red": 100, "green": 100, "blue": 100}
    gains[channel] = 90
    with pytest.raises(ContractViolation) as e:
        audit_output_scaling(scaled(gains=gains))
    assert channel in str(e.value)


def test_a_brightness_limit_below_the_setpoint_refuses() -> None:
    """An enabled limit clamps the output below what `brightness` claims."""
    with pytest.raises(ContractViolation) as e:
        audit_output_scaling(
            scaled(
                brightness=1800, **{"brightness-limit": {"enabled": True, "value": 600}}
            )
        )
    assert "limit" in str(e.value).lower() and "600" in str(e.value)


def test_a_brightness_limit_above_the_setpoint_passes() -> None:
    """Enabled but not binding: it clamps nothing the session will drive."""
    audit_output_scaling(
        scaled(
            brightness=1800, **{"brightness-limit": {"enabled": True, "value": 4000}}
        )
    )


# --- operating mode: attested, because the API cannot see it -------------


def test_panel_state_is_read_from_the_manifest(tmp_path: Path) -> None:
    path = tmp_path / "show_manifest.yaml"
    path.write_text(PER_FEATURE_MANIFEST)
    declared = contract_from_manifest(path)
    assert declared.panel_state == (
        ("operating_mode", "Default"),
        ("selected_calibration", "Internal Colour Cal (Factory)"),
    )


@pytest.mark.parametrize("key", ["operating_mode", "selected_calibration"])
def test_a_manifest_missing_a_panel_state_key_refuses(tmp_path: Path, key: str) -> None:
    """Both move the measurement and neither is readable.

    Switching Selected Calibration on the bench moved white 1035 to 1476
    cd/m² and left all 302 leaves of the processor's API identical but
    for its clock and chassis temperatures. Studio Mode is the same class
    of invisibility. The operator is the only instrument for either.
    """
    path = tmp_path / "show_manifest.yaml"
    stripped = "\n".join(
        line for line in PER_FEATURE_MANIFEST.splitlines() if f"{key}:" not in line
    )
    path.write_text(stripped)
    with pytest.raises(ContractViolation) as e:
        contract_from_manifest(path)
    assert key in str(e.value)


def test_a_manifest_without_a_panel_state_block_refuses(tmp_path: Path) -> None:
    path = tmp_path / "show_manifest.yaml"
    path.write_text(
        PER_FEATURE_MANIFEST[: PER_FEATURE_MANIFEST.index("  panel_state:")]
        + PER_FEATURE_MANIFEST[PER_FEATURE_MANIFEST.index("  processing:") :]
    )
    with pytest.raises(ContractViolation) as e:
        contract_from_manifest(path)
    assert "panel_state" in str(e.value)


def test_panel_state_is_never_compared_against_the_processor(
    tmp_path: Path,
) -> None:
    """Attestation, not reading. A live snapshot carries no panel state at
    all, and the audit shall not pretend to have checked it."""
    path = tmp_path / "show_manifest.yaml"
    path.write_text(PER_FEATURE_MANIFEST)
    declared = contract_from_manifest(path)
    live = state_from_tessera(
        tree(
            **{
                "dark-magic": {"enabled": True},
                "puretone": {"enabled": True},
                "extended-bit-depth": {"enabled": True},
            }
        )
    )
    assert live.panel_state == ()
    recorded = audit_contract(declared, live)
    # Carried forward, so the artifact records what only a human could say.
    assert dict(recorded.panel_state)["selected_calibration"] == (
        "Internal Colour Cal (Factory)"
    )


# --- the consequence gate: does the display do what the processor claims? ----


class TestOutputLevel:
    """Sane-defaults plausibility on the measured white anchor.

    The contract audit reads what the processor claims. This asks whether
    the display actually does it — and catches, without naming, anything the
    API cannot see: Studio Mode, a moved instrument, thermal state, an
    aperture off the panel edge.
    """

    def test_a_peak_near_the_declared_intensity_passes(self) -> None:
        audit_output_level(1927.5, "1800 nits")

    def test_the_studio_mode_run_is_refused(self) -> None:
        """2026-08-28: 1035 cd/m² against 1800 declared, because the panel
        was left in Studio Mode after a low-brightness test."""
        with pytest.raises(ContractViolation) as e:
            audit_output_level(1035.2, "1800 nits")
        assert "1035" in str(e.value) and "1800" in str(e.value)

    def test_a_display_brighter_than_declared_is_refused(self) -> None:
        with pytest.raises(ContractViolation) as e:
            audit_output_level(3600.0, "1800 nits")
        assert "3600" in str(e.value)

    def test_a_dark_display_is_refused_even_without_a_nits_declaration(self) -> None:
        """No signal, a blocked aperture, or an instrument pointed at the
        room reads far too low to be a display showing white — and that is
        knowable without any declaration to compare against."""
        with pytest.raises(ContractViolation) as e:
            audit_output_level(0.4, "100%")
        assert "0.4" in str(e.value)

    def test_a_percentage_declaration_keeps_only_the_absolute_check(self) -> None:
        """`100%` states no luminance, so the fractional check has nothing
        to compare against; the sane-default bounds still apply."""
        audit_output_level(1900.0, "100%")
