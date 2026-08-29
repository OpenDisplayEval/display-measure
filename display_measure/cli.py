"""The `display-measure` command line surface.

One command per session mode (§spec:sessions). `characterize` lands
with the walking skeleton; `verify` and `snapshot` register here when
their workstreams ship — no placeholder commands before then.
`--instrument` chooses what measures: the doubles by default, so a
laptop run and CI need no hardware at all.
"""

import logging
import sys
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

import typer

app = typer.Typer(
    name="display-measure",
    help="display-measure: gated instrument sessions for LED-wall characterization.",
    no_args_is_help=True,
)

DEFAULT_SETTLE_SECONDS = 0.5
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


@app.callback()
def main() -> None:
    """Measure LED walls through the show signal chain (§spec:sessions)."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(message)s",
        stream=sys.stderr,
    )


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
    """Characterize a wall: drive the patch protocol, measure, emit the artifact."""
    # Deferred so `--help` needs no measurement stack loaded.
    from display_measure import session
    from display_measure.artifact import DECLARED_CONTRACT
    from display_measure.hybrid import DEFAULT_LUMINANCE_THRESHOLD as _DEFAULT
    from display_measure.hybrid import DerivationRefused
    from display_measure.processor import ContractViolation, contract_from_manifest

    assert DEFAULT_LUMINANCE_THRESHOLD == _DEFAULT, (
        "the CLI's restated routing default drifted from display_measure.hybrid"
    )

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
        if doubled:
            session.doubles_session(
                out,
                clock=clock,
                settle_seconds=settle,
                hybrid=hybrid,
                luminance_threshold=threshold,
            )
        else:
            session.hardware_session(
                out,
                clock=clock,
                settle_seconds=settle,
                hybrid=hybrid,
                luminance_threshold=threshold,
                processor_host=processor,
                declared=declared,
            )
    except FileExistsError as e:
        typer.echo(f"Error: {e} — measurements artifacts are immutable", err=True)
        raise typer.Exit(1) from e
    except (ContractViolation, DerivationRefused) as e:
        typer.echo(f"Refused: {e}", err=True)
        raise typer.Exit(2) from e
