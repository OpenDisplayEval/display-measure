"""The wire encoding: declared per session, encoded by pypixelpack.

The session drives 12-bit RGB protocol codes and never implements a
conversion; what the device packs is the declared encoding's codes.
"""

import numpy as np
import pytest

from display_measure.artifact import WireEncoding
from display_measure.processor import WireFormat
from display_measure.protocol import FULL_DRIVE, protocol_patches
from display_measure import wire
from display_measure.wire import (
    RGB12,
    V210,
    WIRE_ENCODINGS,
    decode_pixel,
    encode_pixel,
)

WHITE = (FULL_DRIVE, FULL_DRIVE, FULL_DRIVE)

# BT.709 narrow range at 10 bits: luma 64..940, chroma 64..960 about 512.
NARROW_BLACK, NARROW_WHITE, CHROMA_MID = 64, 940, 512


def test_the_default_encoding_is_the_identity_over_12bit_rgb() -> None:
    """What the bench drives today: the frame is `Patch.rgb`, untouched."""
    assert RGB12.identity and RGB12.legal_codes == (("rgb", 0, FULL_DRIVE),)
    for patch in protocol_patches():
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


# --- the range probe: what a link's own codes say about how it is read ----
#
# The luminances below are the bench, 2026-08-30: a 1800-nit LED wall
# behind a Brompton S8, measured with a CR-300 in a darkened room, over
# both links. They are the oracle because the failure this probe exists
# to catch is a processor-side misreading, which no simulation of our own
# encoder can produce.

SDI_NARROW_READ_CORRECTLY = {
    "wire_below_floor": 0.0136,
    "wire_floor": 0.0095,
    "wire_above_floor": 0.0606,
    "wire_below_ceiling": 1451.2892,
    "wire_ceiling": 1481.0965,
    "wire_above_ceiling": 1479.7200,
}
HDMI_FULL_READ_CORRECTLY = {
    "wire_below_floor": 2.3037,
    "wire_floor": 3.0855,
    "wire_above_floor": 4.0780,
    "wire_below_ceiling": 1183.4130,
    "wire_ceiling": 1206.7687,
    "wire_above_ceiling": 1230.4391,
}


def test_the_probe_straddles_the_narrow_span_in_wire_codes() -> None:
    """v210's probe sits either side of 64 and 940, chroma neutral."""
    patches = wire.range_probe_patches(wire.V210)
    codes = [p.wire_codes for p in patches]
    assert all(c is not None for c in codes)
    luma = [c[0] for c in codes if c is not None]
    assert luma == [56, 64, 72, 932, 940, 948]
    assert {c[1] for c in codes if c is not None} == {512}
    assert {c[2] for c in codes if c is not None} == {512}
    assert wire.expects_clipping(wire.V210)


def test_the_probe_straddles_the_limited_span_on_an_identity_link() -> None:
    """rgb12 carries every code, so the probe asks the opposite question.

    The edges are the ones a processor misreading the link as narrow
    would clip to; the session requires that it does not.
    """
    codes = [p.wire_codes for p in wire.range_probe_patches(wire.RGB12)]
    assert all(c is not None for c in codes)
    assert [c[0] for c in codes if c is not None] == [224, 256, 288, 3728, 3760, 3792]
    # neutral, so the probe rides the grey axis rather than a channel
    assert all(len(set(c)) == 1 for c in codes if c is not None)
    assert not wire.expects_clipping(wire.RGB12)


def test_a_narrow_link_read_narrow_agrees() -> None:
    agrees, why = wire.range_probe_verdict(
        SDI_NARROW_READ_CORRECTLY, expect_clipped=True
    )
    assert agrees, why


def test_a_full_link_read_full_agrees() -> None:
    agrees, why = wire.range_probe_verdict(
        HDMI_FULL_READ_CORRECTLY, expect_clipped=False
    )
    assert agrees, why


def test_a_narrow_link_read_full_is_caught() -> None:
    """The failure the protocol's own patches cannot see.

    Every RGB-authored patch encodes inside the legal span, so a
    processor carrying codes outside it looks exactly like a display with
    a lifted black. Judging the narrow bench readings against a full
    expectation is that mistake, and the probe refuses it.
    """
    agrees, why = wire.range_probe_verdict(
        SDI_NARROW_READ_CORRECTLY, expect_clipped=False
    )
    assert not agrees
    assert "floor" in why and "ceiling" in why


def test_a_probe_that_moved_nothing_inside_the_span_is_not_a_pass() -> None:
    """A dead link reads as clipped at every edge; that is not agreement."""
    flat = dict.fromkeys(SDI_NARROW_READ_CORRECTLY, 0.0)
    agrees, why = wire.range_probe_verdict(flat, expect_clipped=True)
    assert not agrees
    assert "moved nothing" in why
