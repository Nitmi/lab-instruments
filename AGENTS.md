# Repository guidance

- This component owns instrument operations; keep Toolkit a thin coordinator.
- Digilent Analog Discovery 3 remains input acquisition only. Never
  add output, supply, pattern, DIO drive, generic SCPI, reset or close-all calls
  as implicit setup/cleanup. Opening the SDK device can initialize its hardware;
  do not describe acquisition as electrically zero-effect.
- The separate SDL1020X-E adapter permits explicitly planned bounded CC loading
  and input-off cleanup. Use only the documented fixed SCPI subset, exact model/
  serial/firmware and one literal IPv4 endpoint. No reset, short, protection
  disable, arbitrary commands or implicit input enable. Test only doubles until
  real equipment and a physical test scope are available.
- Default doctor, plan and saved-evidence inspection must not load a vendor
  library, enumerate hardware, use network or start another executable.
- Hardware paths require an explicit absolute SDK library path/hash and exact
  serial/model selection. No first-device fallback or retries.
- Use one owned handle, bounded single acquisitions, capability/readback checks,
  exclusive new output, a worker watchdog, and explicit cleanup evidence.
- Test doubles and sample files never prove vendor ABI or physical measurements.
  Keep simulator/test evidence distinct from physical capture evidence.
- Preserve failed/partial evidence. Never claim cleanup after killing a worker.
- Run pytest, Ruff, formatting, installed CLI smoke and git diff --check before
  cohesive commits. Do not publish, change Toolkit/locks or install drivers here.
