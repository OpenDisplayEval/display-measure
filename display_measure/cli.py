"""The `display-measure` command line surface.

One command per session mode (§spec:sessions). `characterize` lands
with the walking skeleton; `verify` and `snapshot` register here when
their workstreams ship — no placeholder commands before then.
`--instrument` chooses what measures: the doubles by default, so a
laptop run and CI need no hardware at all.

The session log this command prints is a consumer of the session's
event stream, not a second reporting path (§spec:session-events); the
core defaults its sink to the renderer, so this file wires no logging
of its own beyond the handler. Ctrl-C is the CLI's cancel source.
"""

import logging
import signal
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from types import FrameType

import typer

# Not deferred: `display_measure.events` is stdlib-only by design, so
# the lifecycle contract costs `--help` nothing (§spec:session-events).
from display_measure.events import Cancelled, SessionCancelled

app = typer.Typer(
    name="display-measure",
    help="display-measure: gated instrument sessions for display characterization.",
    no_args_is_help=True,
)

DEFAULT_SETTLE_SECONDS = 0.5
# 128 + SIGINT, the shell convention for a process the operator
# interrupted. Distinct from the refusal code, because a cancelled
# session found nothing wrong with the rig.
CANCELLED_EXIT_CODE = 130
# Restated rather than imported: `display_measure.hybrid` pulls numpy,
# which no `--help` or argument error should pay for. The deferred
# import below asserts the two agree.
DEFAULT_LUMINANCE_THRESHOLD = 10.0


class InstrumentChoice(StrEnum):
    """What a session measures with.

    The doubles run anywhere; the hardware modes discover the
    Colorimetry Research instruments over serial. A hybrid session adds
    the colorimeter, disciplined in session against the
    spectroradiometer, and reads the dark patches with it
    (§spec:sessions).
    """

    DOUBLES = "doubles"
    DOUBLES_HYBRID = "doubles-hybrid"
    SPECTRO = "spectro"
    HYBRID = "hybrid"


class WireChoice(StrEnum):
    """The link the patches ride to the device (§spec:measure-sessions).

    Declared here, held against the processor's input metadata, and
    recorded in the artifact. Restated rather than imported:
    `display_measure.wire` pulls numpy and pypixelpack, which `--help`
    should not pay for; the deferred import below asserts the two agree.
    """

    RGB12 = "rgb12"
    V210 = "v210"
    V210_BT2020 = "v210-bt2020"


@app.callback()
def main() -> None:
    """Measure displays through the show signal chain (§spec:sessions)."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(message)s",
        stream=sys.stderr,
    )


@contextmanager
def _cancel_on_interrupt() -> Iterator[Cancelled]:
    """Ctrl-C asks the session to stop; it stops after the current patch.

    A session holds a DeckLink and an instrument mid-conversation, and
    the gap between patches is the only place it can stop without
    leaving a driven frame unread (§spec:session-events). So the first
    interrupt only raises a flag the session reads there.

    The default handler goes back in behind it, so a second Ctrl-C
    kills the process outright — the escape hatch an instrument read
    that never returns needs, and the reason this does not simply
    swallow SIGINT for the run.
    """
    stop = threading.Event()

    def interrupted(_signum: int, _frame: FrameType | None) -> None:
        stop.set()
        signal.signal(signal.SIGINT, previous)
        typer.echo(
            "Cancelling: stopping after the current patch. "
            "Interrupt again to abort now.",
            err=True,
        )

    previous = signal.signal(signal.SIGINT, interrupted)
    try:
        yield stop.is_set
    finally:
        signal.signal(signal.SIGINT, previous)


def _parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise typer.BadParameter(
            f"{value!r} lacks a timezone offset; timestamps are absolute"
        )
    return parsed


@app.command()
def characterize(
    out: Path = typer.Option(
        ...,
        "--out",
        help="Path for the measurements artifact (refuses to overwrite).",
    ),
    instrument: InstrumentChoice = typer.Option(
        InstrumentChoice.DOUBLES,
        "--instrument",
        help=(
            "What measures: doubles (hardware-free), doubles-hybrid (the "
            "doubles' disciplined-colorimeter pair), spectro (CR-300), or "
            "hybrid (CR-300 disciplining a CR-120)."
        ),
    ),
    threshold: float = typer.Option(
        DEFAULT_LUMINANCE_THRESHOLD,
        "--threshold",
        min=0.0,
        help=(
            "Hybrid routing threshold, cd/m². Patches measuring at or above "
            "it go to the spectroradiometer, the rest to the disciplined "
            "colorimeter. Ignored by the single-instrument modes."
        ),
    ),
    settle: float = typer.Option(
        DEFAULT_SETTLE_SECONDS,
        "--settle",
        help="Settle delay after each patch, seconds.",
    ),
    wire: WireChoice = typer.Option(
        WireChoice.RGB12,
        "--wire",
        help=(
            "Wire encoding the patches ride to the device: rgb12 (12-bit "
            "RGB identity, the bench HDMI link), v210 (10-bit BT.709 "
            "narrow-range 4:2:2 YCbCr) or v210-bt2020 (the same layout "
            "under BT.2020, which the bench processor decodes with). The "
            "session refuses a processor receiving anything else, probes "
            "the link's range, and the artifact records what it drove."
        ),
    ),
    processor: str | None = typer.Option(
        None,
        "--processor",
        help=(
            "Tessera host (IP or name) of the LED processor. Read read-only "
            "before anything is driven; the session refuses if the processor "
            "contradicts the declared contract. Required by the hardware "
            "modes; ignored by the doubles."
        ),
    ),
    manifest: Path | None = typer.Option(
        None,
        "--manifest",
        help=(
            "Show manifest whose signal_contract declares the processor "
            "lockdown to audit against. Without it the session audits "
            "against the built-in recommended contract, which is unlikely "
            "to match a given rig."
        ),
    ),
    assume_attested: bool = typer.Option(
        False,
        "--assume-attested",
        help=(
            "Skip the operator confirmation of the attested panel state. "
            "For scripted runs; an interactive session should confirm it, "
            "since no instrument here can."
        ),
    ),
    timestamp: str | None = typer.Option(
        None,
        "--timestamp",
        help=(
            "Fixed ISO-8601 session clock (timezone required); see "
            "display_measure.artifact's determinism seam."
        ),
    ),
) -> None:
    """Characterize a display: drive the patch protocol, measure, emit the artifact.

    Ctrl-C cancels: the session stops after the patch it is on, leaves
    the display dark, and writes no artifact.
    """
    # Deferred so `--help` needs no measurement stack loaded.
    from display_measure import session
    from display_measure.artifact import DECLARED_CONTRACT
    from display_measure.hybrid import DEFAULT_LUMINANCE_THRESHOLD as _DEFAULT
    from display_measure.hybrid import DerivationRefused
    from display_measure.processor import ContractViolation, contract_from_manifest
    from display_measure.wire import WIRE_ENCODINGS

    assert DEFAULT_LUMINANCE_THRESHOLD == _DEFAULT, (
        "the CLI's restated routing default drifted from display_measure.hybrid"
    )
    assert {w.value for w in WireChoice} == set(WIRE_ENCODINGS), (
        "the CLI's restated wire encodings drifted from display_measure.wire"
    )
    encoding = WIRE_ENCODINGS[wire.value]

    if timestamp is None:
        clock = lambda: datetime.now(UTC)  # noqa: E731
    else:
        fixed = _parse_timestamp(timestamp)
        clock = lambda: fixed  # noqa: E731

    doubled = instrument in (InstrumentChoice.DOUBLES, InstrumentChoice.DOUBLES_HYBRID)
    hybrid = instrument in (InstrumentChoice.DOUBLES_HYBRID, InstrumentChoice.HYBRID)
    declared = (
        DECLARED_CONTRACT if manifest is None else contract_from_manifest(manifest)
    )
    if not doubled and declared.panel_state and not assume_attested:
        # None of this is readable from the processor, and all of it moves
        # the measurement. A manifest field alone goes stale the moment
        # someone changes it at the rig, so the one instrument that can
        # read it is asked directly.
        typer.echo(
            "The manifest attests this panel state, which cannot be read "
            "from the processor — confirm it at the panel OSD:",
            err=True,
        )
        for key, value in declared.panel_state:
            typer.echo(f"  {key}: {value}", err=True)
        if not typer.confirm("Does the panel read exactly this?"):
            typer.echo(
                "Refused: panel state unconfirmed. Update the manifest to "
                "what the panel actually reads, or set the panel to match.",
                err=True,
            )
            raise typer.Exit(2)

    try:
        with _cancel_on_interrupt() as cancelled:
            if doubled:
                session.doubles_session(
                    out,
                    clock=clock,
                    settle_seconds=settle,
                    hybrid=hybrid,
                    luminance_threshold=threshold,
                    encoding=encoding,
                    cancelled=cancelled,
                )
            else:
                session.hardware_session(
                    out,
                    clock=clock,
                    settle_seconds=settle,
                    hybrid=hybrid,
                    luminance_threshold=threshold,
                    processor_host=processor,
                    encoding=encoding,
                    declared=declared,
                    cancelled=cancelled,
                )
    except FileExistsError as e:
        typer.echo(f"Error: {e} — measurements artifacts are immutable", err=True)
        raise typer.Exit(1) from e
    except (ContractViolation, DerivationRefused) as e:
        typer.echo(f"Refused: {e}", err=True)
        raise typer.Exit(2) from e
    except SessionCancelled as e:
        typer.echo(f"Cancelled: {e}", err=True)
        raise typer.Exit(CANCELLED_EXIT_CODE) from e


@app.command()
def floor(
    instrument: InstrumentChoice = typer.Option(
        InstrumentChoice.DOUBLES,
        "--instrument",
        help="What measures. The doubles walk the plausible display.",
    ),
    wire: WireChoice = typer.Option(
        WireChoice.RGB12,
        "--wire",
        help="The link to walk. The floor is a property of the pairing.",
    ),
    axes: str = typer.Option(
        "neutral,red,green,blue",
        "--axes",
        help=(
            "Which axes to walk, comma separated. A saturated primary "
            "leaves black at a higher code than a neutral does."
        ),
    ),
    repeats: int = typer.Option(
        3, "--repeats", min=2, help="Readings per rung; the spread is the point."
    ),
    settle: float = typer.Option(1.0, "--settle", help="Settle per rung, seconds."),
    sigma: float = typer.Option(
        3.0,
        "--sigma",
        help="Separation from black, in combined standard deviations.",
    ),
    out: Path | None = typer.Option(
        None, "--out", help="Write the walk as YAML as well as printing it."
    ),
) -> None:
    """Find the lowest code that reads reproducibly brighter than black.

    A characterization is only as good as its darkest rung, and whether
    that rung is measurable depends on the display's shadow response,
    the instrument's repeatability and the ambient in the room — a
    pairing on a night, not a property of the protocol. Walk it before
    trusting a floor, and after a room changes.
    """
    from display_measure.floor import AXES, measure_floor, render_report
    from display_measure.wire import WIRE_ENCODINGS

    walked = [a.strip() for a in axes.split(",") if a.strip()]
    unknown = [a for a in walked if a not in AXES]
    if unknown:
        raise typer.BadParameter(f"unknown axes {unknown}; choose from {list(AXES)}")

    encoding = WIRE_ENCODINGS[wire.value]
    if instrument in (InstrumentChoice.DOUBLES, InstrumentChoice.DOUBLES_HYBRID):
        from bmd_sg.decklink import MockBMDDeckLink

        from display_measure.plausible_display import PlausibleDisplay

        with MockBMDDeckLink(0) as device:
            device._max_frame_history = len(walked) * 64
            report = measure_floor(
                device,
                PlausibleDisplay(device, encoding=encoding),
                encoding,
                axes=walked,
                repeats=repeats,
                settle_seconds=settle,
                sigma=sigma,
            )
    else:
        from bmd_sg.decklink import BMDDeckLink
        from specio.spectrometers import CRSpectrometer

        from display_measure.session import DECKLINK_INDEX, _setup_drive
        from display_measure.session_log import log_events

        spectroradiometer = CRSpectrometer.discover()
        with BMDDeckLink(DECKLINK_INDEX) as device:
            _setup_drive(device, encoding, log_events)
            report = measure_floor(
                device,
                spectroradiometer,
                encoding,
                axes=walked,
                repeats=repeats,
                settle_seconds=settle,
                sigma=sigma,
            )

    typer.echo(render_report(report))
    if out is not None:
        out.write_text(_floor_yaml(report, wire.value))
        typer.echo(f"wrote {out}")


def _floor_yaml(report: object, wire: str) -> str:
    """The walk as YAML, rendered by hand for byte-determinism like the
    measurements artifact."""
    from display_measure.floor import FloorReport

    assert isinstance(report, FloorReport)
    lines = [
        "# The lowest code each axis reads reproducibly brighter than black.",
        f'wire: "{wire}"',
        f"separation_sigma: {report.separation_sigma:g}",
        f"ambient: {report.ambient:.6f}",
        "axes:",
    ]
    for axis in report.axes:
        lines += [
            f'  - axis: "{axis.axis}"',
            f"    lowest_separable: {axis.lowest_separable if axis.lowest_separable is not None else 'null'}",
            f"    black_mean: {axis.black.mean:.6f}",
            f"    black_sigma: {axis.black.sigma:.6f}",
            "    rungs:",
        ]
        for rung in axis.rungs:
            lines.append(f"      - code: {rung.code}")
            if rung.refused:
                lines.append(f'        refused: "{rung.refused}"')
            else:
                lines.append(f"        mean: {rung.mean:.6f}")
                lines.append(f"        sigma: {rung.sigma:.6f}")
    return "\n".join(lines) + "\n"
