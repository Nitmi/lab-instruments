# PureThermal 3 + radiometric Lepton 3.5

This adapter implements Windows Media Foundation Y16 acquisition, fixed UVC
extension-unit GET readbacks and offline ROI analysis. It is verified with saved
synthetic frames and an explicit native test backend. **No physical PureThermal
camera, USB driver behavior, firmware ABI or temperature accuracy is verified.**

For already saved captures, version 0.11 adds separate
[thermal assertions and CI reports](thermal-checks.md), without camera access.

The initial profile is Lepton 3.5 part `500-0771-01`, native `Y16 `, 160x122,
tightly packed top-down stride 320: 160x120 image followed by 320 telemetry words.
No RGB/YUYV conversion, grayscale-to-temperature guess, alternate layout fallback
or OpenCV camera index is used. Units require matching control readbacks and
frame telemetry. Other firmware layouts, telemetry revisions or missing controls
must be assessed on actual equipment before expanding the profile.

## Commands and installation

The dependency-free Python package adds `thermal plan`, `inspect-plan`, `run`,
`import` and `inspect`. Windows capture also requires the separate x64 helper
`lab-pt3-helper.exe`, built from `native/pt3_capture.cpp`. It uses system Media
Foundation/UVC, WRL and a statically linked CRT; no VISA/libusb replacement or
camera driver installation is performed. The wheel is portable for offline work;
the native helper is Windows x64 only and is not hidden inside a portable wheel.

To build explicitly with an existing Visual Studio C++ installation/Windows SDK:

```powershell
python scripts/build_thermal_helper.py --visual-studio 'C:\Program Files\Microsoft Visual Studio\2022\Community' --output '<new-build-directory>'
```

No build, helper discovery or camera access occurs in default doctor, plan,
inspect-plan, import or inspect. `run` requires one absolute trusted helper path
and its lowercase SHA-256, verified before process launch. The hash binds bytes;
it does not authenticate an unknown helper or authorize hardware by itself.
Use the component's supplied helper, not another executable. No shell is used
for capture. SDK/compiler processes run only in the explicit development build.

Discovery itself uses a plan/run pair:

```powershell
lab-instruments thermal plan --action devices --output devices-plan.json --json
lab-instruments thermal run --plan devices-plan.json --plan-sha256 '<plan-sha256>' --helper '<absolute-helper.exe>' --helper-sha256 '<helper-sha256>' --output '<new-run-directory>' --json
```

This enumerates camera interfaces and returns only PureThermal VID/PID
`1e4e:0100` links; it activates no camera. Copy one complete symbolic link into
an `identify` plan (`--action identify --link '<exact-link>'`). Identify activates
only that interface, GETs part, raw 8-byte Lepton serial and 8-byte Lepton software
revision, then closes. It does not negotiate/start video or issue an explicit SET.
PureThermal generations share USB IDs: a link/Lepton part alone does not prove
that the carrier is PT3. Preserve the physical carrier identity separately.

After identity observation, capture uses the same link and exact raw serial/
software bytes (16 lowercase hex digits each). These are Lepton identifiers,
not a verified PureThermal carrier firmware revision or friendly camera name.

```powershell
lab-instruments thermal plan --link '<exact-link>' --serial-hex '<16-hex-digits>' --software-hex '<16-hex-digits>' --buffers 3 --roi cpu 10 10 20 20 --distance-m 0.25 --ambient-c 25 --output capture-plan.json --json
lab-instruments thermal run --plan capture-plan.json --plan-sha256 '<plan-sha256>' --helper '<absolute-helper.exe>' --helper-sha256 '<helper-sha256>' --output '<new-capture-directory>' --json
lab-instruments thermal inspect '<capture-directory>\thermal-result.json' --sha256 '<result-sha256>' --json
```

ROIs are NAME X Y WIDTH HEIGHT with top-left origin, half-open pixel ranges;
up to 8 unique names, inside 160x120. Default is the full image. Small ROI is a
window-area flag, not proof that a small component covers enough sensor pixels.
Distance/ambient values are user context, never claimed measured or calibrated.

No per-capture interactive approval is added. An agent reuses the agreed task
scope, generates the native typed request and uses new output directories.
Input hashes are not manual approval codes. Each capture owns one exact interface
lease and one helper process; another host/front-panel app is not excluded by
this cooperative local lease.

## State, limits and cleanup

All non-offline actions enumerate Media Foundation interface metadata to locate
the exact requested link. Capture checks the selected Lepton identity **before**
streaming. Native 160x122 Y16 negotiation can cause PureThermal firmware to
change AGC, telemetry, output format and TLinear-related state. These are explicit
plan effects. One declared startup buffer is preserved as `warmup.y16`; it allows
the firmware's negotiated mode to take effect before reading controls. It is not
a retry or an asserted valid temperature frame.

Fixed GETs cover identity, AGC, radiometry, TLinear enable/resolution/auto-resolution,
flux parameters, telemetry enable/location, FFC status, gain mode and shutter.
Windows' 31-control split uses GUID `pt1-lep-rad20000` for RAD2; TLinear controls
use selectors 18/19/20, flux parameters 17. No arbitrary GET selector, UVC SET,
explicit FFC, shutter motion, reset, firmware update or power/output API is exposed.

Temperature analysis requires AGC off, radiometry/TLinear on, fixed 0.01/0.1 K
resolution, footer telemetry and ready FFC control. The full readback set must
remain unchanged before/after the bounded series, including identity and flux
parameters. Frame telemetry must agree on resolution/gain, report completed FFC,
and have no FFC request, gain-change request, AGC, shutter lockout or imminent
overtemperature flag. Closed/unknown shutter control is rejected.

Capture is 1-8 buffers plus one startup buffer, total 5-60 s deadline including
opening/readbacks/cleanup. The parent kills a stuck worker once, preserves partial
files/stdout/stderr and reports unknown camera cleanup; it does not reconnect.
Normal cleanup releases reader/KS interfaces, shuts down the selected media source
and activation, then Media Foundation/COM. It does not restore sensor settings,
reset the camera, alter a driver or close another application's camera handles.

## Interpretation and evidence

Raw uint16 little-endian values convert to apparent surface °C as
`code / 100 - 273.15` or `code / 10 - 273.15`, according to the matching TLinear
state. Resolution is not measurement accuracy. Zero/65535 values and samples
outside this profile's nominal gain interval (-10..140°C high, -10..400°C low)
are marked unusable, retained in raw/CSV, and excluded from ROI statistics.
An ROI with no usable pixels fails; no missing-value interpolation is performed.

Each frame records control/telemetry values, sensor counters/FFC time/gain,
ROI min/max/mean/p95, hotspot coordinates, usable-pixel mask hash and CSV.
Repeated identical frame counters/bytes are deduplicated for trends. Counter
reversal/reset/wrap, or equal counters with different bytes, fail. An elapsed
sensor-time temperature delta requires at least two unique frames and stable
gain, resolution, FFC time and valid-pixel masks. Otherwise trend support is false.
Host receipt time, Media Foundation timestamps and sensor uptime remain distinct;
none is a verified UTC sensor origin. No chip junction temperature is inferred.

Read flux parameters are saved: emissivity, background, window/atmosphere
transmission and temperatures/reflection. They are not reapplied as a second
correction. Their appropriateness for the actual scene, physical emissivity,
distance/ambient values, lens/window, calibration, DUT and absolute temperature
accuracy remain unverified. FLIR describes typical high-gain module accuracy of
±5°C or 5% under specified conditions; 0.01 K quantization is not ±0.01°C accuracy.

Each run saves plan, helper identity, raw GET payloads, unconverted startup/frame
buffers, native completion/cleanup, hash-bound `native/source.json`, derived CSV
and result inventory. GET payload files are not a USB bus trace. A successful
offline inspect verifies all files and regenerates analysis/CSV without camera,
native helper, network or another executable. Failed/partial runs stay failed.

To re-evaluate a source bundle offline into a new directory:

```powershell
lab-instruments thermal import --input '<capture>\native\source.json' --sha256 '<source-sha256>' --plan capture-plan.json --plan-sha256 '<plan-sha256>' --output '<new-evaluation-directory>' --json
```

Saved/external metadata can be fabricated. Hashes bind the supplied evidence,
not its physical authenticity; imported evidence never grants hardware authority
or establishes physical camera/DUT/calibration identity.

## Development and hardware acceptance

`--test-double` permits only `SYNTHETIC-PT3`; native code branches before any
Media Foundation/COM initialization or camera enumeration. Synthetic fault
injection uses `LAB_PT3_TEST_FAULT=agc|ffc` only in that explicit backend. It never
affects real mode. This tests executable orchestration and formats, not UVC ABI.

When the camera arrives: verify actual carrier, USB link, Lepton part/serial/
software, advertised native Y16/stride/telemetry dimensions and XU GET lengths;
compare two fixed TLinear scales with a known source/vendor application; check
telemetry word order, FFC/shutter states, duplicate counters and cleanup. Then
exercise a stable low-temperature scene/ROI, mode drift and a bounded disconnect.
Do not silently replace the driver, firmware or raw format to pass a test.

Primary sources inspected on 2026-10-21:

- [GroupGets Windows capture example](https://github.com/groupgets/purethermal1-uvc-capture/tree/18209a3da11775357d665d94dfc40148a85e2aa6/mediafoundation), reference API/XU/160x122 profile.
- [GroupGets firmware](https://github.com/groupgets/purethermal1-firmware/tree/308175d2b72a5c95922118445a31f403a64b7b30), UVC GUIDs/control split and negotiation effects. This revision is reference material, not asserted installed firmware.
- [FLIR Lepton software IDD Rev 303](https://flir.netx.net/file/asset/12411/original/attachment), TLinear enable/resolution and GET definitions; PDF SHA-256 `95100c00578d0af30abcf831cadcd456be08fed8a2566317ac3cb6a4fb73dfeb`.
- [FLIR Lepton datasheet Rev 400](https://flir.netx.net/file/asset/13333/original/attachment), telemetry Tables 2/3 and radiometry; PDF SHA-256 `74f9d92c84163cebaa1b89a190afbdbb5b468cbee38c38b0a57e2cf98f2c2ad5`.
- [FLIR radiometry explanation](https://oem.flir.com/learn/thermal-integration-made-easy/radiometry-with-lepton/).
- [Microsoft Source Reader](https://learn.microsoft.com/en-us/windows/win32/medfound/using-the-source-reader-to-process-media-data) / [capture device enumeration](https://learn.microsoft.com/en-us/windows/win32/medfound/enumerating-video-capture-devices).

Vendor source is consulted for interoperability; it is not copied/bundled or
executed. The native helper is an independent Windows SDK implementation.
