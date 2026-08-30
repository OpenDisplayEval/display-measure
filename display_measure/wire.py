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

from collections.abc import Mapping

import numpy as np
import numpy.typing as npt
import pypixelpack

from display_measure.artifact import SAMPLING_RGB, SAMPLING_YCBCR, WireEncoding
from display_measure.protocol import CODE_BITS, FULL_DRIVE, Patch

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


# How far outside and inside each edge the probe steps, in wire codes. Big
# enough that the inside step is far above instrument noise on a 1800-nit
# display, small enough that both stay in the same region of the transfer.
PROBE_STEP = 8

# The outside step, as a fraction of the inside one, below which the codes
# outside the span are judged clipped. Measured on the bench at 0.08 and
# 0.05 for the two edges of a correctly-read narrow link, against 1.0 for
# a span the processor is not clipping at all; a quarter sits between the
# two by a wide margin (§spec:measure-sessions).
CLIPPED_STEP_RATIO = 0.25


def range_probe_patches(encoding: WireEncoding) -> tuple[Patch, ...]:
    """Four patches straddling the declared span's edges, in wire codes.

    The protocol's own patches are authored in RGB, so every one of them
    encodes to a code *inside* the legal span — which is exactly why they
    cannot see a range misreading. A processor that treats a narrow link
    as full renders a lifted black and a dim peak, and nothing in a ramp
    of legal codes distinguishes that from a display that simply has a
    lifted black.

    The codes on either side of each edge do distinguish it. Under the
    declared narrow span they clip, so the step outside the edge is far
    smaller than the step inside it; read as full they do not clip, and
    the two steps match.
    """
    lo, hi = _probe_span(encoding)
    step = PROBE_STEP << (encoding.bit_depth - 10)
    if encoding.identity:
        # An identity link carries every code, so the probe rides the
        # neutral axis in RGB rather than luma-with-neutral-chroma.
        def codes(value: int) -> tuple[int, int, int]:
            return (value, value, value)
    else:
        mid = _chroma_mid(encoding)

        def codes(value: int) -> tuple[int, int, int]:
            return (value, mid, mid)

    return tuple(
        Patch(name, (0, 0, 0), role="wire_range_probe", wire_codes=codes(code))
        for name, code in (
            ("wire_below_floor", lo - step),
            ("wire_floor", lo),
            ("wire_above_floor", lo + step),
            ("wire_below_ceiling", hi - step),
            ("wire_ceiling", hi),
            ("wire_above_ceiling", hi + step),
        )
    )


def expects_clipping(encoding: WireEncoding) -> bool:
    """Whether codes outside the probe's span should render as clipped.

    A narrow link declares a span smaller than the code space, so the
    codes outside it are not content and the processor should clip them.
    An identity link declares the whole code space, and the probe instead
    straddles the *limited* span a processor would use if it misread the
    link as narrow — codes it must keep, not clip. Both links therefore
    get a measured range check; only the expected answer differs.
    """
    return not encoding.identity


def _probe_span(encoding: WireEncoding) -> tuple[int, int]:
    """The two edges the probe straddles, in wire codes."""
    if encoding.identity:
        # The limited-range span at this depth: the edges a processor
        # that misread a full link as narrow would clip to.
        scale = 1 << (encoding.bit_depth - 8)
        return 16 * scale, 235 * scale
    codes = pypixelpack.legal_codes(levels=encoding.levels, bits=encoding.bit_depth)
    return codes.luma.start, codes.luma.stop - 1


def _chroma_mid(encoding: WireEncoding) -> int:
    """The code that carries no colour difference, so the probe is neutral."""
    return 1 << (encoding.bit_depth - 1)


def range_probe_verdict(
    luminances: Mapping[str, float], *, expect_clipped: bool
) -> tuple[bool, str]:
    """Whether the processor reads the declared span. `(agrees, why)`.

    Takes the six probe readings by patch name and compares, at each
    edge, the step taken outside the span against the step taken inside
    it. Clipping outside both edges is the declared narrow span being
    honoured; a step outside that rivals the step inside is the processor
    carrying codes the declaration says are not there, which is a
    full-range reading of a narrow link and moves every measurement.
    """
    verdicts: list[str] = []
    clipped = True
    for edge, outside, at, inside in (
        ("floor", "wire_below_floor", "wire_floor", "wire_above_floor"),
        ("ceiling", "wire_above_ceiling", "wire_ceiling", "wire_below_ceiling"),
    ):
        anchor = luminances[at]
        outside_step = abs(luminances[outside] - anchor)
        inside_step = abs(luminances[inside] - anchor)
        if inside_step == 0:
            verdicts.append(f"{edge}: the step inside the span moved nothing")
            clipped = False
            continue
        ratio = outside_step / inside_step
        verdicts.append(f"{edge}: outside/inside = {ratio:.2f}")
        if ratio >= CLIPPED_STEP_RATIO:
            clipped = False
    return clipped == expect_clipped, "; ".join(verdicts)
