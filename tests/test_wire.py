"""The wire encoding: declared per session, encoded by pypixelpack.

The session drives 12-bit RGB protocol codes and never implements a
conversion; what the device packs is the declared encoding's codes.
"""

import pytest
from bmd_sg.decklink import PixelFormatType

from display_measure.processor import WireFormat
from display_measure.protocol import FULL_DRIVE, protocol_patches
from display_measure.wire import (
    RGB12,
    V210,
    WIRE_ENCODINGS,
    encode_pixel,
    pixel_format,
    representable,
)

WHITE = (FULL_DRIVE, FULL_DRIVE, FULL_DRIVE)

# BT.709 narrow range at 10 bits: luma 64..940, chroma 64..960 about 512.
NARROW_BLACK = 64
NARROW_WHITE = 940
CHROMA_MID = 512


def test_the_default_encoding_is_the_identity_over_12bit_rgb() -> None:
    """What the bench drives today: the frame is `Patch.rgb`, untouched."""
    assert pixel_format(RGB12) is PixelFormatType.FORMAT_12BIT_RGB
    assert RGB12.bit_depth == 12 and RGB12.sampling == "rgb"
    for patch in protocol_patches():
        assert encode_pixel(RGB12, patch.rgb) == patch.rgb
        assert representable(RGB12, patch.rgb) == patch.rgb


def test_v210_encodes_through_pypixelpack_to_ycbcr_codes() -> None:
    assert pixel_format(V210) is PixelFormatType.FORMAT_10BIT_YUV
    assert (V210.bit_depth, V210.sampling, V210.subsampling) == (10, "ycbcr", "422")
    assert (V210.matrix, V210.levels) == ("bt709", "narrow")
    assert encode_pixel(V210, (0, 0, 0)) == (NARROW_BLACK, CHROMA_MID, CHROMA_MID)
    assert encode_pixel(V210, WHITE) == (NARROW_WHITE, CHROMA_MID, CHROMA_MID)
    # Full-drive red: luma at KR of the span, Cr at the top of its span.
    y, cb, cr = encode_pixel(V210, (FULL_DRIVE, 0, 0))
    assert y == round(NARROW_BLACK + 0.2126 * (NARROW_WHITE - NARROW_BLACK))
    assert cr == 960 and cb < CHROMA_MID


def test_a_narrow_encoding_does_not_carry_every_protocol_code() -> None:
    """Gray 16 rides the wire as luma 67 and comes back as 14: the code
    the artifact has to record is the one the device received."""
    assert representable(V210, (16, 16, 16)) == (14, 14, 14)
    assert representable(V210, (0, 0, 0)) == (0, 0, 0)
    assert representable(V210, WHITE) == WHITE
    lost = [p for p in protocol_patches() if representable(V210, p.rgb) != p.rgb]
    assert lost, "a narrow-range encoding that carried every code would be full"


def test_legal_codes_name_what_each_link_carries() -> None:
    assert RGB12.legal_codes == (("rgb", 0, FULL_DRIVE),)
    assert V210.legal_codes == (("luma", 64, 940), ("chroma", 64, 960))


def test_the_gate_compares_what_the_encoding_puts_on_the_wire() -> None:
    """A v210 declaration holds the processor to 10-bit YCbCr; the
    identity declaration to 12-bit RGB. Both signal SDR."""
    assert WireFormat.for_encoding(RGB12) == WireFormat(
        bit_depth=12, sampling="rgb", hdr_format="standard-dynamic-range"
    )
    assert WireFormat.for_encoding(V210) == WireFormat(
        bit_depth=10, sampling="ycbcr", hdr_format="standard-dynamic-range"
    )


def test_the_named_encodings_are_the_two_the_cli_offers() -> None:
    assert WIRE_ENCODINGS == {"rgb12": RGB12, "v210": V210}


def test_a_code_above_full_drive_is_refused() -> None:
    with pytest.raises(ValueError, match="4095"):
        encode_pixel(V210, (FULL_DRIVE + 1, 0, 0))
