"""Choosing the spectroradiometer: CR-300 by default, PR-655 on request."""

from pathlib import Path
from typing import Any

import pytest
from bmd_sg.decklink import MockBMDDeckLink
from typer.testing import CliRunner

from display_measure import session
from display_measure.artifact import DECLARED_CONTRACT
from display_measure.cli import SpectroChoice, app

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
        ["characterize", "--out", str(tmp_path / "m.csmf"), "--instrument", *extra],
    )


def test_the_cli_restates_the_session_s_spectroradiometers() -> None:
    assert {s.value for s in SpectroChoice} == set(session.SPECTROS)


def test_the_cr300_is_the_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _record_hardware_session(monkeypatch)

    result = _characterize(tmp_path, "spectro")

    assert result.exit_code == 0, result.output
    assert calls[0]["spectro"] == session.SPECTRO_CR300


@pytest.mark.parametrize("mode", ["spectro", "hybrid"])
def test_pr655_reaches_the_bench_wiring(
    mode: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _record_hardware_session(monkeypatch)

    result = _characterize(tmp_path, mode, "--spectro", "pr655")

    assert result.exit_code == 0, result.output
    assert calls[0]["spectro"] == session.SPECTRO_PR655
    assert calls[0]["hybrid"] is (mode == "hybrid")


@pytest.mark.parametrize(
    ("model", "driver"),
    [
        (session.SPECTRO_CR300, "CRSpectrometer"),
        (session.SPECTRO_PR655, "PRSpectrometer"),
    ],
)
def test_discovery_goes_to_the_named_driver(
    model: str, driver: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    import specio.spectrometers

    found = object()
    monkeypatch.setattr(
        getattr(specio.spectrometers, driver), "discover", classmethod(lambda _: found)
    )

    assert session.discover_spectroradiometer(model) is found


def test_an_unknown_spectroradiometer_is_refused() -> None:
    with pytest.raises(ValueError, match="pr655"):
        session.discover_spectroradiometer("pr650")


class _Releasable:
    """A spectroradiometer that records being handed back."""

    manufacturer = "Photo Research"
    model = "PR-655"
    serial_number = "0"

    def __init__(self) -> None:
        self.closed = False

    def measure(self) -> Any:
        raise AssertionError("not read in these tests")

    def close(self) -> None:
        self.closed = True


@pytest.mark.parametrize("outcome", ["completes", "fails"])
def test_the_session_releases_the_spectroradiometer(
    outcome: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A PR-6xx keeps its touch screen locked until it leaves remote mode."""
    instrument = _Releasable()
    monkeypatch.setattr(session, "_audit_processor", lambda *_: None)
    monkeypatch.setattr(session, "discover_spectroradiometer", lambda _: instrument)
    monkeypatch.setattr(session, "BMDDeckLink", MockBMDDeckLink)

    def characterize(**_: Any) -> None:
        if outcome == "fails":
            raise RuntimeError("the session failed")

    monkeypatch.setattr(session, "characterize", characterize)

    def run() -> None:
        session.hardware_session(
            tmp_path / "m.csmf",
            clock=lambda: None,  # type: ignore[arg-type, return-value]
            settle_seconds=0,
            declared=DECLARED_CONTRACT,
            spectro=session.SPECTRO_PR655,
        )

    if outcome == "fails":
        with pytest.raises(RuntimeError):
            run()
    else:
        run()

    assert instrument.closed
