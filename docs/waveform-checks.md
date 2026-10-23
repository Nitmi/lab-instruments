# Saved voltage waveform assertions

Version 0.9.0 adds `waveform check`, `waveform inspect-check-report` and `waveform junit`.
They read saved native evidence only: no SDK, socket, discovery, subprocess or new
acquisition. Toolkit's currently published instrument pin remains on lab 0.8.0;
select a separately inspected 0.9.0 environment for these commands. Existing HIL/Trace
logic-report import does not accept this new waveform report format.

## Contract and input

```json
{
  "schema_version": "lab-instruments.waveform-check-spec.v1",
  "source_kind": "ad3",
  "checks": [{
    "id": "rail_3v3",
    "channel": 1,
    "window": {"start": 0, "end_exclusive": 4096},
    "expect": {
      "minimum_v": {"minimum": 3.1, "maximum": 3.5},
      "maximum_v": {"minimum": 3.1, "maximum": 3.5},
      "mean_v": {"minimum": 3.25, "maximum": 3.35},
      "peak_to_peak_v": {"minimum": 0, "maximum": 0.1},
      "ac_rms_v": {"minimum": 0, "maximum": 0.025}
    }
  }]
}
```

Set `source_kind` to `ad3` for an AD3 `capture.json` or `sds804x` for an SDS
`scope-result.json` and its unchanged sibling evidence. The type is explicit;
there is no automatic format, channel or device fallback. Up to eight unique IDs,
channels 1..2 (AD3) or 1..4 (SDS), and integer windows within 0..16,384 samples.
Each nonempty `expect` selects only supported metrics with inclusive minimum and
maximum; values are volts. No hidden tolerance, filtering, interpolation,
resampling, bandwidth limit, outlier rejection or automatic window clipping.

AD3 verifies the embedded raw channel-major float64 LE payload and native source
boundary, then uses actual reported rate/count and range/offset readbacks. The
plan/SDK path labels inside the capture are never opened. SDS verifies the native
plan, transcript, partial wire bytes, exact command replay, descriptor, WORD bytes,
derived CSV and cleanup before calculating. It uses the same signed left-aligned,
probe-aware conversion as acquisition; CSV/summary alone cannot pass. The new
reader snapshots each unique source file once for hash checks and replay.
Successful source capture and confirmed source cleanup are required. V1 requires
DC coupling; AC/GND channels produce `invalid_evidence` rather than a rail verdict.

Every check reports its original channel, exact window, raw payload byte span,
conversion inputs and relative first/last sample times. For AD3 spans refer to the
decoded shared float64 payload; for SDS to that channel's original WORD file.
Both use zero-based bytes and exclusive end. A single-sample window is allowed;
its AC RMS is zero and does not establish that the signal was stable over time.

## Metric definitions

For the selected N voltage samples x, `minimum_v` and `maximum_v` are extrema;
`mean_v = sum(x)/N`; `peak_to_peak_v = maximum - minimum`;
`rms_v = sqrt(sum(x*x)/N)` includes DC;
`ac_rms_v = sqrt(sum((x-mean_v)^2)/N)` removes only the window mean and uses
population normalization N, not N-1. It measures variation within these samples,
not a bandwidth-qualified ripple/noise measurement. Calculations use Python binary
float and compensated summation. Choose bounds that account for quantization and
measurement uncertainty; the software does not invent an instrument accuracy.

## CLI and result handling

```powershell
lab-instruments waveform check --spec '<spec.json>' --spec-sha256 '<sha256>' `
  --capture '<capture.json-or-scope-result.json>' --capture-sha256 '<sha256>' `
  --output '<new-report.json>' --json
lab-instruments waveform inspect-check-report '<report.json>' --sha256 '<report-sha256>' `
  --spec '<spec.json>' --spec-sha256 '<sha256>' --capture '<capture>' `
  --capture-sha256 '<sha256>' --json
lab-instruments waveform junit '<report.json>' --sha256 '<report-sha256>' `
  --spec '<spec.json>' --spec-sha256 '<sha256>' --capture '<capture>' `
  --capture-sha256 '<sha256>' --output '<new.xml>' --json
```

`check`: 0 all assertions passed; 3 complete saved report with failed assertion or
`invalid_evidence` (missing channel, unavailable window, unsupported coupling);
2 invalid spec/hash/source, corrupt/incomplete acquisition or output error. It
never treats a source-integrity failure as a completed measurement. No report is
written on those errors. Reports are exclusive new files; stdout preserves the
report even when no output file was requested. One failed check fails the whole
report; passing metrics in another check do not replace it.

`inspect-check-report` requires the explicit original spec/capture and hashes,
recomputes the entire report, and compares canonical JSON including metrics,
assertions, spans, input bindings, cleanup and every false claim. Rehashing an edited
report is insufficient. Verification exit 0 means the report is consistent, even
if its test verdict failed. Inspect reads path labels as labels only: relocate
original byte-identical inputs explicitly, retaining SDS's fixed sibling names.
Waveform producer 0.9.0 is the only supported producer; no optimistic compatibility.

`junit` also performs full recomputation. Exit 0 is all passed, 3 exports actual
failure/error, 2 is input/verification/output error. Each check becomes one case;
`invalid_evidence` is an error, never skipped. Collect XML on exit 3 and retain JSON
for complete conversion evidence. No fabricated duration/timing measurements.

## Qualifications and cost

Input hashes bind bytes, not authenticated capture or calibration. Historical
origin/identity/cleanup/freshness fields remain recorded claims. Reports preserve
test-double origin and false device, DUT, timing, UTC, alignment, clipping-absence,
analog-bandwidth, calibration and authentication flags. A successful assertion
cannot establish unclipped acquisition, sufficient sampling, analog bandwidth,
probe accuracy, between-sample behavior or original waveform freshness. SDS's
already-stopped acquisition can have unknown age. Do not align it with host
serial/RTT or infer board identity or causality.

Spec <=64 KiB, result/report/individual source file <=1 MiB. One evaluation's
source snapshot is bounded to 8 MiB and 1,615 unique fixed native files, with the
existing 800 transaction SDS limit. Report verification adds a <=1 MiB report
read. No input recorded SDK/plan paths are followed; native SDS sibling names
are validated. Path checks reject links/reparse/network paths.

Cost scales with saved source bytes, native SDS replay and at most 8 x 16,384
selected samples. Snapshots retain bounded bytes to avoid rereads; parsing,
decoded Python arrays and CSV replay allocate additional memory. These are not
RSS limits. All supported metrics are computed once per selected window; querying
an already saved report does not skip recomputation. Local warm timings and
allocation measurements belong in acceptance records, not universal latency claims.
