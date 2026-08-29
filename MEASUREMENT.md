# Measurement Protocol

`color-wrangler/characterize/3` — the patch protocol a characterize
session drives (§spec:patch-protocol). The name is a wire identifier
that outlived the repository it was named in; every promoted artifact
records it. The protocol versions with the tool:
a change to the patch set, its spacing, or the presentation rule bumps
the trailing number, and the artifact records the protocol name it was
measured with. The driven order itself is per-session — the seed and
any instrument pins vary within one protocol — and the artifact records
it in full. Applying the rule is not changing it; flattening the
shuffle, or reordering what leads, is.

## Patch inventory

72 patches, device-referred raw code values at the declared wire
format (12-bit RGB, full drive 4095), no OCIO in the loop:

| Group | Patches | Fills |
| --- | --- | --- |
| Anchors | black; full-drive red, green, blue, white | primaries, white point, black level, peak luminance, ambient floor |
| Per-channel ramps | 16 codes each on R, G, B alone | `per_channel_response` |
| Gray ramp | the same 16 codes, r=g=b | `gray_response` |
| Additivity triad | full-drive yellow, cyan, magenta | `additivity` |

## Ramp spacing

Ramp codes double every two steps from 16 to 3072, by alternating
factors 3/2 and 4/3:

```text
16, 24, 32, 48, 64, 96, 128, 192, 256, 384, 512, 768,
1024, 1536, 2048, 3072
```

Half-octave spacing in code space gives near-constant relative
luminance steps through a power-law decode — dense where shadow
response needs it (`§spec:signal-contract`). Code 0 and full drive
belong to the anchors; the artifact folds each full-drive anchor
reading into its ramp as the code-4095 row, so the ramps carry the
anchors' absolute XYZ (the colorimetry section reduces them to xy).

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
below a show wall's peak (the bench CR-120 saturates around
400–500 cd/m² against a 1900 cd/m² wall), and an anchor neither
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

Protocol 3 reads black once, ahead of the shuffle. The closing black
read arrives with the session gates (§road:session-gates) and bumps
the protocol again.

## Session parameters

- Settle: configurable per session (`--settle`), default 0.5 s;
  recorded conditions, not protocol constants.
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
- Instrument: `--instrument` selects it. CR-300 class for
  characterization-grade sessions; a bare colorimeter is drift-check
  grade only. A hybrid session keeps characterization grade while
  spending the colorimeter's speed on the dark patches, correcting it
  in session against the CR-300 (§spec:sessions).
- Routing: `--threshold` sets the luminance (cd/m²) above which a
  hybrid session takes the spectroradiometer's reading, default 10.
  Black falls under it, and belongs there: blocked-aperture reads put
  the bench CR-300's own zero at 0.0149 cd/m² against a wall black
  nearer 0.0014, so the reference instrument reads mostly itself down
  there while the colorimeter is both truer and 6x faster
  (`§road:instrument-floors`).
  Recorded per session in `instrument_routing`, alongside the derived
  matrix and the instrument behind every row.
- Readings: one triggered measurement per patch, absolute XYZ in
  cd/m². A hybrid session also reads the colorimeter on every patch:
  the threshold is stated in measured luminance, and the fast
  instrument is the one that can measure it cheaply.
