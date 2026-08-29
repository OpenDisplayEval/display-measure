"""Gates over the session's own output (§road:session-consistency).

The contract audit establishes what the processor claims and the output
level establishes that the wall does it. Neither catches a session that
contradicts *itself* — a ramp that falls as drive rises, or two
instruments that disagree where they hand over. No amount of correct
external state prevents either.

**Why these run at the end.** Every other gate refuses before the
protocol is spent, because a refusal that costs a round trip beats one
that costs a rig. These cannot: a ramp is not a ramp until it is
measured. What they still prevent is the artifact — a session that
contradicts itself writes nothing, so a measurement nobody can trust
never enters the chain to be promoted later by someone who was not
there.

The 2026-08-28 bench run is the case. It passed every check that
existed, and emitted a gray ramp falling 12.8x across the routing
boundary: code 256 read 6.997 cd/m² on the colorimeter, code 384 read
0.547 on the spectroradiometer. That artifact was diagnosed by hand,
days later, by someone reading YAML.
"""

from __future__ import annotations

__all__ = [
    "InconsistentSession",
    "audit_ramp_monotonicity",
    "audit_routing_boundary",
]

# Readings at or under this are the instrument's noise, not the wall's
# response, and two noise samples in either order say nothing about
# monotonicity. The dark-room bench reads black near 0.0001 cd/m² and
# the CR-120's own floor sits near 0.0014, so a millicandela is a
# generous line under both (§road:instrument-floors).
DEFAULT_FLOOR = 0.001

# How far the two instruments may disagree where they hand over, as a
# ratio between adjacent rungs. Adjacent protocol codes step by 3/2 or
# 4/3, which through a ~2.3 exponent is a luminance step near 1.9x — so
# a boundary step is expected to be large. This bounds it at an order of
# magnitude, which admits any real step and refuses the 12.8x collapse
# that motivated the gate.
MAX_BOUNDARY_RATIO = 10.0

Ramp = list[tuple[int, float]]
RoutedRamp = list[tuple[int, float, str]]


class InconsistentSession(RuntimeError):
    """The session's own measurements contradict each other."""


def audit_ramp_monotonicity(
    ramps: dict[str, Ramp], *, floor: float = DEFAULT_FLOOR
) -> None:
    """Refuse a ramp whose luminance falls as its code rises.

    `ramps` maps a name to its rows in protocol order, ascending code.
    Rows at or under `floor` are skipped on both sides of a comparison:
    below the instrument's noise, an inversion is the instrument talking
    and refusing on it would make a dark room look like a broken
    session.
    """
    problems: list[str] = []
    for name, rows in ramps.items():
        for (low_code, low), (high_code, high) in zip(rows, rows[1:], strict=False):
            if low <= floor or high <= floor:
                continue
            if high < low:
                problems.append(
                    f"{name}: code {low_code} reads {low:.4g} cd/m² and code "
                    f"{high_code} reads {high:.4g} — more drive, less light"
                )
    if problems:
        raise InconsistentSession(
            "the session's ramps contradict themselves; refusing to write an "
            "artifact:\n  - " + "\n  - ".join(problems)
        )


def audit_routing_boundary(
    ramps: dict[str, RoutedRamp], *, max_ratio: float = MAX_BOUNDARY_RATIO
) -> None:
    """Refuse a step across a routing boundary that no drive step explains.

    `ramps` maps a name to rows of (code, luminance, source) in protocol
    order. A boundary is an adjacent pair read by different instruments;
    a step there is expected — adjacent codes differ — but a step of an
    order of magnitude is the two instruments disagreeing, not the wall
    responding.

    This is the check the disciplined-colorimeter design most needs. Its
    correction is derived at half-drive rungs and applied decades below,
    and the boundary is exactly where an extrapolation that went wrong
    becomes visible.
    """
    problems: list[str] = []
    for name, rows in ramps.items():
        for (low_code, low, low_src), (high_code, high, high_src) in zip(
            rows, rows[1:], strict=False
        ):
            if low_src == high_src or low <= 0.0 or high <= 0.0:
                continue
            ratio = max(high / low, low / high)
            if ratio > max_ratio:
                problems.append(
                    f"{name}: {low_src} reads {low:.4g} cd/m² at code "
                    f"{low_code} and {high_src} reads {high:.4g} at code "
                    f"{high_code} — {ratio:.3g}x across the handover, which "
                    "no drive step explains"
                )
    if problems:
        raise InconsistentSession(
            "the session's instruments disagree where they hand over; "
            "refusing to write an artifact:\n  - " + "\n  - ".join(problems)
        )
