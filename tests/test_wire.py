"""The wire encoding: declared per session, encoded by pypixelpack.

The session drives 12-bit RGB protocol codes and never implements a
conversion; what the device packs is the declared encoding's codes.
"""

import numpy as np
import pytest

from display_measure.artifact import WireEncoding
from display_measure.processor import WireFormat
from display_measure.protocol import FULL_DRIVE, VERIFY_SUITE
from display_measure.wire import RGB12, V210, WIRE_ENCODINGS, decode_pixel, encode_pixel

WHITE = (FULL_DRIVE, FULL_DRIVE, FULL_DRIVE)

# BT.709 narrow range at 10 bits: luma 64..940, chroma 64..960 about 512.
NARROW_BLACK, NARROW_WHITE, CHROMA_MID = 64, 940, 512


def test_the_default_encoding_is_the_identity_over_12bit_rgb() -> None:
    """What the bench drives today: the frame is `Patch.rgb`, untouched."""
    assert RGB12.identity and RGB12.legal_codes == (("rgb", 0, FULL_DRIVE),)
    for patch in VERIFY_SUITE.patches:
        assert encode_pixel(RGB12, patch.rgb) == patch.rgb


def test_v210_encodes_through_pypixelpack_to_ycbcr_codes() -> None:
    assert (V210.bit_depth, V210.sampling, V210.subsampling) == (10, "ycbcr", "422")
    assert (V210.matrix, V210.levels) == ("bt709", "narrow")
    assert V210.legal_codes == (
        ("luma", NARROW_BLACK, NARROW_WHITE),
        ("chroma", NARROW_BLACK, 960),
    )
    assert encode_pixel(V210, (0, 0, 0)) == (NARROW_BLACK, CHROMA_MID, CHROMA_MID)
    assert encode_pixel(V210, WHITE) == (NARROW_WHITE, CHROMA_MID, CHROMA_MID)
    # Full-drive red: luma at KR of the span, Cr at the top of its span.
    y, cb, cr = encode_pixel(V210, (FULL_DRIVE, 0, 0))
    assert y == round(NARROW_BLACK + 0.2126 * (NARROW_WHITE - NARROW_BLACK))
    assert cr == 960 and cb < CHROMA_MID


def test_a_narrow_link_does_not_carry_every_protocol_code() -> None:
    """Gray 16 rides the wire as luma 67 and decodes to 14. The artifact
    records the wire codes; what survived is derivable from them."""
    codes = np.array(encode_pixel(V210, (16, 16, 16)), dtype=np.uint16)
    assert codes[0] == 67
    back = np.rint(decode_pixel(V210, codes) * FULL_DRIVE).astype(int)
    assert back.tolist() == [14, 14, 14]


def test_an_rgb_link_at_another_depth_is_refused_not_driven() -> None:
    """A 10-bit RGB declaration is RGB-sampled and still not the identity;
    driving 12-bit codes into it would be a silent wrong frame."""
    r210 = WireEncoding(
        layout="r210", bit_depth=10, sampling="rgb", subsampling="444",
        levels="full", matrix="identity", legal_codes=(("rgb", 0, 1023),),
    )  # fmt: skip
    assert not r210.identity
    with pytest.raises(NotImplementedError, match="r210"):
        encode_pixel(r210, (0, 0, 0))


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
