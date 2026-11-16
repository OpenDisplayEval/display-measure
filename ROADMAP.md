# display-measure — Roadmap

This layer's roadmap. Cross-repo coordination and the pipeline-wide
sequencing live in the umbrella project.

## Extraction §road:extraction

The session core arrives here from the umbrella project and takes over
as the one tool that touches instruments and signal hardware. The
scaffold lands first, the hardware
path follows, and only then does the old path retire — so no window
exists in which neither repository can measure a display.

### Move the bench path §road:move-bench-path

Carry the hardware session and its gates across, and re-run a full
bench characterize from display-measure. §spec:measure-sessions,
§spec:session-gates.

**Verify:** on the bench, `display-measure characterize --processor
<host> --manifest <path>` refuses a contract violation and completes a
full protocol otherwise, producing an artifact display-report reads
without conversion.

## Session gates §road:session-gates

Every signal-contract gate refuses, whichever tool invoked the session.
What is left is the gates the walking skeleton runs without.

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
produced it. CSMF replaced the
YAML artifact, so this section retired a format as well as adding one:
`display-measure characterize` writes one `.csmf` carrying the spectra
and their per-row provenance, with everything CSMF does not model in
the provenance block its ancillary field holds.

**Downstream consumers read the old format.** ocio-display-gen loads
the YAML artifact this layer no longer writes, so the two are out of
step until ocio-display-gen reads CSMF; display-report waits on the
same file.

### Revert the colour-specio pin §road:specio-pin-revert

Return `colour-specio` to its released PyPI pin once the CSMF loader
fix merges upstream, in `pyproject.toml`. §spec:scope.

## Dark displays §road:dark-displays

A single-spectroradiometer session finishes on a display darker than
the instrument can read, recording each too-dark refusal as a bound.

### State the instrument floor §road:stated-floors

Give each spectroradiometer driver a stated nominal floor in cd/m² for
its model and measurement mode, the PR-655's and the CR-300's first, in
colour-specio's device implementations. §spec:below-floor. Cross-repo:
work lands in colour-specio; the PR-655 half depends on its driver
landing.

### Carry bounds in the measurements file §road:bounds-in-artifact

Write bound rows and their instrument, floor and floor source into the
provenance block under schema `color-wrangler/measurements/3`, and read
them back, in `display_measure/artifact.py`. §spec:below-floor,
§spec:measurements-artifact.

### Finish a session on a too-dark patch §road:dark-session

Record a too-dark refusal as a bound and carry on, gate ambient on a
bounded black, narrate each bound on the event stream, and give the
plausible-display double a floor so the doubles exercise it, in
`display_measure/session.py`, `display_measure/events.py` and
`display_measure/plausible_display.py`. §spec:below-floor,
§spec:session-gates. Depends on §road:stated-floors and
§road:bounds-in-artifact.

**Verify:** `display-measure characterize` against the doubles with a
display darker than the double's floor exits 0 and writes a schema-3
file whose black is a bound naming the instrument and its floor, and
the session log names the bound. The same run with an ambient budget
below that floor refuses at black, naming the floor. On the bench, a
dark-room `--instrument spectro --spectro pr655 --suite config`
session finishes with black as a PR-655 bound and the other four
anchors as readings.
