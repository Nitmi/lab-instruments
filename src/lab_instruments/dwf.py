"""Small, explicit input-only WaveForms C ABI surface; no automatic DLL lookup."""

from __future__ import annotations

import ctypes as c
import math
import os
import struct
import time
from pathlib import Path

from .common import LabError, integer, number, read, require
from .plan import DEVICE_ID, SDK_VERSION

Int, U, D, B = c.c_int, c.c_uint, c.c_double, c.c_ubyte
PI, PU, PD, PB = c.POINTER(Int), c.POINTER(U), c.POINTER(D), c.POINTER(B)
SIGNATURES = {
    "FDwfGetVersion": [c.POINTER(c.c_char)],
    "FDwfGetLastErrorMsg": [c.POINTER(c.c_char)],
    "FDwfEnum": [Int, PI],
    "FDwfEnumSN": [Int, c.POINTER(c.c_char)],
    "FDwfEnumDeviceName": [Int, c.POINTER(c.c_char)],
    "FDwfEnumDeviceType": [Int, PI, PI],
    "FDwfEnumDeviceIsOpened": [Int, PI],
    "FDwfDeviceOpenEx": [c.c_char_p, PI],
    "FDwfDeviceClose": [Int],
    "FDwfDeviceAutoConfigureSet": [Int, Int],
    "FDwfAnalogInChannelCount": [Int, PI],
    "FDwfAnalogInFrequencyInfo": [Int, PD, PD],
    "FDwfAnalogInFrequencySet": [Int, D],
    "FDwfAnalogInFrequencyGet": [Int, PD],
    "FDwfAnalogInBufferSizeInfo": [Int, PI, PI],
    "FDwfAnalogInBufferSizeSet": [Int, Int],
    "FDwfAnalogInBufferSizeGet": [Int, PI],
    "FDwfAnalogInChannelEnableSet": [Int, Int, Int],
    "FDwfAnalogInChannelRangeSet": [Int, Int, D],
    "FDwfAnalogInChannelRangeGet": [Int, Int, PD],
    "FDwfAnalogInChannelOffsetSet": [Int, Int, D],
    "FDwfAnalogInChannelOffsetGet": [Int, Int, PD],
    "FDwfAnalogInChannelFilterSet": [Int, Int, Int],
    "FDwfAnalogInAcquisitionModeSet": [Int, Int],
    "FDwfAnalogInTriggerSourceSet": [Int, B],
    "FDwfAnalogInConfigure": [Int, Int, Int],
    "FDwfAnalogInStatus": [Int, Int, PB],
    "FDwfAnalogInStatusSamplesValid": [Int, PI],
    "FDwfAnalogInStatusData": [Int, Int, PD, Int],
    "FDwfDigitalInInternalClockInfo": [Int, PD],
    "FDwfDigitalInClockSourceSet": [Int, Int],
    "FDwfDigitalInBitsInfo": [Int, PI],
    "FDwfDigitalInDividerInfo": [Int, PU],
    "FDwfDigitalInDividerSet": [Int, U],
    "FDwfDigitalInDividerGet": [Int, PU],
    "FDwfDigitalInSampleFormatSet": [Int, Int],
    "FDwfDigitalInSampleFormatGet": [Int, PI],
    "FDwfDigitalInInputOrderSet": [Int, Int],
    "FDwfDigitalInBufferSizeInfo": [Int, PI],
    "FDwfDigitalInBufferSizeSet": [Int, Int],
    "FDwfDigitalInBufferSizeGet": [Int, PI],
    "FDwfDigitalInAcquisitionModeSet": [Int, Int],
    "FDwfDigitalInSampleModeSet": [Int, Int],
    "FDwfDigitalInTriggerSourceSet": [Int, B],
    "FDwfDigitalInTriggerPositionSet": [Int, U],
    "FDwfDigitalInConfigure": [Int, Int, Int],
    "FDwfDigitalInStatus": [Int, Int, PB],
    "FDwfDigitalInStatusSamplesValid": [Int, PI],
    "FDwfDigitalInStatusData": [Int, c.c_void_p, Int],
}


class Dwf:
    evidence_origin = "physical_instrument"

    def __init__(self, library):
        self.library = library
        self.handle = 0
        self.hardware_access = False
        self.calls = []
        for name, types in SIGNATURES.items():
            function = getattr(library, name)
            function.restype = Int
            function.argtypes = types
        self.version = self.string("FDwfGetVersion")
        require(
            self.version == SDK_VERSION,
            "SDK version must be " + SDK_VERSION,
            "SDK_VERSION_MISMATCH",
        )

    @classmethod
    def load(cls, path, sha256):
        require(Path(path).is_absolute(), "SDK path must be absolute")
        _, ref = read(path, maximum=64 * 1024 * 1024, expected=sha256)
        require(
            os.name == "nt" and c.sizeof(c.c_void_p) == 8,
            "initial native adapter supports 64-bit Windows",
            "UNSUPPORTED_HOST",
        )
        try:
            library = c.CDLL(ref["path"], winmode=0x1100)
            return cls(library), ref
        except (OSError, AttributeError) as error:
            raise LabError("SDK_UNAVAILABLE", str(error)[:512]) from error

    def call(self, name, *args):
        require(name in SIGNATURES, "SDK function is outside the input-only surface")
        self.calls.append(name)
        require(len(self.calls) <= 4000, "SDK call limit exceeded", "LIMIT_EXCEEDED")
        if getattr(self.library, name)(*args) != 1:
            message = c.create_string_buffer(512)
            self.library.FDwfGetLastErrorMsg(message)
            raise LabError(
                "SDK_CALL_FAILED", name + ": " + message.value.decode("utf-8", "replace")
            )

    def get(self, name, type_=Int, *args):
        value = type_()
        self.call(name, *args, c.byref(value))
        return value.value

    def string(self, name, *args):
        value = c.create_string_buffer(32)
        self.call(name, *args, value)
        return value.value.decode("ascii", "strict")

    def enumerate(self):
        self.hardware_access = self.evidence_origin == "physical_instrument"
        count = self.get("FDwfEnum", Int, 0x08000001)  # Type | USB; no discovery fallback.
        integer(count, "device count", 0, 64)
        devices = []
        for index in range(count):
            device_id, revision = Int(), Int()
            self.call("FDwfEnumDeviceType", index, c.byref(device_id), c.byref(revision))
            devices.append(
                {
                    "serial": self.string("FDwfEnumSN", index),
                    "name": self.string("FDwfEnumDeviceName", index),
                    "device_id": device_id.value,
                    "revision": revision.value,
                    "already_open": bool(self.get("FDwfEnumDeviceIsOpened", Int, index)),
                }
            )
        return devices

    def open(self, serial):
        matches = [device for device in self.enumerate() if device["serial"] == serial]
        require(len(matches) == 1, "exact serial must match exactly one device", "SELECTION_FAILED")
        selected = matches[0]
        require(
            selected["device_id"] == DEVICE_ID, "selected device is not AD3", "SELECTION_FAILED"
        )
        require(not selected["already_open"], "selected device is busy", "DEVICE_BUSY")
        value = Int()
        try:
            # Serial selection avoids opening a different enumeration index after a hotplug.
            # OpenEx performs its own vendor-defined internal enumeration.
            self.call("FDwfDeviceOpenEx", ("sn:" + serial + "\nconfig:0").encode(), c.byref(value))
        finally:
            self.handle = value.value
        require(self.handle > 0, "SDK returned an invalid handle", "SDK_CALL_FAILED")
        self.call("FDwfDeviceAutoConfigureSet", self.handle, 0)
        return selected

    def configure(self, plan):
        h, n, requested = self.handle, plan["samples"], plan["sample_rate_hz"]
        if plan["instrument"] == "scope":
            require(self.get("FDwfAnalogInChannelCount", Int, h) == 2, "AD3 channel count differs")
            low, high = Int(), Int()
            self.call("FDwfAnalogInBufferSizeInfo", h, c.byref(low), c.byref(high))
            require(
                low.value <= n <= high.value,
                "samples exceed active hardware buffer",
                "CAPABILITY_UNAVAILABLE",
            )
            hz_low, hz_high = D(), D()
            self.call("FDwfAnalogInFrequencyInfo", h, c.byref(hz_low), c.byref(hz_high))
            require(
                hz_low.value <= requested <= hz_high.value,
                "sample rate outside hardware range",
                "CAPABILITY_UNAVAILABLE",
            )
            for ch in (0, 1):
                self.call("FDwfAnalogInChannelEnableSet", h, ch, int(ch + 1 in plan["channels"]))
                if ch + 1 in plan["channels"]:
                    self.call("FDwfAnalogInChannelRangeSet", h, ch, plan["range_v"])
                    self.call("FDwfAnalogInChannelOffsetSet", h, ch, 0.0)
                    self.call("FDwfAnalogInChannelFilterSet", h, ch, 0)  # Decimate.
            self.call("FDwfAnalogInFrequencySet", h, requested)
            self.call("FDwfAnalogInBufferSizeSet", h, n)
            self.call("FDwfAnalogInAcquisitionModeSet", h, 0)  # Single.
            self.call("FDwfAnalogInTriggerSourceSet", h, 0)  # Immediate/none.
            self.call("FDwfAnalogInConfigure", h, 1, 0)
            actual = self.get("FDwfAnalogInFrequencyGet", D, h)
            actual_size = self.get("FDwfAnalogInBufferSizeGet", Int, h)
            scales = {}
            for ch in plan["channels"]:
                range_v = self.get("FDwfAnalogInChannelRangeGet", D, h, ch - 1)
                offset_v = self.get("FDwfAnalogInChannelOffsetGet", D, h, ch - 1)
                number(range_v, "actual range", 1, 50)
                require(
                    math.isfinite(offset_v) and abs(offset_v) < 1e-9,
                    "unexpected channel offset",
                    "READBACK_MISMATCH",
                )
                scales[str(ch)] = {"range_v": range_v, "offset_v": offset_v}
        else:
            clock = self.get("FDwfDigitalInInternalClockInfo", D, h)
            number(clock, "internal clock", 1000, 125_000_000)
            require(self.get("FDwfDigitalInBitsInfo", Int, h) >= 16, "16-bit input unavailable")
            require(
                n <= self.get("FDwfDigitalInBufferSizeInfo", Int, h),
                "samples exceed active hardware buffer",
                "CAPABILITY_UNAVAILABLE",
            )
            divider = max(1, math.ceil(clock / requested))
            require(
                divider <= self.get("FDwfDigitalInDividerInfo", U, h),
                "divider unavailable",
                "CAPABILITY_UNAVAILABLE",
            )
            self.call("FDwfDigitalInDividerSet", h, divider)
            self.call("FDwfDigitalInClockSourceSet", h, 0)  # Internal clock.
            self.call("FDwfDigitalInSampleFormatSet", h, 16)
            self.call("FDwfDigitalInInputOrderSet", h, 1)  # DIO first.
            self.call("FDwfDigitalInSampleModeSet", h, 0)  # Simple, not compressed/noise.
            self.call("FDwfDigitalInBufferSizeSet", h, n)
            self.call("FDwfDigitalInAcquisitionModeSet", h, 0)
            self.call("FDwfDigitalInTriggerSourceSet", h, 0)
            self.call("FDwfDigitalInTriggerPositionSet", h, n)
            self.call("FDwfDigitalInConfigure", h, 1, 0)
            actual_divider = self.get("FDwfDigitalInDividerGet", U, h)
            require(
                actual_divider == divider
                and self.get("FDwfDigitalInSampleFormatGet", Int, h) == 16,
                "digital readback differs",
                "READBACK_MISMATCH",
            )
            actual = clock / actual_divider
            actual_size = self.get("FDwfDigitalInBufferSizeGet", Int, h)
            scales = {}
        number(actual, "actual sample rate", 1, 125_000_000)
        require(
            actual <= requested * (1 + 1e-9) and actual_size == n,
            "rate or sample-count readback exceeds plan",
            "READBACK_MISMATCH",
        )
        return {
            "sample_rate_hz": actual,
            "samples": actual_size,
            "channel_scales": scales,
            "rate_policy": "at_most_requested",
            "trigger": "immediate",
        }

    def acquire(self, plan, deadline):
        prefix = "FDwfAnalogIn" if plan["instrument"] == "scope" else "FDwfDigitalIn"
        h = self.handle
        self.call(prefix + "Configure", h, 0, 1)
        polls = 0
        while True:
            require(time.monotonic() < deadline, "acquisition timed out", "CAPTURE_TIMEOUT")
            state = self.get(prefix + "Status", B, h, 1)
            polls += 1
            if state == 2:  # DwfStateDone.
                break
            require(polls < 3000, "poll limit exceeded", "LIMIT_EXCEEDED")
            time.sleep(0.01)
        count = self.get(prefix + "StatusSamplesValid", Int, h)
        require(count == plan["samples"], "incomplete acquisition", "INCOMPLETE_CAPTURE")
        if plan["instrument"] == "scope":
            chunks = []
            for channel in plan["channels"]:
                samples = (D * count)()
                self.call("FDwfAnalogInStatusData", h, channel - 1, samples, count)
                require(all(math.isfinite(value) for value in samples), "nonfinite voltage data")
                chunks.append(struct.pack("<" + "d" * count, *samples))
            return b"".join(chunks), "f64le_channel_major"
        samples = (c.c_uint16 * count)()
        self.call("FDwfDigitalInStatusData", h, samples, count * 2)  # API size is BYTES.
        return struct.pack("<" + "H" * count, *samples), "u16le_dio0_first"

    def stop(self, instrument):
        prefix = "FDwfAnalogIn" if instrument == "scope" else "FDwfDigitalIn"
        self.call(prefix + "Configure", self.handle, 0, 0)

    def close(self):
        self.call("FDwfDeviceClose", self.handle)
        self.handle = 0
