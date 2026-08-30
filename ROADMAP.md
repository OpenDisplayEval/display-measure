# display-measure — Roadmap

This layer's roadmap. Cross-repo coordination and the pipeline-wide
sequencing live in
[color-wrangler](https://github.com/Fuse-Technical-Group/color-wrangler);
workstreams there are addressed by slug in backticks and resolve in
that repository's ROADMAP.md.

## Extraction §road:extraction

The session core arrives here from color-wrangler and takes over as
the one tool that touches instruments and signal hardware
(`§spec:session-ownership`). The scaffold lands first, the hardware
path follows, and only then does the old path retire — so no window
exists in which neither repository can measure a display.

### Move the bench path §road:move-bench-path

Carry the hardware session and its gates across, and re-run a full
bench characterize from display-measure. §spec:measure-sessions,
§spec:session-gates. Upstream: `§road:move-bench-path`.

**Verify:** on the bench, `display-measure characterize --processor
<host> --manifest <path>` refuses a contract violation and completes a
full protocol otherwise, producing an artifact display-report reads
without conversion.

## Session gates §road:session-gates

Every gate `§spec:signal-contract` names refuses, whichever tool
invoked the session. What is left is the gates the walking skeleton
runs without.

### Standalone snapshot command §road:processor-state-snapshot

Add `display-measure snapshot`: the contract read and diff without a
session, for checking a rig before booking it. §spec:session-gates.

### Read the operating mode off the wire §road:operating-mode-read

Replace the operator attestation of the panel-resident operating mode
with a reading, where the processor exposes one. §spec:session-gates.
The attestation stays until a reading is proven equivalent.

### Carry the pre-session gates on the event stream §road:pre-session-gate-events

Open the session's event stream early enough that the hardware path's
processor audit reports its gates on it. Today the wire-format and
output-scaling gates refuse before the stream exists, so a consumer
sees a refusal with no gate behind it. §spec:session-events,
§spec:session-gates.

**Verify:** a session against a rig whose intensity, gamma, processing
features, wire format or ambient floor contradicts the manifest exits
non-zero naming the field, before any patch is driven.

## Wire encoding as a declared, recorded parameter §road:wire-encoding

A patch is what the processor receives; the encoding between that and
the device is declared by the session, held against the processor's
input metadata, and recorded in the artifact. Umbrella `§spec:architecture`
(color-wrangler) and §spec:measure-sessions.

### Declare the encoding per session §road:declare-wire-encoding

Replace the `DECLARED_WIRE_FORMAT` constant with a session parameter —
bit depth, sampling, range, colour matrix, layout — defaulting to the
bench's 12-bit RGB identity encoding (`display_measure/session.py`,
`display_measure/protocol.py`). The session drives `Patch.rgb` through
the declared encoding via the shared packer and never implements one;
`audit_wire_format` holds the processor to what was declared.
Blocked — the shared packer with a parameterised matrix lands in
`pypixelpack` first (`§road:hoist-packing` in pydecklink). Unblocked
when that ships.

### Record the encoding in the artifact §road:record-wire-encoding

Write the declared encoding into the artifact and bump the schema to
`color-wrangler/measurements/2` (`display_measure/artifact.py`), so two
artifacts of one display over different links are legibly different
measurements and a loader can refuse to compare them silently.
Depends on §road:declare-wire-encoding.

Narrow-range encodings cannot represent every RGB code: 10-bit narrow
gives luma 64–940, and the round trip through the processor's inverse
does not land on every 12-bit RGB value. The artifact therefore records
the code actually representable on the wire, not only the code the
protocol intended.

**Verify:** a session over the bench HDMI link writes `measurements/2`
declaring 12-bit RGB identity and is byte-identical to today's artifact
but for the schema and encoding block; a session declaring v210 is
refused by the wire-format gate unless the processor reports 10-bit
YCbCr; and a reader given one artifact of each refuses to treat them as
the same measurement.

## Self-describing measurement seam §road:measurement-seam

The measure and validate layers meet at one file that states what
produced it. Upstream: `§road:measurement-seam`.

### Retain spectra with per-row provenance §road:spectral-provenance

Keep the spectral distribution behind each reading and record whether
it was measured, reconstructed or absent.
§spec:measurements-artifact.

### Reconstruct shadow spectra §road:spectral-reconstruction

Give colorimeter-routed rows a spectrum scaled from the bright-regime
measurement of the same stimulus, naming the luminance span it was
derived across. §spec:measurements-artifact. Depends on
§road:spectral-provenance.

### Emit the seam file §road:emit-csmf

Write CSMF carrying the spectra, protocol name, declared signal
contract, attested panel state and input hashes.
§spec:measurements-artifact. Depends on §road:spectral-provenance.

### Report-grade protocol tier §road:report-grade-protocol

Add the named protocol tier carrying the colour cube, random samples
and black/white repeats a distribution needs. §spec:patch-protocol.

**Verify:** a file written by `display-measure characterize` names the
protocol and transfer function it was measured under, and its
reconstructed rows are named so an analysis needing a measured
spectrum can exclude them.
