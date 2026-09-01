# display-measure — Specification

The measure layer: gated instrument sessions that drive known code
values at a display through its real signal chain, read the emitted
light, and write an immutable measurement file.

System requirements, the four-layer architecture and the cross-repo
roadmap live in
[color-wrangler](https://github.com/Fuse-Technical-Group/color-wrangler),
which this repository references as upstream context. A slug in
backticks — `§spec:session-ownership`, `§spec:signal-contract`,
`§spec:artifact-chain` — resolves in that repository's SPEC.md, not
this one; so does any slug carried in this repository's source that no
heading here defines. This document covers only what this layer owns.

Where a heading here would otherwise collide with an upstream one, it
takes a distinct slug. Upstream owns `§spec:sessions` and says more
about a session than this layer needs to, so the local section is
§spec:measure-sessions. The ported source's own `§spec:sessions`
citations were written against upstream's section and still resolve
there, which is what they meant. Two documents owning one anchor with
drifting content is not reuse.

## Scope §spec:scope

*Status: in progress*

One repository opens a serial port or a DeckLink, and it is this one
(`§spec:session-ownership`). It drives patches through the show chain,
reads the instruments, holds the session gates, and emits the
measurement file. color-wrangler orchestrates and presents;
ocio-display-gen generates; display-report reports. None of them
imports a device driver.

The boundary is code ownership, not process isolation. A caller runs a
session in its own process, so the process driving the display may also
serve an operator UI. What does not happen is a second implementation
of device access — a gate cannot be bypassed by reaching for the
device directly, because only one repository knows how.

Not owned here: OCIO semantics and config generation (ocio-display-gen),
ΔE analysis and reports (display-report), the show manifest's schema
and the promotion decision (color-wrangler). Device layers are
referenced, never re-specified: bmd-signal-gen owns patch rendering and
wire-format correctness, pydecklink owns device access, colour-specio
owns instrument communication; its `measure()` surface is the driver
contract.

colour-specio is a fork of `colour-science/colour-specio`, org-owned
and tracking upstream, carrying device support upstream does not have
yet. Upstream stays the venue for fixes and the fork's delta stays
small enough to send there; the fork is the pin until a fix merges.
The seam file's format is colour-specio's too
(`§spec:measurement-seam`), so a gap in its measurement file reader is
a gap in this layer's output — fixed upstream, not worked around
here.

## Measurement sessions §spec:measure-sessions

*Status: in progress*

A session is one command run with the display powered and the instrument
aimed. Modes share one core — contract audit, ambient gate, drive,
settle, read, log — and differ only in what they drive and what they
hand off. `characterize` drives the fixed patch protocol and emits the
measurements artifact; `verify` drives config-derived probes and hands
its readings to display-report.

Each stage narrates to the session log, so a run is auditable while it
happens rather than only after it ends.

**The processor is read-only.** The session observes and refuses; it
never mutates show hardware, so a session can run against a live rig
without risk.

**No iteration.** Measure-fit-remeasure convergence is a
control-systems project, not glue. A session characterizes or verifies
once and reports; escalation to fitted or corrective LUTs is a human
decision informed by display-report's analysis.

**The wire is declared.** The encoding between a patch and the device
— bit depth, sampling, levels, colour matrix, layout — is a session
parameter, defaulting to the bench's 12-bit RGB identity. The session
encodes through the shared packer and implements no conversion; the
format-confirmation gate holds the processor to the declaration, and
the artifact records it. Why the wire is its own layer:
`§spec:architecture`.

**Instruments have doubles.** The default double is a deterministic
plausible display — an additive per-channel model that synthesizes the
reading for the frame being driven — so the hardware-free
characterize loop produces physically sensible numbers rather than
noise. A colorimeter double reading the same display through a fixed
filter mismatch pairs with it, so the disciplined-hybrid derivation is
checkable without hardware. colour-specio's random virtual
spectrometer remains available for plumbing-only tests: its
physics-free readings exercise the seams, not the numbers.

**Disciplined hybrid.** Where a colorimeter alone cannot hold
characterization grade, a 3×3 derived in session from paired readings
of the full-drive R/G/B anchors (four-color matrix, Ohno & Hardis
1997; ASTM E1455) corrects the colorimeter onto the spectroradiometer.
A three-primary additive display makes that one matrix valid for every
mixture the display emits. Above a luminance threshold the
spectroradiometer's reading lands in the artifact; below it the
disciplined colorimeter's, which is where spectroradiometer
integration time explodes. The correction is derived per mount — it
absorbs the two instruments' spot geometry — and applied in software,
never through the colorimeter's onboard calibration slots: an
in-instrument matrix is exactly the unauditable correction this system
exists to avoid.

## Session gates §spec:session-gates

*Status: in progress*

A measurement is worth only the conditions it was taken under, and
those conditions are easy to get wrong in ways nothing notices. So a
session refuses before it measures. Each gate names what it read, what
it expected, and why it stopped.

- **Contract audit** — snapshot the processor read-only and diff every
  field the manifest declares (`§spec:signal-contract`). A session
  shall refuse on divergence, and shall refuse on a processing feature
  the manifest does not state: silence is how a rig's state goes
  unchecked.
- **Consistency check** — compare measured white against the declared
  intensity on sane-default bounds. A contract audit establishes only
  what the processor claims; the state that matters most is the one
  the API reports correctly and the operator set wrong.
- **Ambient gate** — open and close the session with a black-floor
  reading and record it. Characterization-grade sessions shall refuse
  when the floor exceeds the budget implied by the dark-patch
  tolerance. Ambient is recorded and gated, never compensated: a
  display cannot emit negative light to cancel reflection, and baking
  a venue's ambient into a characterization poisons the config
  everywhere else.
- **Format confirmation** — read back the processor's input metadata
  per patch batch and confirm the wire format matches the format the
  session declared.
- **Derivation fitness** — where two instruments are paired, refuse
  when the correction between them is unfit to extrapolate, rather
  than spending the protocol on it.
- **Panel-state attestation** — a class of settings lives on the
  receiver card, surfaces only on the panel OSD, and moves the
  measurement, while no leaf of the processor's API reports any of it.
  The session confirms each with the operator — the only instrument
  that can read it — verbatim as the OSD reads it, and the artifact
  records it. Attested values are carried, never compared: naming them
  as readings would be the same silence in a new place.
- **Provenance gate** (`verify`) — refuse to measure against a config
  whose recorded input hashes do not match the files on disk. A stale
  config would produce a report about a characterization that no
  longer exists.

**Why patches ride the show chain.** A processor's internal generator
injects downstream of input decode, bypassing receive, colour-space
conversion, range handling and bit-depth truncation — it characterizes
a system the show signal never traverses. Its only role is manual
troubleshooting.

## Session events §spec:session-events

*Status: complete*

The session core reports its lifecycle as one stream of structured
events and narrates nowhere else. `display_measure.events` defines
them; `display_measure.session_log` renders them as the session log,
and color-wrangler's operator UI renders the same stream from another
repository (`§spec:web-ui`). The log is a consumer, not a second
reporting path.

A session emits session start (mode, protocol name, patch count),
playback, the three per-patch stages, gate outcomes, handoff (artifact
path and hash) and session end — completed, refused, failed or
cancelled. The stream opens with the start and closes with the end
exactly once, whichever way the session left.

**The events are this package's public API.** They are re-exported
from `display_measure`, and the module defining them imports nothing
but the standard library — a frontend renders a session without
loading numpy, specio or colour, and `--help` pays for none of it.
Each event is a frozen dataclass of plain values, so it survives
`asdict` and a wire; none carries a live device, instrument or array.
A consumer ignores event types it does not recognize, so adding one is
not a breaking change.

**Why the count rides the first event.** The patch protocol is fixed
and versioned (§spec:patch-protocol), so the total is known before the
first patch is driven and a consumer renders measured-of-total with no
heuristic. Per-patch durations ride the completion events because
instrument reads dominate a session's display clock and vary by
instrument and patch level — a constant would mislead.

**Cancellation is asked between patch steps and nowhere else.** A
session stopped mid-patch would leave a driven frame with no reading
behind it, and the measurements artifact is immutable and complete
(`§spec:artifact-chain`), so a partial one does not exist: the output
is all or nothing. A cancelled session stops playback, writes no
artifact, and ends the stream cancelled. Ctrl-C is the CLI's cancel
source — the first interrupt raises the flag, the second aborts the
process, because an instrument read that never returns needs an
escape.

**One gap, stated rather than hidden.** The hardware path audits the
processor before the session opens, so the wire-format and
output-scaling gates refuse with no event behind them: nothing has
started to end. They reach the operator as the command's refusal until
§road:pre-session-gate-events opens the stream early enough to carry
them.

### Self-consistency §spec:self-consistency

*Status: complete*

A session refuses to write an artifact whose own rows contradict each
other: a ramp whose luminance falls as its code rises, or two
instruments that disagree by an order of magnitude where they hand
over.

**Why this one resolves late.** Every other gate refuses before the
protocol is spent, because a refusal costing a round trip beats one
costing a rig. A ramp is not a ramp until it is measured, so this can
only judge at the end. What it still prevents is the artifact — and the
artifact is what outlives the session. A measurement that contradicts
itself never enters the chain to be promoted later by someone who was
not in the room.

**Why there is no bypass.** A flag to skip the check would be reached
for the first time a rig misbehaved at 2 a.m., which is exactly when
the artifact matters most. The physics-free virtual spectrometer is
consequently refused too, and its test asserts that: reaching the gate
is what proves the seam, and the numbers behind it were never meant to
be believed.

**Why an order of magnitude at the boundary.** Adjacent protocol codes
step by 3/2 or 4/3, which through a ~2.3 exponent is a luminance step
near 1.9x, so a step across the handover is expected to be large. The
bound admits any real step and refuses the 12.8x collapse that produced
an artifact nobody could trust.

## Patch protocol §spec:patch-protocol

*Status: complete*

Measurement decomposes into named, independently versioned **blocks**.
A block names one thing worth measuring, states what reads it, and
versions on its own. A session drives a composition of blocks; the
artifact records which blocks at which versions; a consumer requires
the blocks it reads. Codes are device-referred, raw, with no OCIO in
the loop, since characterization precedes any config. MEASUREMENT.md is
the human-readable definition of record.

**Blocks rather than a versioned patch list, because a bundle cannot be
reasoned about.** A single list mixes the measurements a model takes as
input with the measurements that test the model, and the ones a report
needs with the ones a config needs. Every addition bumps the bundle,
which says nothing about what changed, and no consumer can state what
it requires beyond a version that moves for reasons unrelated to it.
Characterization grows; in a bundle, new characterization patches
arrive indistinguishable from the verification patches already buried
there.

**Verification is named as verification.** The measurements that feed a
model and the measurements that falsify it are different work with
different consumers, and a step that does both under one name conceals
which it is doing. An OCIO config reads the display's primaries, white
point, black level and peak luminance; whether the channels add, and
whether each follows the declared transfer function, is a separate
question asked of the config's own assumptions. A config generates
without those blocks — it just cannot be known to be wrong.

**Some measurements are adaptive, and those are a different unit.** A
block's patches are fixed and shuffled; a probe's are decided from its
own readings, because what it searches for sits wherever the display
puts it and a fixed code list either misses that or brackets where it
is not. The two are named apart rather than unified, because a probe
cannot join the shuffle that decorrelates thermal drift from signal
level, its cost is a bound rather than a count, and its patch list is a
result rather than an input. Calling both a block would bury all three.

**A probe's codes are recorded, because they are a measurement.** A
block's codes are implied by its id and need not be written down. A
probe's answer is auditable only against the readings that produced it,
so the artifact carries every code it drove and what each read.

**A suite is a label over a composition, not a thing.** It exists so an
operator can say what a session is for without naming eight blocks. It
is not what an artifact means, and it is not what a consumer matches
on.

**A consumer's analysis constrains its blocks, so a block is specified
against the analysis.** The report filters patches against a noise
floor measured as the *spread* of repeated black readings, and clusters
measured error through the RGB volume. A single black reading makes
that filter uncomputable rather than permissive; an axes-only patch set
makes the volume clusters empty. Neither failure is visible on the
rendered page — the report draws, and what it draws is wrong — so the
block is derived from what reads it rather than from what a session can
afford.

**The conditions a measurement assumes travel with its block.** An LED
panel measured on a run of solid patches is not the panel an operator
drives: junction temperature settles somewhere a moving picture never
takes it. A block therefore declares the warmup, inter-patch
conditioning and read attempts its numbers assume, and a suite takes
the strictest across what it composes. Composing a block brings its
conditions rather than leaving them to the caller — a report measured
without the conditioning its analysis was calibrated against is not a
report with a caveat, but one describing a thermal state nothing else
in the pipeline assumes. A session may override one, which is what a
bench investigation needs and a comparable run does not do.

**An artifact carries what its session measured and no more.** A
composition without the response blocks yields an artifact with no
response section — absent, not empty. An empty ramp reports an
unmeasured thing as a measured one.

Presentation is shuffled to decorrelate panel thermal drift from
signal-level response. The shuffle is a sha256 sort keyed by the
session seed — deterministic across Python and library versions — so
the artifact's byte-determinism never depends on an RNG stream's
stability. Black is pinned ahead of the shuffle because the ambient
gate consumes the opening black reading; a session may pin more
patches without touching the patch set or its codes. The driven order
varies per session and the artifact records it in full.

## Measurements artifact §spec:measurements-artifact

*Status: complete*

The session's output is one file: machine-written, immutable, never
hand-edited (`§spec:artifact-chain`). It carries the measured
primaries and white point, black level and peak luminance in absolute
cd/m², per-channel response, ambient floor, instrument identity and
firmware, the processor-state snapshot, the wire encoding, the protocol
name and driven order, and timestamps. Where two instruments were
paired it also records the correction matrix, the routing threshold,
and the instrument behind every row.

**The wire encoding is recorded, and what the wire carried.** Two
artifacts of one display over different links are different
measurements, and a loader can only refuse to compare them if the
artifact says which link. The block records the declaration and, per
driven patch, the codes the wire carried — a fact of the session. What
a narrow-range link could not represent is derivable from those and
the encoding (`§spec:encoding` in pypixelpack); the artifact records
what happened, not a model of the processor's inverse.

Humans do not edit it. Editing measured values only injects error, and
machine-attestable device state is readable from device APIs, so
transcription is an error source with no compensating value. The human
act at the seam is acceptance: promoting a run by recording its hash.

**Byte-determinism and timestamps reconcile through injection.** The
session takes a clock as input, so fixed inputs produce identical
bytes; the claim is "same inputs, same bytes", not "no timestamps".
Rendering is deterministic by construction: fixed key order, fixed
nine-decimal float formatting below any instrument's repeatability, LF
line endings, UTF-8.

**The schema and protocol strings are wire identifiers, not package names.** The artifact
schema is `color-wrangler/measurements/2` and the characterize
protocol string is `color-wrangler/characterize/3`. Both outlived the
repository they were named in: the session core moved here and the
strings did not follow it. Every promoted artifact carries them, and
downstream loaders dispatch on them, so renaming either breaks the
provenance of every artifact already promoted. They change only when
the format they name changes: schema 2 added the wire encoding block,
and a schema-1 artifact implied the bench's 12-bit RGB link.

The protocol string is the one being retired. Artifacts record their
measurement blocks and versions, which is what a consumer requires; the
string survives only because ocio-display-gen matches it today and
every artifact already promoted carries it. The `verify` composition —
the blocks that protocol drove — carries it forward as a legacy name
until `§road:ocio-reads-csmf` lands. No new composition claims one
(MEASUREMENT.md).

**The artifact is the seam file** (`§spec:measurement-seam`). It is
CSMF, carrying the spectra behind each reading and its per-row
spectral provenance, with everything above — contract, panel state,
protocol name, instrument identity, hashes — in the provenance block
CSMF's reserved ancillary field holds. One file: CSMF replaces the
YAML rendering rather than joining it, because a pipeline with two
measurements files of record has none.

**Determinism survives the format change through the projection, not
the bytes.** Byte-determinism was a property of the YAML rendering,
and protobuf guarantees round-trip rather than canonical encoding — a
digest over raw file bytes would rotate on a dependency upgrade. The
digest therefore covers the same canonical rendering as before, now
computed from the parsed values instead of written to disk: fixed key
order, nine-decimal floats, LF, UTF-8. The renderer is retained and
repurposed; the artifact stays self-verifying, and a re-serialized
file with identical content still verifies. The projection carries
each row's spectrum as a digest rather than as samples: an
instrument's grid is hundreds of bins per row, and restating every one
would multiply the file's size for data it already holds.

**Spectral provenance is per row** (`§spec:spectral-retention`). Each
row records whether its spectrum was measured, reconstructed or
absent, so an analysis needing a real spectrum refuses the rows that
lack one instead of treating a scaled estimate as a measurement. A
colorimeter-routed row carries the bright-regime reading of the same
stimulus scaled to the luminance it measured, and names the luminance
span the scaling reached across. Its tristimulus stays the one the
disciplined colorimeter measured: a reconstruction carries its
anchor's chromaticity by construction, and letting it overwrite the
measured value would launder an estimate into a measurement. Black
stays absent — it is panel leakage plus reflected ambient, not a
dimmer white, so no measured row stands in for it.
