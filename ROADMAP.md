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

## Self-describing measurement seam §road:measurement-seam

The measure and validate layers meet at one file that states what
produced it. Upstream: `§road:measurement-seam`. CSMF replaced the
YAML artifact, so this section retired a format as well as adding one:
`display-measure characterize` writes one `.csmf` carrying the spectra
and their per-row provenance, with everything CSMF does not model in
the provenance block its ancillary field holds.

**Downstream consumers read the old format.** ocio-display-gen loads
the YAML artifact this layer no longer writes, so the two are out of
step until `§road:ocio-reads-csmf` lands there; the same file is what
`§road:read-seam-file` unblocks in display-report.

### Revert the colour-specio pin §road:specio-pin-revert

Return `colour-specio` to its released PyPI pin once the CSMF loader
fix merges upstream, in `pyproject.toml`. §spec:scope.
