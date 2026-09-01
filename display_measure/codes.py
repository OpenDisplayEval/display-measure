"""The code space every measurement is expressed in (§spec:patch-protocol).

Its own module because both the patch protocol and the probes need it,
and neither should import the other to get it: a probe searching for
first light and a block naming a ramp rung are the same code space, and
that shared fact is what this module is.
"""

# Protocol codes are 12-bit RGB whatever link carries them; the wire
# encoding is a session parameter (`display_measure.wire`), and a
# narrower link records which of these codes it could represent.
CODE_BITS = 12
FULL_DRIVE = 2**CODE_BITS - 1

# Ramps start above the codes lost in the black floor; full drive
# belongs to the anchor patches.
RAMP_FLOOR = 16

# The narrowest link a session may declare is 10-bit narrow range: 877
# levels across the 4096-code grid, one every 4.675 codes. Two protocol
# codes closer than that are the same patch on that link — measured
# twice, and read as a ramp inversion the moment either reading carries
# noise. Five is that step rounded up.
MIN_CODE_SEPARATION = 5
