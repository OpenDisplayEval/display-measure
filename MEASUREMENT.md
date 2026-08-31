# Measurement Protocol

Measurement decomposes into named, independently versioned **blocks**
(§spec:patch-protocol). A block names one thing worth measuring and says
what reads it. A session drives a composition of blocks; the artifact
records which blocks at which versions; a consumer requires the blocks
it reads.

| Block | Patches | What reads it |
| --- | --- | --- |
| `anchors/1` | 5 | the OCIO config — every value it takes from an artifact |
| `response/1` | 64 | verifies each channel against the declared transfer function |
| `additivity/1` | 3 | verifies that the channels add |
| `tracking/1` | 88 | the report's grey-tracking and EOTF-accuracy plots |
| `noise-floor/1` | 19 | the report's patch filter, which divides by their spread |
| `white-repeat/1` | 4 | the report's outlier-robust primary-matrix fit |
| `volume-mesh/1` | 512 | the report's chromaticity clusters |
| `volume-scatter/1` | 100 | volume coverage off the mesh lattice |

**Why blocks and not one versioned patch list.** A bundle mixes the
measurements a model takes as *input* with the measurements that *test*
the model, and the ones a report needs with the ones a config needs.
Every addition then bumps the bundle, which says nothing about what
changed, and no consumer can state what it requires beyond a version
number that moves for reasons unrelated to it. Characterization will
grow — more measurements to sharpen the OCIO profile — and in a bundle
those arrive indistinguishable from the verification patches already
buried there.

## Suites

A suite is a named composition, a label rather than a thing. What an
artifact records, and what a consumer matches on, is the block list.

| Suite | Blocks | Patches | Session |
| --- | --- | --- | --- |
| `config` | `anchors` | 5 | minutes |
| `verify` | `anchors`, `response`, `additivity` | 72 | ~10 min |
| `report` | all eight | 795 | ~110 min |

`--suite` selects a preset; `--blocks anchors,noise-floor` composes
directly; `--list-blocks` prints the blocks and what reads each.

Each suite is a superset of the one above it, so measuring at report
grade yields config- and verify-grade artifacts from the same session.

**`verify` is what `color-wrangler/characterize/3` drove** — the same 72
patches at the same codes, under the same conditions. That protocol was
a characterization step with a verification step buried in it. The
decomposition names the two apart without changing what either
measures.

## Naming and versions

Each block versions with the tool: a change to its patches, their
spacing, or the conditions it requires bumps that block's number, and
leaves every other block's meaning untouched. Nothing the report needs
can bump what the config reads.

Consumers match on block ids. One compatibility affordance remains: the
`verify` suite carries `legacy_name: color-wrangler/characterize/3` in
the artifact's `protocol.name`, because ocio-display-gen matches that
string today and every artifact already promoted carries it.
`§road:ocio-reads-csmf` retires the field. No new suite claims one.

The driven order is per-session — the seed and any instrument pins vary
within one composition — and the artifact records it in full. Applying
the shuffle rule is not changing it; flattening the shuffle, or
reordering what leads, is.

## What an artifact carries

An artifact carries what its suite measured and no more. A `config`
session has no `per_channel_response` and no `additivity` — not empty
ones, absent ones, because an empty ramp reports an unmeasured thing as
a measured one.

## Codes

Device-referred 12-bit RGB code values (full drive 4095), no OCIO in the
loop, since characterization precedes any config. The codes are the
protocol's whatever link carries them; the wire encoding is a session
parameter (see Session parameters), and a link that cannot carry every
code records which it did.

The codes are fixed rather than derived from the display in front of
them. display-report's retired measure path bounded its ramps by
`--max-luminance` and `--min-above-black`, so two panels measured
different codes and their reports compared different experiments. What
an analysis needs is the count and the spread, not the particular codes.

**The analysis constrains the block, so the block is specified against
the analysis.** `noise-floor` is twenty readings because the report
filters patches against their *spread*: with one reading there is no
spread, the divisor lands at -1.6e-17, and every patch in the file is
rejected as indistinguishable from black. `volume-mesh` exists because
the report clusters measured error through the volume, and a volume
nothing measured clusters into empty and duplicate groups. Neither
failure shows on the rendered page — the report draws, and what it
draws is wrong.

## Ramp spacing

`response` carries the half-octave ladder; `tracking` carries the even
codes between its rungs. A suite composing both drives their union.

The half-octave ladder
doubles every two steps from 16 to 3072, by alternating factors 3/2 and
4/3:

```text
16, 24, 32, 48, 64, 96, 128, 192, 256, 384, 512, 768,
1024, 1536, 2048, 3072
```

Half-octave spacing in code space gives near-constant relative
luminance steps through a power-law decode — dense where shadow
response needs it (`§spec:signal-contract`), and it is what mapped this
bench panel's toe.

The parity ramp is 25 evenly spaced codes from 16 to full drive, the
spacing display-report's grey-tracking and EOTF-accuracy plots read as
their independent variable. Even spacing undersamples the shadows,
which is why `tracking` adds to `response` rather than replacing it.

Where a parity code lands within 5 codes of a ladder rung it is
dropped. The narrowest link a session may declare is 10-bit narrow
range — 877 levels across the 4096-code grid, one every 4.675 codes —
so a closer pair is the same patch on that link, measured twice, and
read as a ramp inversion the moment either reading carries noise. The
ladder wins those ties: its rungs are exact on the power-of-two floor,
and dropping one would leave a hole in the shadow sampling the parity
ramp is too coarse to fill.

The union is 38 codes:

```text
16, 24, 32, 48, 64, 96, 128, 186, 192, 256, 356, 384, 512, 526,
696, 768, 866, 1024, 1036, 1206, 1376, 1536, 1546, 1716, 1886,
2048, 2056, 2225, 2395, 2565, 2735, 2905, 3072, 3245, 3415, 3585,
3755, 3925
```

Code 0 and full drive belong to the anchors; the artifact folds each
full-drive anchor reading into its ramp as the code-4095 row, so the
ramps carry the anchors' absolute XYZ (the colorimetry section reduces
them to xy).

## Presentation

Patches are driven in shuffled order, decorrelating panel thermal
drift from signal-level response. Black is pinned first: the ambient
gate consumes the opening reading. The rest sort by
`sha256("{seed}:{name}")` — deterministic for a given seed,
independent of any RNG stream's cross-version stability. The artifact
records the driven order as `protocol.presentation_order`, the
unshuffle key; artifact response rows stay protocol-ordered
(ascending code).

A hybrid session leads with its three derivation rungs, and black
follows them: nothing can route until the correction exists, black
included. A single-instrument session declares no rungs and so still
opens on black, driving the order protocol 1 drove — the rule changed,
not every realization of it.

**This is what bumped protocol 1 to 2.** Reordering what leads is a
change to the presentation rule, and the rule is versioned even though
the per-session order is not. Protocol 1 read black first because
nothing needed to precede it; protocol 2 reads it after any pins the
instrument declares, so the colorimeter can take the session's darkest
and most expensive patch (`§road:instrument-floors`). Both flavors of
protocol 2 carry one name, so hybrid and spectroradiometer-only
artifacts stay comparable.

The rungs are half drive, not full: a colorimeter's ceiling sits far
below a show display's peak (the bench CR-120 saturates around
400–500 cd/m² against a 1900 cd/m² display), and an anchor neither
instrument can read is no anchor. An LED primary's spectrum barely
moves with drive level, which is what filter mismatch responds to, so
a dimmer rung samples the same emitter.

**This is what bumped protocol 2 to 3.** White now follows black ahead
of the shuffle, so both of the session's gating readings arrive before
the shuffle rather than wherever it scattered them — white landed at
patch 69 of 72 under protocol 2, which is no gate at all. White is
pinned *after* black, never before: black is the session's most
delicate reading, and driving the protocol's brightest patch into it
would leave the panel and the instrument recovering from it. The order
leaves black's conditions exactly as protocol 2 read them.

A suite composing `noise-floor` reads black twenty times: once ahead of the shuffle, where
the ambient gate consumes it, and nineteen more scattered through the
session by the shuffle rule. Scattered is the point — a noise floor
measured from twenty consecutive readings describes one minute of the
session, not the session.

The closing black read arrives with the session gates
(§road:session-gates) and bumps a protocol again.

## Session parameters

- Settle: configurable per session (`--settle`), default 0.5 s;
  recorded conditions, not protocol constants.
- Conditioning and read attempts belong to the **block**, not the
  session, because they are part of what makes two artifacts
  comparable. `--conditioning`, `--warmup` and `--read-attempts`
  override one for a bench investigation; a comparable run does not.

  The report blocks — `tracking`, `noise-floor`, `white-repeat`,
  `volume-mesh`, `volume-scatter` — require random colour before every
  patch (5 s) and before the first reading (10 minutes), and ten
  attempts at each reading. A suite takes the strictest across what it
  composes, so composing one of these brings its conditions rather than
  leaving them to the caller. An LED panel measured on a run of solid patches is not the
  panel an operator drives: junction temperature settles somewhere a
  moving picture never takes it, and the response measured there is the
  response of a thermal state the display does not otherwise occupy.
  These are what display-report's retired measure path drove, which is
  the state its numbers describe.

  `anchors`, `response` and `additivity` require none, so `config` and
  `verify` condition for zero and read once — how protocol 3 always
  drove, and how every artifact promoted under that name was measured.

  Conditioning colours come from the session seed, not an RNG: nothing
  records them, but they reach the device, and one seed drives one
  sequence of frames. The doubles condition for zero whatever the
  protocol says — a mock has no junction temperature.

  A retry is warranted because the instrument's failures at the bottom
  of a panel are transient: a timed-out integration or a truncated
  serial reply says the link stumbled, not that the patch is
  unreadable. A gate refusal is a verdict and is not retried. A patch
  no attempt can read fails the session: the artifact is
  all-or-nothing, and a session with a hole in it is not a shorter
  session but one whose ramps have a missing rung nothing downstream
  can see.
- Timing: the artifact records elapsed seconds per patch
  (`protocol.patch_seconds`, presentation order) — workflow telemetry
  for driving session time down (`§road:session-throughput`). Durations
  come from the session clock, so fixed-clock reproduction runs render
  zeros.
- Panel state: the manifest attests `operating_mode` and
  `selected_calibration`, and the session confirms them interactively
  (`--assume-attested` skips the prompt for scripted runs). Neither is
  readable from the processor and both move the measurement, so the
  operator is the only instrument that can report them. The artifact
  records them; nothing compares them (`§spec:signal-contract`).
- Level: the white anchor's reading is checked against the declared
  intensity on sane-default bounds, and the session refuses outside them.
  The check names no cause, which is the point: it catches an operating
  mode, a moved instrument, thermal state or an aperture off the panel
  edge through the one number they all move.
- Contract: `--processor` names the Tessera host and `--manifest` the
  show manifest whose `signal_contract` declares the lockdown. The
  session reads the processor read-only and refuses ahead of instrument
  discovery on any divergence — EOTF, intensity, each processing
  feature, output scaling, and input metadata against the declared wire
  format (§road:session-gates). Brightness is the one luminance knob the
  contract declares, so the gains (`intensity` and per-channel) shall sit
  at 100 and any brightness limit shall not bind — an operator who wants
  half the light sets brightness, not a gain the manifest cannot see. Processing is declared per feature: static
  linearization on is expected and measured, frame-adaptive processing
  is refused (`§spec:signal-contract`). Without `--manifest` it audits
  against the built-in recommended contract, which is unlikely to match
  a given rig; without `--processor` a hardware session refuses
  outright. The doubles declare compliance and need neither.
- Wire encoding: `--wire` declares the link the patches ride to the
  device (`rgb12`, the bench link and the default; `v210`). The gate
  holds the processor's input metadata to the declaration
  (§spec:session-gates), and the artifact records the declaration and
  the codes the wire carried per patch (§spec:measurements-artifact).
- Instrument: `--instrument` selects it. CR-300 class for
  characterization-grade sessions; a bare colorimeter is drift-check
  grade only. A hybrid session keeps characterization grade while
  spending the colorimeter's speed on the dark patches, correcting it
  in session against the CR-300 (§spec:measure-sessions).
- Routing: `--threshold` sets the luminance (cd/m²) above which a
  hybrid session takes the spectroradiometer's reading, default 10.
  Black falls under it, and belongs there: blocked-aperture reads put
  the bench CR-300's own zero at 0.0149 cd/m² against a display black
  nearer 0.0014, so the reference instrument reads mostly itself down
  there while the colorimeter is both truer and 6x faster
  (`§road:instrument-floors`).
  Recorded per session in `instrument_routing`, alongside the derived
  matrix and the instrument behind every row.
- Readings: one triggered measurement per patch, absolute XYZ in
  cd/m². A hybrid session also reads the colorimeter on every patch:
  the threshold is stated in measured luminance, and the fast
  instrument is the one that can measure it cheaply.

## Artifact schema

`color-wrangler/measurements/2` (§spec:measurements-artifact). Schema 2
adds the `wire_encoding` block — the declaration, and `wire_codes`, the
codes the wire carried per driven patch — and changes nothing else, so
a schema-1 artifact reads as schema 2 over `rgb12`. A loader given
artifacts whose blocks differ shall refuse to compare them as one
measurement.
