"""The contract audit gate: what the processor is, against what was declared.

A session's numbers mean nothing without the processor state that produced
them (§spec:signal-contract). The gate reads that state over the Tessera
HTTP API — read-only, always (§spec:sessions) — and refuses the session on
any divergence from the declared contract, before a single patch is driven.

The refusal is the point. On 2026-08-28 the bench ran a full 72-patch
protocol against a processor left at 66 nits after an unrelated test: the
artifact recorded a declared contract nobody checked, and the run was
lost (§road:processor-state-snapshot). Reading the state is cheap; driving
72 patches at the wrong one is twenty minutes and a display.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

from display_measure.artifact import (
    CONTENT_DEPENDENT_FEATURES,
    PANEL_STATE_KEYS,
    PROCESSING_FEATURES,
    ProcessorStateSnapshot,
)

__all__ = [
    "ContractViolation",
    "InputMetadata",
    "TesseraProcessor",
    "WireFormat",
    "audit_contract",
    "audit_output_level",
    "audit_output_scaling",
    "audit_wire_format",
    "contract_from_manifest",
    "state_from_tessera",
]

log = logging.getLogger("display_measure.processor")

# Gamma is a float off an HTTP API; compare at the precision the processor
# reports rather than by identity.
GAMMA_TOLERANCE = 0.001

# Neutral position for every output gain the processor exposes, percent.
FULL_GAIN = 100

# How far the measured white may sit from the declared intensity. Wide,
# because it is a plausibility band and not a specification: the bench
# display measured 1.07x its 1800-nit setpoint in a healthy August run, and
# 0.58x with the panel left in Studio Mode. The band admits the first and
# refuses the second.
MIN_PEAK_FRACTION = 0.70
MAX_PEAK_FRACTION = 1.30

# Below this, no declaration is needed to know something is wrong: a display
# showing full white does not read a fraction of a nit.
MIN_PLAUSIBLE_PEAK = 1.0

HTTP_TIMEOUT_SECONDS = 5.0


class ContractViolation(RuntimeError):
    """The processor contradicts the declared contract, so the session stops."""


@dataclass(frozen=True)
class WireFormat:
    """The link the session declares it drives (§spec:signal-contract)."""

    bit_depth: int
    sampling: str
    hdr_format: str


@dataclass(frozen=True)
class InputMetadata:
    """The processor's own view of the link, read back from the active port."""

    bit_depth: int
    sampling: str
    hdr_format: str


def contract_from_manifest(path: Path) -> ProcessorStateSnapshot:
    """The declared contract, read from a show manifest's `signal_contract`.

    The manifest is the human-authored, reviewed source of truth for the
    processor lockdown the generated config is valid for (§spec:provenance).
    Reading it here keeps the session and the config generator auditing the
    same declaration, rather than an operator retyping it into a flag where
    it can drift.
    """
    # Deferred: the artifact writer emits YAML by hand, so nothing else on
    # the session path pays for the parser.
    import yaml

    document = yaml.safe_load(path.read_text()) or {}
    contract = document.get("signal_contract")
    if not isinstance(contract, dict):
        raise ContractViolation(
            f"{path} declares no signal_contract; a session cannot audit a "
            "contract the manifest does not state (§spec:signal-contract)"
        )
    processing = contract.get("processing")
    if not isinstance(processing, dict):
        raise ContractViolation(
            f"{path} declares no `signal_contract.processing:` block. A single "
            "`processing_disabled` boolean conflated static linearization "
            "(dark-magic, puretone, extended-bit-depth — part of the display, "
            "and measured) with frame-adaptive processing (overdrive — which "
            "breaks the measurement), so it cannot be translated into "
            "per-feature intent. Declare each of "
            f"{list(PROCESSING_FEATURES)} as true or false."
        )
    undeclared = [f for f in PROCESSING_FEATURES if f not in processing]
    if undeclared:
        raise ContractViolation(
            f"{path} does not declare {undeclared}. A feature nobody declared "
            "is a feature nobody checked; state it as true or false so the "
            "gate can hold the processor to it."
        )
    panel = contract.get("panel_state")
    if not isinstance(panel, dict):
        raise ContractViolation(
            f"{path} declares no signal_contract.panel_state block. Panel "
            f"state ({', '.join(PANEL_STATE_KEYS)}) moves the measurement and "
            "no leaf of the processor's API reports it, so the operator is "
            "the only instrument that can state it."
        )
    missing = [k for k in PANEL_STATE_KEYS if not panel.get(k)]
    if missing:
        raise ContractViolation(
            f"{path} does not declare signal_contract.panel_state.{missing}. "
            "Each moves the measurement and none is readable from the "
            "processor; state it as it reads on the panel OSD."
        )
    eotf = contract.get("eotf") or {}
    return ProcessorStateSnapshot(
        eotf_type=str(eotf.get("type")),
        gamma_value=(
            float(eotf["gamma_value"]) if eotf.get("gamma_value") is not None else None
        ),
        intensity=str(contract["intensity"]),
        processing_enabled=frozenset(
            f for f in PROCESSING_FEATURES if bool(processing[f])
        ),
        panel_state=tuple((k, str(panel[k])) for k in PANEL_STATE_KEYS),
    )


def state_from_tessera(global_colour: dict[str, Any]) -> ProcessorStateSnapshot:
    """Map a `GET /api/output/global-colour` subtree to the recorded state.

    `intensity` carries the processor's brightness verbatim in nits: the
    manifest declares nits, and a percentage would not survive comparison
    against it.
    """
    return ProcessorStateSnapshot(
        eotf_type="GAMMA",
        gamma_value=float(global_colour["gamma"]),
        intensity=f"{global_colour['brightness']} nits",
        processing_enabled=frozenset(enabled_processing(global_colour)),
    )


def _feature_enabled(global_colour: dict[str, Any], feature: str) -> bool:
    node = global_colour.get(feature)
    return bool(node.get("enabled")) if isinstance(node, dict) else False


def enabled_processing(global_colour: dict[str, Any]) -> list[str]:
    """The processing features the processor currently has on, named."""
    return [f for f in PROCESSING_FEATURES if _feature_enabled(global_colour, f)]


def audit_contract(
    declared: ProcessorStateSnapshot,
    snapshot: ProcessorStateSnapshot | None,
) -> ProcessorStateSnapshot:
    """Refuse unless the live processor matches `declared`; return what to record.

    Raises `ContractViolation` listing every divergence at once — an
    operator walking to the processor should fix all of them in one trip.
    """
    if snapshot is None:
        raise ContractViolation(
            "processor unreachable: the session cannot audit a contract it "
            "cannot read (§road:processor-state-snapshot). Pass --processor "
            "with the Tessera host."
        )

    problems: list[str] = []
    if declared.eotf_type != snapshot.eotf_type:
        problems.append(
            f"eotf: declared {declared.eotf_type}, processor {snapshot.eotf_type}"
        )
    if not _gamma_matches(declared.gamma_value, snapshot.gamma_value):
        problems.append(
            f"gamma: declared {declared.gamma_value}, processor {snapshot.gamma_value}"
        )
    if declared.intensity != snapshot.intensity:
        problems.append(
            f"intensity: declared {declared.intensity}, processor {snapshot.intensity}"
        )
    for feature in PROCESSING_FEATURES:
        want, got = declared.enabled(feature), snapshot.enabled(feature)
        if want == got:
            continue
        adaptive = (
            " — frame-adaptive processing stops one code implying one luminance"
            if feature in CONTENT_DEPENDENT_FEATURES and got
            else ""
        )
        problems.append(
            f"processing/{feature}: declared {'on' if want else 'off'}, "
            f"processor has it {'on' if got else 'off'}{adaptive}"
        )

    if problems:
        raise ContractViolation(
            "processor state contradicts the declared contract; refusing to "
            "measure:\n  - " + "\n  - ".join(problems)
        )

    # The attestation is not comparable, so it is carried rather than
    # checked: the recorded state is what the processor said, plus the one
    # field only a human could report.
    return replace(snapshot, panel_state=declared.panel_state)


def _gamma_matches(declared: float | None, live: float | None) -> bool:
    if declared is None or live is None:
        return declared is live
    return abs(declared - live) <= GAMMA_TOLERANCE


def audit_output_level(measured_peak: float, declared_intensity: str) -> None:
    """Sane-defaults plausibility on the display's measured white.

    The contract audit reads what the processor claims; this asks whether
    the display does it. The check is on the consequence, so it needs no name
    for the cause — it catches Studio Mode, a moved instrument, thermal
    state, an aperture off the panel edge, and whatever else is invisible
    to the API, all through the one number they all move.

    That generality is the point. The 2026-08-28 bench run passed every
    contract check and still measured 1035 cd/m² against a display declared
    at 1800, because the panel was left in an operating mode no leaf of
    the processor's API reports (§spec:signal-contract).

    Two checks, both defaults rather than declarations:

    - Against the declared intensity, when it states nits. A display that
      lands far off its own setpoint is not the display the manifest
      describes, whatever the reason.
    - Absolute, always. A display showing full white does not read a
      fraction of a nit; that is no signal, a blocked aperture, or an
      instrument pointed at the room, and it is knowable with nothing to
      compare against.
    """
    problems: list[str] = []

    if measured_peak < MIN_PLAUSIBLE_PEAK:
        problems.append(
            f"white measured {measured_peak:.4g} cd/m², below the "
            f"{MIN_PLAUSIBLE_PEAK:g} cd/m² any lit display clears — no signal, a "
            "blocked aperture, or an instrument that is not looking at the display"
        )

    declared_nits = _declared_nits(declared_intensity)
    if declared_nits is not None and measured_peak >= MIN_PLAUSIBLE_PEAK:
        fraction = measured_peak / declared_nits
        if not MIN_PEAK_FRACTION <= fraction <= MAX_PEAK_FRACTION:
            problems.append(
                f"white measured {measured_peak:.4g} cd/m² against "
                f"{declared_nits:g} declared ({fraction:.2f}x, outside "
                f"{MIN_PEAK_FRACTION:g}-{MAX_PEAK_FRACTION:g}x) — the "
                "processor reports the declared intensity, so the display is not "
                "doing what the processor says. Check the panel operating "
                "mode, the instrument's aim and aperture, and the display's "
                "thermal state"
            )

    if problems:
        raise ContractViolation(
            "the measured display contradicts the declared contract; refusing to "
            "measure:\n  - " + "\n  - ".join(problems)
        )


def _declared_nits(declared_intensity: str) -> float | None:
    """The nits a declared intensity states, or None when it states none.

    `"1800 nits"` is a luminance; `"100%"` is a fraction of a maximum the
    manifest never gives, so it grounds no comparison.
    """
    text = declared_intensity.strip().lower()
    if text.endswith("%"):
        return None
    head = text.split()[0] if text.split() else ""
    try:
        return float(head)
    except ValueError:
        return None


def audit_output_scaling(global_colour: dict[str, Any]) -> None:
    """Refuse unless `brightness` is the only thing scaling the output.

    The contract declares one luminance figure, but the processor has
    several knobs that reach the same place: a percentage intensity gain,
    three per-channel gains, and a brightness limit that clamps the
    setpoint. A 50% intensity gain halves the display while `brightness`
    still reads 1800, and a per-channel gain moves the white point
    without touching any value the manifest states.

    Rather than declare all of them, require them neutral: an operator who
    wants half the light sets `brightness`, which the contract records and
    the gate checks. One knob, declared, auditable.
    """
    problems: list[str] = []
    gains = global_colour.get("gains") or {}
    for knob in ("intensity", "red", "green", "blue"):
        value = gains.get(knob, FULL_GAIN)
        if value != FULL_GAIN:
            problems.append(
                f"gains/{knob}: {value}, not {FULL_GAIN} — set the luminance "
                "with brightness, which the contract declares"
            )

    limit = global_colour.get("brightness-limit") or {}
    brightness = global_colour.get("brightness")
    if (
        limit.get("enabled")
        and brightness is not None
        and limit.get("value") is not None
        and limit["value"] < brightness
    ):
        problems.append(
            f"brightness-limit: {limit['value']} clamps the {brightness} "
            "setpoint, so the display never reaches the declared intensity"
        )

    if problems:
        raise ContractViolation(
            "the processor scales the output outside the declared contract; "
            "refusing to measure:\n  - " + "\n  - ".join(problems)
        )


def audit_wire_format(declared: WireFormat, live: InputMetadata) -> None:
    """Refuse unless the processor sees the link the session declares it drives.

    A declared 12-bit RGB SDR link that the processor reports as 10-bit, or
    as PQ, bakes its own quantization and transfer into the "measured"
    response (§req:wire-format).
    """
    problems: list[str] = []
    if declared.bit_depth != live.bit_depth:
        problems.append(
            f"bit depth: session drives {declared.bit_depth}-bit, "
            f"processor receives {live.bit_depth}-bit"
        )
    if declared.sampling != live.sampling:
        problems.append(
            f"sampling: session drives {declared.sampling}, "
            f"processor receives {live.sampling}"
        )
    if declared.hdr_format != live.hdr_format:
        problems.append(
            f"hdr signalling: session declares {declared.hdr_format}, "
            f"processor receives {live.hdr_format}"
        )
    if problems:
        raise ContractViolation(
            "the processor's input metadata contradicts the declared wire "
            "format; refusing to measure:\n  - " + "\n  - ".join(problems)
        )


class TesseraProcessor:
    """Read-only Brompton Tessera HTTP client (§spec:sessions).

    Reads are `GET /api/<path>`. There is deliberately no write surface
    here: the tool observes and refuses, and never mutates show hardware.
    """

    def __init__(self, host: str, *, timeout: float = HTTP_TIMEOUT_SECONDS) -> None:
        self.host = host
        self.timeout = timeout

    def _get(self, path: str) -> Any:
        url = f"http://{self.host}/api/{path}"
        try:
            with urllib.request.urlopen(url, timeout=self.timeout) as response:
                return json.loads(response.read())
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            raise ContractViolation(f"processor unreachable at {self.host}: {e}") from e

    def global_colour(self) -> dict[str, Any]:
        """The `output/global-colour` subtree, unwrapped."""
        return dict(self._get("output/global-colour")["global-colour"])

    def read_state(self) -> ProcessorStateSnapshot:
        """The processor's colour state as the artifact records it."""
        return state_from_tessera(self.global_colour())

    def input_metadata(self) -> InputMetadata:
        """What the processor reports it is receiving on the active port."""
        active = self._get("input/active/source")["source"]
        port_type, port = active["port-type"], active["port-number"]
        ports = self._get(f"input/ports/{port_type}")[port_type]
        # Tessera numbers the tree from 0 while `active` reports from 1 on
        # the S8; fall back rather than guess which the firmware means.
        node = ports.get(str(port - 1)) or ports.get(str(port))
        if node is None:
            raise ContractViolation(
                f"processor reports active {port_type} port {port}, which is "
                f"absent from input/ports/{port_type}"
            )
        meta = node["meta-data"]
        return InputMetadata(
            bit_depth=int(meta["bit-depth"]),
            sampling=str(meta["sampling"]),
            hdr_format=str(meta["hdr"]["format"]),
        )
