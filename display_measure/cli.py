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


class SuiteChoice(StrEnum):
    """A preset composition of measurement blocks.

    Named for what the session is *for*: an operator knows what they
    need the numbers for, not which blocks that takes. `--blocks`
    composes explicitly where no preset fits.
    """

    CONFIG = "config"
    VERIFY = "verify"
    REPORT = "report"


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


def _confirm_panel_state(panel_state) -> None:
    """Ask the operator to read the panel back.

    None of this is readable from the processor, and all of it moves the
    measurement. A manifest field alone goes stale the moment someone
    changes it at the rig, so the one instrument that can read it is
    asked directly.
    """
    typer.echo(
        "The manifest attests this panel state, which cannot be read "
        "from the processor — confirm it at the panel OSD:",
        err=True,
    )
    for key, value in panel_state:
        typer.echo(f"  {key}: {value}", err=True)
    if not typer.confirm("Does the panel read exactly this?"):
        typer.echo(
            "Refused: panel state unconfirmed. Update the manifest to "
            "what the panel actually reads, or set the panel to match.",
            err=True,
        )
        raise typer.Exit(2)


def _print_blocks(blocks: dict) -> None:
    """The blocks, and what reads each. `--list-blocks`."""
    for block in blocks.values():
        typer.echo(f"{block.id}  ({len(block.patches)} patches)")
        typer.echo(f"    {block.why}\n")


def _resolve_suite(suite_name: str, blocks: str | None, suites: dict, compose):
    """The composition to drive: explicit blocks, else the named preset.

    A typo in `--blocks` drives a session measuring the wrong thing and
    says nothing about it, so an unknown name exits rather than falling
    back to a preset.
    """
    if blocks is None:
        return suites[suite_name]
    names = [name.strip() for name in blocks.split(",") if name.strip()]
    if not names:
        typer.echo("Error: --blocks was given no block names", err=True)
        raise typer.Exit(2)
    try:
        return compose(*names)
    except KeyError as e:
        typer.echo(f"Error: {e.args[0]}", err=True)
        raise typer.Exit(2) from e


@app.command()
def characterize(
    out: Path = typer.Option(
        ...,
        "--out",
        help=(
            "Path for the measurements seam file, a .csmf carrying the "
            "spectra and the provenance block (refuses to overwrite)."
        ),
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
    suite: SuiteChoice = typer.Option(
        SuiteChoice.VERIFY,
        "--suite",
        help=(
            "Preset composition of measurement blocks. 'config' drives "
            "only what an OCIO config reads (5 patches). 'verify' adds the "
            "blocks that test the model a config assumes — response and "
            "additivity — which is what protocol 3 drove (72 patches, ~10 "
            "min). 'report' adds everything the fidelity report's analysis "
            "reads (795 patches, ~110 min). Ignored when --blocks is given."
        ),
    ),
    blocks: str | None = typer.Option(
        None,
        "--blocks",
        help=(
            "Comma-separated measurement blocks to drive, composed "
            "explicitly instead of by preset — 'anchors,response' or "
            "'anchors,noise-floor'. Each block versions on its own and "
            "the artifact records which it carries, so a consumer requires "
            "blocks rather than a bundle version. Conditions come from the "
            "strictest block composed. Run --list-blocks to see them."
        ),
    ),
    list_blocks: bool = typer.Option(
        False,
        "--list-blocks",
        help="Print the measurement blocks, what reads each, and exit.",
    ),
    warmup: float | None = typer.Option(
        None,
        "--warmup",
        min=0.0,
        help=(
            "Random colour driven before the first reading, seconds. An "
            "LED panel's output drifts for minutes after it starts "
            "driving, and the session opens on its most delicate patch. "
            "Defaults to what the chosen protocol specifies."
        ),
    ),
    conditioning: float | None = typer.Option(
        None,
        "--conditioning",
        min=0.0,
        help=(
            "Random colour driven between patches, seconds. Holds the "
            "panel at video-like load, so a reading is not taken from a "
            "thermal state a run of solid patches produces and a moving "
            "picture never does. Recorded as a session condition; "
            "defaults to what the chosen protocol specifies."
        ),
    ),
    read_attempts: int | None = typer.Option(
        None,
        "--read-attempts",
        min=1,
        help=(
            "Attempts at each patch before the session fails. The "
            "instrument's failures at the bottom of a panel are "
            "transient; a session of hundreds of patches should not be "
            "lost to one of them. Defaults to the chosen protocol's."
        ),
    ),
    wire: WireChoice = typer.Option(
        WireChoice.RGB12,
        "--wire",
        help=(
            "Wire encoding the patches ride to the device: rgb12 (12-bit "
            "RGB identity, the bench HDMI link) or v210 (10-bit BT.709 "
            "narrow-range 4:2:2 YCbCr). The session refuses a processor "
            "receiving anything else, and the artifact records it."
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
        _confirm_panel_state(declared.panel_state)

    # Deferred with the rest of the measurement stack: `--help` should
    # not import a protocol to print a default.
    from display_measure.protocol import BLOCKS, SUITES, compose

    if list_blocks:
        _print_blocks(BLOCKS)
        raise typer.Exit(0)

    chosen = _resolve_suite(suite.value, blocks, SUITES, compose)

    try:
        with _cancel_on_interrupt() as cancelled:
            if doubled:
                session.doubles_session(
                    out,
                    clock=clock,
                    settle_seconds=settle,
                    suite=chosen,
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
                    suite=chosen,
                    warmup_seconds=warmup,
                    conditioning_seconds=conditioning,
                    read_attempts=read_attempts,
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
    except ValueError as e:
        # The seam-file suffix, refused before anything is driven.
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(1) from e
    except (ContractViolation, DerivationRefused) as e:
        typer.echo(f"Refused: {e}", err=True)
        raise typer.Exit(2) from e
    except SessionCancelled as e:
        typer.echo(f"Cancelled: {e}", err=True)
        raise typer.Exit(CANCELLED_EXIT_CODE) from e
