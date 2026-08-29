# display-measure

**Gated instrument sessions for display characterization.**

display-measure drives known code values at a display through its real
signal chain, reads the emitted light with a spectroradiometer or
colorimeter, and writes an immutable measurement file. It is the one
tool in the pipeline that touches instruments and signal hardware.

## Why the gates

A measurement is only worth the conditions it was taken under, and
those conditions are easy to get wrong in ways nothing notices. A
bench session once drove a full 72-patch protocol against a processor
left at 66 nits after an unrelated test: peak measured 90 cd/m² on a
wall that does 1900, and the artifact recorded a contract nobody had
read. Twenty minutes and a rig, lost to a state one HTTP request would
have caught.

So a session refuses before it measures. It reads the processor
read-only and compares every declared field; it checks that nothing
but the declared luminance knob scales the output; it confirms the
processor sees the link the session drives; it checks the wall
actually does what the processor claims; and where two instruments are
paired, it checks the correction between them is fit to extrapolate
before spending the protocol on it.

What no API reports, the operator attests and the artifact records —
named as an attestation, never as a reading.

The gates and what each refuses on are specified in §spec:session-gates;
the session stages they sit in, in §spec:sessions.

## Where this fits

| Layer | Repo | Role |
| --- | --- | --- |
| Orchestrate/present | [color-wrangler](https://github.com/Fuse-Technical-Group/color-wrangler) | Show-side orchestration, operator UI, umbrella governance |
| Measure | display-measure (here) | Gated instrument sessions → measurement files |
| Generate | [ocio-display-gen](https://github.com/Fuse-Technical-Group/ocio-display-gen) | Manifest + measurements → OCIO config + predictions |
| Validate | [display-report](https://github.com/OpenDisplayEval/display-report) | Independent analysis and reports from a measurement file |

Every seam is a file, and every file records the sha256 of its inputs.
This layer owns the measurement file and nothing downstream of it
(§spec:scope, §spec:measurements-artifact).

System requirements, architecture and the cross-component roadmap live
in color-wrangler, which this repository references as upstream
context.

## License

BSD 3-Clause.
