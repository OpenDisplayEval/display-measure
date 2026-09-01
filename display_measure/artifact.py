"""The measurements artifact: machine-written, immutable (§spec:artifact-chain).

Every required field of the artifact contract is carried here: measured
native primaries and white point (CIE xy), black level and peak
luminance (cd/m², absolute), ambient floor, instrument identity and
firmware, processor-state snapshot, and timestamps. The protocol,
response, and routing sections are optional: a session fills the ones
it measured, and ocio-display-gen's loader tolerates the rest being
absent.

The seam file
-------------
The artifact is written as CSMF, colour-specio's measurement file
(§spec:measurement-seam). CSMF carries the rows — tristimulus, and the
spectrum behind each one — and everything it does not model rides in
the provenance block its reserved `ancillary` field holds: the declared
contract, the attested panel state, the protocol name and driven order,
the instrument identity, the processor snapshot, the wire encoding and
the correction matrix. One file, because a pipeline with two
measurements files of record has none. Existing CSMF readers open it
and ignore the field.

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
LF line endings, UTF-8. The rendering is no longer written to disk as
an artifact: it is the hashing projection, and the digest over it is
what a promotion records. Protobuf guarantees round-trip rather than a
canonical encoding, so a digest over the seam file's own bytes would
rotate on a dependency upgrade and every promoted artifact would stop
verifying.
"""

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta
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

# The seam file is CSMF (§spec:measurement-seam), and colour-specio's
# loader opens no other suffix.
SEAM_SUFFIX = ".csmf"

# The provenance block riding in CSMF's reserved `ancillary` field: an
# envelope naming the digest, the marker, then the projection. Its own
# wire identifier, because a reader dispatches on it exactly as it does
# on SCHEMA.
ANCILLARY_SCHEMA = "color-wrangler/measurements-provenance/1"
# A YAML document separator, so the block is a two-document stream and a
# reader can take it apart with a parser rather than a string search.
PROJECTION_MARKER = "---"

# Nine decimals sits below any instrument's repeatability while keeping
# the rendered bytes independent of Python's float repr.
_FLOAT_DECIMALS = 9

# Spectral radiance runs over decades between a black patch and full
# white, so samples render in scientific notation rather than the fixed
# point the rest of the artifact uses: nine significant figures hold the
# same relative precision at either end.
_SAMPLE_DIGITS = 9
# Instruments report their grid to the nanometre at best.
_WAVELENGTH_DECIMALS = 3


# How a row got its spectrum (§spec:spectral-retention). Per row, not per
# file: a disciplined session reads its dark end with a colorimeter, and a
# colorimeter has no spectrum at all. An analysis that needs a real
# spectrum can then refuse the rows that lack one rather than treating a
# scaled estimate as a measurement.
SPECTRUM_MEASURED = "measured"
SPECTRUM_RECONSTRUCTED = "reconstructed"
SPECTRUM_ABSENT = "absent"
SPECTRUM_PROVENANCE = (SPECTRUM_MEASURED, SPECTRUM_RECONSTRUCTED, SPECTRUM_ABSENT)


@dataclass(frozen=True)
class Spectrum:
    """The spectral distribution behind one reading, and where it came from.

    Judgment-grade analysis is spectral — noise floors, metamerism,
    observer variation, camera match — and a tristimulus triple answers
    none of it, so the spectrum is retained rather than discarded at the
    session boundary (§spec:spectral-retention).

    `values` are absolute spectral radiance at `wavelengths` (nm), the
    units colour-specio's instruments report. `derived_across` is the
    luminance span (cd/m², low to high) a reconstruction was scaled
    across, so a reader can judge how far it was extrapolated; it is
    present exactly for a reconstruction.
    """

    wavelengths: tuple[float, ...]
    values: tuple[float, ...]
    provenance: str
    derived_across: tuple[float, float] | None = None

    def __post_init__(self) -> None:
        if self.provenance not in SPECTRUM_PROVENANCE:
            raise ValueError(
                f"spectral provenance is one of {list(SPECTRUM_PROVENANCE)}; "
                f"got {self.provenance!r}"
            )
        if len(self.values) != len(self.wavelengths):
            raise ValueError(
                "a spectrum carries as many values as wavelengths; got "
                f"{len(self.values)} against {len(self.wavelengths)}"
            )
        if bool(self.values) == (self.provenance == SPECTRUM_ABSENT):
            raise ValueError(
                f"a {SPECTRUM_ABSENT!r} spectrum carries no samples and every "
                f"other provenance carries some; got {self.provenance!r} with "
                f"{len(self.values)}"
            )
        if (self.derived_across is not None) != (
            self.provenance == SPECTRUM_RECONSTRUCTED
        ):
            raise ValueError(
                "derived_across names the luminance span a reconstruction was "
                f"scaled across, so it belongs to {SPECTRUM_RECONSTRUCTED!r} "
                f"alone; got {self.provenance!r} with {self.derived_across!r}"
            )

    @property
    def digest(self) -> str:
        """sha256 of the samples' canonical rendering.

        The projection carries this rather than the samples themselves:
        an instrument's grid is hundreds of bins per row, and restating
        every one would multiply the artifact's size for data the seam
        file already holds.
        """
        rows = (
            f"{w:.{_WAVELENGTH_DECIMALS}f} {v:.{_SAMPLE_DIGITS}e}\n"
            for w, v in zip(self.wavelengths, self.values, strict=True)
        )
        return hashlib.sha256("".join(rows).encode("utf-8")).hexdigest()


# What a colorimeter's reading carries, and what a session records for a
# row no instrument gave a spectrum for.
ABSENT_SPECTRUM = Spectrum(wavelengths=(), values=(), provenance=SPECTRUM_ABSENT)


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
from display_measure.protocol import CODE_BITS, ProbeResult  # noqa: E402

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
            *((f"legal_codes[{c}]", c) for c, _, _ in self.legal_codes),
        ):
            _renderable(value, name)
        if self.sampling not in (SAMPLING_RGB, SAMPLING_YCBCR):
            raise ValueError(
                f"sampling is {SAMPLING_RGB!r} or {SAMPLING_YCBCR!r}, "
                f"got {self.sampling!r}"
            )

    @property
    def identity(self) -> bool:
        """The frame is the protocol's RGB codes, untouched.

        Computed, not inferred from sampling alone: a 10-bit RGB link is
        RGB-sampled and still not the identity.
        """
        return self.sampling == SAMPLING_RGB and self.bit_depth == CODE_BITS


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
    # The blocks the session drove, name and version each. This is what
    # a consumer matches on: it states what an artifact actually
    # carries, so a consumer can require the blocks it reads rather
    # than a bundle version that moves for reasons unrelated to it
    # (§spec:patch-protocol). `protocol_name` is the label beside it.
    protocol_blocks: tuple[str, ...] | None = None
    # What each probe found, and every patch it drove finding it. A
    # static block's codes are implied by its id; a probe's are a
    # result, so they are recorded rather than reconstructed
    # (§spec:patch-protocol).
    probe_results: tuple[ProbeResult, ...] = ()
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
    # The codes the wire carried for each driven patch, presentation
    # order: what the device received, as a fact of the session. Whether
    # a protocol code survived the link is derivable from these and the
    # encoding; the artifact records only what happened.
    wire_codes: tuple[tuple[int, int, int], ...] | None = None
    # The spectrum behind each driven patch, presentation order, each
    # naming its own provenance (§spec:spectral-retention). A session
    # whose instrument returns no spectrum records every row absent
    # rather than leaving the block out: "no spectrum" is a fact about
    # the reading, and silence is not.
    spectra: tuple[Spectrum, ...] | None = None
    # The absolute XYZ (cd/m²) behind each driven patch, presentation
    # order. The response sections above are the same readings sorted
    # into protocol order, which is the shape a config generator reads;
    # these are the seam file's own rows, and recording them is what
    # lets the projection's digest cover the tristimulus the file
    # carries.
    readings: tuple[tuple[float, float, float], ...] | None = None
    # The protocol code values driven for each patch, presentation
    # order. Recorded rather than looked up from the protocol's name: a
    # reader should not have to own the patch table to know what was
    # driven, and `wire_codes` records only what the link carried, which
    # over a YCbCr link is not an RGB triple at all.
    driven_codes: tuple[tuple[int, int, int], ...] | None = None

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
        routing = self.instrument_routing
        for field, values in (
            ("patch_seconds", self.patch_seconds),
            ("wire_codes", self.wire_codes),
            ("spectra", self.spectra),
            ("readings", self.readings),
            ("driven_codes", self.driven_codes),
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


def _fmt_nm(wavelength: float) -> str:
    return f"{wavelength:.{_WAVELENGTH_DECIMALS}f}"


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
    wire_codes: tuple[tuple[int, int, int], ...] | None,
) -> list[str]:
    lines = [
        "# The link the patches rode to the device (§spec:measurements-artifact).",
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
    if wire_codes is not None:
        lines += [
            "  # The codes the wire carried per driven patch, in the same order",
            "  # as presentation_order.",
            "  wire_codes:",
            *(f"    - [{a}, {b}, {c}]" for a, b, c in wire_codes),
        ]
    return [*lines, ""]


def _protocol_lines(artifact: MeasurementsArtifact) -> list[str]:
    """The patch protocol and what it drove, or nothing for a session
    that drove none."""
    if artifact.protocol_name is None or artifact.presentation_order is None:
        return []
    lines = [
        "# Patch protocol (MEASUREMENT.md, versioned with the tool).",
        "# presentation_order is the unshuffle key: patch names in the",
        "# order driven (§spec:sessions).",
        "protocol:",
        f'  name: "{artifact.protocol_name}"',
    ]
    if artifact.protocol_blocks is not None:
        lines += [
            "  # The measurement blocks this session drove, name and",
            "  # version each. A consumer requires blocks, not a bundle.",
            "  blocks:",
            *(f'    - "{block}"' for block in artifact.protocol_blocks),
        ]
    if artifact.probe_results:
        lines += [
            "  # Adaptive measurements: what each probe found, and every",
            "  # patch it drove finding it. A block's codes are implied by",
            "  # its id; a probe's are a result.",
            "  probes:",
        ]
        for result in artifact.probe_results:
            lines += [
                f'    - id: "{result.probe_id}"',
                "      findings:",
                *(
                    f"        {key}: "
                    + ("null" if value is None else _fmt(float(value)))
                    for key, value in result.findings.items()
                ),
                "      driven:",
                *(
                    f"        - {{ rgb: [{rgb[0]}, {rgb[1]}, {rgb[2]}], "
                    f"luminance: {_fmt(luminance)} }}"
                    for rgb, luminance in result.driven
                ),
            ]
    lines += [
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
    if artifact.driven_codes is not None:
        lines += [
            "  # The protocol code values driven per patch, same order.",
            "  driven_codes:",
            *(f"    - [{r}, {g}, {b}]" for r, g, b in artifact.driven_codes),
        ]
    return [*lines, ""]


def _row_lines(artifact: MeasurementsArtifact) -> list[str]:
    """The seam file's own rows: what was read, and how its spectrum came."""
    lines = []
    if artifact.readings is not None:
        lines += [
            "# The absolute XYZ (cd/m²) read for each driven patch, in",
            "# presentation order — the seam file's own rows. The response",
            "# sections below are the same readings in protocol order.",
            "readings:",
            *(f"  - {_fmt_xyz(row)}" for row in artifact.readings),
            "",
        ]
    if artifact.spectra is not None:
        lines += _spectra_lines(artifact.spectra)
    return lines


def _spectra_lines(spectra: tuple[Spectrum, ...]) -> list[str]:
    """The per-row spectral provenance, presentation order.

    Samples are digested rather than restated: the seam file already
    carries them, and this rendering is the hashing projection, so the
    digest is what makes the projection cover them.
    """
    lines = [
        "# How each row got its spectrum, in presentation order",
        "# (§spec:spectral-retention). sha256 digests the row's samples,",
        "# which the seam file carries; derived_across is the luminance",
        "# span (cd/m²) a reconstruction was scaled across, so a reader",
        "# can judge how far it was extrapolated.",
        "spectra:",
    ]
    for spectrum in spectra:
        if spectrum.provenance == SPECTRUM_ABSENT:
            lines.append(f'  - {{provenance: "{SPECTRUM_ABSENT}"}}')
            continue
        fields = [
            f'provenance: "{spectrum.provenance}"',
            f"samples: {len(spectrum.values)}",
            f"range: [{_fmt_nm(spectrum.wavelengths[0])}, "
            f"{_fmt_nm(spectrum.wavelengths[-1])}]",
        ]
        if spectrum.derived_across is not None:
            low, high = spectrum.derived_across
            fields.append(f"derived_across: [{_fmt(low)}, {_fmt(high)}]")
        fields.append(f'sha256: "{spectrum.digest}"')
        lines.append("  - {" + ", ".join(fields) + "}")
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
        *_encoding_lines(artifact.wire_encoding, artifact.wire_codes),
    ]
    lines += _protocol_lines(artifact)
    lines += _row_lines(artifact)
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


def digest(projection: str) -> str:
    """The artifact's sha256, over the canonical projection.

    Not over the seam file's bytes: protobuf guarantees round-trip, not
    a canonical encoding, so a digest over those would rotate on a
    dependency upgrade and every promoted artifact would stop verifying
    (§spec:measurements-artifact). The rendering is canonical by
    construction — fixed key order, fixed nine-decimal floats, LF,
    UTF-8 — so a re-serialized file with identical content still
    verifies.
    """
    return hashlib.sha256(projection.encode("utf-8")).hexdigest()


def provenance_block(artifact: MeasurementsArtifact) -> bytes:
    """The bytes the seam file's ancillary field carries.

    Two YAML documents: an envelope naming the digest, then the
    canonical projection it covers — everything CSMF does not model,
    which is the declared contract, the attested panel state, the
    protocol name and driven order, the instrument identity, the
    processor snapshot, the wire encoding and the correction matrix.
    specio neither interprets nor validates these bytes, so an existing
    CSMF reader opens the file and ignores them.

    The projection rides verbatim rather than being reduced to the
    fields, because the projection *is* their canonical serialization:
    carrying it twice, once as data and once as the hashing basis,
    would be two records that can disagree.
    """
    projection = render(artifact)
    envelope = "\n".join(
        [
            "# Provenance for the CSMF file this rides in",
            "# (§spec:measurements-artifact). Everything below the marker",
            "# is the artifact's canonical projection; projection_sha256",
            "# is its digest, and the promotion hash of this measurement.",
            f'schema: "{ANCILLARY_SCHEMA}"',
            f'projection_sha256: "{digest(projection)}"',
            PROJECTION_MARKER,
            "",
        ]
    )
    return (envelope + projection).encode("utf-8")


def carried_projection(ancillary: bytes) -> tuple[str, str]:
    """The digest recorded in a provenance block and the projection it covers.

    Raises ValueError when the bytes are not a provenance block this
    layer wrote.
    """
    text = ancillary.decode("utf-8")
    marker = f"\n{PROJECTION_MARKER}\n"
    envelope, separator, projection = text.partition(marker)
    if not separator:
        raise ValueError(
            "the seam file's ancillary field carries no provenance block: "
            f"no {PROJECTION_MARKER!r} marker separating the envelope from "
            "the projection"
        )
    for line in envelope.splitlines():
        if line.startswith("projection_sha256:"):
            return line.split('"')[1], projection
    raise ValueError("the provenance block records no projection_sha256")


def verify(path: Path) -> str:
    """Re-read the seam file at `path` and return its verified digest.

    The artifact is self-verifying: the digest covers the projection the
    provenance block carries, so a reader recomputes it from the file
    rather than trusting the bytes it was handed. Raises ValueError when
    the file's own record disagrees with itself.
    """
    from specio.serialization.csmf import load_csmf_file

    loaded = load_csmf_file(path)
    recorded, projection = carried_projection(loaded.ancillary)
    recomputed = digest(projection)
    if recomputed != recorded:
        raise ValueError(
            f"{path} does not verify: its provenance block records "
            f"{recorded} and its projection digests to {recomputed}"
        )
    rows = len(loaded.measurements)
    # Parsed, not pattern-counted. This counted every `    - "` line in
    # the projection, which silently meant "the driven order" only for
    # as long as nothing else was a list at that indentation — adding
    # the block list broke it, and the failure looked like a corrupt
    # artifact rather than a miscount.
    import yaml

    document = yaml.safe_load(projection) or {}
    driven = (document.get("protocol") or {}).get("presentation_order") or []
    if driven and rows != len(driven):
        raise ValueError(
            f"{path} does not verify: it carries {rows} measurement rows "
            f"against {len(driven)} patches in the driven order"
        )
    return recomputed


def write(artifact: MeasurementsArtifact, path: Path) -> str:
    """Write the seam file at `path`; return its digest.

    One file at the seam (§spec:measurement-seam): CSMF carrying the
    spectra and the tristimulus, with everything CSMF does not model in
    the provenance block its reserved ancillary field holds. CSMF
    replaced the YAML rendering rather than joining it, because a
    pipeline with two measurements files of record has none — the
    renderer stayed as the hashing projection.

    The artifact is immutable once written (§spec:artifact-chain), so an
    existing file at `path` raises FileExistsError rather than being
    replaced, and a path that is not a `.csmf` raises ValueError.
    """
    check_seam_path(path)
    if artifact.readings is None:
        raise ValueError(
            "a seam file carries the session's rows and this artifact "
            "records none; readings is what CSMF is a file of"
        )
    with path.open("xb") as f:
        f.write(_seam_bytes(artifact))
    return digest(render(artifact))


def check_seam_path(path: Path) -> None:
    """Refuse a path colour-specio's loader would not open.

    Checked at the top of a session as well as here: a session that
    spends twenty minutes measuring and then cannot name its output
    file has thrown the protocol away.
    """
    if path.suffix != SEAM_SUFFIX:
        raise ValueError(
            f"the measurements seam file is CSMF, so {path} needs the "
            f"{SEAM_SUFFIX} suffix; colour-specio's loader opens no other"
        )


def _seam_bytes(artifact: MeasurementsArtifact) -> bytes:
    """The CSMF file's bytes.

    Deferred imports: colour-specio drags colour and scipy behind it,
    which no `--help` and no session before its handoff needs.
    """
    import numpy as np
    from specio.serialization.csmf import (
        CSMF_Data,
        CSMF_Metadata,
        csmf_data_to_buffer,
    )

    rows = _measurement_rows(artifact)
    data = CSMF_Data(
        test_colors=np.asarray(artifact.driven_codes or (), dtype=np.int64),
        # The rows are already in driven order, so each indexes itself.
        order=list(range(len(rows))),
        measurements=np.asarray(rows, dtype=object),
        metadata=CSMF_Metadata(
            notes=artifact.protocol_name,
            location=None,
            author=None,
            software="display-measure",
        ),
        ancillary=provenance_block(artifact),
    )
    serialized: bytes = csmf_data_to_buffer(data).SerializeToString()
    return serialized


# colour-specio reports an instrument's integration time per reading and
# this layer does not carry one: the session records elapsed seconds per
# patch instead, which is drive through read, not exposure. Written as
# zero rather than invented.
UNKNOWN_EXPOSURE = 0.0


def _row_instrument(artifact: MeasurementsArtifact, index: int) -> str:
    """The instrument behind row `index`, as CSMF names one."""
    routing = artifact.instrument_routing
    who = artifact.instrument
    if routing is not None:
        who = (
            routing.spectroradiometer
            if routing.sources[index] == SOURCE_SPECTRORADIOMETER
            else routing.colorimeter
        )
    return f"{who.manufacturer} {who.model} {who.serial_number}"


def _row_times(artifact: MeasurementsArtifact, count: int) -> list[datetime]:
    """When each row was read, from the injected session clock.

    Derived rather than stamped at write time, so the seam file stays
    byte-deterministic under a fixed clock — colour-specio's measurement
    types otherwise default their timestamp to `datetime.now`.
    """
    elapsed = 0.0
    times = []
    for index in range(count):
        if artifact.patch_seconds is not None:
            elapsed += artifact.patch_seconds[index]
        times.append(artifact.session_start + timedelta(seconds=elapsed))
    return times


def _measurement_rows(artifact: MeasurementsArtifact) -> list[object]:
    """One CSMF row per driven patch, spectral where a spectrum exists.

    A row's tristimulus is always the one the session recorded, never
    the integral of the spectrum beside it: a reconstructed spectrum
    carries its anchor's chromaticity by construction, and overwriting
    the measured value with it would launder an estimate into a
    measurement. The derived fields colour-specio computes — CCT,
    dominant wavelength, purity — describe the spectrum, which for a
    reconstruction is what they are about.
    """
    import numpy as np
    from colour import SpectralDistribution
    from specio.common import ColorimeterMeasurement, SPDMeasurement

    readings = artifact.readings or ()
    spectra = artifact.spectra or (ABSENT_SPECTRUM,) * len(readings)
    names = artifact.presentation_order or tuple(
        str(index) for index in range(len(readings))
    )
    times = _row_times(artifact, len(readings))
    rows: list[object] = []
    for index, (xyz, spectrum) in enumerate(zip(readings, spectra, strict=True)):
        instrument = _row_instrument(artifact, index)
        row: SPDMeasurement | ColorimeterMeasurement
        if spectrum.provenance == SPECTRUM_ABSENT:
            row = ColorimeterMeasurement(
                XYZ=np.asarray(xyz),
                exposure=UNKNOWN_EXPOSURE,
                device_id=instrument,
            )
        else:
            row = SPDMeasurement(
                spd=SpectralDistribution(
                    np.asarray(spectrum.values),
                    np.asarray(spectrum.wavelengths),
                    # Named for the patch. colour's default name embeds
                    # the object's id, which would put an address in the
                    # file and cost byte-determinism.
                    name=names[index],
                ),
                exposure=UNKNOWN_EXPOSURE,
                spectrometer_id=instrument,
            )
            row.XYZ = np.asarray(xyz)
            row.xy = np.asarray(xyz[:2]) / sum(xyz)
        row.time = times[index]
        rows.append(row)
    return rows
