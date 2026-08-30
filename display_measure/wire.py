"""The wire encoding a session drives its patches through (§spec:measure-sessions).

A patch is a 12-bit RGB code triple (`display_measure.protocol`); the
device packs whatever the declared encoding puts on the wire. For the
identity encoding that is the triple itself. For a YCbCr encoding the
triple is normalised and handed to pypixelpack, so this repository
implements no conversion (`§spec:architecture` in color-wrangler).
Patches are flat fields, so one pixel is encoded and broadcast.

Which device pixel format packs a layout is the drive's knowledge, not
this module's: pypixelpack's layout names are the DeckLink FourCCs, and
`session._setup_drive` resolves them there.
"""

import numpy as np
import numpy.typing as npt
import pypixelpack

from display_measure.artifact import SAMPLING_RGB, SAMPLING_YCBCR, WireEncoding
from display_measure.protocol import CODE_BITS, FULL_DRIVE

__all__ = ["RGB12", "V210", "WIRE_ENCODINGS", "decode_pixel", "encode_pixel"]

# No matrix is applied on an RGB link; recorded by name so the artifact
# says so rather than leaving the field out.
IDENTITY_MATRIX = "identity"
FULL_LEVELS = "full"
SUBSAMPLING_444 = "444"

# The bench link: 12-bit RGB, the frame is the protocol's codes.
RGB12 = WireEncoding(
    layout="r12b",
    bit_depth=CODE_BITS,
    sampling=SAMPLING_RGB,
    subsampling=SUBSAMPLING_444,
    levels=FULL_LEVELS,
    matrix=IDENTITY_MATRIX,
    legal_codes=(("rgb", 0, FULL_DRIVE),),
)


def _ycbcr(layout: str, *, matrix: str, levels: str) -> WireEncoding:
    """A YCbCr encoding over a pypixelpack layout; depth and subsampling
    come from the layout so the declaration cannot contradict the wire."""
    bits, subsampling = pypixelpack.encoding_for(layout)
    legal = pypixelpack.legal_codes(levels=levels, bits=bits)
    return WireEncoding(
        layout=layout,
        bit_depth=bits,
        sampling=SAMPLING_YCBCR,
        subsampling=subsampling,
        levels=levels,
        matrix=matrix,
        legal_codes=(
            ("luma", legal.luma.start, legal.luma[-1]),
            ("chroma", legal.chroma.start, legal.chroma[-1]),
        ),
    )


# 10-bit BT.709 narrow-range 4:2:2: the YCbCr link a broadcast chain drives.
V210 = _ycbcr("v210", matrix="bt709", levels="narrow")

# What `--wire` offers, by name.
WIRE_ENCODINGS: dict[str, WireEncoding] = {"rgb12": RGB12, "v210": V210}


def _check(rgb: tuple[int, int, int]) -> None:
    if any(not 0 <= c <= FULL_DRIVE for c in rgb):
        raise ValueError(f"{rgb!r} is outside the protocol's 0..{FULL_DRIVE} codes")


def _refuse_unsupported(encoding: WireEncoding) -> None:
    # pypixelpack encodes component layouts only; an RGB link at another
    # depth would need a requantisation nobody has written. Refusing
    # here keeps a 10-bit RGB declaration from driving 12-bit codes.
    if encoding.sampling == SAMPLING_RGB and not encoding.identity:
        raise NotImplementedError(
            f"{encoding.layout}: an RGB link at {encoding.bit_depth} bits is "
            f"not the protocol's {CODE_BITS}-bit identity and has no encoder"
        )


def encode_pixel(
    encoding: WireEncoding, rgb: tuple[int, int, int]
) -> tuple[int, int, int]:
    """The codes the wire carries for a protocol pixel."""
    _check(rgb)
    _refuse_unsupported(encoding)
    if encoding.identity:
        return rgb
    normalised = np.array(rgb, dtype=np.float32).reshape(1, 1, 3) / np.float32(
        FULL_DRIVE
    )
    codes = pypixelpack.encode(
        normalised,
        matrix=encoding.matrix,
        levels=encoding.levels,
        layout=encoding.layout,
    )
    y, cb, cr = (int(c) for c in codes[0, 0])
    return (y, cb, cr)


def decode_pixel(
    encoding: WireEncoding, codes: npt.NDArray[np.integer]
) -> npt.NDArray[np.float64]:
    """Normalised RGB in [0, 1] for the codes a device received — what a
    display decodes before its transfer function (the display double's
    view of the wire)."""
    _refuse_unsupported(encoding)
    if encoding.identity:
        scaled: npt.NDArray[np.float64] = np.asarray(codes, dtype=np.float64)
        scaled /= FULL_DRIVE
        return scaled
    rgb = pypixelpack.decode(
        np.asarray(codes).reshape(1, 1, 3),
        matrix=encoding.matrix,
        levels=encoding.levels,
        layout=encoding.layout,
    )
    return np.asarray(rgb[0, 0], dtype=np.float64)
