# Lab Instruments handoff

Read this file and AGENTS.md before a new task. Cross-repository state is in the
workspace HANDOFF.md and desktop plan.

Version 0.9.0 was published at 5eb1e8e, with separate provenance and formal cold
acceptance. Current 0.10.0 adds local saved SDL check/report/JUnit commands;
no vendor SDK/ABI or physical instrument qualification has occurred. Do not publish this candidate,
activate Toolkit or change station pins without the corresponding task scope.

- Load checks snapshot result/fixed plan/transcript once; use the shared native
  inspect_snapshot plus strict in-memory successful-command replay. Unknown or
  missing writes/queries cannot be ignored just because measurement summaries
  match. Failed sources preserve unscored partial readings/error/cleanup but
  every check is invalid_evidence, not a subset pass. Never reconnect or acquire
  hardware during saved-data evaluation. Read docs/load-checks.md for schema.
- V/I/P are sequential queries; use the returned power, never derive synchronized
  power/energy or measured timing. Historical OFF readback is not current state.
  Native failed/cleanup-unknown evidence and all false qualification flags must
  survive report recomputation and JUnit. Inspection exit 0 is consistency,
  check/JUnit exit 3 is a complete failing/error report, exit 2 is input failure.
- Version 0.10 retains logic producers 0.7/0.8/0.9/0.10 and waveform producers
  0.9/0.10 with unchanged v1 semantics. Preserve actual producer in regeneration;
  never make a version bump silently reject old saved reports or relabel them.
- New runtime modules change release.py's exact wheel inventory AND the
  independent test fixture member counts. The installed smoke reuses its six
  existing SDL sessions for offline checks; do not reacquire per tamper case.

- Waveform checks read only explicit native AD3 captures or SDS saved evidence.
  Reuse native ADC conversion and SDS replay; do not calculate from CSV/summary
  alone. Source snapshots cap 8 MiB/1,615 files and read each unique file once.
  Strict sample windows never clamp; DC coupling only. RMS includes DC; AC RMS
  removes the population mean only, with no filtering or bandwidth guarantee.
  Preserve false calibration/clipping/timing/authentication and native cleanup.
- Saved waveform report inspection/JUnit regenerates every field using explicit
  original inputs. Rehashed reports and stored path labels are not authority.
  Missing channels/windows/coupling are invalid_evidence, not passed or skipped;
  corrupt or failed source acquisition is an input failure, no measurement pass.
- The first new SDS tests used result.json instead of native scope-result.json;
  fix the fixture path, not the product format. Existing successful native tests
  were unaffected. New SDS fixtures use the normal 2 s double budget and saved
  copies; offline mutations never reacquire hardware or hide timeout failures.
- A new runtime module changes the release verifier's independent exact member
  list and minimal fixture counts. Keep both current. Version 0.9 leaves logic
  decoder/assertion semantics unchanged, so logic producers 0.7/0.8/0.9 are
  explicitly compatible; use an actually unknown version for the negative test.
  Shared HIL/Trace dev313 does not install lab: source pytest needs an explicit
  process-only src path; installed smoke uses the isolated wheel and -I instead.

- Keep runtime modules, README/package metadata and uv.lock unchanged during
  release-only work so the accepted installed payloads and 74-file Toolkit pin
  stay byte-identical. A different SOURCE_DATE_EPOCH changes the wheel ZIP hash
  without changing its payload. Formal builds use Hatchling's fixed default
  1580601600; record new asset hashes, retain old candidates and never call them
  identical archives. Source distributions/manifests change with development files.
- Run installed smoke before committing; stage new files for hatch sdist, use a
  fixed SOURCE_DATE_EPOCH for provisional builds, and compare the post-commit
  source-bound rebuild. Recheck all source archive members against Git, not only
  runtime files. A local manifest with an intended tag is not remote attestation.
- Verify reads and parses bounded local archives without extraction, component
  imports or subprocesses. Build explicitly invokes Git/uv; neither accesses
  hardware. Release assets are Python wheel/sdist/manifest/SHA256SUMS only.
- The PureThermal helper is not shipped as a qualified binary. CI builds it from
  source and uses only its explicit synthetic backend. Source and local-build
  instructions remain in the sdist. Never fall back to a real camera or SDK.
- CI checks use Windows x64, Python 3.11/3.13 and locked tools. One version per
  job avoids fixed synthetic identities competing for local product leases.
- Parser costs scale with bounded compressed/inflated archive bytes (8 MiB
  payload budget, 128 wheel / 256 source members). Input limits are not process
  RSS limits. Preserve corruption, tag/source drift and structured errors.
- Initial hosted CI exposed a test equating a completed unacknowledged TCP OFF
  write with the server thread already receiving it. With malformed/unread data,
  Windows close/reset can discard queued peer data; client send evidence is not
  receipt or effect. Assert the native TX bytes exactly once and keep cleanup
  unknown. For the separate partial-send resynchronization test, wait for the
  owned double's EOF before asserting its received bytes. Never add a product
  retry, delay, extra query or stronger cleanup claim to satisfy a test race.
- CI always retains pytest's explicit basetemp and JUnit, including negative
  adapter evidence. A second hosted run failed while acquiring the synthetic
  SDS baseline for an offline tamper case; the old assertion truncated the error
  and the old artifact list omitted pytest outputs. Preserve the complete result
  and evidence to diagnose it; do not weaken assertions or add capture retries.
- Saved SDS tamper checks now use private byte copies of one fully verified
  synthetic baseline, acquired once with the normal 2 s I/O budget. Reacquiring
  for every file mutation under the transport-fault fixture's 150 ms budget
  made unrelated file-integrity tests depend on hosted I/O timing. Dedicated
  transport/deadline tests keep their live peer and short budgets; no runtime
  timeout, gate, command or retry policy changes.
