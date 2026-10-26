# Saved logic report verification and JUnit

Local 0.8.0 adds `logic inspect-check-report` and `logic junit`. Both read one
saved `logic-check-report.v1`, one original assertion spec and one original
capture, each explicitly supplied with its SHA-256. They recompute the full
native analyses and assertions, then compare the entire saved report to the
recomputed result. Checking only the report hash or its internal analysis hashes
would not detect an edited report whose hashes were recalculated.

## Use

First generate a report using [logic check](logic-checks.md). Preserve the
original spec, capture and all three hashes along with that report. For example:

```powershell
lab-instruments logic inspect-check-report '<report.json>' --sha256 '<report-sha256>' `
  --spec '<spec.json>' --spec-sha256 '<spec-sha256>' `
  --capture '<capture.json>' --capture-sha256 '<capture-sha256>' --json

lab-instruments logic junit '<report.json>' --sha256 '<report-sha256>' `
  --spec '<spec.json>' --spec-sha256 '<spec-sha256>' `
  --capture '<capture.json>' --capture-sha256 '<capture-sha256>' `
  --output '<new-results.xml>' --json
```

All three files must be explicitly selected. No stored paths, SDK/plan paths,
commands or links are followed. Each file is read once, with all checks using
that in-memory snapshot. Files copied into another directory are supported when
their exact bytes/hashes/lengths still match the report bindings. The result
shows both the recorded path label and supplied file reference, and whether the
labels differ. Recorded labels are not authenticated historical paths. Neither
hash verification nor recomputation proves the authenticity of the original
capture, producer version string, hardware, firmware or measurement.

The verifier accepts producer versions 0.7.0, 0.8.0, 0.9.0 and 0.10.0: they share identical
v1 evaluator/decoder semantics. It retains the recorded producer version when
recomputing, while separately recording the verifier version. Unknown older or
future versions are rejected. A future decoder change needs explicit semantic
compatibility handling rather than silently rewriting old reports.

Missing/extra/changed report fields, reordered checks, changed native data/raw
spans/assertions, altered results/summaries or upgraded physical/safety flags
are rejected. Comparison is over canonical JSON, including distinctions between
booleans, integers and floats; whitespace/key ordering is not meaningful.
Input byte hashes and lengths remain exact, including spec/capture formatting.
The spec remains bounded to 64 KiB, report/capture to 1 MiB and checks to 1–8.

## Verdicts and exit codes

Inspection success means the saved result matches its explicit originals. It
does not change a failing assertion into a passing test: the original
`verdict.ok`, `verdict.status`, summary and each check status remain visible.

| Operation | Exit 0 | Exit 3 | Exit 2 |
| --- | --- | --- | --- |
| inspect-check-report | Full recomputation matches, even for a failing report | Not used | Invalid hash/input, unsupported producer, mismatch, limits or I/O error |
| junit | Verified report with every check passed; XML written | Verified failing report; failure/error XML still written | Invalid input/binding/report, limit or output error; no completed XML |

JUnit has one testcase per declared check, in spec order. `failed` maps to a
`failure`; `invalid_evidence` maps to an `error`. Neither becomes skipped or
success. Failure/error text preserves the assertions, native-analysis hash,
native/source status and original source error/cleanup. The full native analysis
and raw spans stay in the bound JSON report. Properties include all three input
hashes, both versions, evidence origin and physical/safety flags. No duration or
test timestamp is invented. Output is deterministic for identical files and
bounded to 1 MiB. Diagnostics use escaped JSON inside escaped XML text, so
control characters and markup cannot create extra testcases or invalid XML.

The output must be a new regular path with an existing parent. An existing file
is rejected before any input read; input/verification and XML/envelope limits are
checked before output creation. No overwrite, retry, subprocess, SDK/network
access, device enumeration or physical operation is involved. There are no new
runtime dependencies. Synthetic origin and all physical verification fields
remain unchanged/false.

For CI, run `junit` as the test step and configure the existing JUnit collector
to upload the new XML even when that step returns 3. Keep exit 2 as an input or
export failure, not a passing or skipped test. Archive the original JSON report,
spec, capture and hashes alongside XML for later recomputation; XML alone is a
summary rather than the full capture evidence. This command does not install or
configure a CI service.
