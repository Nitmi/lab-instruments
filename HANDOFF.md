# Lab Instruments handoff

Read this file and AGENTS.md before a new task. Cross-repository state is in the
workspace HANDOFF.md and desktop plan.

Version 0.8.0 is a software candidate; no vendor SDK/ABI or physical instrument
qualification has occurred. Release preparation adds development-only bounded
archive verification, source-bound reproducible builds and GitHub Actions. The
intended destination Nitmi/lab-instruments is not evidence a remote exists.
Do not publish, create that repository, activate Toolkit or change station pins
without the corresponding task scope.

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
