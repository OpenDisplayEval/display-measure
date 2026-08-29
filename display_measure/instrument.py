"""The instrument seam: what a session reads (§spec:architecture).

Sessions consume instruments structurally: colour-specio's
``SpecRadiometer`` and ``Colorimeter`` satisfy the protocols below
without subclassing, and so do this package's doubles
(:mod:`display_measure.plausible_wall`) and the disciplined hybrid
(:mod:`display_measure.hybrid`). `XYZReading` is the concrete reading a
synthesized or corrected measurement returns — the one shape every
producer here needs, so none of them roll their own.
"""

from dataclasses import dataclass
from typing import Protocol

import numpy as np
import numpy.typing as npt

from display_measure.artifact import InstrumentIdentity

__all__ = [
    "Instrument",
    "InstrumentReading",
    "XYZReading",
    "chromaticity",
    "identity",
    "luminance",
    "triple",
    "xyz",
]


class InstrumentReading(Protocol):
    """The slice of specio's measurement types the session consumes."""

    @property
    def XYZ(self) -> npt.NDArray[np.float64]: ...

    @property
    def xy(self) -> npt.NDArray[np.float64]: ...


class Instrument(Protocol):
    """The instrument the session drives patches at and reads."""

    @property
    def manufacturer(self) -> str: ...

    @property
    def model(self) -> str: ...

    @property
    def serial_number(self) -> str: ...

    def measure(self) -> InstrumentReading: ...


@dataclass(frozen=True, eq=False)
class XYZReading:
    """A reading carried as absolute XYZ alone (cd/m²).

    Satisfies `InstrumentReading`. `xy` matches ``colour.XYZ_to_xy``
    for the positive XYZ a lit or leaking display produces; hand-rolled
    so the default session path keeps colour-science (and its ~0.8 s
    import) off the wiring.
    """

    XYZ: npt.NDArray[np.float64]

    @property
    def xy(self) -> npt.NDArray[np.float64]:
        return self.XYZ[:2] / self.XYZ.sum()


def triple(values: npt.NDArray[np.float64]) -> tuple[float, float, float]:
    """A 3-vector as the plain floats the artifact dataclasses declare."""
    x, y, z = values
    return (float(x), float(y), float(z))


def xyz(measurement: InstrumentReading) -> tuple[float, float, float]:
    """The reading's absolute XYZ (cd/m²)."""
    return triple(measurement.XYZ)


def luminance(measurement: InstrumentReading) -> float:
    """The reading's absolute luminance (cd/m²) — Y of XYZ."""
    return float(measurement.XYZ[1])


def chromaticity(measurement: InstrumentReading) -> tuple[float, float]:
    """The reading's CIE xy."""
    x, y = measurement.xy
    return (float(x), float(y))


def identity(instrument: Instrument) -> InstrumentIdentity:
    """The instrument's identity as the artifact records it.

    colour-specio exposes no firmware surface, so the field is read
    optionally and omitted until a driver provides it.
    """
    return InstrumentIdentity(
        manufacturer=instrument.manufacturer,
        model=instrument.model,
        serial_number=instrument.serial_number,
        firmware=getattr(instrument, "firmware", None),
    )
