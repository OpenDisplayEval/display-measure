"""CLI surface tests: the entry point exists and drives a doubles session."""

import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from display_measure import session
from display_measure.cli import app

runner = CliRunner()

# Rich styles each option name in several spans, so a colored `--help`
# renders "--threshold" as "-" and "-threshold" with escapes between.
# CI forces color and a terminal does not, which is the whole
# difference between this passing locally and failing there.
_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def plain(text: str) -> str:
    """`text` with ANSI styling removed."""
    return _ANSI.sub("", text)


# Must match conftest.FIXED_TIME: the CLI test compares its artifact
# bytes against the wall session fixture's bytes.
FIXED_TIMESTAMP = "2026-08-10T12:00:00+00:00"


def test_help_lists_characterize() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "characterize" in result.output


def test_help_defers_the_measurement_stack() -> None:
    # Subprocess probe: colour may already sit in this process's
    # sys.modules from other tests, so an in-process check is vacuous.
    probe = (
        "import sys; "
        "from typer.testing import CliRunner; "
        "from display_measure.cli import app; "
        "assert CliRunner().invoke(app, ['--help']).exit_code == 0; "
        "assert 'colour' not in sys.modules"
    )
    subprocess.run([sys.executable, "-c", probe], check=True)


def test_characterize_wiring_matches_the_session_core(
    wall_artifact: Path, tmp_path: Path
) -> None:
    out = tmp_path / "measurements.yaml"
    result = runner.invoke(
        app,
        [
            "characterize",
            "--out",
            str(out),
            "--settle",
            "0",
            "--timestamp",
            FIXED_TIMESTAMP,
        ],
    )
    assert result.exit_code == 0, result.output
    assert out.read_bytes() == wall_artifact.read_bytes()


def test_help_lists_every_instrument_mode() -> None:
    result = runner.invoke(app, ["characterize", "--help"])
    assert result.exit_code == 0
    output = " ".join(plain(result.output).split())
    for mode in ("doubles", "doubles-hybrid", "spectro", "hybrid"):
        assert mode in output
    assert "--threshold" in output


def test_doubles_hybrid_wiring_matches_the_session_core(
    hybrid_artifact: Path, full_ramp_threshold: float, tmp_path: Path
) -> None:
    out = tmp_path / "measurements.yaml"
    result = runner.invoke(
        app,
        [
            "characterize",
            "--out",
            str(out),
            "--instrument",
            "doubles-hybrid",
            "--threshold",
            str(full_ramp_threshold),
            "--settle",
            "0",
            "--timestamp",
            FIXED_TIMESTAMP,
        ],
    )
    assert result.exit_code == 0, result.output
    assert out.read_bytes() == hybrid_artifact.read_bytes()


@pytest.mark.parametrize(
    ("mode", "hybrid"),
    [("spectro", False), ("hybrid", True)],
)
def test_hardware_modes_reach_the_bench_wiring(
    mode: str, hybrid: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The instruments are the orchestrator's to drive; what the CLI
    owes is the right call."""
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        session,
        "hardware_session",
        lambda out_path, **kwargs: calls.append({"out": out_path, **kwargs}),
    )
    result = runner.invoke(
        app,
        [
            "characterize",
            "--out",
            str(tmp_path / "m.yaml"),
            "--instrument",
            mode,
            "--threshold",
            "4",
        ],
    )
    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    assert calls[0]["hybrid"] is hybrid
    assert calls[0]["luminance_threshold"] == 4.0


def test_characterize_refuses_an_unknown_instrument(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "characterize",
            "--out",
            str(tmp_path / "m.yaml"),
            "--instrument",
            "guesswork",
        ],
    )
    assert result.exit_code != 0


def test_characterize_refuses_a_negative_threshold(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        ["characterize", "--out", str(tmp_path / "m.yaml"), "--threshold", "-1"],
    )
    assert result.exit_code != 0


def test_characterize_refuses_naive_timestamp(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "characterize",
            "--out",
            str(tmp_path / "m.yaml"),
            "--timestamp",
            "2026-08-10T12:00:00",
        ],
    )
    assert result.exit_code != 0
    assert "timezone" in result.output
