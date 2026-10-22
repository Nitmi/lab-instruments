# Offline protocol and PWM assertions

Local 0.7.0 adds `logic check`: a fixed, declarative assertion spec evaluated
against one hash-bound saved AD3 logic capture. It reads the spec once, validates
the entire contract before reading capture data, then reads the capture once.
Every check uses the same in-memory snapshot and the native UART/PWM/I2C/SPI
decoder. It does not trust or consume a previously generated analysis/report.
There are no callbacks, arbitrary commands, file references inside the spec,
downloads or hardware operations. Runtime dependencies remain empty.

## Use

Start with the repository/sdist's [example spec](../examples/logic-checks.json).
It is illustrative, not a known board configuration. Choose only the checks,
channel map, decoding settings and expectations appropriate to your saved data.
All configuration fields are required; there are no inferred UART/SPI settings.
Compute both SHA-256 hashes after preparing the inputs, then run:

```powershell
lab-instruments logic check --spec '<spec.json>' --spec-sha256 '<spec-sha256>' `
  --capture '<capture.json>' --capture-sha256 '<capture-sha256>' `
  --output '<new-report.json>' --json
```

The optional report file must be new with an existing parent. A mismatch or
invalid decoded signal still saves a useful report. Existing output is refused
before reading either input. Malformed specs, wrong hashes, unsupported/missing
capture channels, unsupported sampling rates or byte/record limits are input
errors, not assertion successes. No new user approval is required for these
offline checks; hashes bind bytes rather than grant authority.

## Contract

`lab-instruments.logic-check-spec.v1` has exactly `schema_version` and `checks`.
There must be 1–8 checks. Every entry has exactly `id`, `kind`, `configuration`
and `expect`. IDs are unique, start with a letter and contain at most 48 ASCII
letters/digits/underscores/hyphens. Unknown fields or kinds are rejected.

| kind | Required configuration | Expectation |
| --- | --- | --- |
| uart | channel, baud, data_bits, parity, stop_bits, inverted | bytes_hex: exact nonempty lowercase even-length hex, up to 512 bytes |
| pwm | channel, active_level | minimum_cycles, inclusive frequency_hz and duty_cycle bounds |
| i2c | scl, sda, min_phase_samples | exact ordered transfers, including address, direction, start/end conditions, data and acks |
| spi | sck, cs, mosi, miso, mode, word_bits, bit_order, cs_active, min_phase_samples | exact ordered transfers, each with all selected data directions and integer word arrays |

The native profiles' limits remain unchanged; see [UART/PWM](logic-analysis.md)
and [I2C/SPI](bus-analysis.md). Boolean values cannot stand in for channels,
integers or numeric ranges. SPI must explicitly set unused data directions to
null and select at least one; all selected physical channels are distinct.
Expected UART values must fit its configured 5–8 data bits. SPI word expectations
must fit its configured 1–32 bits and have the same word count in both directions.

UART compares the **whole decoded stream**. Extra bytes, an otherwise matching
substring, truncation or a bad frame cannot pass. I2C compares all transfers and
all bytes/ACKs, including the address ACK. Each `acks` list has one boolean for
the address plus one per data byte; true means ACK. Expecting a read-final NACK
or an address NACK is allowed. Data after NACK is not. First/last boundaries and
repeated-start relationships must form a closed expected sequence. SPI compares
all observed transactions/words without joining across CS boundaries. Omitted,
extra or changed traffic fails. This first contract has no wildcard, prefix,
mask, regex or ignore-unmatched-transaction option.

PWM requires 1–512 minimum closed cycles. Both range objects have exactly
`minimum`/`maximum`, finite and ordered. Frequency bounds are 0–125,000,000 Hz;
duty bounds are fractions 0–1. Every observed closed cycle must be in both
inclusive ranges, even after the minimum count is met. A good mean cannot hide
an outlier; each outlier records its cycle index, value and raw sample span.
Window fragments remain excluded by the native PWM profile. A constant level or
insufficient waveform does not become 0 Hz or a passing 0%/100% PWM result.
No automatic rounding, tolerance or measurement uncertainty is added to bounds.
Choose ranges with sampling resolution and reference uncertainty in mind.

I2C/SPI expectations allow 1–64 transfers, with at most 512 total transfer and
byte/word records. The spec file is capped at 64 KiB. Native analysis keeps its
512-record limit; capture/report JSON remains capped at 1 MiB. A large combined
report that exceeds the bound fails with LIMIT_EXCEEDED rather than silently
dropping evidence. The CLI also checks the enclosing JSON size before writing
the report. Empty expectations are rejected rather than becoming vacuous passes.

## Results and evidence

`lab-instruments.logic-check-report.v1` binds both input file hashes and the
runner version. Each check keeps its configuration/expectation, full native
analysis, native analysis SHA-256, comparison and status. Native raw spans and
sampling indices remain available. The report is a derived local artifact, not
a signed/authenticated proof; downstream consumers should re-evaluate explicit
original inputs when they need a fresh conclusion. Saved SDK/plan paths are
not reopened, and inputs are not re-read after the snapshot.

Every check first gates on source acquisition/cleanup and supported valid
decoding. A failed source or invalid signal yields `invalid_evidence`; expected
payload comparisons are `not_evaluated`, never matched against partial data.
A supported native result that differs from the expectation yields `failed`.
Independent checks continue after assertion/signal failures so one report shows
all outcomes. A hard input/hash/capability/limit error aborts evaluation with exit
2 and no completed report. Nothing triggers a retry, recovery or physical test.

The overall status is passed only if every declared check passed. `complete=true`
means all checks were evaluated, not that the source signal or physical test is
complete. Exit 0 means all checks passed, 3 means a completed failing report,
and 2 means an input/output/limit error. Synthetic origin, source status/error/
cleanup, model and sampling limits remain unchanged. Physical identity, actual
timing, calibration, electrical compliance and endpoint identity remain false.

A passed spec is only the particular digital assertion requested against the
saved capture. It does not prove the correct sensor, register, board or firmware
was measured. Contract authors still choose the correct protocol settings and
expected application data; this tool does not infer them from convenient output.
