"""Adaptive measurements (§spec:patch-protocol, MEASUREMENT.md).

A probe searches for something whose location is a property of the
display, so a fixed code list cannot express it. These tests hold the
first-light probe to the bench panel's measured behaviour: red lights at
code 6, green and blue at 8, and nothing below emits at all.
"""

from dataclasses import replace

import pytest

from display_measure.probes import FIRST_LIGHT, FirstLight
from display_measure.protocol import FULL_DRIVE

# The bench panel, as the toe map measured it: nothing below code 6, red
# first at 6, green and blue at 8. Luminance rises steeply above that.
BENCH_THRESHOLDS = {"red": 6, "green": 8, "blue": 8}
BENCH_BLACK = 0.000122


def probe(floor=BENCH_BLACK):
    """The probe as a session builds it: the template plus a measured floor."""
    return replace(FIRST_LIGHT, floor=floor)


# What the panel emits at its very first lit code, as a multiple of its
# black. The bench toe map measured 778 ucd/m2 at code 8 against a
# 122 ucd/m2 black — a step, not a gentle rise from nothing. That step
# is why first light is findable at all: a panel whose emission crept
# up from zero would have no code at which a reading became
# distinguishable from the floor.
FIRST_STEP_OVER_BLACK = 5.4


def bench_display(thresholds=None, black=BENCH_BLACK):
    """A display that emits nothing below its per-channel threshold."""
    thresholds = thresholds or BENCH_THRESHOLDS
    axes = {"red": 0, "green": 1, "blue": 2}
    driven = []

    def read(rgb):
        driven.append(rgb)
        light = 0.0
        for channel, axis in axes.items():
            code = rgb[axis]
            if code >= thresholds[channel]:
                over = code - thresholds[channel] + 1
                light += black * FIRST_STEP_OVER_BLACK * over**2.35
        return black + light

    read.driven = driven
    return read


class TestFindsFirstLight:
    def test_it_finds_the_bench_panel_s_thresholds(self) -> None:
        result = probe().run(bench_display())

        assert result.findings["red"] == 6
        assert result.findings["green"] == 8
        assert result.findings["blue"] == 8

    def test_it_finds_a_threshold_a_fixed_code_list_would_miss(self) -> None:
        """The point of searching. A static block over codes 1-14 asserts
        the answer is in 1-14; this panel's is not."""
        result = probe().run(bench_display({"red": 40, "green": 96, "blue": 300}))

        assert result.findings == {"red": 40, "green": 96, "blue": 300}

    def test_it_reports_a_channel_that_never_lights(self) -> None:
        """A dead channel is a finding, not a crash or a wrong number."""
        result = probe().run(
            bench_display({"red": 6, "green": 8, "blue": FULL_DRIVE + 1})
        )

        assert result.findings["blue"] is None


class TestCosts:
    def test_the_search_is_far_cheaper_than_bracketing(self) -> None:
        """Not the main argument for a probe, but it should hold: a
        binary search over 4096 codes is ~12 reads, not 4096."""
        read = bench_display()
        result = probe().run(read)

        assert result.patch_count <= probe().max_patches
        assert result.patch_count < 60

    def test_it_records_every_patch_it_drove(self) -> None:
        """A probe's answer is auditable only against the readings that
        produced it, and its codes are not implied by its id."""
        result = probe().run(bench_display())

        assert result.patch_count == len(result.driven)
        for rgb, luminance in result.driven:
            assert len(rgb) == 3
            assert luminance >= 0

    def test_it_never_exceeds_its_bound(self) -> None:
        """A session promises a bound; a probe that runs past it has
        made the promise a lie."""
        result = probe().run(bench_display({"red": 1, "green": 1, "blue": 1}))

        assert result.patch_count <= probe().max_patches


class TestIdentity:
    def test_it_is_named_versioned_and_says_what_it_measures(self) -> None:
        assert FIRST_LIGHT.id == "first-light/1"
        assert len(FIRST_LIGHT.measures) > 80

    def test_the_result_carries_the_probe_that_produced_it(self) -> None:
        result = probe().run(bench_display())

        assert result.probe_id == probe().id

    def test_a_probe_satisfies_the_protocol(self) -> None:
        from display_measure.protocol import Probe

        assert isinstance(probe(), Probe)


class TestDeterminism:
    def test_one_display_drives_one_search(self) -> None:
        """A probe's patches are an output, so two runs against one
        display are the artifact's reproducibility claim."""
        first = probe().run(bench_display())
        second = probe().run(bench_display())

        assert first.driven == second.driven

    def test_a_different_display_drives_a_different_search(self) -> None:
        default = probe().run(bench_display())
        other = probe().run(bench_display({"red": 40, "green": 40, "blue": 40}))

        assert default.driven != other.driven


def test_a_probe_needs_black_measured_first() -> None:
    """First light is 'above the noise floor', which is a number the
    anchors and noise-floor blocks carry. A probe with no floor to
    compare against is guessing."""
    with pytest.raises(ValueError, match="floor"):
        FirstLight(floor=None).run(bench_display())
