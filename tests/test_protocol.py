"""Patch-protocol tests (§spec:patch-protocol, MEASUREMENT.md).

Measurement decomposes into named, independently versioned blocks. The
tests that matter here are the ones about composition: that a block
means one thing, that a suite is only its blocks, and that composing a
block brings the conditions its numbers assume.
"""

import itertools

import pytest

from display_measure.protocol import (
    ADDITIVITY,
    ANCHORS,
    BLOCKS,
    CONFIG_SUITE,
    FULL_DRIVE,
    MIN_CODE_SEPARATION,
    NOISE_FLOOR,
    RAMP_CODES,
    REPORT_SUITE,
    RESPONSE,
    VERIFY_SUITE,
    Patch,
    _half_octave_ladder,
    compose,
    presentation_order,
)

SHADOW_CEILING = FULL_DRIVE // 8


def by_name(patches: tuple[Patch, ...]) -> dict[str, Patch]:
    return {p.name: p for p in patches}


class TestBlocksAreTheUnit:
    def test_every_block_is_named_versioned_and_explains_itself(self) -> None:
        """A block that cannot say what it measures is a bundle with
        fewer patches, which is the thing blocks exist to stop being."""
        for name, block in BLOCKS.items():
            assert block.name == name
            assert block.version >= 1
            assert block.id == f"{name}/{block.version}"
            assert block.patches, f"{block.id} drives nothing"
            assert len(block.measures) > 80, f"{block.id} says nothing it measures"

    def test_patch_names_are_unique_across_every_block(self) -> None:
        """A session keys readings by patch name, so a name shared
        between two blocks would silently drop one of the pair."""
        names = [p.name for block in BLOCKS.values() for p in block.patches]

        assert len(names) == len(set(names))

    def test_a_block_versions_without_disturbing_the_others(self) -> None:
        """The whole point: adding or bumping one block leaves every
        other block's meaning, and its id, untouched."""
        ids = {block.id for block in BLOCKS.values()}
        bumped = BLOCKS["volume-mesh"].__class__(
            **{**vars(BLOCKS["volume-mesh"]), "version": 2}
        )

        assert bumped.id == "volume-mesh/2"
        assert ids - {"volume-mesh/1"} == {
            block.id for block in BLOCKS.values() if block.name != "volume-mesh"
        }


class TestSuitesAreOnlyTheirBlocks:
    def test_a_suite_carries_exactly_its_blocks_patches(self) -> None:
        for suite in (CONFIG_SUITE, VERIFY_SUITE, REPORT_SUITE):
            expected = [p for block in suite.blocks for p in block.patches]

            assert list(suite.patches) == expected
            assert suite.block_ids == tuple(b.id for b in suite.blocks)

    def test_the_config_suite_is_what_an_ocio_config_reads(self) -> None:
        """ocio-display-gen takes colorimetry.primaries,
        colorimetry.white_point, luminance.black_level and
        luminance.peak_luminance from an artifact, and nothing else.
        Five patches carry all four."""
        assert CONFIG_SUITE.blocks == (ANCHORS,)
        assert len(CONFIG_SUITE.patches) == 5

    def test_verify_adds_the_blocks_that_test_the_model(self) -> None:
        """A config assumes the channels add and follow a power law.
        Verifying that is a separate step from measuring the inputs, and
        naming it separately is what keeps later characterization work
        from being mixed in with it."""
        assert VERIFY_SUITE.blocks == (ANCHORS, RESPONSE, ADDITIVITY)
        assert len(VERIFY_SUITE.patches) == 72

    def test_verify_drives_what_protocol_3_drove(self) -> None:
        """Identical patches at identical codes, so artifacts promoted
        under that name stay comparable across the decomposition. The
        legacy name rides along until consumers match on blocks."""
        assert VERIFY_SUITE.legacy_name == "color-wrangler/characterize/3"
        assert CONFIG_SUITE.legacy_name is None
        assert REPORT_SUITE.legacy_name is None

    def test_each_suite_is_a_superset_of_the_one_before(self) -> None:
        config = {p.name: p.rgb for p in CONFIG_SUITE.patches}
        verify = {p.name: p.rgb for p in VERIFY_SUITE.patches}
        report = {p.name: p.rgb for p in REPORT_SUITE.patches}

        assert set(config) < set(verify) < set(report)
        assert all(verify[n] == rgb for n, rgb in config.items())
        assert all(report[n] == rgb for n, rgb in verify.items())


class TestConditionsTravelWithTheBlock:
    def test_composing_a_block_brings_its_conditions(self) -> None:
        """`noise-floor`'s numbers come from a panel held at video-like
        load. Composing it without that would measure something other
        than what the block describes."""
        bare = compose("anchors")
        with_floor = compose("anchors", "noise-floor")

        assert bare.conditioning_seconds == 0.0
        assert with_floor.conditioning_seconds == NOISE_FLOOR.conditioning_seconds
        assert with_floor.warmup_seconds == NOISE_FLOOR.warmup_seconds

    def test_a_suite_takes_the_strictest_across_its_blocks(self) -> None:
        assert REPORT_SUITE.conditioning_seconds == 5.0
        assert REPORT_SUITE.warmup_seconds == 600.0
        assert REPORT_SUITE.read_attempts == 10

    def test_the_verify_suite_measures_as_protocol_3_did(self) -> None:
        """Solid patches back to back, one read each — how every
        artifact promoted under that name was measured."""
        assert VERIFY_SUITE.conditioning_seconds == 0.0
        assert VERIFY_SUITE.warmup_seconds == 0.0
        assert VERIFY_SUITE.read_attempts == 1


class TestComposing:
    def test_compose_builds_a_suite_no_preset_covers(self) -> None:
        suite = compose("anchors", "noise-floor", suite_name="dark-end")

        assert suite.name == "dark-end"
        assert suite.block_ids == ("anchors/1", "noise-floor/1")

    def test_an_unknown_block_names_what_there_is(self) -> None:
        """A typo drives a session measuring the wrong thing and says
        nothing about it, so it fails loudly here instead."""
        with pytest.raises(KeyError, match="volume-mesh"):
            compose("anchors", "volume-mash")


class TestRampCodes:
    def test_ramp_codes_are_strictly_increasing_and_shadow_dense(self) -> None:
        assert list(RAMP_CODES) == sorted(set(RAMP_CODES))
        assert RAMP_CODES[0] > 0
        assert RAMP_CODES[-1] < FULL_DRIVE  # full drive belongs to the anchors
        shadow = [c for c in RAMP_CODES if c <= SHADOW_CEILING]
        assert len(shadow) >= len(_half_octave_ladder(16, SHADOW_CEILING))

    def test_no_two_ramp_codes_are_one_patch_on_a_narrow_link(self) -> None:
        """10-bit narrow range puts a level every 4.675 codes. A closer
        pair is measured twice for one reading, and reads as a ramp
        inversion the moment either carries noise."""
        gaps = [b - a for a, b in itertools.pairwise(RAMP_CODES)]

        assert min(gaps) >= MIN_CODE_SEPARATION

    def test_response_keeps_the_ladder_and_tracking_takes_the_rest(self) -> None:
        ladder = set(_half_octave_ladder(16, FULL_DRIVE))
        response_codes = {
            p.rgb[0] for p in RESPONSE.patches if p.role == "red_response"
        }
        tracking_codes = {
            p.rgb[0] for p in BLOCKS["tracking"].patches if p.role == "red_response"
        }

        assert response_codes == ladder
        assert not (tracking_codes & ladder)
        assert response_codes | tracking_codes == set(RAMP_CODES)


class TestAnchorsAndRoles:
    def test_anchor_codes(self) -> None:
        anchors = by_name(ANCHORS.patches)

        assert anchors["black"].rgb == (0, 0, 0)
        assert anchors["red"].rgb == (FULL_DRIVE, 0, 0)
        assert anchors["green"].rgb == (0, FULL_DRIVE, 0)
        assert anchors["blue"].rgb == (0, 0, FULL_DRIVE)
        assert anchors["white"].rgb == (FULL_DRIVE, FULL_DRIVE, FULL_DRIVE)

    def test_additivity_drives_the_full_drive_secondaries(self) -> None:
        triad = by_name(ADDITIVITY.patches)

        assert triad["yellow"].rgb == (FULL_DRIVE, FULL_DRIVE, 0)
        assert triad["cyan"].rgb == (0, FULL_DRIVE, FULL_DRIVE)
        assert triad["magenta"].rgb == (FULL_DRIVE, 0, FULL_DRIVE)

    def test_ramp_patches_drive_one_channel_and_gray_drives_all(self) -> None:
        named = by_name(REPORT_SUITE.patches)
        for code in RAMP_CODES:
            assert named[f"red_{code:04d}"].rgb == (code, 0, 0)
            assert named[f"green_{code:04d}"].rgb == (0, code, 0)
            assert named[f"blue_{code:04d}"].rgb == (0, 0, code)
            assert named[f"gray_{code:04d}"].rgb == (code, code, code)

    def test_every_role_maps_to_an_artifact_destination(self) -> None:
        roles = {p.role for p in REPORT_SUITE.patches}

        assert roles == {
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
            "noise_floor",
            "white_repeat",
            "volume_mesh",
            "volume_random",
        }


class TestPresentation:
    def test_black_opens_the_session_and_white_follows_it(self) -> None:
        """Both gating readings arrive before the shuffle, in this
        order: black is the most delicate reading, and driving the
        brightest patch into it would leave the panel recovering."""
        order = [p.name for p in presentation_order(VERIFY_SUITE.patches, seed=0)]

        assert order[:2] == ["black", "white"]

    def test_the_shuffle_is_deterministic_for_a_seed(self) -> None:
        first = presentation_order(REPORT_SUITE.patches, seed=3)
        second = presentation_order(REPORT_SUITE.patches, seed=3)
        other = presentation_order(REPORT_SUITE.patches, seed=4)

        assert first == second
        assert first != other

    def test_the_shuffle_drives_every_patch_once(self) -> None:
        driven = presentation_order(REPORT_SUITE.patches, seed=0)

        assert sorted(p.name for p in driven) == sorted(
            p.name for p in REPORT_SUITE.patches
        )

    def test_a_pin_naming_no_patch_is_refused(self) -> None:
        with pytest.raises(KeyError, match="chartreuse"):
            presentation_order(
                VERIFY_SUITE.patches, seed=0, pinned=("black", "chartreuse")
            )
