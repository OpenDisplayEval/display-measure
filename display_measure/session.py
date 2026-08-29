"""The characterize session core (§spec:sessions).

One shared flow, each stage a function that reports itself as an event:
contract audit, ambient gate, patch drive, settle, instrument read,
handoff — `characterize` is their composition. The session drives the
versioned patch protocol (`display_measure.protocol`, MEASUREMENT.md) in
shuffled presentation order and emits the immutable measurements
artifact (§spec:artifact-chain). The gates that do not yet hold
anything report themselves as stubs, so the seams exist before
§road:session-gates fills them; verify-mode composes the same gate
functions unchanged.

The core narrates through `emit` and never through a logger
(§spec:session-events). One seam, N consumers: the session log
(`display_measure.session_log`) is one of them and the operator UI in
color-wrangler is another, so nothing here knows what a frontend
looks like. `emit` defaults to the log renderer, which keeps a library
caller and the CLI on the same path rather than privileging either.

Determinism: see the determinism-seam design in
`display_measure.artifact`'s module docstring.
"""

import hashlib
import logging
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Protocol

import numpy as np
import numpy.typing as npt
from bmd_sg.decklink import (
    BMDDeckLink,
    DeckLinkOutput,
    EOTFType,
    HDRMetadata,
    MockBMDDeckLink,
    PixelFormatType,
)

from display_measure.artifact import (
    DECLARED_CONTRACT,
    AdditivityTriad,
    MeasurementsArtifact,
    PerChannelResponse,
    ProcessorStateSnapshot,
    ResponsePoint,
    write,
)
from display_measure.events import (
    EventSink,
    Gate,
    GateEvaluated,
    GateVerdict,
    HandoffCompleted,
    Outcome,
    PatchCompleted,
    PatchSettling,
    PatchStarted,
    PlaybackStarted,
    SessionEnded,
    SessionMode,
    SessionStarted,
)
from display_measure.hybrid import (
    DEFAULT_LUMINANCE_THRESHOLD,
    DerivationRefused,
    DisciplinedInstrument,
    HybridInstrument,
)
from display_measure.instrument import (
    Instrument,
    InstrumentReading,
    chromaticity,
    identity,
    luminance,
    xyz,
)
from display_measure.plausible_wall import MismatchedColorimeter, PlausibleWall
from display_measure.processor import (
    ContractViolation,
    TesseraProcessor,
    WireFormat,
    audit_contract,
    audit_output_level,
    audit_output_scaling,
    audit_wire_format,
    state_from_tessera,
)
from display_measure.protocol import (
    BLACK_PATCH,
    FULL_DRIVE,
    OPENING_PINS,
    PATCH_PIXEL_FORMAT,
    PROTOCOL_NAME,
    WHITE_PATCH,
    Patch,
    presentation_order,
    protocol_patches,
)
from display_measure.session_log import log_events

__all__ = [
    "FULL_DRIVE",
    "PATCH_PIXEL_FORMAT",
    "Clock",
    "Instrument",
    "InstrumentReading",
    "PatchDrive",
    "characterize",
    "doubles_session",
    "hardware_session",
]

# The DeckLink a hardware session drives. One card on the bench rig;
# multi-device selection waits for a session that needs it.
DECKLINK_INDEX = 0

# The link the session declares, derived from what `_setup_drive` sets so the
# declaration cannot drift from the drive (§spec:signal-contract). Compared
# against the processor's own input metadata by the wire-format gate.
DECLARED_WIRE_FORMAT = WireFormat(
    bit_depth=PATCH_PIXEL_FORMAT.bit_depth,
    sampling="rgb",
    hdr_format="standard-dynamic-range",
)

# Pre-session narration only — instrument discovery and the processor
# pre-audit run before `characterize` opens the event stream, so there
# is nothing to emit into yet. Everything inside a session goes through
# `emit` (§spec:session-events); nothing here is a second path for it.
log = logging.getLogger("display_measure.session")

Clock = Callable[[], datetime]

# bmd-signal-gen's HD 1080p default frame geometry, restated locally so
# nothing imports the private bmd_sg.decklink.bmd_decklink module.
FRAME_WIDTH = 1920
FRAME_HEIGHT = 1080


class PatchDrive(DeckLinkOutput, Protocol):
    """bmd-signal-gen's ``DeckLinkOutput`` plus the pixel-format setter.

    Sessions declare the wire format explicitly (§spec:sessions), but
    upstream's protocol omits the ``pixel_format`` property both device
    classes carry — an upstream gap worth closing. Until then this
    session-side protocol adds it; ``BMDDeckLink`` and
    ``MockBMDDeckLink`` both satisfy it structurally.
    """

    @property
    def pixel_format(self) -> PixelFormatType: ...

    @pixel_format.setter
    def pixel_format(self, pixel_format_type: PixelFormatType) -> None: ...


def _frame(rgb: tuple[int, int, int]) -> npt.NDArray[np.uint16]:
    return np.full((FRAME_HEIGHT, FRAME_WIDTH, 3), rgb, dtype=np.uint16)


@contextmanager
def _gate(emit: EventSink, gate: Gate) -> Iterator[None]:
    """Report a refusal as the gate's own outcome before it propagates.

    A session-end event carries the message but not the check that
    produced it, and "the wall measures 0.56x its declared intensity"
    calls for a different trip than "the processor has overdrive on".
    Naming the gate is what lets a consumer show the operator where to
    go (§spec:web-ui).
    """
    try:
        yield
    except ContractViolation as e:
        emit(GateEvaluated(gate, GateVerdict.REFUSED, str(e)))
        raise


@contextmanager
def _session_outcome(emit: EventSink, clock: Clock) -> Iterator[None]:
    """Close the stream with exactly one `SessionEnded`, on every path.

    A consumer's progress display waits forever on a session that
    stopped without saying so, and the paths that stop one — a gate
    refusing, an instrument raising, an operator cancelling — are
    exactly the ones nobody remembers to instrument by hand.

    The failure outcomes are distinguished because the operator's next
    move differs: refused sends them to the rig, failed to a bug report.
    """
    try:
        yield
    except (ContractViolation, DerivationRefused) as e:
        emit(SessionEnded(Outcome.REFUSED, clock(), str(e)))
        raise
    except Exception as e:
        emit(SessionEnded(Outcome.FAILED, clock(), f"{type(e).__name__}: {e}"))
        raise
    emit(SessionEnded(Outcome.COMPLETED, clock()))


def ambient_gate(reading: InstrumentReading) -> float:
    """Ambient gate (§spec:sessions): returns the recorded floor.

    Consumes the session's opening black reading. STUB until
    §road:session-gates: the budget refusal is not enforced, which the
    gate's own event says out loud — a consumer that rendered an unrun
    check as a pass would be claiming a gate nobody held. verify-mode
    composes this gate unchanged.
    """
    return luminance(reading)


def _setup_drive(device: PatchDrive, emit: EventSink) -> None:
    """Declare pixel format and EOTF signaling, then start playback.

    bmd-signal-gen defaults to PQ InfoFrames, which would fault an
    SDR-contract wall, so the session signals explicitly
    (§spec:sessions).
    """
    device.pixel_format = PATCH_PIXEL_FORMAT
    device.set_hdr_metadata(HDRMetadata(eotf=EOTFType.SDR))
    device.start_playback()
    # The enum names, not the enums: an event crossing a wire should
    # not drag bmd-signal-gen in behind it.
    emit(PlaybackStarted(PATCH_PIXEL_FORMAT.name, EOTFType.SDR.name))


def _drive(device: PatchDrive, patch: Patch, index: int, emit: EventSink) -> None:
    emit(PatchStarted(index, patch.name, patch.rgb))
    device.display_frame(_frame(patch.rgb))


def _settle(seconds: float, index: int, emit: EventSink) -> None:
    # Announced before the wait, not after: a settle and an instrument
    # read are where a session sits still, and saying so as it starts
    # is what makes a run auditable while it happens (§spec:sessions).
    emit(PatchSettling(index, seconds))
    time.sleep(seconds)


def _read(instrument: Instrument, patch: Patch) -> InstrumentReading:
    # A disciplined instrument routes by patch, so it is read by name;
    # the patch is already in hand here, which keeps the instrument
    # from shadowing the session's own iteration (§spec:sessions).
    if isinstance(instrument, DisciplinedInstrument):
        return instrument.measure_patch(patch.name)
    return instrument.measure()


def _log_instrument(instrument: Instrument, note: str = "") -> None:
    """Narrate a discovered instrument. Pre-session; see `log` above."""
    who = identity(instrument)
    log.info(
        "instrument: %s %s (serial %s)%s",
        who.manufacturer,
        who.model,
        who.serial_number,
        note,
    )


def _handoff(artifact: MeasurementsArtifact, out_path: Path, emit: EventSink) -> None:
    """Write the immutable artifact and report its hash."""
    data = write(artifact, out_path)
    emit(HandoffCompleted(str(out_path), hashlib.sha256(data).hexdigest()))


def _ramp(
    protocol: tuple[Patch, ...],
    readings: dict[str, InstrumentReading],
    role: str,
    anchor: str,
) -> tuple[ResponsePoint, ...]:
    """The role's readings as protocol-ordered (ascending-code) rows.

    The full-drive anchor reading closes the ramp as its top row: the
    anchors' absolute XYZ would otherwise leave the session only as
    derived chromaticities, starving additivity and gain analysis.
    """
    points = tuple(
        ResponsePoint(code=max(patch.rgb), xyz=xyz(readings[patch.name]))
        for patch in protocol
        if patch.role == role
    )
    return (*points, ResponsePoint(code=FULL_DRIVE, xyz=xyz(readings[anchor])))


def characterize(
    device: PatchDrive,
    instrument: Instrument,
    out_path: Path,
    *,
    clock: Clock,
    settle_seconds: float,
    seed: int = 0,
    declared: ProcessorStateSnapshot = DECLARED_CONTRACT,
    reading: ProcessorStateSnapshot | None = None,
    emit: EventSink = log_events,
) -> None:
    """Run one characterize session and write the measurements artifact.

    Drives the versioned patch protocol through `device` in shuffled
    presentation order (`seed` keys the shuffle and nothing else —
    instrument behavior is the caller's wiring), reads each patch with
    `instrument`, and hands off the immutable artifact at `out_path`.
    Raises FileExistsError rather than overwriting.

    An instrument that also satisfies `DisciplinedInstrument` gets the
    patches its correction needs driven first and reports its routing
    into the artifact; drive, settle, and read are the same stages
    either way.

    Reports its whole lifecycle to `emit` (§spec:session-events),
    which defaults to the session log.
    """
    session_start = clock()
    emit(
        SessionStarted(
            mode=SessionMode.CHARACTERIZE,
            protocol_name=PROTOCOL_NAME,
            patch_count=len(protocol_patches()),
            at=session_start,
        )
    )
    with _session_outcome(emit, clock):
        _characterize(
            device,
            instrument,
            out_path,
            clock=clock,
            settle_seconds=settle_seconds,
            seed=seed,
            declared=declared,
            reading=reading,
            emit=emit,
            session_start=session_start,
        )


def _characterize(
    device: PatchDrive,
    instrument: Instrument,
    out_path: Path,
    *,
    clock: Clock,
    settle_seconds: float,
    seed: int,
    declared: ProcessorStateSnapshot,
    reading: ProcessorStateSnapshot | None,
    emit: EventSink,
    session_start: datetime,
) -> None:
    """The session body `characterize` brackets with start and end events."""
    # The contract audit gates the session: nothing is driven until the
    # processor is known to match what was declared (§spec:sessions).
    with _gate(emit, Gate.CONTRACT_AUDIT):
        recorded_state = audit_contract(declared, reading)
    emit(
        GateEvaluated(
            Gate.CONTRACT_AUDIT,
            GateVerdict.PASS,
            f"gamma {recorded_state.gamma_value}, "
            f"intensity {recorded_state.intensity}, processing "
            f"{', '.join(sorted(recorded_state.processing_enabled)) or 'all off'}",
        )
    )
    if recorded_state.panel_state:
        # ATTESTED is not a pass: no leaf of the processor's API reports
        # any of this, so the gate carries what the operator confirmed
        # rather than comparing it (§spec:session-gates).
        emit(
            GateEvaluated(
                Gate.PANEL_STATE,
                GateVerdict.ATTESTED,
                "; ".join(f"{k}={v}" for k, v in recorded_state.panel_state),
            )
        )

    _setup_drive(device, emit)

    # The session opens on black — presentation_order pins it first;
    # the ambient gate consumes that opening reading and black_level
    # records the same measurement. §road:session-gates adds the
    # closing black read (SPEC: the ambient budget opens and closes
    # the session). A disciplined instrument pins the patches its
    # correction is derived from behind black, and learns the driven
    # order before the first read (§spec:sessions).
    # The derivation rungs lead a disciplined session: black is the
    # session's darkest and most expensive read, and the colorimeter is
    # the better instrument for it (§road:instrument-floors), which it
    # can only be once its correction exists. Black follows them, still
    # ahead of the shuffle, and the ambient gate consumes it wherever it
    # lands. A single-instrument session pins nothing and opens on black
    # as before.
    disciplined = instrument if isinstance(instrument, DisciplinedInstrument) else None
    pinned = OPENING_PINS
    if disciplined is not None:
        pinned = (*disciplined.derivation_patches(), *OPENING_PINS)
    presented = presentation_order(protocol_patches(), seed, pinned=pinned)
    try:
        readings, patch_seconds, ambient_floor = _drive_presentation(
            device,
            instrument,
            presented,
            clock=clock,
            settle_seconds=settle_seconds,
            declared_intensity=declared.intensity,
            emit=emit,
        )
    finally:
        # Playback stops however the session leaves the loop — handed
        # off, refused or failed. A wall left holding the last patch
        # after a session ends is a rig in an unknown state.
        device.stop_playback()

    session_end = clock()
    protocol = protocol_patches()
    artifact = MeasurementsArtifact(
        red_xy=chromaticity(readings["red"]),
        green_xy=chromaticity(readings["green"]),
        blue_xy=chromaticity(readings["blue"]),
        white_xy=chromaticity(readings["white"]),
        black_level=luminance(readings[BLACK_PATCH]),
        # The white patch fills both white_point and peak_luminance.
        peak_luminance=luminance(readings["white"]),
        ambient_floor=ambient_floor,
        instrument=identity(instrument),
        processor_state=recorded_state,
        session_start=session_start,
        session_end=session_end,
        protocol_name=PROTOCOL_NAME,
        presentation_order=tuple(patch.name for patch in presented),
        per_channel_response=PerChannelResponse(
            red=_ramp(protocol, readings, "red_response", anchor="red"),
            green=_ramp(protocol, readings, "green_response", anchor="green"),
            blue=_ramp(protocol, readings, "blue_response", anchor="blue"),
        ),
        gray_response=_ramp(protocol, readings, "gray_response", anchor="white"),
        additivity=AdditivityTriad(
            yellow_xyz=xyz(readings["yellow"]),
            cyan_xyz=xyz(readings["cyan"]),
            magenta_xyz=xyz(readings["magenta"]),
        ),
        instrument_routing=None if disciplined is None else disciplined.routing(),
        patch_seconds=patch_seconds,
    )
    _handoff(artifact, out_path, emit)


def _drive_presentation(
    device: PatchDrive,
    instrument: Instrument,
    presented: tuple[Patch, ...],
    *,
    clock: Clock,
    settle_seconds: float,
    declared_intensity: str,
    emit: EventSink,
) -> tuple[dict[str, InstrumentReading], tuple[float, ...], float]:
    """Drive every patch; return the readings, their durations, and the floor.

    Each patch is one drive-settle-read step, timed end to end by the
    session clock so a consumer estimates the remaining time from
    observed pace (§spec:session-events).
    """
    readings: dict[str, InstrumentReading] = {}
    patch_seconds: list[float] = []
    ambient_floor: float | None = None

    for index, patch in enumerate(presented, start=1):
        began = clock()
        _drive(device, patch, index, emit)
        _settle(settle_seconds, index, emit)
        measurement = _read(instrument, patch)
        seconds = (clock() - began).total_seconds()
        readings[patch.name] = measurement
        patch_seconds.append(seconds)
        emit(PatchCompleted(index, patch.name, xyz(measurement), seconds))

        if patch.name == BLACK_PATCH:
            # Gated the moment black is read, not at handoff: the budget
            # refusal §road:session-gates adds has to stop a session
            # early to be worth anything.
            ambient_floor = ambient_gate(measurement)
            emit(
                GateEvaluated(
                    Gate.AMBIENT,
                    GateVerdict.STUB,
                    f"recorded floor {ambient_floor:.4f} cd/m²; budget not "
                    "enforced (§road:session-gates)",
                )
            )
        elif patch.name == WHITE_PATCH:
            # Gated where white is read, which protocol 3 pins second:
            # the contract audit established what the processor claims,
            # and this is where the wall either does it or does not.
            peak = luminance(measurement)
            with _gate(emit, Gate.OUTPUT_LEVEL):
                audit_output_level(peak, declared_intensity)
            emit(
                GateEvaluated(
                    Gate.OUTPUT_LEVEL,
                    GateVerdict.PASS,
                    f"white measured {peak:.4g} cd/m² against "
                    f"{declared_intensity} declared",
                )
            )

    if ambient_floor is None:
        raise RuntimeError(
            f"the protocol drove no {BLACK_PATCH!r} patch; the ambient gate "
            "has nothing to consume"
        )
    return readings, tuple(patch_seconds), ambient_floor


def normalized_reading(
    declared: ProcessorStateSnapshot = DECLARED_CONTRACT,
) -> ProcessorStateSnapshot:
    """A reading that matches `declared` exactly.

    The doubles have no processor to read, and a gate they cannot pass
    would put hardware in the way of every laptop run and CI job. They
    declare compliance instead; only a hardware session earns the audit.
    """
    return declared


def doubles_session(
    out_path: Path,
    *,
    clock: Clock,
    settle_seconds: float,
    seed: int | None = None,
    hybrid: bool = False,
    luminance_threshold: float = DEFAULT_LUMINANCE_THRESHOLD,
    declared: ProcessorStateSnapshot = DECLARED_CONTRACT,
    emit: EventSink = log_events,
) -> MockBMDDeckLink:
    """Run one characterize session against the device doubles.

    The default instrument is the plausible wall (rationale and model in
    :mod:`display_measure.plausible_wall`). Passing `seed` selects
    colour-specio's random virtual spectrometer instead, seeded through
    numpy's global RNG — plumbing-only readings — and also keys the
    presentation shuffle (the default shuffles with seed 0). `hybrid`
    pairs the wall with the mismatched colorimeter double, exercising
    the disciplined-colorimeter path without hardware. Returns the
    closed mock device; its recorded history lets tests observe the
    drive without duplicating this wiring.
    """
    with MockBMDDeckLink(DECKLINK_INDEX) as device:
        # The mock caps frame history at 10 — too small to observe a
        # full-protocol drive. Widening it here keeps the observability
        # this function promises; an upstream gap worth closing
        # (MockBMDDeckLink hardcodes _max_frame_history).
        device._max_frame_history = len(protocol_patches())
        instrument: Instrument
        if hybrid:
            wall = PlausibleWall(device)
            instrument = HybridInstrument(
                wall,
                MismatchedColorimeter(wall),
                luminance_threshold=luminance_threshold,
            )
        elif seed is not None:
            # Deferred: specio drags colour + scipy (~0.8 s) that the
            # default wall path never needs.
            from specio.spectrometers import VirtualSpectrometer

            np.random.seed(seed)
            instrument = VirtualSpectrometer()
        else:
            instrument = PlausibleWall(device)
        characterize(
            device=device,
            instrument=instrument,
            out_path=out_path,
            clock=clock,
            settle_seconds=settle_seconds,
            seed=seed if seed is not None else 0,
            declared=declared,
            reading=normalized_reading(declared),
            emit=emit,
        )
    return device


def _audit_processor(
    processor_host: str | None,
    declared: ProcessorStateSnapshot,
) -> ProcessorStateSnapshot:
    """Read the processor and gate on it, before any hardware is touched."""
    if processor_host is None:
        raise ContractViolation(
            "no processor host given: a session cannot audit a contract it "
            "cannot read (§road:processor-state-snapshot). Pass --processor "
            "with the Tessera host."
        )
    processor = TesseraProcessor(processor_host)
    global_colour = processor.global_colour()
    reading = audit_contract(declared, state_from_tessera(global_colour))
    audit_output_scaling(global_colour)
    audit_wire_format(DECLARED_WIRE_FORMAT, processor.input_metadata())
    return reading


def hardware_session(
    out_path: Path,
    *,
    clock: Clock,
    settle_seconds: float,
    hybrid: bool = False,
    luminance_threshold: float = DEFAULT_LUMINANCE_THRESHOLD,
    processor_host: str | None = None,
    declared: ProcessorStateSnapshot = DECLARED_CONTRACT,
    emit: EventSink = log_events,
) -> None:
    """Run one characterize session against the bench instruments.

    Audits the processor before anything else: `processor_host` is read
    read-only over the Tessera API and the session refuses on any
    divergence from `declared` (§spec:sessions). The gate runs ahead of
    instrument discovery and the DeckLink open, so a refusal costs a
    round trip rather than a rig.

    Discovers the Colorimetry Research spectroradiometer over serial —
    and, for `hybrid`, the colorimeter it disciplines against — then
    drives the protocol through the DeckLink. Discovery is by instrument
    type, so both instruments may stay plugged in.
    """
    reading = _audit_processor(processor_host, declared)

    # Deferred: specio drags colour + scipy (~0.8 s) that no doubles
    # path and no `--help` ever needs.
    from specio.spectrometers import CRSpectrometer

    spectroradiometer = CRSpectrometer.discover()
    instrument: Instrument = spectroradiometer
    _log_instrument(spectroradiometer)
    if hybrid:
        from specio.colorimeters import CRColorimeter

        colorimeter = CRColorimeter.discover()
        _log_instrument(colorimeter, note=", disciplined in session")
        instrument = HybridInstrument(
            spectroradiometer,
            colorimeter,
            luminance_threshold=luminance_threshold,
        )
    with BMDDeckLink(DECKLINK_INDEX) as device:
        characterize(
            device=device,
            instrument=instrument,
            out_path=out_path,
            clock=clock,
            settle_seconds=settle_seconds,
            declared=declared,
            reading=reading,
            emit=emit,
        )
