"""The measurements artifact: machine-written, immutable (§spec:artifact-chain).

Every required field of the artifact contract is carried here: measured
native primaries and white point (CIE xy), black level and peak
luminance (cd/m², absolute), ambient floor, instrument identity and
firmware, processor-state snapshot, and timestamps. The protocol,
response, and routing sections are optional: a session fills the ones
it measured, and ocio-display-gen's loader tolerates the rest being
absent.

Determinism seam
----------------
§spec:artifact-chain requires timestamps in the artifact and also makes
generated artifacts byte-deterministic so hashing and reproducibility
enforce each other. The two reconcile through injection: the session
takes a clock as input, so fixed inputs produce identical bytes — the
determinism claim is "same inputs, same bytes", not "no timestamps".
The CLI defaults to the real clock; tests and reproduction runs inject
a fixed one. The default instrument double is deterministic by
construction (see `display_measure.plausible_display`); only the explicitly
requested random instrument involves an RNG, seeded at wiring time.

Rendering is deterministic by construction: fixed key order, fixed
9-decimal float formatting (below any instrument's repeatability),
LF line endings, UTF-8.
"""

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

# A wire identifier, not a package name. It outlived the repository it
# was named in: the artifact writer moved to display-measure, and this
# string did not follow it. Every promoted artifact carries it, and
# downstream loaders dispatch on it — renaming it breaks the provenance
# of every artifact already promoted. It changes only when the format
# changes: 2 added `wire_encoding`, so a loader can tell two artifacts
# of one display over different links apart; a schema-1 artifact
# implied the bench's 12-bit RGB link.
SCHEMA = "color-wrangler/measurements/2"

# Nine decimals sits below any instrument's repeatability while keeping
# the rendered bytes independent of Python's float repr.
_FLOAT_DECIMALS = 9


@dataclass(frozen=True)
class InstrumentIdentity:
    """Identity of the instrument that produced the measurements.

    `firmware` is None when the instrument reports none; the rendered
    artifact omits the key rather than writing a sentinel.
    """

    manufacturer: str
    model: str
    serial_number: str
    firmware: str | None


# Processor features split by what they do to a characterization, not by
# how the vendor markets them (§spec:signal-contract).
#
# Content-independent processing is a static per-pixel transfer: the same
# code produces the same light every frame. That is part of the display,
# and a characterization measures it. Brompton's Dark Magic raises the
# effective bit depth the panel can resolve at low drive (26-bit internal
# on the R2+ card, +2 b on this rig's 14-bit driver), and PureTone
# corrects the LED and driver non-linearity that otherwise casts the
# shadows off-neutral as each primary runs out of PWM resolution. Both
# fix constraints below the layer any OCIO config can reach, so the
# recommendation is to leave them on and measure the display that ships.
CONTENT_INDEPENDENT_FEATURES = ("dark-magic", "puretone", "extended-bit-depth")

# Content-dependent processing adapts to the frames around it, so one code
# no longer implies one luminance and the measurement stops meaning
# anything. Overdrive is temporal by construction.
CONTENT_DEPENDENT_FEATURES = ("overdrive",)

PROCESSING_FEATURES = CONTENT_INDEPENDENT_FEATURES + CONTENT_DEPENDENT_FEATURES

# Panel-resident state the operator attests, because no leaf of the
# processor's HTTP API reports it and every one of these moves the
# measurement (§spec:signal-contract).
#
# Measured on the bench 2026-08-29: switching `selected_calibration` took
# white from 1035 to 1476 cd/m² and moved it from 0.0075 to 0.0040 du'v'
# of D65, while all 302 API leaves stayed identical but for the clock and
# the chassis temperatures. `operating_mode` (Brompton's Studio Mode)
# trades peak luminance for low-end bit depth and stores its own PureTone
# correction, so it moves the level and the transfer together.
#
# These live on the receiver card and surface on the panel OSD. Until a
# session reads them off the wire (§road:operating-mode-read), the
# operator is the only instrument that can. Two artifacts recorded under
# different panel state describe different displays.
PANEL_STATE_KEYS = ("operating_mode", "selected_calibration")


# The artifact is rendered by hand (see `render`), so a string reaching a
# double-quoted YAML scalar has to be representable there. A double quote
# closes the scalar and a newline ends the line, which is not a formatting
# nuisance but a provenance hole: a newline in an attested value injects a
# sibling key into `processor_state`, and the artifact still parses, so
# nothing downstream can tell a machine-written field from one an operator
# typed.
#
# Refused at construction rather than at render. A session builds its
# declared contract before it drives a patch, so a bad value stops it at
# the gate; refusing at handoff would throw away the protocol it had just
# spent measuring.
_UNRENDERABLE = {'"', "\\"}


def _renderable(value: str, field: str) -> str:
    """The value unchanged, or ValueError naming the field that carries it."""
    bad = sorted(
        {c for c in value if c in _UNRENDERABLE or ord(c) < 0x20 or ord(c) == 0x7F}
    )
    if bad:
        raise ValueError(
            f"{field} carries {bad!r}, which the artifact's "
            "renderer cannot represent in a double-quoted scalar; a newline "
            "there would inject a sibling key into the recorded state"
        )
    return value


@dataclass(frozen=True)
class ProcessorStateSnapshot:
    """Processor state recorded with the measurements (§spec:signal-contract).

    `gamma_value` is present exactly when `eotf_type` is ``"GAMMA"``.
    `processing_enabled` names the processing features that are on, which
    is what the contract audit compares feature by feature.
    """

    eotf_type: str
    gamma_value: float | None
    intensity: str
    processing_enabled: frozenset[str] = frozenset()
    # Operator attestation, not a reading; see PANEL_STATE_KEYS. Empty on
    # a live snapshot, which is the point — there is nothing to compare an
    # attestation to. Carried as sorted pairs so the record stays hashable
    # and renders deterministically.
    panel_state: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if (self.eotf_type == "GAMMA") != (self.gamma_value is not None):
            raise ValueError(
                "gamma_value is required for GAMMA and forbidden otherwise; "
                f"got eotf_type={self.eotf_type!r}, gamma_value={self.gamma_value!r}"
            )
        _renderable(self.intensity, "intensity")
        _renderable(self.eotf_type, "eotf_type")
        for key, value in self.panel_state:
            _renderable(value, key)
        unknown = self.processing_enabled - set(PROCESSING_FEATURES)
        if unknown:
            raise ValueError(
                f"unknown processing features {sorted(unknown)}; known features "
                f"are {list(PROCESSING_FEATURES)}"
            )

    @property
    def processing_disabled(self) -> bool:
        """No *content-dependent* processing is on.

        Derived rather than stored, so it cannot drift from the features
        it summarizes. The name predates the distinction and is kept for
        the loaders that read it (ocio-display-gen records it in the
        config description); what it asserts is the property that decides
        whether a measurement means anything — that nothing in the path
        adapts to the content.
        """
        return not (self.processing_enabled & set(CONTENT_DEPENDENT_FEATURES))

    def enabled(self, feature: str) -> bool:
        return feature in self.processing_enabled

    @property
    def attested(self) -> dict[str, str]:
        """The panel-state attestation as a mapping."""
        return dict(self.panel_state)


# The recommended SDR lockdown the walking skeleton declares
# (§spec:signal-contract), and what the doubles declare compliance with.
# A real rig states its own contract in the show manifest; this constant
# only has to be a defensible default, and the defensible default is the
# panel's own linearization on and its frame-adaptive processing off.
# Lives beside the type so the display double can derive its decode gamma
# from it without importing the session.
DECLARED_CONTRACT = ProcessorStateSnapshot(
    eotf_type="GAMMA",
    gamma_value=2.4,
    intensity="100%",
    processing_enabled=frozenset(CONTENT_INDEPENDENT_FEATURES),
)


# What the artifact names a link's samples: RGB, or luma and chroma. A
# session's protocol codes are RGB either way (§spec:patch-protocol); the
# sampling says what the device received.
SAMPLING_RGB = "rgb"
SAMPLING_YCBCR = "ycbcr"


@dataclass(frozen=True)
class WireEncoding:
    """The encoding between a patch and the device (§spec:measure-sessions).

    A patch is what the processor receives; this is how it got there.
    Declared by the session, held against the processor's input metadata
    by the wire-format gate, and recorded so two artifacts of one display
    over different links read as different measurements. `layout` is the
    pypixelpack layout name; `legal_codes` names the inclusive code span
    each component can carry, because a narrow-range encoding cannot
    represent every RGB code the protocol drives.
    """

    layout: str
    bit_depth: int
    sampling: str
    subsampling: str
    levels: str
    matrix: str
    legal_codes: tuple[tuple[str, int, int], ...]

    def __post_init__(self) -> None:
        for name, value in (
            ("layout", self.layout),
            ("sampling", self.sampling),
            ("subsampling", self.subsampling),
            ("levels", self.levels),
            ("matrix", self.matrix),
        ):
            _renderable(value, name)
        if self.sampling not in (SAMPLING_RGB, SAMPLING_YCBCR):
            raise ValueError(
                f"sampling is {SAMPLING_RGB!r} or {SAMPLING_YCBCR!r}, "
                f"got {self.sampling!r}"
            )

    @property
    def identity(self) -> bool:
        """The frame is the protocol's RGB codes, untouched."""
        return self.sampling == SAMPLING_RGB


@dataclass(frozen=True)
class ResponsePoint:
    """One ramp reading: the driven code and the measured absolute XYZ."""

    code: int
    xyz: tuple[float, float, float]


@dataclass(frozen=True)
class PerChannelResponse:
    """Shadow-dense single-channel ramp readings (§spec:sessions).

    Rows are protocol-ordered (ascending code) whatever the shuffled
    presentation drove; the unshuffle key preserves the driven order.
    """

    red: tuple[ResponsePoint, ...]
    green: tuple[ResponsePoint, ...]
    blue: tuple[ResponsePoint, ...]


@dataclass(frozen=True)
class AdditivityTriad:
    """Full-drive two-channel sums (Y/C/M), absolute XYZ (§spec:sessions)."""

    yellow_xyz: tuple[float, float, float]
    cyan_xyz: tuple[float, float, float]
    magenta_xyz: tuple[float, float, float]


# The two instruments a routed row can name.
SOURCE_SPECTRORADIOMETER = "spectroradiometer"
SOURCE_COLORIMETER = "colorimeter"
SOURCES = (SOURCE_SPECTRORADIOMETER, SOURCE_COLORIMETER)


@dataclass(frozen=True)
class InstrumentRouting:
    """Two instruments, one artifact (§spec:sessions).

    A hybrid session reads bright patches with the spectroradiometer and
    dark ones with a colorimeter disciplined by a correction derived
    in-session from paired readings of the full-drive R/G/B anchors.
    Recording the derivation and the routing is what keeps the
    correction auditable: `correction_matrix` maps colorimeter XYZ to
    corrected XYZ (``XYZ_corrected = M @ XYZ_colorimeter``), rows
    outermost; `sources` names the instrument behind every row, in
    presentation order.
    """

    method: str
    spectroradiometer: InstrumentIdentity
    colorimeter: InstrumentIdentity
    correction_matrix: tuple[
        tuple[float, float, float],
        tuple[float, float, float],
        tuple[float, float, float],
    ]
    luminance_threshold: float
    sources: tuple[str, ...]

    def __post_init__(self) -> None:
        unnamed = sorted(set(self.sources) - set(SOURCES))
        if unnamed:
            raise ValueError(
                f"routing sources name instruments the artifact does not "
                f"carry: {', '.join(unnamed)}"
            )


@dataclass(frozen=True)
class MeasurementsArtifact:
    """One characterize session's measurements (§spec:artifact-chain).

    The protocol sections default to None so skeleton-era artifacts
    (and tests) still render; a protocol session fills them all.
    """

    red_xy: tuple[float, float]
    green_xy: tuple[float, float]
    blue_xy: tuple[float, float]
    white_xy: tuple[float, float]
    black_level: float
    peak_luminance: float
    ambient_floor: float
    instrument: InstrumentIdentity
    processor_state: ProcessorStateSnapshot
    session_start: datetime
    session_end: datetime
    wire_encoding: WireEncoding
    protocol_name: str | None = None
    presentation_order: tuple[str, ...] | None = None
    per_channel_response: PerChannelResponse | None = None
    gray_response: tuple[ResponsePoint, ...] | None = None
    additivity: AdditivityTriad | None = None
    # Present only for hybrid sessions; a single-instrument session
    # leaves the whole section out.
    instrument_routing: InstrumentRouting | None = None
    # Per-patch elapsed seconds (drive through read), presentation
    # order, measured by the injected session clock — so fixed-clock
    # reproduction runs render zeros and stay byte-deterministic.
    # Workflow telemetry for driving session time down, not colorimetry.
    patch_seconds: tuple[float, ...] | None = None
    # The protocol code each driven patch reached the device as, after
    # the round trip through the declared encoding; presentation order.
    # Present exactly when the link is not the identity: an identity
    # link carries every code, and a narrow one that listed none would
    # be hiding the loss.
    representable_codes: tuple[tuple[int, int, int], ...] | None = None

    def __post_init__(self) -> None:
        for name, value in (
            ("session_start", self.session_start),
            ("session_end", self.session_end),
        ):
            if value.tzinfo is None:
                raise ValueError(f"{name} lacks a timezone; timestamps are absolute")
        if (self.presentation_order is None) != (self.protocol_name is None):
            raise ValueError(
                "protocol_name and presentation_order name one protocol "
                "together; got one without the other"
            )
        if (self.representable_codes is None) != self.wire_encoding.identity:
            raise ValueError(
                "representable_codes is required for a non-identity wire "
                "encoding and forbidden for the identity; got "
                f"{self.wire_encoding.layout} with "
                f"{'none' if self.representable_codes is None else 'a list'}"
            )
        routing = self.instrument_routing
        for field, values in (
            ("patch_seconds", self.patch_seconds),
            ("representable_codes", self.representable_codes),
            ("sources", None if routing is None else routing.sources),
        ):
            if values is not None and (
                self.presentation_order is None
                or len(values) != len(self.presentation_order)
            ):
                raise ValueError(
                    f"{field} parallels presentation_order: one entry per driven patch"
                )


def _fmt(value: float) -> str:
    return f"{value:.{_FLOAT_DECIMALS}f}"


def _fmt_xy(xy: tuple[float, float]) -> str:
    return f"[{_fmt(xy[0])}, {_fmt(xy[1])}]"


def _fmt_xyz(xyz: tuple[float, float, float]) -> str:
    return f"[{_fmt(xyz[0])}, {_fmt(xyz[1])}, {_fmt(xyz[2])}]"


def _identity_lines(identity: InstrumentIdentity, indent: str) -> list[str]:
    lines = [
        f'{indent}manufacturer: "{identity.manufacturer}"',
        f'{indent}model: "{identity.model}"',
        f'{indent}serial_number: "{identity.serial_number}"',
    ]
    if identity.firmware is not None:
        lines.append(f'{indent}firmware: "{identity.firmware}"')
    return lines


def _routing_lines(routing: InstrumentRouting) -> list[str]:
    sources = ", ".join(f'"{source}"' for source in routing.sources)
    return [
        "# Two instruments, one artifact (§spec:sessions).",
        "# The correction is derived in-session from paired readings of",
        "# the full-drive R/G/B anchors and applied in software, so it is",
        "# auditable here rather than buried in the colorimeter.",
        "instrument_routing:",
        f'  method: "{routing.method}"',
        "  spectroradiometer:",
        *_identity_lines(routing.spectroradiometer, "    "),
        "  colorimeter:",
        *_identity_lines(routing.colorimeter, "    "),
        "  # XYZ_corrected = correction_matrix @ XYZ_colorimeter.",
        "  correction_matrix:",
        *(f"    - {_fmt_xyz(row)}" for row in routing.correction_matrix),
        "  # Patches measuring at or above this corrected luminance",
        "  # (cd/m²) route to the spectroradiometer, the rest to the",
        "  # disciplined colorimeter.",
        f"  luminance_threshold: {_fmt(routing.luminance_threshold)}",
        "  # The instrument behind each row, in presentation order.",
        f"  sources: [{sources}]",
        "",
    ]


def _encoding_lines(
    encoding: WireEncoding,
    representable: tuple[tuple[int, int, int], ...] | None,
) -> list[str]:
    lines = [
        "# The link the patches rode to the device: declared per session,",
        "# held against the processor's input metadata, and recorded so two",
        "# artifacts of one display over different links read as different",
        "# measurements (§spec:measurements-artifact).",
        "wire_encoding:",
        f'  layout: "{encoding.layout}"',
        f"  bit_depth: {encoding.bit_depth}",
        f'  sampling: "{encoding.sampling}"',
        f'  subsampling: "{encoding.subsampling}"',
        f'  levels: "{encoding.levels}"',
        f'  matrix: "{encoding.matrix}"',
        "  # The inclusive code span the link carries, per component.",
        "  legal_codes:",
        *(f"    {name}: [{lo}, {hi}]" for name, lo, hi in encoding.legal_codes),
    ]
    if representable is not None:
        lines += [
            "  # This link cannot carry every 12-bit RGB protocol code. The",
            "  # code each driven patch reached the device as — encoded, then",
            "  # decoded through the declared matrix and levels — in the same",
            "  # order as presentation_order.",
            "  representable_codes:",
            *(f"    - [{r}, {g}, {b}]" for r, g, b in representable),
        ]
    return [*lines, ""]


def _response_lines(points: tuple[ResponsePoint, ...], indent: str) -> list[str]:
    return [
        f"{indent}- {{code: {point.code}, xyz: {_fmt_xyz(point.xyz)}}}"
        for point in points
    ]


def render(artifact: MeasurementsArtifact) -> str:
    """The artifact's canonical YAML text (see module docstring)."""
    state = artifact.processor_state
    eotf_lines = [f'    type: "{state.eotf_type}"']
    if state.gamma_value is not None:
        eotf_lines.append(f"    gamma_value: {_fmt(state.gamma_value)}")
    instrument_lines = ["instrument:", *_identity_lines(artifact.instrument, "  ")]
    lines = [
        "# Machine-written by a `display-measure characterize` session;",
        "# immutable, never hand-edited (§spec:artifact-chain). Promote",
        "# into a show manifest as {file, sha256}.",
        f'schema: "{SCHEMA}"',
        "",
        f'measurement_date: "{artifact.session_start.date().isoformat()}"',
        "",
        "session:",
        f'  start: "{artifact.session_start.isoformat()}"',
        f'  end: "{artifact.session_end.isoformat()}"',
        "",
        *instrument_lines,
        "",
        "# CIE xy chromaticities measured through the full chain",
        "# (processor + panels), end-to-end black box.",
        "colorimetry:",
        "  primaries:",
        f"    red: {_fmt_xy(artifact.red_xy)}",
        f"    green: {_fmt_xy(artifact.green_xy)}",
        f"    blue: {_fmt_xy(artifact.blue_xy)}",
        f"  white_point: {_fmt_xy(artifact.white_xy)}",
        "",
        "# Absolute luminance, cd/m².",
        "luminance:",
        f"  black_level: {_fmt(artifact.black_level)}",
        f"  peak_luminance: {_fmt(artifact.peak_luminance)}",
        "",
        "# Ambient floor at measurement time, cd/m² (display showing black;",
        "# reflected ambient plus panel leakage).",
        f"ambient_floor: {_fmt(artifact.ambient_floor)}",
        "",
        "# Processor state read at measurement time (read-only snapshot),",
        "# except panel_state, which is carried and never checked.",
        "# processing_disabled asserts only that nothing in the path adapts",
        "# to the content; the per-feature block below is the full state,",
        "# and static linearization being on is expected, not a fault.",
        "processor_state:",
        "  eotf:",
        *eotf_lines,
        f'  intensity: "{state.intensity}"',
        *(
            [
                "  # Operator attestation: no API leaf reports these, and each",
                "  # moves the measurement. Artifacts recorded under different",
                "  # panel state describe different displays.",
                "  panel_state:",
                *[f'    {key}: "{value}"' for key, value in state.panel_state],
            ]
            if state.panel_state
            else []
        ),
        f"  processing_disabled: {'true' if state.processing_disabled else 'false'}",
        "  processing:",
        *[
            f"    {feature}: {'true' if state.enabled(feature) else 'false'}"
            for feature in PROCESSING_FEATURES
        ],
        "",
        *_encoding_lines(artifact.wire_encoding, artifact.representable_codes),
    ]
    if artifact.protocol_name is not None and artifact.presentation_order is not None:
        lines += [
            "# Patch protocol (MEASUREMENT.md, versioned with the tool).",
            "# presentation_order is the unshuffle key: patch names in the",
            "# order driven (§spec:sessions).",
            "protocol:",
            f'  name: "{artifact.protocol_name}"',
            "  presentation_order:",
            *(f'    - "{name}"' for name in artifact.presentation_order),
        ]
        if artifact.patch_seconds is not None:
            seconds = ", ".join(_fmt(s) for s in artifact.patch_seconds)
            lines += [
                "  # Elapsed seconds per patch (drive through read), same",
                "  # order as presentation_order; session-clock derived.",
                f"  patch_seconds: [{seconds}]",
            ]
        lines += [""]
    if artifact.instrument_routing is not None:
        lines += _routing_lines(artifact.instrument_routing)
    if artifact.per_channel_response is not None:
        response = artifact.per_channel_response
        lines += [
            "# Shadow-dense single-channel ramps: absolute XYZ (cd/m²) per",
            "# driven code, protocol-ordered.",
            "per_channel_response:",
            "  red:",
            *_response_lines(response.red, "    "),
            "  green:",
            *_response_lines(response.green, "    "),
            "  blue:",
            *_response_lines(response.blue, "    "),
            "",
        ]
    if artifact.gray_response is not None:
        lines += [
            "# Gray tracking ramp (r=g=b), same codes as the channel ramps.",
            "gray_response:",
            *_response_lines(artifact.gray_response, "  "),
            "",
        ]
    if artifact.additivity is not None:
        lines += [
            "# Full-drive two-channel sums for additivity analysis, absolute XYZ.",
            "additivity:",
            f"  yellow: {_fmt_xyz(artifact.additivity.yellow_xyz)}",
            f"  cyan: {_fmt_xyz(artifact.additivity.cyan_xyz)}",
            f"  magenta: {_fmt_xyz(artifact.additivity.magenta_xyz)}",
            "",
        ]
    if artifact.per_channel_response is None:
        lines += [
            "# per_channel_response: omitted — not measured by this session.",
            "# Loaders tolerate its absence.",
            "",
        ]
    return "\n".join(lines)


def write(artifact: MeasurementsArtifact, path: Path) -> bytes:
    """Write the artifact to `path` (refusing to overwrite); return its bytes.

    The artifact is immutable once written (§spec:artifact-chain), so an
    existing file at `path` raises FileExistsError rather than being
    replaced.
    """
    data = render(artifact).encode("utf-8")
    with path.open("xb") as f:
        f.write(data)
    return data
