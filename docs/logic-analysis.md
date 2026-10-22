# Offline UART and PWM from saved AD3 samples

Local 0.5.0 introduced offline analysis of `lab-instruments.capture.v1` logic payloads.
Local 0.6.0 also provides [multi-channel I2C/SPI decoding](bus-analysis.md).
It reads a single hash-bound capture, validates its plan and raw u16le payload,
and uses the **saved actual sampling-rate readback**, never the requested rate.
It does not load a vendor DLL, start a worker, enumerate/open a device, or follow
the saved SDK/plan paths. It needs no additional runtime dependency or approval.

This analyzes sampled digital levels. It cannot establish voltage compliance,
edge slew, signal integrity, clock calibration, absolute UTC, or physical timing.
The original `evidence_origin`, failed status, cleanup and error remain visible;
synthetic data remains synthetic. An analyzed payload cannot repair failed
acquisition/cleanup: the enclosing result remains `source_failed` and exit 3.

## UART

```powershell
lab-instruments logic uart '<capture.json>' --sha256 '<capture-sha256>' `
  --channel 0 --baud 115200 --output '<new-uart.json>' --json
```

Explicit options: `--data-bits 5|6|7|8` (default 8), `--parity none|even|odd`
(default none), `--stop-bits 1|2` (default 1), `--inverted` (default false).
The configured baud is 50–3,000,000, with at least eight actual samples per bit.
There is no auto-baud, 1.5 stop-bit mode, nine-bit mode or protocol/application
identity assertion. Incorrect baud settings can still produce plausible bytes;
framing success does not prove the requested baud or message identity.

Normal idle is high. A start transition must follow at least floor(samples/bit)
consecutive idle samples. The decoder estimates an edge halfway between the
preceding and first active sample, then reads the nearest sample at each bit
center (ties to the later sample). Bits are LSB-first. Parity and each configured
stop midpoint are checked. This does not measure edge timing or verify every
sample throughout a bit. Conservative complete-frame windows must fit the
capture, including the latest possible final stop end. Noninteger sample/baud
ratios and back-to-back frames are supported without baud inference.

Frames retain center indices, candidate byte values, errors and raw payload byte
spans. Initial active level, insufficient idle, false start, parity/framing error
or a truncated frame makes the analysis invalid. The decoder can retain later
candidate frames for diagnosis; it is not verified resynchronization. A leading
active fragment cannot be promoted to a complete byte stream. No frames is
`no_frames`, not a successful empty message. `bytes_base64` is available only
when all recorded frames and boundaries pass; good bytes are never silently
joined across errors. Compare decoded bytes with the project's expected data
separately. Terminal escape sequences are not rendered as text by this CLI.

## PWM

```powershell
lab-instruments logic pwm '<capture.json>' --sha256 '<capture-sha256>' `
  --channel 15 --active-level high --output '<new-pwm.json>' --json
```

Use `--active-level low` for active-low pulses. Only cycles between two observed
inactive-to-active edges are included. Each has the intervening inactive edge,
period/active/inactive sample counts, seconds, frequency and duty fraction.
Statistics give min/max/arithmetic mean of per-cycle values; mean frequency is
not the inverse of mean period. Boundary fragments are excluded and counted.
`window_active_sample_fraction` describes the whole capture, not PWM duty.

A constant level or a lone pulse has `insufficient_cycles` and null statistics;
it is not a verified 0 Hz or 0%/100% PWM signal. Edge positions use the first
sample with the new level. Resolution is one nominal sample interval; the stated
one-sample interval quantization bound assumes uniform sampling and excludes
clock, threshold and electrical uncertainty. Aliased or missed pulses cannot be
recovered. Higher sampling rates and an appropriate capture window are needed
for useful measurements; software calculations do not calibrate the instrument.

## Evidence and bounds

`--channel` must have been selected in the original plan even though complete
16-bit words were saved. Capture files remain capped at 1 MiB and 16,384 samples.
Each analysis allows at most 512 UART frame/issue records or 512 closed PWM
cycles; exceeding the cap is `LIMIT_EXCEEDED`, never silent truncation. Output
JSON remains bounded at 1 MiB. SHA-256 binds the original JSON and raw payload;
hashes do not authenticate physical provenance.

`lab-instruments.logic-analysis.v1` includes source status/cleanup, configuration,
sample rate, time domain and derivation. Raw spans address decoded u16le payload
bytes, with end exclusive; the preceding sample is included to show the start
edge bracket. They are not offsets into JSON/base64. Report files are optional
and must be new, with existing parents. Failed analyses with useful evidence are
also saved. Exit 0 means valid requested analysis of a successful source, 3
means missing/invalid signal or failed source, and 2 means input/hash/limit/output
failure. Report time is sample-relative; no UTC origin is inferred.

Software tests and installed-wheel smoke use explicit synthetic saved waveforms,
including parity/framing errors, truncated frames, noninteger sample ratios,
active-low PWM and failed cleanup. None proves SDK ABI or real measurement
accuracy. Follow [physical acceptance](hardware-acceptance.md) after an AD3 is
available. I2C/SPI use their separately selected bus profiles; analog threshold
conversion remains unsupported by these digital-only commands.
