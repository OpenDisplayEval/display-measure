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

### Hold the ramp floor against the instrument's §road:ramp-floor

Refuse, or route, the ramp rungs a session cannot resolve: compare each
rung's predicted step against the instrument's repeatability at that
luminance and say so before driving 72 patches.
§spec:patch-protocol. Depends on §road:pre-session-gate-events.

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
