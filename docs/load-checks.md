# Saved SDL1020X-E measurement assertions

Local 0.10.0 adds `load check`, `load inspect-check-report` and `load junit`.
They use saved native `load-result.json` plus its fixed sibling `plan.json` and
`transcript.jsonl`. No endpoint, SDK, lease or executable is opened. Existing
live `load plan/run/inspect` commands and their hardware scope remain separate.
The published Toolkit 0.33 pin still selects lab 0.9; use a separately selected
0.10 candidate environment for this development feature.

## Spec

```json
{
  "schema_version": "lab-instruments.load-check-spec.v1",
  "checks": [{
    "id": "rail_5v_loaded",
    "phase": "loaded",
    "window": {"start": 0, "end_exclusive": 5},
    "expect": {
      "minimum_voltage_v": {"minimum": 4.8, "maximum": 5.2},
      "maximum_current_a": {"minimum": 0.09, "maximum": 0.11},
      "mean_power_w": {"minimum": 0.4, "maximum": 0.6},
      "input_on": true
    }
  }]
}
```

There are 1..8 unique check IDs. Select `before_load` (the single CC preflight
sample), `loaded` (CC measurements) or `observe` explicitly. The zero-based
half-open window is relative to that phase and limited to 0..50 measurements;
the native baseline retains its original index -1. Windows never clamp or
fall back to another phase. Identify/off results have no measurement phase.

For each of `voltage_v`, `current_a` and `power_w`, select any of the prefixed
metrics `minimum_`, `maximum_` and `mean_` with finite nonnegative inclusive
bounds. Each valid window reports all nine metrics. `input_on` is an optional
exact boolean expectation checked against every selected sample's reported
input state. At least one expectation is required; unknown metrics, energy,
timing thresholds and arbitrary commands are rejected.

Voltage, current and power are **three sequential instrument queries**. Power
uses the returned `MEAS:POW:DC?` readings, never a product of separately read V/I.
Mean is the arithmetic population mean of the selected readings, not a
time-weighted mean. No energy integration, interpolation, filtering, settled
state detection or new tolerance is implied. A one-sample window can check a
reading but cannot establish stability between observations.

## CLI

```powershell
python -I -B -m lab_instruments load check --spec checks.json --spec-sha256 <hash> `
  --capture run/load-result.json --capture-sha256 <hash> --output new-report.json --json
python -I -B -m lab_instruments load inspect-check-report new-report.json --sha256 <hash> `
  --spec checks.json --spec-sha256 <hash> --capture run/load-result.json --capture-sha256 <hash> --json
python -I -B -m lab_instruments load junit new-report.json --sha256 <hash> `
  --spec checks.json --spec-sha256 <hash> --capture run/load-result.json --capture-sha256 <hash> `
  --output new.xml --json
```

Check returns 0 for all passed checks, 3 for a complete report containing failed
assertions or invalid evidence, and 2 for spec/hash/source/output errors. Input
integrity errors write no measurement report. JUnit fully recomputes first,
returns 0 or 3 for the actual verdict and uses `<failure>` for assertion failure,
`<error>` for invalid evidence, never `<skipped>`. Collect XML on exit 3. Report
inspection returns 0 for consistent evidence even if the stored test failed;
read its full verdict. Existing outputs fail before reading inputs.

Hashes are byte bindings, not per-command confirmation prompts. Ordinary saved
file analysis uses the existing development-task consent. No command here can
energize or turn off a load, and a report is not live-operation authorization.

## Native evidence and recomputation

The reader snapshots the explicitly selected result and the two fixed sibling
files once, reusing the existing native saved-evidence inspector. It checks the
plan's canonical identity and transcript hash/size. For a successful native run,
it then validates journal framing and replays the exact finite command sequence:
IDN, applicable safe preconditions, configuration/readbacks, measurement order,
input state, OFF/completion/readback where owned, and socket close. Replay uses
in-memory responses with the native setting/envelope checks; no SCPI is sent.
Recorded phase/index, values and query windows must match. Missing writes,
unexpected commands, altered readings or self-consistently rehashed summary
edits cannot become a measurement pass.

Each selected sample retains the three original query spans in transcript JSONL
bytes, their hashes, TX hashes and raw RX bytes. These are evidence locations,
not physical sample timestamps. Host UTC and monotonic fields remain recorded
query metadata; no instrument ADC clock or synchronized DUT timeline is verified.

An intact failed native result can still produce a complete check report, but
**every check is invalid_evidence** with `source_failed`. Preserve its error,
recorded cleanup/identity/effects and unscored partial measurements. Failed
source records get bounded framing/byte checks, not successful-command replay;
`source_success_replayed=false` and `failed_source_measurements_evaluated=false`
stay explicit. Do not use plausible partial readings to override a timeout,
unknown OFF state or native failure. Malformed/truncated or hash-damaged input
instead fails with exit 2.

For a successful source, an unavailable phase or insufficient window is an
invalid-evidence error. Successful `observe` can intentionally leave an existing
load ON; its recorded cleanup says `not_established` for instrument state. A
historical OFF readback is not proof of the load's current state. The new report
and JUnit always keep hardware access, outputs commanded, physical/DUT identity,
calibration, simultaneous readings, physical timing, energy measurement,
authorization, report authenticity and current instrument-state verification
false. Source `hardware_access` is preserved separately as a recorded effect.

Report inspection binds the explicitly supplied original inputs, regenerates
all fields and compares canonical JSON including metrics, spans, cleanup and
false claims. Rehashing an edited report is insufficient. Recorded path labels
are never opened; explicit byte-identical relocated inputs are allowed, keeping
the native sibling filenames. Only producer 0.10.0 is supported for load-check
reports. Digital producer 0.7/0.8/0.9/0.10 and voltage producer 0.9/0.10 retain
their existing v1 semantics and are verified without relabeling old producers.
Existing HIL/Trace instrument/waveform schemas do not accept this new load report;
their integration is a separate step.

## Bounds and cost

Spec <=64 KiB; native result and transcript <=1 MiB each; native plan <=32 KiB.
The three source files total at most 2,129,920 bytes, plus spec. Verification
adds one <=1 MiB report; XML and new report are each separately capped at 1 MiB.
Journal records <=4,000, readings <=50 and checks <=8. The reader does not
follow recorded filesystem/network paths; local links/reparse paths fail.

Evaluation parses/replays the entire transcript once even for a one-sample
window, then costs at most 8 x 50 selected readings. Overlapping checks retain
their own evidence; JSON/XML/base64 copies add allocations and may hit the output
limit. These byte bounds are not RSS guarantees or real-time cutoffs. Measure
same-process evaluation and allocation cost separately from cold disk/CLI startup.
Synthetic sources qualify software behavior only; no SDL hardware was tested.
