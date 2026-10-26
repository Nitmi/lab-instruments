# SIGLENT SDL1020X-E adapter (0.2.0 candidate)

Status: implemented against the official E02B programming guide and a local TCP
protocol double. **No SDL1020X-E hardware or electrical behavior has been tested.**
This adapter is separate from the existing input-only Digilent AD3 adapter.

The selected instrument supports raw SCPI TCP port 5025. No NI-VISA, PyVISA,
vendor DLL, USB driver or persistent PATH change is needed for this LAN adapter.
Use a dedicated, trusted instrument/network: SCPI identity is self-reported and
has no TLS authentication. There is no subnet scan, DNS resolution or device
fallback. One session opens one literal IPv4 endpoint, matches model, serial and
firmware exactly, and never reconnects or retries a hardware command.

## Operations

All actions use an offline plan followed by explicit `load run`. There are no
interactive approval prompts. The agent reuses the user's existing test/task
scope; hashes bind inputs and do not establish permission to energize a DUT.

- `identify`: exactly one `*IDN?`, then close; the response can supply the serial/
  firmware for a later plan. Unsupported models fail without sending controls.
- `observe`: identity match, then bounded voltage/current/power and input-state
  queries. It sends no configuration or OFF and leaves an existing load alone.
- `cc`: require input OFF, short OFF, internal control, external input OFF and
  remote sense OFF. Observe the unloaded baseline, select static CC/5 A/36 V
  ranges and exact whole-mA setpoint, synchronize/read back, recheck preconditions,
  then enable once. Record loaded measurements and finish with OFF/readback.
- `off`: match exact identity, send OFF once, check operation completion and
  reported input state. This can stop a previously active instrument.

First-profile CC setpoints are **0.010–5.000 A**, with a **24 V / 5 A / 50 W**
maximum software envelope (below the instrument's 150 V/30 A/200 W rating).
The user selects lower task limits as needed; setpoint × maximum task voltage
must fit the task power bound. Samples: 1–50, intervals 100–1000 ms, planned
duration samples × interval ≤30 s. CC starts sampling 50 ms after ON readback.
Current agreement uses max(2 mA, 2% of setpoint), an application assertion, not a
calibration or manufacturer accuracy claim. Below 10 mA needs a future profile.

The active deadline starts before sending ON, includes its readback and all
samples, and triggers cleanup at the duration limit. Main connection/setup time
is separately bounded; each I/O uses the smaller transaction/overall deadline.
Cleanup gets at most 3 additional seconds. Host scheduling and instrument/network
latency mean this is not a hard real-time cutoff or autonomous protection.

## Example workflow after purchasing

Replace the documentation-only IP below with the instrument's actual LAN IP.
Plan and inspect remain offline. Only `load run` connects.

```powershell
lab-instruments load plan --action identify --host 192.0.2.10 --output id-plan.json --json
lab-instruments load inspect-plan id-plan.json --sha256 <id-plan-sha256> --json
lab-instruments load run --plan id-plan.json --sha256 <id-plan-sha256> --output id-run --json
```

Record the exact observed model, serial and firmware. For a 100 mA / one-second
test, replace the identity placeholders and use:

```powershell
lab-instruments load plan --action cc --host 192.0.2.10 --serial <serial> --firmware-version <firmware> --current-a 0.100 --min-voltage-v 4.5 --max-voltage-v 5.5 --max-current-a 0.150 --max-power-w 1 --samples 5 --interval-ms 200 --output cc-plan.json --json
lab-instruments load run --plan cc-plan.json --sha256 <cc-plan-sha256> --output cc-run --json
lab-instruments load inspect cc-run/load-result.json --sha256 <result-sha256> --json
```

`--action observe` omits `--current-a`; `--action off` uses the same exact identity.
Each file/run directory must be new, with an existing parent. A stale digest,
extra field, malformed limit or existing destination fails before TCP access.

## Stop, uncertainty and ownership

On the first main-operation failure or Ctrl+C, an owned CC operation attempts
one OFF. Cleanup is a declared operation, not a retry of ON/configuration. If a
query timed out or a send was partial, the stream is desynchronized: the adapter
sends a leading LF plus OFF and closes without accepting potentially stale
replies as proof of shutdown. It records `instrument_state=unknown` and fails.
It never reconnects, clears the error queue, resets the instrument or reenables.
An OFF send returning successfully proves bytes were transmitted, not that the
instrument applied them. Only a synchronized completion/input-state readback
allows `input_off_confirmed=true`, still a reported instrument state.

Loss of the host/network or forced process termination can leave the load ON.
Use the instrument's own protections and an independently reachable physical OFF
for the wiring/DUT under test. The adapter does not change or certify those
protections, infer safe wiring, emulate an interlock or restore prior settings.
Software voltage/current/power checks occur after measurements and cannot stop
an electrical transient before it occurs. No short-circuit, battery, dynamic,
list/program, OCP/OPP test, firmware update, power source or generic SCPI API is
exposed. Static CC leaves its configured function/ranges/setpoint in place, OFF.

A cooperative local OS file lock prevents this CLI from simultaneously taking
the same serial on this host. It is not a physical instrument lock: another PC,
application, front-panel operator or external controller is outside its scope.
The tiny lock file remains in the OS temporary directory; no lease is inferred
from its mere existence, and the kernel lock is released when the handle closes.

## Evidence and limitations

Local 0.10.0 adds [saved measurement assertions and JUnit](load-checks.md).
These offline commands replay saved bytes and never invoke `load run`.

`plan.json`, `transcript.jsonl` and `load-result.json` preserve input bytes,
commands, raw TX/RX fragments (base64), host UTC/monotonic windows, partial/errors,
samples, effects and cleanup. Measurements are three separate queries, not
simultaneous instrument ADC samples; no sample-rate, timing, calibration or DUT
identity verification is implied. The inspector verifies transcript byte hashes
and rederives successful report identity/measurements/last OFF replies from raw
SCPI bytes, without opening any endpoint or following original input paths.

`--test-double` plans are restricted to 127.0.0.1 and label results `test_double`,
`hardware_access=false`, `physical_device_verified=false`. This assumes the named
endpoint is a local protocol test server, not a bridge to real equipment. Real
sessions refuse loopback and keep physical/DUT/calibration/ownership verification
false; matching a self-reported IDN is recorded separately. Integrity hashes are
not signatures or independent proof that measurements came from real hardware.

E02B has a copy/paste error in the current-protection-state query table. This
adapter does not guess that query or introduce undocumented `SYST:ERR?` commands.
`*OPC?` plus settings readback verifies the required subset; it is not a claim
that an instrument error queue or every concurrent front-panel state was audited.
Firmware-specific behavior still needs the acceptance below.

## Physical acceptance once equipment is available

1. Inspect the official ratings/wiring, terminal polarity, cooling, local OFF
   and protections; use a dedicated current-limited low-voltage source.
2. Run identify once and bind the actual serial/firmware. Read-only observe once
   with input OFF, compare instrument display and an independent meter.
3. Plan one low-energy 100 mA run in the actual source envelope; inspect its
   exact configuration commands, preconditions, readings and OFF cleanup.
4. Run once, compare CC current and voltage with independent measurement,
   confirm the physical input is OFF, then archive evidence. Do not automatically
   retry an uncertain state-changing operation or test network loss on an
   unprotected DUT. Only after these checks mark that exact setup qualified.

Official references:

- [Programming guide E02B](https://siglentna.com/wp-content/uploads/dlm_uploads/2023/10/SDL1000X_programming_guide_E02B.pdf), SHA-256 `bd3ec6896e35b1a59da11f9adef408fb4dd308d0e83724bd740d276ac92d1ba3`;
  printed pp.9–15, 51 and 53–55 cover this subset.
- [SIGLENT socket support table](https://siglentna.com/operating-tip/instrument-socket-and-telnet-port-information/).
- [SDL1000X release notes](https://www.siglent.com/products-overview/sdl1000x/) document socket support since 1.1.1.21. Do not auto-upgrade firmware.
