# SIGLENT SDS804X HD (0.3.0 local candidate)

Implemented from the vendor's SDS800X HD EN11G programming guide. Verified with
local TCP protocol doubles and isolated Python installs, **not a physical SDS804X**.
The adapter targets the official SDS804X HD model; its expected IDN model field
is exactly `SDS804X HD`. The real unit's IDN spelling, firmware behavior, binary
framing and measurement accuracy still need hardware acceptance. An unexpected
model/serial/firmware fails before any configuration. Do not enable fuzzy matching.

Runtime uses the Python standard library and one literal IPv4 raw SCPI socket,
port 5025 by default. No VISA package, vendor DLL, USB driver, browser automation,
network scan, DNS or first-device fallback is used. No SDK is needed for this
adapter; the root `doctor` remains the existing static AD3 SDK check.

## Agent workflow

An agent can reuse the user's agreed instrument task scope across captures;
there is no per-capture interactive approval or manual hash confirmation.
The SHA-256 binds plan bytes, not permission. Each capture has a new output
directory, one owned worker, one socket, a local cooperative serial lease,
bounded time/bytes and zero reconnects/retries. Other hosts/front-panel users
can still access the scope; this is not an instrument-side exclusive lock.

| Action | Behavior |
| --- | --- |
| `identify` | One `*IDN?` on the supplied endpoint; record manufacturer/model/serial/firmware. No transfer or acquisition writes. |
| `stopped` | Download the first requested points from C1-C4 of an existing frozen capture. Age and freshness are unknown. |
| `single` | Arm SINGLE once using the existing trigger setup; wait for STOP plus observed activity or a changed nonzero acquisition counter, then download. No forced trigger. |

Capture modes require an already stopped, normal YT, non-sequence acquisition,
with selected channels already enabled, AC/DC coupled and in voltage units.
Channel scale, offset, probe factor, coupling and unit are read before/after;
the binary descriptor's scale/probe/coupling must agree. This version does not
change analog ranges, probes, channel enable, timebase or trigger level/type.
Prepare these on the front panel. Physical probe attenuation must match the
instrument setting. Scope input grounds are earth-referenced; follow the vendor's
input ratings and probe/grounding instructions when connecting the DUT.

Generate a plan offline (the address, serial and version below are placeholders):

```powershell
lab-instruments scope plan --host 192.168.0.50 --serial YOURSERIAL --firmware-version YOURVERSION --action single --channels 1 2 3 4 --points 4096 --output scope-plan.json --json
lab-instruments scope inspect-plan scope-plan.json --sha256 '<plan-sha256>' --json
lab-instruments scope run --plan scope-plan.json --sha256 '<plan-sha256>' --output '<new-run-directory>' --json
lab-instruments scope inspect '<run-directory>\scope-result.json' --sha256 '<result-sha256>' --json
```

Use a separate `identify` plan/run first to obtain the unit's self-reported
identity. Identify intentionally takes no serial/firmware arguments. An identity
reply is not a cryptographic device or DUT identity proof: TCP has no TLS/auth.
No instrument commands are issued by plan generation, inspect-plan or inspect.

## Transfer, timing and cleanup

Capture selects WORD, LSB, start=0, interval=1, requested point count and waveform
source. These **transfer settings remain changed**. Limits are 16-16,384 points
per selected channel, 1-4 ascending unique channels, 0.1-5 s per transaction and
5-60 s main operation. Cleanup has up to 3 s; the parent watchdog allows 8 s after
the main budget before killing a stuck worker. Parent failure reports hardware
access/state unknown, preserves worker stdout/stderr and does not reconnect.
Timeouts include every fragment of one reply, not a fresh timeout per chunk.

The EN11G wire profile is an IEEE definite-length `#N<length>` block followed by
two LF bytes. Indefinite blocks, extra/missing terminators, oversized lengths,
partial responses, invalid descriptors and transfer readback mismatches fail.
This first implementation accepts only a 346-byte WAVEACE/WAVEDESC descriptor
with WORD/LSB, normal analog voltage and a supported timebase index. A larger
record can be partially downloaded; the payload must contain exactly the
requested number of WORD points. Sequence, math, digital/logic and FFT data are
outside this profile. No automatic alternative protocol is attempted.

Signed left-aligned 16-bit codes are kept intact; do not shift by ADC resolution.
Voltage is `(code * scale_without_probe / code_per_div - offset_without_probe) * probe`.
Time is `trigger_delay - timebase * 10 / 2 + index * sample_interval`. Probe is
applied once. CSV uses 12 significant digits; raw WORD data, full descriptor and
full precision JSON metadata remain authoritative. Time is instrument-relative,
not verified UTC. Four channels come from a stable stopped acquisition/count;
independent cross-channel timing/calibration is not certified.

Successful captures finish stopped and close/release only the owned socket and
local lease. A SINGLE failure attempts STOP once **only on a synchronized stream**.
An incomplete send/reply forbids more SCPI; cleanup/state may remain unknown.
No RUN, reset, autoset, force trigger, calibration, firmware/option modification,
AWG output, arbitrary SCPI or automatic repair is exposed. An immediate SINGLE
that stops with the same counter and no observed armed state conservatively
returns `FRESHNESS_UNCONFIRMED`; this can reject a legitimate very fast trigger.

## Evidence and later physical acceptance

Each run preserves plan, per-transaction TX/RX binary files (including partial
bytes), a hash/size-bound JSONL transcript, per-channel preamble/WORD/CSV files,
identity, settings, freshness basis, extrema/mean/time range, cleanup and result.
Offline inspect replays successful commands in order and re-derives metadata,
CSV and summaries from the native bytes. Failed results stay failed. File hashes
provide integrity against the supplied expected digest, not a trusted signature.

After the unit arrives: inspect IDN; test stopped C1 on a known low-voltage
calibration source; compare raw point count, voltage/time/probe scaling with the
front panel and vendor export; test four enabled inputs, SINGLE and no-trigger
timeout, then clean disconnect/partial receive. Save real firmware/version and
raw wire replies. Until then all test evidence is explicitly `test_double`,
`hardware_access=false`, `physical_device_verified=false`.

## Primary references

- [SDS800X HD official product](https://www.siglent.com/int/products-overview/sds800x-hd/)
- [Official EN11G programming guide](https://www.siglent.com/wp-content/uploads/2024/09/SDS800XHD_Series_ProgrammingGuide_EN11G.pdf), downloaded 2026-10-21; SHA-256 `9063254852074c8476e93f434d20ca43fc8e926167c13d410a9f21e68bdc43cf`.
  Printed pp.24 (socket), 29 (IDN), 42 (counter), 51-61 (channels), 482-484
  (trigger), 747-759 (transfer/descriptor/scaling), 838-840 (Python conversion).
  The timebase table index 9 prints `200E-0`; use `200e-9` from the guide's
  Python enumeration, not that apparent printing error. Vendor PDF is not bundled.
- [SDS800X HD user manual](https://www.siglent.com/wp-content/uploads/2024/08/SDS800X_HD_UserManual_EN01A.pdf)
