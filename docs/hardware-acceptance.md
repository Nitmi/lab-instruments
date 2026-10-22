# Hardware acceptance still required

Software tests and the mock C ABI do not establish vendor DLL, USB driver or
measurement behavior. The first real AD3 test must keep these statuses separate.

1. Verify trusted x64 WaveForms 3.25.1/Adept installation and exact DLL hash.
   Static doctor does not execute the DLL. Record initial device/wiring state.
2. Explicitly enumerate USB devices and record the exact serial/model/revision.
   Use a dedicated freshly initialized AD3, close competing instrument applications.
3. Plan one bounded input capture using that serial. First observe known safe
   external references; do not implicitly start AWG, pattern, supply or DIO output.
4. Scope: check a known DC voltage against the SDK volts and expected range,
   then a known periodic signal against sample-rate/readback. Record reference
   instrument/source uncertainty. This is a functional check, not a calibration.
5. Logic: compare known transitions against DIO bit order and byte count, exercise
   a non-exact divider rate, and confirm analysis uses actual rate. Input-only
   capture cannot confirm analog signal integrity or voltage compliance.
   For offline UART/PWM, use known external serial bytes and pulse references,
   including configured parity, polarity, complete/truncated windows and a
   noninteger sample/baud ratio. Compare source bytes and frequency/duty against
   independent references; synthetic decoder tests do not prove physical accuracy.
   I2C: compare known address/data, repeated START and target/controller ACK/NACK
   with an independent reference; SPI: compare CS-framed MOSI/MISO and configured
   polarity/phase/order, plus deliberate truncated/undersampled captures. A
   successful decoded structure does not prove application success or pin timing.
6. Exercise ordinary acquisition timeout, SDK errors and cleanup; use a controlled
   disconnect only when separately appropriate. Require native stop/close evidence
   for successful cleanup; a killed worker leaves instrument state unknown.
7. Preserve hashes, complete raw samples, journal and supervisor exit. Inspect
   offline and compare sample values independently. Report software, SDK and
   physical results separately. Never turn a failed/partial capture into a pass.

Future repeated measurements reuse the user's bounded task authorization. No
per-capture approval prompt is added. Any future stimulus/output capability must
declare its exact channels, electrical ranges, duration and final state separately.
