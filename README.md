# Lab Instruments

Local **0.11.0** adds [saved thermal ROI assertions](docs/thermal-checks.md):
apparent surface temperature bounds, hotspot regions and qualified temperature
deltas, with complete raw-frame recomputation and JUnit. Explicit invalid-pixel
budgets and unavailable trend evidence remain errors. No camera is opened.

It retains **0.10.0** [saved SDL measurement assertions](docs/load-checks.md):
voltage/current/power ranges, exact input state and phase windows, raw-command
replay, full report recomputation and JUnit. Failed sources keep invalid-evidence
errors and recorded cleanup; sequential queries do not become simultaneous
measurements or energy estimates. Published 0.10 remains available separately.

It retains [saved report verification and JUnit export](docs/logic-reports.md).
Both recompute every assertion from explicit hash-bound original inputs; JUnit
keeps assertion failures and invalid evidence as distinct failing outcomes.
Version 0.9.0 adds [saved analog waveform assertions](docs/waveform-checks.md) for
AD3 and SDS804X HD: bounded voltage extrema/mean/peak-to-peak/RMS/AC RMS checks,
full report recomputation and JUnit from original raw evidence. No device is
opened by these commands. Physical instrument/SDK qualification remains pending.

It retains [offline protocol/PWM assertions](docs/logic-checks.md):
exact UART/SPI data, I2C address/direction/ACK/boundaries, and per-cycle PWM
frequency/duty ranges, evaluated against one hash-bound original capture.
It retains [I2C and SPI offline decoding](docs/bus-analysis.md):
repeated START/ACK/NACK and four explicit SPI modes, with transaction/raw-sample
evidence and strict incomplete/ambiguous sampling results. It retains
[offline UART decoding and PWM measurements](docs/logic-analysis.md)
from hash-bound saved AD3 logic captures. It uses recorded actual sampling rates,
retains signal errors and failed-source status, and never opens a device.

An instrument CLI for embedded agents. It supports **Digilent Analog Discovery 3**
input acquisition on Windows x64 with **WaveForms SDK 3.25.1**, and bounded
**SIGLENT SDL1020X-E** electronic load sessions over SCPI TCP.

Status: local software candidate, **not yet tested with a vendor DLL or real
instrument**. This repository includes a real C ABI acquisition path and tests
against an explicit C ABI test double and SDL protocol tests over local TCP.
Neither those tests nor another project's
hardware reports establish that this adapter works on your AD3. Runtime code
uses only Python's standard library. The vendor SDK/Adept runtime is separate.

## Initial capability

- Static host doctor and hash-bound acquisition plans, with no SDK load/device access.
- Explicit USB enumeration and exact serial/model selection; no first-device fallback.
- Bounded single scope or logic acquisitions, actual settings readback, full data hashes.
- A separate worker, host watchdog, input stop and exact owned-handle close evidence.
- Offline sample inspection and voltage/transition summaries without reopening devices.
- Offline UART frames and closed-cycle PWM frequency/duty measurements with raw sample spans.
- Offline 7-bit I2C and CS-framed full-duplex SPI, preserving observed bus boundaries and errors.
- Declarative exact-data and per-cycle assertions with source-failure gates and saved reports.
- Saved report binding verification and deterministic JUnit with failure/error preservation.

The AD3 input-only API has no supply, AWG, pattern, DIO drive, generic SDK call,
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
and [hardware acceptance](docs/hardware-acceptance.md). Protocol decoders,
AD3 output control and MCP integration are later stages.

## SDL1020X-E electronic load

Version 0.2.0 adds typed `load plan`, `load inspect-plan`, `load run` and `load inspect`
for identity-only, query-only observation, bounded static CC and explicit input OFF.
Plans are offline; runs use one literal IPv4 address, exact serial/firmware, one
connection and no retries. There is no interactive confirmation before each run.
The CC profile is capped at 24 V/5 A/50 W and 30 seconds; it preserves input/OFF
readback and raw TX/RX evidence. TCP loss leaves instrument state unknown even
after OFF was sent. Protocol-double tests do not establish electrical shutdown.
See [commands, limits and physical acceptance](docs/sdl1020xe.md) before use.

## SIGLENT SDS804X HD oscilloscope

Version 0.3.0 adds `scope plan`, `scope inspect-plan`, `scope run` and `scope inspect`:
exact-identity LAN access, stopped waveform download or bounded SINGLE acquisition,
and C1-C4 WORD/LSB waveform data with probe-aware voltage/time conversion. Each
session saves native binary replies, descriptor, CSV, statistics and cleanup.
Offline inspect replays the native evidence without instrument access. There is
no per-capture confirmation prompt. Current verification uses TCP protocol doubles;
physical SDS804X HD behavior and measurement accuracy await the instrument.
See [SDS804X HD workflow and limits](docs/sds804x.md).

## PureThermal 3 / Lepton 3.5 radiometric camera

Version 0.4.0 adds typed `thermal plan`, `inspect-plan`, `run`, `import` and `inspect`.
A separate hash-bound Windows x64 Media Foundation helper captures native Y16;
Python verifies raw GET state and matching telemetry before calculating apparent
surface temperatures, ROI/hotspots and temperature deltas. Saved evidence can be
evaluated offline without starting the helper. The native helper is built and
tested using its explicit synthetic backend; physical camera/UVC behavior and
measurement accuracy are pending. See [workflow, effects and limits](docs/purethermal3.md).
