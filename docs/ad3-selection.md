# First target and sources

Checked 2026-10-20. First target: **Digilent Analog Discovery 3, 410-415 standard
edition**, covering both analog scope and digital logic acquisition on Windows.
The official SDK makes it possible to implement before purchase. The BNC Adapter
and probes are relevant when the advertised 30+ MHz analog bandwidth is required;
125 MS/s is the sample rate, not the analog bandwidth. This initial adapter is
for ordinary low-voltage MCU bench measurements, not a power-stage probe substitute.

Selection references:

- [AD3 official specification and accessories](https://digilent.com/shop/analog-discovery-3/):
  two differential 14-bit scope channels, 16 digital I/O, up to 125 MS/s.
- [WaveForms SDK guide](https://digilent.com/reference/test-and-measurement/guides/waveforms-sdk-getting-started)
  and [official 3.25.1 runtime/installer documentation](https://files.digilent.com/manuals/WaveForms/3.25.1/main.html).
- [Current and previous official installers](https://digilent.com/reference/software/waveforms/waveforms-3/previous-versions).
  The automated downloads returned access challenges/403 during development;
  no downloaded vendor DLL was loaded, no installer/driver was executed.
- [pydwf 1.1.19](https://pypi.org/project/pydwf/1.1.19/) provides inspectable C ABI
  declarations based on SDK 3.20.1. The wheel was read as source, not bundled or
  used as a runtime dependency. Wheel SHA-256:
  `aefd97cfb8d315d2c4daf623682ae9e235a4f7483618c01e3f9362588663f9f2`.
- [dwf-mcp](https://github.com/best-effort-labs/dwf-mcp), reviewed revision
  `95165815b9186c0b1126ee04fb3e9c3ced8553f7`: useful reference for AD3 ID 10,
  USB enumeration, actual-rate/capability handling and instrument resource groups.
  No project source was copied into this implementation. Its real-hardware claims
  are upstream claims, not our acceptance results. In particular this adapter
  does not label the SDK version as firmware version or use close-all recovery.

Scope choices: Saleae's official Automation API is a strong logic-only alternative,
but relies on Logic software and does not provide this two-channel scope capability.
DSLogic U3Pro16 remains an option for a later logic-focused driver. AD3 is the first
automation-oriented target, not a universal best scope for every bandwidth/budget.
Current China delivered cost was not verified; do not present the historic US$379
launch price as a current quote. Purchase the standard model rather than an
academic-only listing unless eligible.
