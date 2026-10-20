from __future__ import annotations

import ctypes as c

import pytest

from lab_instruments.dwf import SIGNATURES, Dwf


class FakeLibrary:
    """Real ctypes C-callable signatures with synthetic data, never a device."""

    def __init__(self):
        self.calls = []
        self.fail = None
        self.device_id = 10
        self.busy = False
        self.devices = 1
        self.version = b"3.25.1"
        self.rate = 1_000_000.0
        self.samples = 4096
        self.divider = 125
        self.max_buffer = 32768
        self.state = 2
        self.valid_samples = None
        self.nan = False
        self.close_failed = False
        self.rate_readback = None
        self.callbacks = []
        for name, parameters in SIGNATURES.items():

            def callback(*args, name=name):
                return self.dispatch(name, args)

            function = c.CFUNCTYPE(c.c_int, *parameters)(callback)
            self.callbacks.append(function)
            setattr(self, name, function)

    def dispatch(self, name, args):
        self.calls.append((name, args))
        if name == self.fail or (name == "FDwfDeviceClose" and self.close_failed):
            return 0
        if name in ("FDwfGetVersion", "FDwfGetLastErrorMsg", "FDwfEnumSN", "FDwfEnumDeviceName"):
            text = {
                "FDwfGetVersion": self.version,
                "FDwfGetLastErrorMsg": b"synthetic SDK failure",
                "FDwfEnumSN": b"210321ABC",
                "FDwfEnumDeviceName": b"Analog Discovery 3",
            }[name]
            c.memmove(args[-1], text + b"\0", len(text) + 1)
        elif name == "FDwfEnum":
            args[-1][0] = self.devices
        elif name == "FDwfEnumDeviceType":
            args[-2][0], args[-1][0] = self.device_id, 1
        elif name == "FDwfEnumDeviceIsOpened":
            args[-1][0] = int(self.busy)
        elif name == "FDwfDeviceOpenEx":
            args[-1][0] = 27
        elif name == "FDwfAnalogInChannelCount":
            args[-1][0] = 2
        elif name == "FDwfAnalogInFrequencyInfo":
            args[-2][0], args[-1][0] = 1.0, 125_000_000.0
        elif name == "FDwfAnalogInBufferSizeInfo":
            args[-2][0], args[-1][0] = 16, self.max_buffer
        elif name in ("FDwfAnalogInBufferSizeSet", "FDwfDigitalInBufferSizeSet"):
            self.samples = args[-1]
        elif name == "FDwfAnalogInFrequencySet":
            self.rate = args[-1]
        elif name == "FDwfDigitalInDividerSet":
            self.divider = args[-1]
        elif name in ("FDwfAnalogInBufferSizeGet", "FDwfDigitalInBufferSizeGet"):
            args[-1][0] = self.samples
        elif name in ("FDwfAnalogInStatus", "FDwfDigitalInStatus"):
            args[-1][0] = self.state
        elif name.endswith("StatusSamplesValid"):
            args[-1][0] = self.samples if self.valid_samples is None else self.valid_samples
        elif name == "FDwfAnalogInFrequencyGet":
            args[-1][0] = self.rate if self.rate_readback is None else self.rate_readback
        elif name == "FDwfAnalogInChannelRangeGet":
            args[-1][0] = 10.0
        elif name == "FDwfAnalogInChannelOffsetGet":
            args[-1][0] = 0.0
        elif name == "FDwfDigitalInInternalClockInfo":
            args[-1][0] = 125_000_000.0
        elif name in ("FDwfDigitalInBitsInfo", "FDwfDigitalInSampleFormatGet"):
            args[-1][0] = 16
        elif name == "FDwfDigitalInBufferSizeInfo":
            args[-1][0] = self.max_buffer
        elif name == "FDwfDigitalInDividerInfo":
            args[-1][0] = 100000
        elif name == "FDwfDigitalInDividerGet":
            args[-1][0] = self.divider
        elif name == "FDwfAnalogInStatusData":
            for index in range(args[-1]):
                args[-2][index] = float("nan") if self.nan else args[1] + index / 1000
        elif name == "FDwfDigitalInStatusData":
            pointer = c.cast(args[1], c.POINTER(c.c_uint16))
            for index in range(args[-1] // 2):
                pointer[index] = index % 2 | ((index % 3 == 0) << 15)
        return 1


class SyntheticDwf(Dwf):
    evidence_origin = "test_double"


@pytest.fixture
def sdk():
    return SyntheticDwf(FakeLibrary())


@pytest.fixture(autouse=True)
def no_settling_delay(monkeypatch):
    monkeypatch.setattr("lab_instruments.capture.time.sleep", lambda seconds: None)
