"""Patch-protocol tests (§spec:sessions, MEASUREMENT.md)."""

import pytest

from display_measure.protocol import (
    FULL_DRIVE,
    PROTOCOL_NAME,
    RAMP_CODES,
    Patch,
    presentation_order,
    protocol_patches,
)

SHADOW_CEILING = FULL_DRIVE // 8


def by_name(patches: tuple[Patch, ...]) -> dict[str, Patch]:
    return {p.name: p for p in patches}


def test_ramp_codes_are_shadow_dense_and_strictly_increasing() -> None:
    assert list(RAMP_CODES) == sorted(set(RAMP_CODES))
    assert RAMP_CODES[0] > 0
    assert RAMP_CODES[-1] < FULL_DRIVE  # full drive belongs to the anchors
    shadow = [c for c in RAMP_CODES if c <= SHADOW_CEILING]
    assert len(shadow) > len(RAMP_CODES) // 2


def test_protocol_inventory() -> None:
    patches = protocol_patches()
    names = [p.name for p in patches]
    assert len(names) == len(set(names)), "patch names collide"
    # Anchors + three per-channel ramps + gray ramp + Y/C/M triad.
    assert len(patches) == 5 + 3 * len(RAMP_CODES) + len(RAMP_CODES) + 3

    anchors = by_name(patches)
    assert anchors["black"].rgb == (0, 0, 0)
    assert anchors["red"].rgb == (FULL_DRIVE, 0, 0)
    assert anchors["green"].rgb == (0, FULL_DRIVE, 0)
    assert anchors["blue"].rgb == (0, 0, FULL_DRIVE)
    assert anchors["white"].rgb == (FULL_DRIVE, FULL_DRIVE, FULL_DRIVE)
    assert anchors["yellow"].rgb == (FULL_DRIVE, FULL_DRIVE, 0)
    assert anchors["cyan"].rgb == (0, FULL_DRIVE, FULL_DRIVE)
    assert anchors["magenta"].rgb == (FULL_DRIVE, 0, FULL_DRIVE)


def test_ramp_patches_drive_one_channel_and_gray_drives_all_equally() -> None:
    patches = protocol_patches()
    for code in RAMP_CODES:
        named = by_name(patches)
        assert named[f"red_{code:04d}"].rgb == (code, 0, 0)
        assert named[f"green_{code:04d}"].rgb == (0, code, 0)
        assert named[f"blue_{code:04d}"].rgb == (0, 0, code)
        assert named[f"gray_{code:04d}"].rgb == (code, code, code)


def test_roles_map_every_patch_to_an_artifact_destination() -> None:
    roles = {p.role for p in protocol_patches()}
    assert {
        "black_level",
        "red_primary",
        "green_primary",
        "blue_primary",
        "white_point",
        "red_response",
        "green_response",
        "blue_response",
        "gray_response",
        "additivity_yellow",
        "additivity_cyan",
        "additivity_magenta",
    } == roles


def test_presentation_is_a_black_first_deterministic_permutation() -> None:
    patches = protocol_patches()
    order = presentation_order(patches, seed=0)
    assert order[0].name == "black", "the ambient gate consumes the opening black"
    assert sorted(p.name for p in order) == sorted(p.name for p in patches)
    assert order == presentation_order(patches, seed=0)


def test_presentation_shuffles_and_seed_changes_the_order() -> None:
    patches = protocol_patches()
    seed0 = [p.name for p in presentation_order(patches, seed=0)]
    seed1 = [p.name for p in presentation_order(patches, seed=1)]
    protocol = [p.name for p in patches]
    assert seed0 != protocol, "presentation must not be protocol order"
    assert seed0 != seed1


def test_pinned_patches_open_the_session_in_the_order_named() -> None:
    """A disciplined-colorimeter session must pair on the R/G/B anchors
    before it can route a patch, so it pins them ahead of the shuffle
    (§spec:sessions)."""
    patches = protocol_patches()
    pinned = ("black", "red", "green", "blue")
    order = presentation_order(patches, seed=0, pinned=pinned)
    assert [p.name for p in order[: len(pinned)]] == list(pinned)
    assert sorted(p.name for p in order) == sorted(p.name for p in patches)
    # The unpinned tail keeps the shuffle it would have had otherwise.
    tail = [p.name for p in order[len(pinned) :]]
    # Compared against pinning nothing: the default pins are themselves a
    # choice, and the claim here is about the shuffle underneath them.
    assert tail == [
        p.name
        for p in presentation_order(patches, seed=0, pinned=())
        if p.name not in pinned
    ]


def test_presentation_refuses_a_pin_no_patch_answers_to() -> None:
    with pytest.raises(KeyError, match="chartreuse"):
        presentation_order(protocol_patches(), seed=0, pinned=("black", "chartreuse"))


def test_protocol_name_versions_the_patch_set() -> None:
    assert PROTOCOL_NAME == "color-wrangler/characterize/3"


def test_black_opens_the_session_and_white_follows_it() -> None:
    """Both gating readings arrive before the shuffle, and in this order.

    Black is the session's most delicate reading; driving the protocol's
    brightest patch into it would leave the panel and the instrument
    recovering. White second keeps black's conditions as protocol 2 read
    them and still moves the level gate to patch 2 of 72.
    """
    order = [p.name for p in presentation_order(protocol_patches(), seed=0)]
    assert order[:2] == ["black", "white"]
