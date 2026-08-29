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

## Scope §spec:scope

*Status: in progress*

One repository opens a serial port or a DeckLink, and it is this one
(`§spec:session-ownership`). It drives patches through the show chain,
reads the instruments, holds the session gates, and emits the
measurement file. color-wrangler orchestrates and presents;
ocio-display-gen generates; display-report reports. None of them
imports a device driver.

The boundary is code ownership, not process isolation. A caller runs a
session in its own process, so the process driving the wall may also
serve an operator UI. What does not happen is a second implementation
of device access — a gate cannot be bypassed by reaching for the
device directly, because only one repository knows how.

Not owned here: OCIO semantics and config generation (ocio-display-gen),
ΔE analysis and reports (display-report), the show manifest's schema
and the promotion decision (color-wrangler). Device layers are
referenced, never re-specified: bmd-signal-gen owns patch rendering and
wire-format correctness, pydecklink owns device access, colour-specio
owns instrument communication. colour-specio is pinned to a PyPI
release and never forked; its `measure()` surface is the driver
contract, and upstream is the venue for fixes.

## Measurement sessions §spec:sessions

*Status: in progress*

A session is one command run with the wall powered and the instrument
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

**Instruments have doubles.** The default double is a deterministic
plausible wall — an additive per-channel model that synthesizes the
reading for the frame being driven — so the hardware-free
characterize loop produces physically sensible numbers rather than
noise. A colorimeter double reading the same wall through a fixed
filter mismatch pairs with it, so the disciplined-hybrid derivation is
checkable without hardware. colour-specio's random virtual
spectrometer remains available for plumbing-only tests: its
physics-free readings exercise the seams, not the numbers.

**Disciplined hybrid.** Where a colorimeter alone cannot hold
characterization grade, a 3×3 derived in session from paired readings
of the full-drive R/G/B anchors (four-color matrix, Ohno & Hardis
1997; ASTM E1455) corrects the colorimeter onto the spectroradiometer.
A three-primary additive display makes that one matrix valid for every
mixture the wall emits. Above a luminance threshold the
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

## Patch protocol §spec:patch-protocol

*Status: complete*

The characterize protocol is fixed, versioned and device-referred:
anchors, shadow-dense per-channel ramps, a gray tracking ramp and the
additivity triad, in raw code values with no OCIO in the loop, since
characterization precedes any config. MEASUREMENT.md is the
human-readable definition of record, and `PROTOCOL_NAME` versions the
two together. A change to the patch set, its spacing, or the
presentation rule bumps the trailing number.

Presentation is shuffled to decorrelate panel thermal drift from
signal-level response. The shuffle is a sha256 sort keyed by the
session seed — deterministic across Python and library versions — so
the artifact's byte-determinism never depends on an RNG stream's
stability. Black is pinned ahead of the shuffle because the ambient
gate consumes the opening black reading; a session may pin more
patches without touching the patch set or its codes. The driven order
varies per session and the artifact records it in full.

## Measurements artifact §spec:measurements-artifact

*Status: in progress*

The session's output is one file: machine-written, immutable, never
hand-edited (`§spec:artifact-chain`). It carries the measured
primaries and white point, black level and peak luminance in absolute
cd/m², per-channel response, ambient floor, instrument identity and
firmware, the processor-state snapshot, the protocol name and driven
order, and timestamps. Where two instruments were paired it also
records the correction matrix, the routing threshold, and the
instrument behind every row.

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

**Two strings are wire identifiers, not package names.** The artifact
schema is `color-wrangler/measurements/1` and the characterize
protocol is `color-wrangler/characterize/3`. Both outlived the
repository they were named in: the session core moved here and the
strings did not follow it. Every promoted artifact carries them, and
downstream loaders dispatch on them, so renaming either breaks the
provenance of every artifact already promoted. They change only when
the format they name changes.
