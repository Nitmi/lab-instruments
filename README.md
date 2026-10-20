# Lab Instruments

An instrument CLI for embedded agents. The first adapter targets **Digilent
Analog Discovery 3** on 64-bit Windows with **WaveForms SDK 3.25.1**.

Status: local software candidate, **not yet tested with a vendor DLL or real
instrument**. This repository includes a real C ABI acquisition path and tests
against an explicit C ABI test double. Neither those tests nor another project's
hardware reports establish that this adapter works on your AD3. Runtime code
uses only Python's standard library. The vendor SDK/Adept runtime is separate.

## Initial capability

- Static host doctor and hash-bound acquisition plans, with no SDK load/device access.
- Explicit USB enumeration and exact serial/model selection; no first-device fallback.
- Bounded single scope or logic acquisitions, actual settings readback, full data hashes.
- A separate worker, host watchdog, input stop and exact owned-handle close evidence.
- Offline sample inspection and voltage/transition summaries without reopening devices.

The input-only API has no supply, AWG, pattern, DIO drive, generic SDK call,
device reset or close-all operations. SDK device opening may initialize its FPGA
and hardware; this is not a zero-effect connection. Use an AD3 dedicated to the
test and the vendor's input/wiring limits. The adapter does not verify the DUT,
probe connection, calibration, absolute sample time or physical safety.

## Install and prepare without an instrument

```powershell
uv sync --frozen --extra dev --python 3.11
uv run --frozen lab-instruments doctor --json
uv run --frozen lab-instruments capture plan --serial YOURSERIAL --instrument scope --channels 1 2 --sample-rate 1000000 --samples 4096 --range-v 10 --output scope-plan.json --json
uv run --frozen lab-instruments capture inspect-plan scope-plan.json --sha256 '<plan-sha256>' --json
```

`YOURSERIAL` is an explicit placeholder, not a device claim. Replace it with the
SDK's observed serial once the AD3 is available. Plan generation is not hardware
authorization. There is no per-capture confirmation prompt: an agent reuses the
user's agreed task scope and calls the explicit run command for ordinary input
acquisition. A hash binds input bytes; it is not an approval token.

Outputs must be new files/directories with existing parents. A malformed/changed
plan, invalid limits or an existing output is rejected before instrument access.

## After installing the vendor SDK and connecting the selected AD3

Obtain WaveForms/Adept from the [official vendor page](https://digilent.com/reference/software/waveforms/waveforms-3/previous-versions).
Do not replace another device's USB driver or modify PATH to run this CLI.
Choose the installed x64 `dwf.dll` explicitly and record its SHA-256:

```powershell
lab-instruments doctor --sdk '<absolute-path-to-dwf.dll>' --sdk-sha256 '<sdk-sha256>' --json
lab-instruments devices --sdk '<absolute-path-to-dwf.dll>' --sdk-sha256 '<sdk-sha256>' --json
lab-instruments capture run --plan scope-plan.json --sha256 '<plan-sha256>' --sdk '<absolute-path-to-dwf.dll>' --sdk-sha256 '<sdk-sha256>' --output '<new-run-directory>' --json
```

Doctor only inspects file bytes; it never loads the DLL. `devices` loads the
explicit library, requires version 3.25.1, and enumerates SDK USB devices without
opening one. `capture run` loads the same hash-bound library in a separate worker,
matches AD3 device ID 10 and exact serial, refuses a busy device and opens by
serial. OpenEx performs its own vendor-defined internal enumeration. There is
no SDK/library discovery fallback. The SDK hash does not authenticate its publisher,
dependencies or the machine loading it; only use trusted vendor installations.

Run is immediate-trigger, single-buffer input acquisition, up to 16,384 samples,
1 kS/s–125 MS/s requested and 3–30 seconds total worker deadline. The active
hardware buffer/configuration can impose a smaller cap. Digital sampling uses
an integer divider and an achieved rate no greater than requested; scope
rounding above the requested rate is rejected. Readback values are saved.
The scope range is full-scale peak-to-peak volts at zero offset, not a safe
voltage or attenuation claim. SDK voltage samples are not raw ADC counts.

SDK calls can initialize/reconfigure input instruments. Scope acquisition waits
2 seconds after configuration for analog settling; that is not a calibration
certificate. Parent timeout includes SDK loading, opening, settling and cleanup.
The watchdog kills a stuck worker and marks cleanup **unknown**, retaining its
journal. It cannot guarantee that the instrument has stopped. Never auto-retry
an uncertain operation. Normal cleanup stops only the selected input instrument
and closes only the owned handle. It does not restore arbitrary previous settings.

## Saved evidence

Each new run directory contains the original plan copy, request, stage journal,
capture evidence when produced, and supervisor result. Capture data is base64
plus byte count and SHA-256: scope uses channel-major IEEE754 float64 little-endian
volts; logic uses complete 16-bit little-endian words, DIO0 first. The digital
channel list selects analysis channels; the raw words retain all 16 inputs.
Sample-relative time is index / actual sample rate, with no verified UTC origin.

```powershell
lab-instruments inspect '<run-directory>\capture.json' --sha256 '<capture-sha256>' --json
```

This checks the bound saved data and reports per-channel voltage extrema or
digital transitions. It does not load the SDK, recheck physical inputs or upgrade
test-double data into physical evidence. Failed captures remain failed, including
data that was acquired before cleanup failed. Journal UTC timestamps describe
host recording, not the instrument trigger/sample clock.

## Development

```powershell
uv run --frozen pytest -ra
uv run --frozen ruff check .
uv run --frozen ruff format --check .
uv build --no-build-isolation --python .venv --out-dir '<new-dist-directory>'
```

No real-device test runs automatically. See [selection and sources](docs/ad3-selection.md)
and [hardware acceptance](docs/hardware-acceptance.md). Electronic load, thermal
camera, protocol decoders, output control and MCP integration are later stages.
