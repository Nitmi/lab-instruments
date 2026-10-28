# Saved thermal ROI assertions

Version 0.11 adds `thermal check`, `inspect-check-report` and `junit` for already
saved native PureThermal/Lepton capture results. These commands use only local
files. They never open a camera, start a helper, import a vendor library or
acquire replacement evidence. Hashes select inputs without interactive approval.
The [capture profile and radiometric limitations](purethermal3.md) still apply.

## Define assertions

See [the example](../examples/thermal-checks.json). A spec contains exactly
`schema_version=lab-instruments.thermal-check-spec.v1` and 1..8 `checks`.
Every check has a unique letter-leading ID (1..32 letters/digits/underscore/hyphen),
an explicit `roi`, `window`, `maximum_invalid_pixels` and nonempty `expect`.

ROIs use integer `x,y,width,height` in the 160x120 image with top-left origin,
half-open bounds and no clipping. Footer telemetry is never a temperature pixel.
Check ROIs are independent of the capture plan's ROIs. A buffer window contains
`start` and `end_exclusive` (0..8), selecting 1..8 original delivered buffers;
it is not an elapsed-time interval and does not silently clamp to available data.
The explicit invalid-pixel budget is 0..(ROI area-1). Zero is a useful default
when writing a spec, but the field is required; unusable pixels are never filled.

Each `expect` key is one of:

| Key | Inclusive assertion |
| --- | --- |
| `min_c`, `max_c`, `mean_c`, `p95_c` | Every selected buffer's valid-pixel ROI statistic is within `{minimum,maximum}` |
| `mean_delta_c`, `max_delta_c` | Last minus first unique frame's ROI mean or maximum is within `{minimum,maximum}` |
| `hotspot_within` | Each buffer's native hotspot is inside this explicit `x,y,width,height` rectangle, contained in the ROI |

Temperatures are apparent surface degrees Celsius; temperature deltas are Celsius
differences. Bounds must be finite (-10..400 for temperatures, -410..410 for
deltas). There is no hidden tolerance. P95 uses nearest rank `ceil(0.95*n)` over
valid pixels. Equal maxima select the first pixel in row-major ROI order, as the
native analyzer does. This is not a claim that it is the only hotspot.

The native raw uint16/TLinear conversion requires matching GET readbacks and
frame telemetry. Capture success, identity/readback consistency, CSV regeneration
and native cleanup are checked before scoring. Unknown TLinear units, unstable
FFC/gain, corrupted source bindings or native recomputation mismatch cannot pass.
Zero/65535/out-of-profile pixels remain in the original raw bytes. If no valid
pixels exist or the explicit budget is exceeded, the check is `invalid_evidence`.

Temperature delta assertions reuse the native trend rules: at least two unique
frames, increasing frame counters and uptime, identical FFC time, effective gain,
resolution and ROI valid-pixel mask. Exact duplicate frames are deduplicated only
for the trend. A changed mask or unsupported trend invalidates a check requesting
a delta; it does not prohibit a separate absolute-temperature-only check.
`max_delta_c` compares maxima, possibly at different pixels; it is not tracking
one physical point, a heating rate, or the maximum of per-pixel changes.

## Evaluate, verify and export

Keep the original native `thermal-result.json`, `plan.json`, `native/` files,
derived CSVs and other inventoried artifacts together and unchanged. Select the
result and spec by their actual lowercase SHA-256:

```powershell
lab-instruments thermal check --spec checks.json --spec-sha256 '<hash>' --capture '<saved>/thermal-result.json' --capture-sha256 '<hash>' --output new-report.json --json
lab-instruments thermal inspect-check-report new-report.json --sha256 '<hash>' --spec checks.json --spec-sha256 '<hash>' --capture '<saved>/thermal-result.json' --capture-sha256 '<hash>' --json
lab-instruments thermal junit new-report.json --sha256 '<hash>' --spec checks.json --spec-sha256 '<hash>' --capture '<saved>/thermal-result.json' --capture-sha256 '<hash>' --output new.xml --json
```

Outputs must be new files outside the source capture directory. The check report
has independent `lab-instruments.thermal-check-report.v1` schema identity. It binds
all original input bytes and preserves native source analysis, controls, telemetry,
cleanup, error, test-double origin and the source's recorded effects. Each scored
buffer includes raw SHA-256 and ROI byte/row spans; partial observations of an
invalid check remain unscored (`assertions=[]`). Failed source captures keep their
inventoried raw evidence and error/unknown cleanup, and every check is invalid.
No passing subset of a failed capture is accepted.

Report inspection and JUnit regenerate the entire report from explicitly supplied
original inputs. Stored path labels are never opened; copying an unchanged whole
capture directory to an explicit new path is supported. Rehashing edited metrics,
qualification flags, verdicts or input inventories cannot bypass recomputation.
SHA-256 is consistency relative to selected bytes, not authentication of an
unsigned coherent set or proof that a camera ever captured it.

Check/JUnit exit 0 for a passing verdict, 3 for a complete failing/invalid report,
2 for input/consistency/output errors. Inspection exit 0 means consistency even
when the original verdict failed. JUnit maps assertion failures to `failure`,
invalid evidence to `error`, never `skipped`; preserve XML and JSON on exit 3.
Version 0.11 explicitly retains old digital0.7..0.10, waveform0.9/0.10 and load0.10
producer semantics without relabeling old reports. HIL/Trace thermal import is a
separate next step; older instrument/load schemas must not be used to disguise it.

## Qualification and cost

Sensor uptime, Media Foundation timestamps and host timestamps remain distinct.
No verified UTC/physical clock, heating rate or cross-device alignment is inferred.
Flux/emissivity/window/atmosphere parameters are recorded, not applied again.
The scene's emissivity, calibration, absolute temperature accuracy, physical
camera/DUT identity and chip junction temperature stay unverified. Synthetic
evidence proves software behavior only. No new physical qualification is created
by a numerical passing check.

The source snapshot holds at most 101 files and 16 MiB total; per artifact/result,
report and XML limits are 1 MiB, spec 64 KiB. The bounded source directory walk
accepts only the fixed top-level/native layout, at most 128 entries and no links
or reparse points. Each unique source artifact is read once, including native
CSV/analysis replay. Each of up to 8 checks analyzes at most 8 frames of 19,200
pixels with native conversion/validity masks; no per-check CSV is generated.
Source validation still regenerates its original CSVs. Full-frame masks and ROI
sorting, JSON/canonical copies and Python overhead add memory/time beyond raw
byte caps. These are not RSS, latency or worst-case guarantees. Local measured
warm-runtime cost belongs in acceptance evidence with workload and conditions.
