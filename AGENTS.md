# Repository guidance

- This component owns instrument operations; keep Toolkit a thin coordinator.
- Initial target is Digilent Analog Discovery 3, input acquisition only. Never
  add output, supply, pattern, DIO drive, generic SCPI, reset or close-all calls
  as implicit setup/cleanup. Opening the SDK device can initialize its hardware;
  do not describe acquisition as electrically zero-effect.
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
