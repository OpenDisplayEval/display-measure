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

Cancellation is asked between patch steps and nowhere else. Stopping
mid-patch would leave a driven frame with no reading, and the artifact
is all-or-nothing (§spec:artifact-chain) — so a cancelled session
stops playback, writes nothing, and raises `SessionCancelled`.

Determinism: see the determinism-seam design in
`display_measure.artifact`'s module docstring.
"""

import hashlib
import logging
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import replace
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
    WireEncoding,
    check_seam_path,
    verify,
    write,
)
from display_measure.consistency import InconsistentSession
from display_measure.events import (
    Cancelled,
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
    ProbeCompleted,
    ProbeStarted,
    SessionCancelled,
    SessionEnded,
    SessionMode,
    SessionStarted,
    UnreadablePatch,
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
    spectrum,
    xyz,
)
from display_measure.plausible_display import MismatchedColorimeter, PlausibleDisplay
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
    VERIFY_SUITE,
    WHITE_PATCH,
    MeasurementSuite,
    Patch,
    ProbeResult,
    presentation_order,
)
from display_measure.session_log import log_events
from display_measure.wire import RGB12, encode_pixel

# Conditions are a property of the protocol (`MeasurementSuite`),
# not of the session: they are part of what makes two artifacts
# comparable, and a caller that has to remember them is a caller that
# will not. A session may still override one, which is what a bench
# investigation needs and a comparable run does not do.

__all__ = [
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


def _frame(encoding: WireEncoding, rgb: tuple[int, int, int]) -> npt.NDArray[np.uint16]:
    """A flat field of the codes the wire carries for `rgb`.

    One pixel is encoded and broadcast: a patch is a flat field, so
    converting 1920x1080 of it per patch would buy nothing.
    """
    codes = encode_pixel(encoding, rgb)
    return np.full((FRAME_HEIGHT, FRAME_WIDTH, 3), codes, dtype=np.uint16)


def never_cancelled() -> bool:
    """The default cancel source: a session with none runs to handoff."""
    return False


@contextmanager
def _gate(
    emit: EventSink,
    gate: Gate,
    refusals: tuple[type[Exception], ...] = (ContractViolation,),
) -> Iterator[None]:
    """Report a refusal as the gate's own outcome before it propagates.

    A session-end event carries the message but not the check that
    produced it, and "the display measures 0.56x its declared intensity"
    calls for a different trip than "the processor has overdrive on".
    Naming the gate is what lets a consumer show the operator where to
    go (§spec:web-ui).

    `refusals` names the exceptions this gate refuses with, so a gate
    only ever claims a failure it actually raised.
    """
    try:
        yield
    except refusals as e:
        emit(GateEvaluated(gate, GateVerdict.REFUSED, str(e)))
        raise


@contextmanager
def _session_outcome(emit: EventSink, clock: Clock) -> Iterator[None]:
    """Close the stream with exactly one `SessionEnded`, on every path.

    A consumer's progress display waits forever on a session that
    stopped without saying so, and the paths that stop one — a gate
    refusing, an instrument raising, an operator cancelling — are
    exactly the ones nobody remembers to instrument by hand.

    The three failure outcomes are distinguished because the operator's
    next move differs: cancelled is what they asked for, refused sends
    them to the rig, and failed sends them to a bug report.

    A `BaseException` passes through unreported — the second Ctrl-C
    after the CLI's handler stands down is one, and aborting now is
    exactly what it means.
    """
    try:
        yield
    except SessionCancelled as e:
        emit(SessionEnded(Outcome.CANCELLED, clock(), str(e)))
        raise
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


def _pixel_format(encoding: WireEncoding) -> PixelFormatType:
    """The device format that packs the declared layout.

    pypixelpack's layout names are the DeckLink FourCCs, so the map is
    the enum itself; the bit depths are checked against each other so a
    declaration cannot drift from what the device is set to.
    """
    by_fourcc = {f.value.lower(): f for f in PixelFormatType}
    packed = by_fourcc.get(encoding.layout)
    if packed is None:
        raise ValueError(f"no DeckLink pixel format packs {encoding.layout!r}")
    if packed.bit_depth != encoding.bit_depth:
        raise ValueError(
            f"{encoding.layout} is declared {encoding.bit_depth}-bit but "
            f"{packed.name} packs {packed.bit_depth}-bit"
        )
    return packed


def _setup_drive(device: PatchDrive, encoding: WireEncoding, emit: EventSink) -> None:
    """Declare pixel format and EOTF signaling, then start playback.

    The pixel format is the declared encoding's, so the wire the gate
    held the processor to is the wire the device packs. bmd-signal-gen
    defaults to PQ InfoFrames, which would fault an SDR-contract
    display, so the session signals explicitly (§spec:sessions).
    """
    packed = _pixel_format(encoding)
    device.pixel_format = packed
    device.set_hdr_metadata(HDRMetadata(eotf=EOTFType.SDR))
    device.start_playback()
    # The enum names, not the enums: an event crossing a wire should
    # not drag bmd-signal-gen in behind it.
    emit(PlaybackStarted(packed.name, EOTFType.SDR.name))


def _drive(
    device: PatchDrive,
    encoding: WireEncoding,
    patch: Patch,
    index: int,
    emit: EventSink,
) -> None:
    emit(PatchStarted(index, patch.name, patch.rgb))
    device.display_frame(_frame(encoding, patch.rgb))


def _settle(seconds: float, index: int, emit: EventSink) -> None:
    # Announced before the wait, not after: a settle and an instrument
    # read are where a session sits still, and saying so as it starts
    # is what makes a run auditable while it happens (§spec:sessions).
    emit(PatchSettling(index, seconds))
    time.sleep(seconds)


# Conditioning frames go out at roughly this rate, which is what
# display-report's retired measure path drove. The interval is the
# floor, not the achieved rate: driving a frame takes time of its own.
CONDITIONING_INTERVAL = 3 / 24


def _condition(
    device: PatchDrive,
    encoding: WireEncoding,
    seconds: float,
    *,
    seed: int,
    label: str,
) -> None:
    """Drive random colour for `seconds`, to hold the panel at video-like load.

    An LED panel measured on a run of solid patches is not the panel an
    operator drives: junction temperature settles somewhere a moving
    picture never takes it, and the response measured there is the
    response of a thermal state the display does not otherwise occupy.
    The retired measure path ran this between every patch and for ten
    minutes before the first, which is why its numbers are the ones the
    report's analysis was calibrated against.

    Colours come from the session seed, not an RNG: nothing records
    them, but they reach the device, and the determinism seam holds
    that two runs of one seed drive one sequence of frames
    (§spec:artifact-chain).
    """
    if seconds <= 0:
        return
    deadline = time.monotonic() + seconds
    frame = 0
    while time.monotonic() < deadline:
        digest = hashlib.sha256(f"{seed}:{label}:{frame}".encode()).digest()
        rgb = tuple(
            int.from_bytes(digest[word * 4 : word * 4 + 4], "big") % (FULL_DRIVE + 1)
            for word in range(3)
        )
        device.display_frame(_frame(encoding, rgb))  # type: ignore[arg-type]
        frame += 1
        # Never past the deadline: conditioning is a duration the
        # session was given, and a frame interval is not a reason to
        # spend more of it than that.
        time.sleep(min(CONDITIONING_INTERVAL, max(0.0, deadline - time.monotonic())))


def _read(
    instrument: Instrument, patch: Patch, *, attempts: int = 1
) -> InstrumentReading:
    # A disciplined instrument routes by patch, so it is read by name;
    # the patch is already in hand here, which keeps the instrument
    # from shadowing the session's own iteration (§spec:sessions).
    #
    # A read is retried because the failures this instrument produces at
    # the bottom of a panel are transient: a timed-out integration or a
    # truncated serial reply says the link stumbled, not that the patch
    # is unreadable. The retired measure path allowed ten attempts, and
    # a session of 799 patches cannot be thrown away by one of them.
    # Gate refusals are not retried — a refusal is a verdict, and asking
    # again does not change it.
    last: Exception | None = None
    for _ in range(max(attempts, 1)):
        try:
            if isinstance(instrument, DisciplinedInstrument):
                return instrument.measure_patch(patch.name)
            return instrument.measure()
        except DerivationRefused:
            raise
        except Exception as failure:
            last = failure
    raise UnreadablePatch(
        f"no reading for patch {patch.name!r} in {attempts} attempts"
    ) from last


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
    """Write the immutable seam file, then read it back and report its hash.

    The read-back is not ceremony: the reported hash is what a promotion
    records, and a session that has just spent its protocol should prove
    the file it wrote parses and verifies before it claims a handoff
    (§spec:measurements-artifact).
    """
    written = write(artifact, out_path)
    verified = verify(out_path)
    if verified != written:
        raise RuntimeError(
            f"{out_path} did not survive its own round trip: written as "
            f"{written}, read back as {verified}"
        )
    emit(HandoffCompleted(str(out_path), verified))


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
    # Sorted by code, not taken in patch order. A ramp is assembled from
    # whichever blocks the suite composed — `response` carries the
    # half-octave ladder and `tracking` the even codes between its rungs
    # — so patch order is block order, which interleaves nothing and
    # ascends nowhere. The artifact promises protocol order, and the
    # self-consistency gate reads these rows as a ramp.
    points = sorted(
        (
            ResponsePoint(code=max(patch.rgb), xyz=xyz(readings[patch.name]))
            for patch in protocol
            if patch.role == role
        ),
        key=lambda point: point.code,
    )
    return (*points, ResponsePoint(code=FULL_DRIVE, xyz=xyz(readings[anchor])))


def characterize(
    device: PatchDrive,
    instrument: Instrument,
    out_path: Path,
    *,
    clock: Clock,
    settle_seconds: float,
    suite: MeasurementSuite,
    warmup_seconds: float | None = None,
    conditioning_seconds: float | None = None,
    read_attempts: int | None = None,
    seed: int = 0,
    encoding: WireEncoding = RGB12,
    declared: ProcessorStateSnapshot = DECLARED_CONTRACT,
    reading: ProcessorStateSnapshot | None = None,
    emit: EventSink = log_events,
    cancelled: Cancelled = never_cancelled,
) -> None:
    """Run one characterize session and write the measurements artifact.

    Drives the versioned patch protocol through `device` in shuffled
    presentation order (`seed` keys the shuffle and nothing else —
    instrument behavior is the caller's wiring), reads each patch with
    `instrument`, and hands off the immutable artifact at `out_path`.
    Raises FileExistsError rather than overwriting. `encoding` is the
    link the patches ride to the device; the caller has already held
    the processor to it where there is one to hold.

    An instrument that also satisfies `DisciplinedInstrument` gets the
    patches its correction needs driven first and reports its routing
    into the artifact; drive, settle, and read are the same stages
    either way.

    Reports its whole lifecycle to `emit` (§spec:session-events),
    which defaults to the session log. `cancelled` is asked between
    patch steps: when it answers true the session stops playback,
    writes no artifact, and raises `SessionCancelled`.
    """
    # Before the stream opens and before anything is driven: a session
    # that measures for twenty minutes and then cannot name its output
    # file has thrown the protocol away.
    check_seam_path(out_path)
    session_start = clock()
    emit(
        SessionStarted(
            mode=SessionMode.CHARACTERIZE,
            protocol_name=suite.legacy_name or suite.name,
            patch_count=len(suite.patches),
            at=session_start,
        )
    )
    # The protocol carries the conditions it is measured under; an
    # explicit argument overrides one, which is what a bench
    # investigation needs and a comparable session does not do.
    warmup_seconds = suite.warmup_seconds if warmup_seconds is None else warmup_seconds
    conditioning_seconds = (
        suite.conditioning_seconds
        if conditioning_seconds is None
        else conditioning_seconds
    )
    read_attempts = suite.read_attempts if read_attempts is None else read_attempts

    with _session_outcome(emit, clock):
        _characterize(
            device,
            instrument,
            out_path,
            clock=clock,
            settle_seconds=settle_seconds,
            suite=suite,
            warmup_seconds=warmup_seconds,
            conditioning_seconds=conditioning_seconds,
            read_attempts=read_attempts,
            seed=seed,
            encoding=encoding,
            declared=declared,
            reading=reading,
            emit=emit,
            cancelled=cancelled,
            session_start=session_start,
        )


def _characterize(
    device: PatchDrive,
    instrument: Instrument,
    out_path: Path,
    *,
    clock: Clock,
    settle_seconds: float,
    suite: MeasurementSuite,
    warmup_seconds: float,
    conditioning_seconds: float,
    read_attempts: int,
    seed: int,
    encoding: WireEncoding,
    declared: ProcessorStateSnapshot,
    reading: ProcessorStateSnapshot | None,
    emit: EventSink,
    cancelled: Cancelled,
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

    _setup_drive(device, encoding, emit)

    # Warmup precedes everything measured. An LED panel's output drifts
    # for minutes after it starts driving, and a session that opens on
    # its most delicate reading — black — reads that drift as the
    # display's black level. Ten minutes is what the retired measure
    # path allowed, and the report's analysis was calibrated on panels
    # that had it.
    _condition(device, encoding, warmup_seconds, seed=seed, label="warmup")

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
    presented = presentation_order(suite.patches, seed, pinned=pinned)
    try:
        readings, patch_seconds, ambient_floor = _drive_presentation(
            device,
            encoding,
            instrument,
            presented,
            clock=clock,
            settle_seconds=settle_seconds,
            conditioning_seconds=conditioning_seconds,
            read_attempts=read_attempts,
            seed=seed,
            declared_intensity=declared.intensity,
            emit=emit,
            cancelled=cancelled,
        )
    finally:
        # Playback stops however the session leaves the loop — handed
        # off, refused, failed or cancelled. A display left holding the
        # last patch after a session ends is a rig in an unknown state,
        # and cancellation in particular promises the drive stops
        # (§spec:session-events).
        device.stop_playback()

    # Probes run here: after every shuffled patch, because each of a
    # probe's own patches depends on the reading before it, and because
    # it searches against the black the anchors just measured.
    probe_results = _run_probes(
        suite,
        device,
        encoding,
        instrument,
        ambient_floor,
        read_attempts=read_attempts,
        settle_seconds=settle_seconds,
        emit=emit,
    )

    session_end = clock()
    patches = suite.patches
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
        wire_encoding=encoding,
        # What the wire carried for each patch, as driven.
        wire_codes=tuple(encode_pixel(encoding, patch.rgb) for patch in presented),
        # The spectrum behind each reading, and how it was obtained
        # (§spec:spectral-retention). Discarding it at this boundary
        # would be unrecoverable, and keeping it is free.
        spectra=tuple(spectrum(readings[patch.name]) for patch in presented),
        # The seam file's own rows: what was driven, and what was read.
        driven_codes=tuple(patch.rgb for patch in presented),
        readings=tuple(xyz(readings[patch.name]) for patch in presented),
        protocol_name=suite.legacy_name or suite.name,
        protocol_blocks=suite.block_ids,
        probe_results=probe_results,
        presentation_order=tuple(patch.name for patch in presented),
        # An artifact carries what its suite measured and no more. A
        # session composing only `anchors` has no ramps and no
        # secondaries, and inventing empty ones would report an
        # unmeasured thing as a measured one (§spec:patch-protocol).
        per_channel_response=PerChannelResponse(
            red=_ramp(patches, readings, "red_response", anchor="red"),
            green=_ramp(patches, readings, "green_response", anchor="green"),
            blue=_ramp(patches, readings, "blue_response", anchor="blue"),
        )
        if _measured(patches, "red_response")
        else None,
        gray_response=(
            _ramp(patches, readings, "gray_response", anchor="white")
            if _measured(patches, "gray_response")
            else None
        ),
        additivity=AdditivityTriad(
            yellow_xyz=xyz(readings["yellow"]),
            cyan_xyz=xyz(readings["cyan"]),
            magenta_xyz=xyz(readings["magenta"]),
        )
        if _measured(patches, "additivity_yellow")
        else None,
        instrument_routing=None if disciplined is None else disciplined.routing(),
        patch_seconds=patch_seconds,
    )
    # The last gate, and the only one that cannot refuse early: a ramp is
    # not a ramp until it is measured. It still prevents the artifact,
    # which is the thing that outlives the session — a measurement that
    # contradicts itself never enters the chain to be promoted later by
    # someone who was not in the room (§road:session-consistency).
    _audit_self_consistency(artifact, emit)
    _handoff(artifact, out_path, emit)


def _run_probes(
    suite: MeasurementSuite,
    device: PatchDrive,
    encoding: WireEncoding,
    instrument: Instrument,
    floor: float | None,
    *,
    read_attempts: int,
    settle_seconds: float,
    emit: EventSink,
) -> tuple[ProbeResult, ...]:
    """Run the suite's probes, each against the floor the session measured.

    A probe is handed a `read` that drives a code and returns its
    luminance; everything else — settle, instrument, retry — stays the
    session's, so a probe holds nothing but its search.
    """
    if not suite.probes:
        return ()

    def read(rgb: tuple[int, int, int]) -> float:
        device.display_frame(_frame(encoding, rgb))
        time.sleep(settle_seconds)
        probe_patch = Patch(f"probe_{rgb[0]}_{rgb[1]}_{rgb[2]}", rgb, role="probe")
        return luminance(_read(instrument, probe_patch, attempts=read_attempts))

    results = []
    for probe in suite.probes:
        emit(ProbeStarted(probe.id, probe.max_patches))
        result = replace(probe, floor=floor).run(read)
        emit(ProbeCompleted(probe.id, result.patch_count, dict(result.findings)))
        results.append(result)
    return tuple(results)


def _measured(patches: tuple[Patch, ...], role: str) -> bool:
    """Whether the driven suite carried any patch filling `role`."""
    return any(patch.role == role for patch in patches)


def _audit_self_consistency(artifact: MeasurementsArtifact, emit: EventSink) -> None:
    """Refuse an artifact whose own rows disagree (§road:session-consistency)."""
    from display_measure.consistency import (
        audit_ramp_monotonicity,
        audit_routing_boundary,
    )

    ramps = {
        "gray": [(p.code, p.xyz[1]) for p in artifact.gray_response or ()],
    }
    if artifact.per_channel_response is not None:
        for name in ("red", "green", "blue"):
            rows = getattr(artifact.per_channel_response, name)
            ramps[name] = [(p.code, p.xyz[1]) for p in rows]

    with _gate(emit, Gate.SELF_CONSISTENCY, (InconsistentSession,)):
        audit_ramp_monotonicity({k: v for k, v in ramps.items() if v})
        routing = artifact.instrument_routing
        if routing is not None:
            by_patch = dict(
                zip(artifact.presentation_order or (), routing.sources, strict=False)
            )
            routed = {
                name: _routed_rows(name, rows, by_patch)
                for name, rows in ramps.items()
                if rows
            }
            audit_routing_boundary({k: v for k, v in routed.items() if v})
    emit(
        GateEvaluated(
            Gate.SELF_CONSISTENCY,
            GateVerdict.PASS,
            "ramps rise and the instruments agree where they hand over",
        )
    )


def _routed_rows(
    name: str,
    rows: list[tuple[int, float]],
    by_patch: dict[str, str],
) -> list[tuple[int, float, str]]:
    """Ramp rows carrying the instrument that produced each one.

    The artifact records sources in presentation order and rows in
    protocol order, so the patch name is what joins them. A row whose
    patch is not in the map — the full-drive anchor folded into each
    ramp — is dropped rather than guessed at.
    """
    prefix = "gray" if name == "gray" else name
    out: list[tuple[int, float, str]] = []
    for code, y in rows:
        source = by_patch.get(f"{prefix}_{code:04d}")
        if source is not None:
            out.append((code, y, source))
    return out


def _drive_presentation(
    device: PatchDrive,
    encoding: WireEncoding,
    instrument: Instrument,
    presented: tuple[Patch, ...],
    *,
    clock: Clock,
    settle_seconds: float,
    conditioning_seconds: float,
    read_attempts: int,
    seed: int,
    declared_intensity: str,
    emit: EventSink,
    cancelled: Cancelled,
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
        if cancelled():
            # Between steps and nowhere else. A patch stopped mid-step
            # leaves a frame on the display with no reading behind it, and
            # the artifact is all-or-nothing (§spec:artifact-chain), so
            # there is nothing to salvage by stopping sooner.
            raise SessionCancelled(
                f"cancelled after {index - 1} of {len(presented)} patches; "
                "no artifact written"
            )
        began = clock()
        # Conditioning precedes the patch, so the panel arrives at every
        # reading from video-like load rather than from the previous
        # solid patch (§spec:patch-protocol).
        _condition(
            device,
            encoding,
            conditioning_seconds,
            seed=seed,
            label=f"patch:{index}",
        )
        _drive(device, encoding, patch, index, emit)
        _settle(settle_seconds, index, emit)
        # A disciplined instrument derives its correction from the
        # rungs it reads first, and refuses here when that correction
        # is unfit to extrapolate (§spec:session-gates). The gate lives
        # inside the instrument, so the read is where the session can
        # name it.
        with _gate(emit, Gate.DERIVATION_FITNESS, (DerivationRefused,)):
            measurement = _read(instrument, patch, attempts=read_attempts)
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
            # and this is where the display either does it or does not.
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
    suite: MeasurementSuite = VERIFY_SUITE,
    # The doubles have no junction temperature to hold anywhere, and no
    # serial link to stumble on. Conditioning a mock would buy nothing
    # and cost the report protocol's 795 patches five seconds each, so
    # the doubles override the protocol's conditions rather than
    # inheriting them.
    warmup_seconds: float = 0.0,
    conditioning_seconds: float = 0.0,
    read_attempts: int = 1,
    seed: int | None = None,
    hybrid: bool = False,
    luminance_threshold: float = DEFAULT_LUMINANCE_THRESHOLD,
    encoding: WireEncoding = RGB12,
    declared: ProcessorStateSnapshot = DECLARED_CONTRACT,
    emit: EventSink = log_events,
    cancelled: Cancelled = never_cancelled,
) -> MockBMDDeckLink:
    """Run one characterize session against the device doubles.

    The default instrument is the plausible display (rationale and model in
    :mod:`display_measure.plausible_display`), decoding the declared
    `encoding` as a display whose processor passed the wire-format gate
    would. Passing `seed` selects colour-specio's random virtual
    spectrometer instead, seeded through numpy's global RNG —
    plumbing-only readings — and also keys the presentation shuffle (the
    default shuffles with seed 0). `hybrid` pairs the display with the
    mismatched colorimeter double, exercising the disciplined-colorimeter
    path without hardware. Returns the closed mock device; its recorded
    history lets tests observe the drive without duplicating this wiring.
    """
    with MockBMDDeckLink(DECKLINK_INDEX) as device:
        # The mock caps frame history at 10 — too small to observe a
        # full-protocol drive. Widening it here keeps the observability
        # this function promises; an upstream gap worth closing
        # (MockBMDDeckLink hardcodes _max_frame_history).
        device._max_frame_history = len(suite.patches)
        instrument: Instrument
        if hybrid:
            display = PlausibleDisplay(device, encoding=encoding)
            instrument = HybridInstrument(
                display,
                MismatchedColorimeter(display),
                luminance_threshold=luminance_threshold,
            )
        elif seed is not None:
            # Deferred: specio drags colour + scipy (~0.8 s) that the
            # default display path never needs.
            from specio.spectrometers import VirtualSpectrometer

            np.random.seed(seed)
            instrument = VirtualSpectrometer()
        else:
            instrument = PlausibleDisplay(device, encoding=encoding)
        characterize(
            device=device,
            instrument=instrument,
            out_path=out_path,
            clock=clock,
            settle_seconds=settle_seconds,
            suite=suite,
            warmup_seconds=warmup_seconds,
            conditioning_seconds=conditioning_seconds,
            read_attempts=read_attempts,
            seed=seed if seed is not None else 0,
            encoding=encoding,
            declared=declared,
            reading=normalized_reading(declared),
            emit=emit,
            cancelled=cancelled,
        )
    return device


def _audit_processor(
    processor_host: str | None,
    declared: ProcessorStateSnapshot,
    encoding: WireEncoding,
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
    audit_wire_format(WireFormat.for_encoding(encoding), processor.input_metadata())
    return reading


def hardware_session(
    out_path: Path,
    *,
    clock: Clock,
    settle_seconds: float,
    suite: MeasurementSuite = VERIFY_SUITE,
    warmup_seconds: float | None = None,
    conditioning_seconds: float | None = None,
    read_attempts: int | None = None,
    hybrid: bool = False,
    luminance_threshold: float = DEFAULT_LUMINANCE_THRESHOLD,
    processor_host: str | None = None,
    encoding: WireEncoding = RGB12,
    declared: ProcessorStateSnapshot = DECLARED_CONTRACT,
    emit: EventSink = log_events,
    cancelled: Cancelled = never_cancelled,
) -> None:
    """Run one characterize session against the bench instruments.

    Audits the processor before anything else: `processor_host` is read
    read-only over the Tessera API and the session refuses on any
    divergence from `declared`, or an input link other than `encoding`
    (§spec:sessions). The gate runs ahead of instrument discovery and
    the DeckLink open, so a refusal costs a round trip rather than a rig.

    Discovers the Colorimetry Research spectroradiometer over serial —
    and, for `hybrid`, the colorimeter it disciplines against — then
    drives the protocol through the DeckLink. Discovery is by instrument
    type, so both instruments may stay plugged in.
    """
    reading = _audit_processor(processor_host, declared, encoding)

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
            suite=suite,
            warmup_seconds=warmup_seconds,
            conditioning_seconds=conditioning_seconds,
            read_attempts=read_attempts,
            encoding=encoding,
            declared=declared,
            reading=reading,
            emit=emit,
            cancelled=cancelled,
        )
