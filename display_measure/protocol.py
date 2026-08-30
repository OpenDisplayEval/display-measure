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

# A wire identifier, not a package name. It outlived the repository it
# was named in: the session core moved to display-measure, and this
# string did not follow it. Every promoted artifact records the protocol
# it was measured under, and ocio-display-gen matches on that name —
# renaming it breaks the provenance of every artifact already promoted.
PROTOCOL_NAME = "color-wrangler/characterize/3"

# Protocol codes are 12-bit RGB whatever link carries them; the wire
# encoding is a session parameter (`display_measure.wire`), and a
# narrower link records which of these codes it could represent.
CODE_BITS = 12
FULL_DRIVE = 2**CODE_BITS - 1

# Ramps start above the codes lost in the black floor; full drive
# belongs to the anchor patches.
RAMP_FLOOR = 16

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


RAMP_CODES = _half_octave_ladder(RAMP_FLOOR, FULL_DRIVE)


def protocol_patches() -> tuple[Patch, ...]:
    """The protocol's patch set, in protocol (unshuffled) order."""
    anchors = (
        Patch("black", (0, 0, 0), role="black_level"),
        Patch("red", (FULL_DRIVE, 0, 0), role="red_primary"),
        Patch("green", (0, FULL_DRIVE, 0), role="green_primary"),
        Patch("blue", (0, 0, FULL_DRIVE), role="blue_primary"),
        Patch("white", (FULL_DRIVE, FULL_DRIVE, FULL_DRIVE), role="white_point"),
    )
    ramps = tuple(
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
        for code in RAMP_CODES
    )
    triad = (
        Patch("yellow", (FULL_DRIVE, FULL_DRIVE, 0), role="additivity_yellow"),
        Patch("cyan", (0, FULL_DRIVE, FULL_DRIVE), role="additivity_cyan"),
        Patch("magenta", (FULL_DRIVE, 0, FULL_DRIVE), role="additivity_magenta"),
    )
    return anchors + ramps + triad


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
