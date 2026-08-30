"""The wire encoding a session drives its patches through (§spec:measure-sessions).

A patch is a 12-bit RGB code triple (`display_measure.protocol`); the
device packs whatever codes the declared encoding puts on the wire. For
the identity encoding that is the triple itself, as the bench HDMI link
has always been driven. For a YCbCr encoding the triple is normalised
and handed to pypixelpack — the shared packer, so this repository never
implements a conversion and the wire the session declares is the wire
the transport stack encodes (`§spec:architecture`).

Patches are flat fields, so one pixel is encoded and the session
broadcasts it; no full frame is converted per patch.
"""

import numpy as np
import numpy.typing as npt
import pypixelpack
from bmd_sg.decklink import PixelFormatType

from display_measure.artifact import SAMPLING_RGB, SAMPLING_YCBCR, WireEncoding
from display_measure.protocol import CODE_BITS, FULL_DRIVE

__all__ = [
    "RGB12",
    "V210",
    "WIRE_ENCODINGS",
    "decode_pixel",
    "encode_pixel",
    "pixel_format",
    "representable",
]

# No matrix is applied on an RGB link; recorded by name so the artifact
# says so rather than leaving the field out.
IDENTITY_MATRIX = "identity"
FULL_LEVELS = "full"
SUBSAMPLING_444 = "444"

# The bench link: 12-bit RGB, the frame is the protocol's codes
# (validated on the bench rig over HDMI, §spec:signal-contract).
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


# 10-bit BT.709 narrow-range 4:2:2: the SDI/HDMI YCbCr link a broadcast
# chain drives.
V210 = _ycbcr("v210", matrix="bt709", levels="narrow")

# What `--wire` offers, by name.
WIRE_ENCODINGS: dict[str, WireEncoding] = {"rgb12": RGB12, "v210": V210}

# The DeckLink pixel format that packs each layout's codes.
_PIXEL_FORMATS = {
    RGB12.layout: PixelFormatType.FORMAT_12BIT_RGB,
    V210.layout: PixelFormatType.FORMAT_10BIT_YUV,
}


def pixel_format(encoding: WireEncoding) -> PixelFormatType:
    """The pixel format the device is set to for `encoding`."""
    return _PIXEL_FORMATS[encoding.layout]


def _normalised(rgb: tuple[int, int, int]) -> npt.NDArray[np.float32]:
    """One protocol pixel as `(1, 1, 3)` float RGB in [0, 1]."""
    if any(not 0 <= c <= FULL_DRIVE for c in rgb):
        raise ValueError(f"{rgb!r} is outside the protocol's 0..{FULL_DRIVE} codes")
    return np.array(rgb, dtype=np.float32).reshape(1, 1, 3) / np.float32(FULL_DRIVE)


def encode_pixel(
    encoding: WireEncoding, rgb: tuple[int, int, int]
) -> tuple[int, int, int]:
    """The codes the wire carries for a protocol pixel.

    Identity: `rgb` itself. YCbCr: pypixelpack's `[Y, Cb, Cr]` for the
    declared matrix, levels and layout — 4:2:2 chroma is the pair
    average, which for a flat field is the pixel's own.
    """
    normalised = _normalised(rgb)
    if encoding.identity:
        return rgb
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
    """Normalised RGB in [0, 1] for the codes a device received.

    What a display decodes before its transfer function — the display
    double's view of the wire (`display_measure.plausible_display`).
    """
    if encoding.identity:
        normalised: npt.NDArray[np.float64] = np.asarray(codes, dtype=np.float64)
        normalised /= FULL_DRIVE
        return normalised
    rgb = pypixelpack.decode(
        np.asarray(codes).reshape(1, 1, 3),
        matrix=encoding.matrix,
        levels=encoding.levels,
        layout=encoding.layout,
    )
    return np.asarray(rgb[0, 0], dtype=np.float64)


def representable(
    encoding: WireEncoding, rgb: tuple[int, int, int]
) -> tuple[int, int, int]:
    """The protocol code the wire carried, after the round trip.

    A narrow-range encoding cannot represent every 12-bit RGB code: gray
    16 rides v210 as luma 67 and decodes to 14. The artifact records this
    code, not only the one the protocol intended (§spec:measurements-artifact).
    """
    if encoding.identity:
        return rgb
    codes = np.array(encode_pixel(encoding, rgb), dtype=np.uint16)
    back = np.rint(decode_pixel(encoding, codes) * FULL_DRIVE).astype(int)
    return (int(back[0]), int(back[1]), int(back[2]))
