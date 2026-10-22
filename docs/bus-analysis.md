# Offline I2C and SPI from saved AD3 samples

Local 0.6.0 extends [UART/PWM analysis](logic-analysis.md) with multi-channel
digital bus decoding. A single capture is read and hash-checked once, then all
selected channels are extracted from its original u16le words. Channels must be
distinct and selected in the original plan; no SDK, network, helper, interpreter
or provenance-path access is needed. Runtime dependencies remain empty.

Source `evidence_origin`, failed acquisition/cleanup and errors remain visible.
A valid digital interpretation cannot change a failed source into a passing
capture. Time is sample-relative using saved actual sampling-rate readback.
`physical_device_verified`, `physical_timing_verified`, `calibration_verified`,
`protocol_timing_compliance_verified` and `endpoint_identity_verified` remain
false. Hashes establish byte consistency, not authentic physical measurements.

## I2C

```powershell
lab-instruments logic i2c '<capture.json>' --sha256 '<capture-sha256>' `
  --scl 0 --sda 1 --output '<new-i2c.json>' --json
```

The bounded `i2c-7bit-sampled-v1` profile supports ordinary 7-bit addresses
0x08–0x77, address direction, data bytes, ACK/NACK, STOP and repeated START.
It observes SDA on sampled SCL rising edges. Start/stop delimiters are SDA
transitions with SCL high in both adjacent samples. Eight MSB-first data bits
and the ninth ACK level form a byte. The address byte retains its original
eight-bit value alongside the derived seven-bit address and read/write direction.
Each byte retains all nine sampling indices and its raw payload byte span.

A transfer is a START-to-STOP or START-to-repeated-START segment. The latter
starts a new segment with its own address; a final STOP must still be observed.
Observed NACK is retained, not automatically classified as an application error:
it may be a normal read termination or an unacknowledged address. A decoder
success means a supported sampled structure, not that the device acknowledged
the requested operation. Compare address, direction, bytes and ACK expectations
with the application separately. Data clocks after a recorded NACK invalidate
that segment. `ack_receiver_role` is the protocol's expected role, not proof of
which physical device drove SDA.

STOP or repeated-START preparation may add one SCL rise after a completed byte.
A single pending bit in the final high phase is recorded as
`boundary_setup_clock` only when its level matches STOP-low or repeated-START-high.
It is excluded from data. An aborted one-bit continuation with the same sampled
shape cannot be distinguished; this is not a full protocol conformance checker.
Other incomplete data/ACK groups, missing address/final delimiter, active capture
prefix, orphan clock/STOP, or simultaneous SCL/SDA transitions are errors.
The simultaneous-edge rule is deliberately conservative; no within-sample edge
ordering is invented. A missing or partial transaction does not receive a guessed
address, completed ACK or fabricated data bytes.

`--min-phase-samples` (default 2, range 2–64) is an explicit sampling-quality
floor, not an electrical timing standard. Short observed clock phases/start holds
and delimiter high phases invalidate the view. Slow/extended SCL-low intervals
are accepted; they do not independently prove clock stretching or its cause.
Reserved/general-call/10-bit addresses, Hs/UFm/I3C, arbitration analysis, glitch
filtering, bus recovery and device/register semantics are unsupported. Do not
claim an unsupported request succeeded because it produced plausible bytes.

## SPI

```powershell
lab-instruments logic spi '<capture.json>' --sha256 '<capture-sha256>' `
  --sck 0 --cs 1 --mosi 2 --miso 3 --mode 0 `
  --word-bits 8 --bit-order msb --cs-active low `
  --output '<new-spi.json>' --json
```

At least one of MOSI/MISO is required. `--mode 0|1|2|3` is explicit; the decoder
does not infer it. CPOL specifies idle SCK, CPHA 0 samples the leading edge and
CPHA 1 the trailing edge. Words have 1–32 bits, MSB-first by default or explicit
LSB-first. Chip select is explicit, active-low by default or active-high. Clock
activity while unselected is excluded.

Only transactions with observed CS assertion and deassertion qualify. Each word
requires the complete paired clock cycles, including the last trailing edge for
CPHA 0. Leading/trailing edges must alternate, SCK must start/end at the configured
idle state, and each selected phase/CS hold must meet `--min-phase-samples`.
Simultaneous CS/SCK transitions have unknown within-sample order and invalidate
the view. Data must be unchanged from the preceding sample through one sample
after its selected sampling edge. A transition at that boundary is an explicit
sampling ambiguity. This local rule is not the target datasheet's setup/hold
requirement, and incorrect modes or aliased signals can still look plausible.

Words retain direction-specific values, sample indices and raw spans. CS
boundaries remain separate transactions. Only a completely valid eight-bit view
offers per-direction `bytes_base64`, one array element per transaction; it never
silently joins bytes across invalid/truncated words or across CS boundaries.
Other widths remain integer words. Partial initial/final CS, incomplete words,
missing trailing edges or clock/data ambiguity invalidate the analysis; available
word/partial sampling evidence is preserved for diagnosis. There is no automatic
resynchronization proof, mode detection, no-CS framing, three-wire turnaround,
Dual/Quad SPI or device command interpretation.

## Limits and results

Existing bounds remain: 1 MiB input/output JSON and at most 16,384 samples.
Each bus analysis allows 512 records total (transfers, byte/word records and
issues/errors). Exceeding this cap fails with `LIMIT_EXCEEDED`; no result is
silently truncated. Small fields bounded by byte/word width remain attached to
their record. All raw spans address decoded u16le payload bytes, end exclusive,
including the preceding sample at the start edge. They are not JSON/base64
offsets. Original source files remain unmodified.

Report schema remains `lab-instruments.logic-analysis.v1`, with `analysis.kind`
`i2c` or `spi` and a fixed profile. `--output` is optional, requires a new file
and existing parent, and saves useful invalid/failed-source evidence as well.
Exit 0 means supported complete analysis of a successful source; exit 3 means
invalid/no transfers or failed source; exit 2 means bad input/hash/options/limit
or output. I2C NACK is a decoded observation that the application must assess.

Tests and installed-wheel CLI smoke use independent synthetic signal producers
and saved captures. They cover repeated START/NACK, all SPI modes, widths/orders/
polarities, clipping, data/clock ambiguity and source failures. These checks do
not establish vendor ABI, wiring, signal integrity, physical timing or measurement
accuracy. Real-device references remain in [hardware acceptance](hardware-acceptance.md).

Protocol references: [NXP UM10204 rev.7](https://www.nxp.com/docs/en/user-guide/UM10204.pdf)
(START/STOP, byte/ACK and addressing) and [Analog Devices SPI introduction](https://www.analog.com/en/resources/analog-dialogue/articles/2018/08/13/15/31/introduction-to-spi-interface.html)
(clock polarity/phase and four-wire framing). Our sampled-profile limits above
are implementation choices, not additional claims from those references.
