"""The characterize patch protocol (§spec:patch-protocol, §spec:sessions).

The protocol is the fixed, versioned, device-referred patch set a
characterize session drives: anchors (black, full-drive R/G/B/W),
shadow-dense per-channel ramps, a gray tracking ramp, and the Y/C/M
additivity triad — raw code values, no OCIO in the loop. MEASUREMENT.md
is the human-readable definition of record; `PROTOCOL_NAME` versions
both together.

Presentation is shuffled to decorrelate panel thermal drift from
signal-level response. The shuffle is a sha256 sort keyed by the
session seed — deterministic across Python and library versions, so
the determinism seam (`display_measure.artifact`) never depends on an
RNG stream's stability. Black is pinned ahead of the shuffle: the
ambient gate consumes the opening black reading (§spec:sessions). A
session may pin more patches — a disciplined-colorimeter session pins
the R/G/B anchors it derives its correction from — without touching the
patch set or its codes.
"""

import hashlib
from dataclasses import dataclass
from typing import TYPE_CHECKING

from display_measure.codes import (
    CODE_BITS,
    FULL_DRIVE,
    MIN_CODE_SEPARATION,
    RAMP_FLOOR,
)

__all__ = ["CODE_BITS", "FULL_DRIVE", "MIN_CODE_SEPARATION", "RAMP_FLOOR"]

if TYPE_CHECKING:
    from display_measure.probes import Probe

# A wire identifier, not a package name. It outlived the repository it
# was named in: the session core moved to display-measure, and this
# string did not follow it. Every promoted artifact records the protocol
# it was measured under, and ocio-display-gen matches on that name —
# renaming it breaks the provenance of every artifact already promoted.
# The OCIO config's suite. Unchanged since protocol 3 and versioned
# separately from the report's: ocio-display-gen matches on this name
# and every artifact already promoted carries it.
OCIO_PROTOCOL_NAME = "color-wrangler/characterize/3"

# The fidelity report's suite. A superset of the OCIO one — every
# anchor, every ladder rung, the same triad — plus what the report's
# analysis needs and the config's does not: repeated black readings for
# the noise floor, repeated white for the primary-matrix fit, and a
# sampled RGB volume for the chromaticity clusters. Measuring at report
# grade therefore yields a config-grade artifact too; the reverse does
# not hold, which is the whole reason these are two protocols.
REPORT_PROTOCOL_NAME = "color-wrangler/report/1"


# Parity with the measure path display-report retired in `dd7425e`, whose
# CLI defaults these reproduce (`--grey-n`, `--cube-n`, `--black-n`,
# `--white-n`, `--random`). The report's analysis was written against that
# patch set, and protocol 3's 72 axis-only patches are not a smaller
# version of it but a different experiment: the chromaticity chart
# clusters the RGB volume, and a volume nothing measured clusters into
# empty and duplicate groups.
#
# The codes are chosen in device space rather than from a display's
# declared luminance, which is where this departs from a transliteration.
# display-report bounded its ramps by `--max-luminance` and
# `--min-above-black`, making the patch set a function of the display in
# front of it; two panels then measured different codes and their reports
# compared different experiments. Protocol 3 fixed the codes to make
# sessions comparable, and that property is worth keeping. What the
# analysis needs is the count and the spread, not the particular codes.
RAMP_SAMPLES = 25
MESH_SIZE = 8
BLACK_REPEATS = 20
WHITE_REPEATS = 5
RANDOM_PATCHES = 100

# Patches driven ahead of the shuffle, in this order. Black opens every
# session — the ambient gate consumes that reading — and white follows
# it, so the session's two gating readings both arrive before the
# shuffle (§spec:sessions).
#
# White is pinned *after* black, never before: black is the session's
# most delicate reading, and driving full white into it would leave the
# panel and the instrument recovering from the brightest stimulus in the
# protocol. This order leaves black's conditions exactly as protocol 2
# read them, and still moves the level gate from patch 69 to patch 2.
BLACK_PATCH = "black"
WHITE_PATCH = "white"
OPENING_PINS: tuple[str, ...] = (BLACK_PATCH, WHITE_PATCH)


@dataclass(frozen=True)
class Patch:
    """A solid device-referred patch: raw code values, no OCIO in the loop.

    `role` names the artifact destination the patch's reading fills.
    """

    name: str
    rgb: tuple[int, int, int]
    role: str


def _half_octave_ladder(floor: int, ceiling: int) -> tuple[int, ...]:
    """Codes from `floor` upward, doubling every two steps (3/2 then 4/3).

    Half-octave spacing in code space gives near-constant relative
    luminance steps through a power-law decode — dense where the shadow
    response needs it (§spec:signal-contract), sparse where it does not.
    Both factors are exact on the power-of-two floor, so every rung is
    an integer code.
    """
    codes = [floor]
    factors = (3, 2), (4, 3)
    while True:
        numerator, denominator = factors[(len(codes) - 1) % 2]
        rung = codes[-1] * numerator // denominator
        if rung >= ceiling:
            return tuple(codes)
        codes.append(rung)


def _even_ramp(floor: int, ceiling: int, samples: int) -> tuple[int, ...]:
    """`samples` evenly spaced codes from `floor` to `ceiling`.

    The spacing display-report's ramps used, which its grey-tracking and
    EOTF-accuracy plots read as their independent variable. Even spacing
    in code space undersamples the shadows, which is why it is unioned
    with the half-octave ladder rather than replacing it.
    """
    step = (ceiling - floor) / (samples - 1)
    return tuple(round(floor + index * step) for index in range(samples))


def _merged_ramp(ladder: tuple[int, ...], even: tuple[int, ...]) -> tuple[int, ...]:
    """Both spacings, with near-duplicates resolved in the ladder's favour.

    Parity needs the even ramp and the toe needs the ladder, so the
    protocol drives both. Where a parity code lands within
    `MIN_CODE_SEPARATION` of a ladder rung it is dropped rather than
    driven: the two are one patch on a narrow link, and the pair costs
    a measurement to learn nothing on any link.

    The ladder wins ties because its rungs are exact on the power-of-two
    floor, and because dropping one would leave a hole in the shadow
    sampling the even ramp is too coarse to fill.
    """
    kept = set(ladder)
    for code in even:
        if all(abs(code - rung) >= MIN_CODE_SEPARATION for rung in kept):
            kept.add(code)
    # Full drive belongs to the anchors — the artifact folds each
    # full-drive anchor reading into its ramp as the code-4095 row.
    return tuple(sorted(kept - {FULL_DRIVE}))


RAMP_CODES = _merged_ramp(
    _half_octave_ladder(RAMP_FLOOR, FULL_DRIVE),
    _even_ramp(RAMP_FLOOR, FULL_DRIVE, RAMP_SAMPLES),
)


def _mesh_codes(
    size: int, floor: int, ceiling: int
) -> tuple[tuple[int, int, int], ...]:
    """A `size`^3 lattice through the RGB cube, floor to ceiling.

    No channel reaches zero: a mesh point with a dark channel sits on a
    face of the cube, and the axes are already sampled by the ramps. The
    cube's interior is what the chromaticity chart clusters.
    """
    step = (ceiling - floor) / (size - 1)
    axis = tuple(round(floor + index * step) for index in range(size))
    return tuple((r, g, b) for r in axis for g in axis for b in axis)


def _random_codes(
    count: int, floor: int, ceiling: int
) -> tuple[tuple[int, int, int], ...]:
    """`count` scattered codes, off the mesh lattice.

    Derived from sha256 rather than an RNG, for the reason the shuffle
    is: the determinism seam (`display_measure.artifact`) cannot depend
    on a library's random stream staying stable across versions.
    display-report seeded numpy from a hash of its config, which is the
    same intent through a weaker guarantee.
    """
    span = ceiling - floor + 1
    scattered = []
    for index in range(count):
        digest = hashlib.sha256(
            f"{REPORT_PROTOCOL_NAME}:random:{index}".encode()
        ).digest()
        scattered.append(
            tuple(
                floor + int.from_bytes(digest[word * 4 : word * 4 + 4], "big") % span
                for word in range(3)
            )
        )
    return tuple(scattered)  # type: ignore[arg-type]


MESH_CODES = _mesh_codes(MESH_SIZE, RAMP_FLOOR, FULL_DRIVE)
RANDOM_CODES = _random_codes(RANDOM_PATCHES, RAMP_FLOOR, FULL_DRIVE)


def _channel_ramps(codes: tuple[int, ...]) -> tuple[Patch, ...]:
    """Per-channel and gray ramps over `codes`, in protocol order."""
    return tuple(
        Patch(
            f"{channel}_{code:04d}",
            (code * axes[0], code * axes[1], code * axes[2]),
            role=role,
        )
        for channel, axes, role in (
            ("red", (1, 0, 0), "red_response"),
            ("green", (0, 1, 0), "green_response"),
            ("blue", (0, 0, 1), "blue_response"),
            ("gray", (1, 1, 1), "gray_response"),
        )
        for code in codes
    )


@dataclass(frozen=True)
class PatchBlock:
    """A named, independently versioned group of patches.

    Measurement is decomposable, and this is the unit it decomposes
    into. A block names one thing worth measuring, says what reads it,
    and versions on its own — so adding a block, or bumping one, leaves
    every other block's meaning untouched (§spec:patch-protocol).

    Bundles were the alternative and they do not survive contact with a
    growing pipeline. A single versioned patch list mixes the
    measurements a model takes as input with the measurements that test
    the model, and the ones a report needs with the ones a config
    needs. Every addition then bumps the bundle, which says nothing
    about what changed, and no consumer can state what it actually
    requires beyond a version number that moves for reasons unrelated
    to it.

    `measures` says what the block measures, in the display's terms —
    not who reads it. display-measure imports nothing from its consumers
    and describes none of them: a consumer states the blocks it requires
    on its own side, against the ids an artifact records
    (§spec:patch-protocol).

    The conditions state what this block's numbers assume. A suite
    takes the strictest across the blocks it composes, so composing in
    a block that was measured under panel conditioning brings the
    conditioning with it rather than leaving it to the caller.
    """

    name: str
    version: int
    patches: tuple[Patch, ...]
    measures: str
    warmup_seconds: float = 0.0
    conditioning_seconds: float = 0.0
    read_attempts: int = 1

    @property
    def id(self) -> str:
        """What an artifact records and a consumer matches on."""
        return f"{self.name}/{self.version}"


ANCHORS = PatchBlock(
    name="anchors",
    version=1,
    patches=(
        Patch("black", (0, 0, 0), role="black_level"),
        Patch("red", (FULL_DRIVE, 0, 0), role="red_primary"),
        Patch("green", (0, FULL_DRIVE, 0), role="green_primary"),
        Patch("blue", (0, 0, FULL_DRIVE), role="blue_primary"),
        Patch("white", (FULL_DRIVE, FULL_DRIVE, FULL_DRIVE), role="white_point"),
    ),
    measures=(
        "The display's primaries and white point as chromaticities, its "
        "black level and its peak luminance, all in absolute cd/m². The "
        "corners of what the display can do; every other block is "
        "measured against these."
    ),
)

RESPONSE = PatchBlock(
    name="response",
    version=1,
    patches=_channel_ramps(_half_octave_ladder(RAMP_FLOOR, FULL_DRIVE)),
    measures=(
        "Each channel's luminance against drive level, at half-octave "
        "code spacing — dense where a shadow response departs from a "
        "power law, sparse where it does not. Falsifies a declared "
        "transfer function rather than deriving one."
    ),
)

ADDITIVITY = PatchBlock(
    name="additivity",
    version=1,
    patches=(
        Patch("yellow", (FULL_DRIVE, FULL_DRIVE, 0), role="additivity_yellow"),
        Patch("cyan", (0, FULL_DRIVE, FULL_DRIVE), role="additivity_cyan"),
        Patch("magenta", (FULL_DRIVE, 0, FULL_DRIVE), role="additivity_magenta"),
    ),
    measures=(
        "The full-drive secondaries, against which the sum of their "
        "component primaries can be checked. Falsifies channel "
        "additivity at the one drive level where it is cheapest to test."
    ),
)

# Blocks below here come from the measure path display-report retired in
# `dd7425e`, and carry its conditions: its numbers are what the report's
# analysis reads, and they were measured on a panel held at video-like
# load. That is why the conditions travel on the block.
REPORT_WARMUP_SECONDS = 600.0
REPORT_CONDITIONING_SECONDS = 5.0
REPORT_READ_ATTEMPTS = 10

TRACKING = PatchBlock(
    name="tracking",
    version=1,
    patches=_channel_ramps(
        tuple(
            code
            for code in RAMP_CODES
            if code not in set(_half_octave_ladder(RAMP_FLOOR, FULL_DRIVE))
        )
    ),
    measures=(
        "Each channel's luminance at evenly spaced codes across the "
        "range, filling between `response`'s half-octave rungs. Even "
        "spacing undersamples the shadows, so this adds to that block "
        "rather than replacing it."
    ),
    warmup_seconds=REPORT_WARMUP_SECONDS,
    conditioning_seconds=REPORT_CONDITIONING_SECONDS,
    read_attempts=REPORT_READ_ATTEMPTS,
)

NOISE_FLOOR = PatchBlock(
    name="noise-floor",
    version=1,
    patches=tuple(
        Patch(f"black_{index:02d}", (0, 0, 0), role="noise_floor")
        for index in range(2, BLACK_REPEATS + 1)
    ),
    measures=(
        "The spread across twenty black readings — the floor below which "
        "this instrument on this display cannot distinguish a patch from "
        "no light at all. One reading has no spread and measures "
        "nothing. The repeats scatter through the session by the shuffle "
        "rule, so the floor describes the session rather than one minute "
        "of it."
    ),
    warmup_seconds=REPORT_WARMUP_SECONDS,
    conditioning_seconds=REPORT_CONDITIONING_SECONDS,
    read_attempts=REPORT_READ_ATTEMPTS,
)

WHITE_REPEAT = PatchBlock(
    name="white-repeat",
    version=1,
    patches=tuple(
        Patch(f"white_{index:02d}", (FULL_DRIVE,) * 3, role="white_repeat")
        for index in range(2, WHITE_REPEATS + 1)
    ),
    measures=(
        "The spread across five full-white readings, giving the white "
        "point a distribution rather than a single point for anything "
        "fitting robustly to it."
    ),
    warmup_seconds=REPORT_WARMUP_SECONDS,
    conditioning_seconds=REPORT_CONDITIONING_SECONDS,
    read_attempts=REPORT_READ_ATTEMPTS,
)

VOLUME_MESH = PatchBlock(
    name="volume-mesh",
    version=1,
    patches=tuple(
        Patch(f"mesh_{index:04d}", rgb, role="volume_mesh")
        for index, rgb in enumerate(MESH_CODES)
    ),
    measures=(
        "The display's response through the interior of the RGB cube, on "
        "a regular lattice — where a model fitted to the axes alone is "
        "unconstrained and can be wrong without any axis measurement "
        "showing it. The largest block by far, and the first to drop "
        "when session time binds."
    ),
    warmup_seconds=REPORT_WARMUP_SECONDS,
    conditioning_seconds=REPORT_CONDITIONING_SECONDS,
    read_attempts=REPORT_READ_ATTEMPTS,
)

VOLUME_SCATTER = PatchBlock(
    name="volume-scatter",
    version=1,
    patches=tuple(
        Patch(f"random_{index:04d}", rgb, role="volume_random")
        for index, rgb in enumerate(RANDOM_CODES)
    ),
    measures=(
        "The display's response at codes off the lattice, catching what "
        "a regular grid steps over. A hundredth of `volume-mesh`'s cost, "
        "and a different question about coverage, so it is its own block."
    ),
    warmup_seconds=REPORT_WARMUP_SECONDS,
    conditioning_seconds=REPORT_CONDITIONING_SECONDS,
    read_attempts=REPORT_READ_ATTEMPTS,
)

BLOCKS = {
    block.name: block
    for block in (
        ANCHORS,
        RESPONSE,
        ADDITIVITY,
        TRACKING,
        NOISE_FLOOR,
        WHITE_REPEAT,
        VOLUME_MESH,
        VOLUME_SCATTER,
    )
}


@dataclass(frozen=True)
class MeasurementSuite:
    """An ordered composition of blocks, and the conditions they need.

    A suite is a convenience over blocks, not a thing in its own right.
    It exists so an operator can say what a session is *for* without
    listing eight names, and so the common compositions have somewhere
    to be written down. What the artifact records, and what a consumer
    matches on, is the block list — a suite name is a label
    (§spec:patch-protocol).

    Conditions are the strictest across the composed blocks, never the
    suite's own opinion. Compose in a block whose numbers assume panel
    conditioning and the conditioning comes with it; leave it out and it
    does not. That is what keeps a composition from quietly measuring
    something other than what its blocks describe.
    """

    name: str
    blocks: tuple[PatchBlock, ...]
    # Probes run after every static block, never among them: each of a
    # probe's patches depends on the reading before it, so it cannot
    # join the shuffle that decorrelates panel drift from signal level,
    # and it needs the anchors measured to have a floor to search
    # against (§spec:patch-protocol).
    probes: tuple["Probe", ...] = ()
    # Until consumers match on blocks, an artifact still carries one
    # string and ocio-display-gen still matches it. A suite whose blocks
    # are exactly what a released protocol named keeps that name here so
    # artifacts stay comparable across the change (§road:ocio-reads-csmf
    # retires this field). No new suite should claim one.
    legacy_name: str | None = None

    @property
    def patches(self) -> tuple[Patch, ...]:
        """Every block's patches, in block order.

        Names are unique across blocks by construction, and the session
        keys readings by name, so a duplicate would silently drop a
        reading. Checked rather than assumed.
        """
        patches = tuple(patch for block in self.blocks for patch in block.patches)
        names = [patch.name for patch in patches]
        if len(names) != len(set(names)):
            duplicated = sorted({n for n in names if names.count(n) > 1})
            raise ValueError(
                f"suite {self.name!r} composes blocks that share patch "
                f"names: {duplicated}. A session keys readings by name, so "
                "one of each pair would be lost with no error."
            )
        return patches

    @property
    def block_ids(self) -> tuple[str, ...]:
        """What the artifact records: name and version, per block."""
        return tuple(block.id for block in self.blocks)

    @property
    def probe_ids(self) -> tuple[str, ...]:
        return tuple(probe.id for probe in self.probes)

    @property
    def max_patches(self) -> int:
        """What a session can promise up front.

        A bound, not a count, wherever the suite runs a probe: a probe's
        patches are decided from its readings, so the number driven is
        known only afterwards.
        """
        return len(self.patches) + sum(probe.max_patches for probe in self.probes)

    @property
    def warmup_seconds(self) -> float:
        return max(block.warmup_seconds for block in self.blocks)

    @property
    def conditioning_seconds(self) -> float:
        return max(block.conditioning_seconds for block in self.blocks)

    @property
    def read_attempts(self) -> int:
        return max(block.read_attempts for block in self.blocks)


# Imported here, below ProbeResult and above the suites that compose
# probes: `display_measure.probes` reads ProbeResult from this module,
# so the dependency runs one way.
from display_measure.probes import FIRST_LIGHT  # noqa: E402

# What an OCIO config reads, and nothing else. Five patches: a session
# that only needs a config need not spend an hour proving things the
# config does not consult.
CONFIG_SUITE = MeasurementSuite(name="config", blocks=(ANCHORS,))

# The config's inputs plus the measurements that test the model built on
# them. This is exactly what `color-wrangler/characterize/3` drove — the
# same 72 patches at the same codes — which is the point: that protocol
# was a characterization step with a verification step buried in it.
VERIFY_SUITE = MeasurementSuite(
    name="verify",
    blocks=(ANCHORS, RESPONSE, ADDITIVITY),
    legacy_name="color-wrangler/characterize/3",
)

# Everything the fidelity report's analysis reads.
REPORT_SUITE = MeasurementSuite(
    name="report",
    blocks=(
        ANCHORS,
        RESPONSE,
        ADDITIVITY,
        TRACKING,
        NOISE_FLOOR,
        WHITE_REPEAT,
        VOLUME_MESH,
        VOLUME_SCATTER,
    ),
    probes=(FIRST_LIGHT,),
)

SUITES = {suite.name: suite for suite in (CONFIG_SUITE, VERIFY_SUITE, REPORT_SUITE)}


def compose(*names: str, suite_name: str = "custom") -> MeasurementSuite:
    """A suite from block names, for a composition no preset covers.

    Raises KeyError naming what is available, because a typo here drives
    a session that measures the wrong thing and says nothing about it.
    """
    unknown = [name for name in names if name not in BLOCKS]
    if unknown:
        raise KeyError(f"no such block(s): {unknown}. Available: {sorted(BLOCKS)}")
    return MeasurementSuite(
        name=suite_name, blocks=tuple(BLOCKS[name] for name in names)
    )


def presentation_order(
    patches: tuple[Patch, ...],
    seed: int,
    *,
    pinned: tuple[str, ...] = OPENING_PINS,
) -> tuple[Patch, ...]:
    """The driven order: `pinned` patches first, the rest sha256-shuffled.

    Black is pinned by default — the ambient gate consumes the opening
    reading. A session whose instrument must be calibrated against
    patches the protocol already carries pins those too
    (§spec:sessions); the patch set, its codes, and the
    shuffle rule over the tail are unchanged, and the artifact records
    the resulting names as the unshuffle key (§spec:artifact-chain).
    Sorting patches back to protocol order reconstructs the analysis
    view.

    Raises KeyError when a pin names no patch, ValueError when a patch
    is pinned twice — either would drive a patch the protocol does not
    carry, or carry one twice.
    """
    by_name = {patch.name: patch for patch in patches}
    head = []
    for name in pinned:
        if name not in by_name:
            raise KeyError(f"pinned patch {name!r} is not in the protocol")
        head.append(by_name[name])
    heads = set(pinned)
    if len(heads) != len(pinned):
        raise ValueError(f"a patch is pinned more than once: {pinned!r}")
    rest = [patch for patch in patches if patch.name not in heads]

    def sort_key(patch: Patch) -> str:
        return hashlib.sha256(f"{seed}:{patch.name}".encode()).hexdigest()

    return (*head, *sorted(rest, key=sort_key))
