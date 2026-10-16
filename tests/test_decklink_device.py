"""Choosing the DeckLink output a hardware session drives."""

from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from display_measure import cli, session
from display_measure.artifact import DECLARED_CONTRACT
from display_measure.cli import app

runner = CliRunner()


def _record_hardware_session(
    monkeypatch: pytest.MonkeyPatch,
) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        session,
        "hardware_session",
        lambda out_path, **kwargs: calls.append({"out": out_path, **kwargs}),
    )
    return calls


def _characterize(tmp_path: Path, *extra: str) -> Any:
    return runner.invoke(
        app,
        [
            "characterize",
            "--out",
            str(tmp_path / "m.csmf"),
            "--instrument",
            "spectro",
            *extra,
        ],
    )


def test_the_cli_restates_the_session_s_default() -> None:
    assert cli.DEFAULT_DECKLINK_INDEX == session.DECKLINK_INDEX


def test_device_zero_is_the_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _record_hardware_session(monkeypatch)

    result = _characterize(tmp_path)

    assert result.exit_code == 0, result.output
    assert calls[0]["decklink_index"] == 0


def test_the_named_device_reaches_the_bench_wiring(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _record_hardware_session(monkeypatch)

    result = _characterize(tmp_path, "--decklink", "4")

    assert result.exit_code == 0, result.output
    assert calls[0]["decklink_index"] == 4


def test_a_negative_index_is_refused_before_anything_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _record_hardware_session(monkeypatch)

    result = _characterize(tmp_path, "--decklink", "-1")

    assert result.exit_code != 0
    assert calls == []


def test_the_session_opens_the_named_device(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened: list[int] = []

    class Device:
        def __init__(self, index: int) -> None:
            opened.append(index)

        def __enter__(self) -> "Device":
            return self

        def __exit__(self, *_: object) -> None:
            pass

    class Spectroradiometer:
        manufacturer = "Colorimetry Research"
        model = "CR-300"
        serial_number = "0"

        @classmethod
        def discover(cls) -> "Spectroradiometer":
            return cls()

    import specio.spectrometers

    monkeypatch.setattr(session, "_audit_processor", lambda *_: None)
    monkeypatch.setattr(specio.spectrometers, "CRSpectrometer", Spectroradiometer)
    monkeypatch.setattr(session, "BMDDeckLink", Device)
    monkeypatch.setattr(session, "characterize", lambda **_: None)

    session.hardware_session(
        tmp_path / "m.csmf",
        clock=lambda: None,  # type: ignore[arg-type, return-value]
        settle_seconds=0,
        declared=DECLARED_CONTRACT,
        decklink_index=4,
    )

    assert opened == [4]
