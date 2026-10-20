from __future__ import annotations

import base64
import copy
import ctypes
import json
import struct
import time
from pathlib import Path

import pytest

from lab_instruments.capture import inspect_capture, perform
from lab_instruments.common import LabError, digest, encode, write_new
from lab_instruments.dwf import SIGNATURES, Dwf
from lab_instruments.plan import create


def run_capture(tmp_path, sdk, instrument="scope", channels=None):
    plan = create(
        "210321ABC",
        instrument,
        channels or ([1, 2] if instrument == "scope" else [0, 15]),
        samples=16,
    )
    return perform(plan, sdk, tmp_path, {"sha256": digest(encode(plan))}, {"test_double": True})


@pytest.mark.parametrize("instrument", ["scope", "logic"])
def test_c_abi_capture_and_offline_inspection(tmp_path, sdk, instrument):
    result, ref = run_capture(tmp_path, sdk, instrument)
    assert result["ok"] and result["cleanup"]["complete"]
    assert result["evidence_origin"] == "test_double" and not result["hardware_access"]
    assert not result["physical_device_verified"] and not result["calibration_verified"]
    payload = result["payload"]
    raw = base64.b64decode(payload["raw_base64"], validate=True)
    assert digest(raw) == payload["sha256"]
    names = [name for name, args in sdk.library.calls]
    assert names.count("FDwfDeviceOpenEx") == names.count("FDwfDeviceClose") == 1
    open_args = next(args for name, args in sdk.library.calls if name == "FDwfDeviceOpenEx")
    assert open_args[0] == b"sn:210321ABC\nconfig:0"
    if instrument == "scope":
        assert len(raw) == 16 * 2 * 8
        values = struct.unpack("<32d", raw)
        assert values[0] == 0 and values[16] == 1
    else:
        assert len(raw) == 16 * 2
        call = next(args for name, args in sdk.library.calls if name == "FDwfDigitalInStatusData")
        assert call[-1] == 32  # SDK argument is bytes, not samples.
        assert struct.unpack("<16H", raw)[0] == 0x8000
    # Saved inspection must not load the SDK or read either provenance path.
    inspected = inspect_capture(Path(ref["path"]), ref["sha256"])
    assert inspected["payload_length"] == len(raw) and not inspected["hardware_access"]
    if instrument == "logic":
        assert inspected["digital_transitions"]["0"] == 15
    else:
        assert inspected["channel_statistics"]["2"]["maximum_v"] == 1.015


@pytest.mark.parametrize("change", ["missing", "duplicate", "wrong-model", "busy"])
def test_selection_never_opens_fallback(tmp_path, sdk, change):
    if change == "missing":
        sdk.library.devices = 0
    elif change == "duplicate":
        sdk.library.devices = 2
    elif change == "wrong-model":
        sdk.library.device_id = 4
    else:
        sdk.library.busy = True
    result, _ = run_capture(tmp_path, sdk)
    assert not result["ok"] and result["cleanup"]["complete"]
    assert not any(name == "FDwfDeviceOpenEx" for name, args in sdk.library.calls)


@pytest.mark.parametrize(
    "failure", ["FDwfAnalogInFrequencySet", "FDwfAnalogInStatus", "FDwfAnalogInStatusData"]
)
def test_sdk_failure_is_not_retried_and_owned_handle_is_closed(tmp_path, sdk, failure):
    sdk.library.fail = failure
    result, _ = run_capture(tmp_path, sdk)
    assert not result["ok"] and result["error"]["code"] == "SDK_CALL_FAILED"
    names = [name for name, args in sdk.library.calls]
    assert names.count(failure) == 1 and names.count("FDwfDeviceClose") == 1
    assert result["cleanup"]["owned_handle_closed"] is True


def test_close_failure_preserves_data_but_cannot_pass(tmp_path, sdk):
    sdk.library.close_failed = True
    result, ref = run_capture(tmp_path, sdk)
    assert "payload" in result and not result["ok"]
    assert not result["cleanup"]["complete"] and not result["cleanup"]["owned_handle_closed"]
    assert inspect_capture(Path(ref["path"]), ref["sha256"])["status"] == "failed"


@pytest.mark.parametrize("failure", ["buffer", "readback", "incomplete", "nan"])
def test_capabilities_and_data_are_checked(tmp_path, sdk, failure):
    if failure == "buffer":
        sdk.library.max_buffer = 8
    elif failure == "readback":
        sdk.library.rate_readback = 2_000_000.0
    elif failure == "incomplete":
        sdk.library.valid_samples = 15
    else:
        sdk.library.nan = True
    result, _ = run_capture(tmp_path, sdk)
    assert not result["ok"] and "payload" not in result
    assert result["cleanup"]["owned_handle_closed"]


def test_digital_divider_uses_achieved_rate(sdk):
    sdk.open("210321ABC")
    settings = sdk.configure(create("210321ABC", "logic", [0], sample_rate_hz=10_000_000))
    assert sdk.library.divider == 13
    assert settings["sample_rate_hz"] == 125_000_000 / 13 < 10_000_000
    sdk.close()


def test_acquisition_deadline_is_checked(sdk):
    plan = create("210321ABC", "logic", [0])
    sdk.open(plan["serial"])
    sdk.configure(plan)
    with pytest.raises(LabError, match="timed out"):
        sdk.acquire(plan, time.monotonic() - 1)
    sdk.stop("logic")
    sdk.close()


def test_version_mismatch_stops_before_enumeration(sdk):
    sdk.library.version = b"3.20.1"
    sdk.library.calls.clear()
    with pytest.raises(LabError, match="SDK version"):
        Dwf(sdk.library)
    assert [name for name, args in sdk.library.calls] == ["FDwfGetVersion"]


def test_surface_and_exact_c_types_are_explicit():
    assert not any(
        word in name
        for name in SIGNATURES
        for word in ("AnalogOut", "AnalogIO", "DigitalOut", "DigitalIO", "Reset", "CloseAll")
    )
    assert SIGNATURES["FDwfDigitalInStatusData"][-1] is ctypes.c_int
    assert SIGNATURES["FDwfAnalogInStatus"][-1] == ctypes.POINTER(ctypes.c_ubyte)


@pytest.mark.parametrize("mutation", ["payload", "length", "nan", "channel", "cleanup"])
def test_changed_evidence_is_rejected_even_with_recomputed_file_hash(tmp_path, sdk, mutation):
    result, _ = run_capture(tmp_path, sdk)
    changed = copy.deepcopy(result)
    if mutation == "payload":
        changed["payload"]["raw_base64"] = "AAAA"
    elif mutation == "length":
        changed["payload"]["length"] += 1
    elif mutation == "channel":
        changed["payload"]["channels"] = [2]
    elif mutation == "cleanup":
        changed["cleanup"]["complete"] = False
    else:
        raw = struct.pack("<32d", *([float("inf")] * 32))
        changed["payload"].update(raw_base64=base64.b64encode(raw).decode(), sha256=digest(raw))
    path = tmp_path / "changed.json"
    ref = write_new(path, encode(changed))
    with pytest.raises(LabError):
        inspect_capture(path, ref["sha256"])


def test_original_capture_hash_is_required(tmp_path, sdk):
    _, ref = run_capture(tmp_path, sdk)
    with pytest.raises(LabError, match="SHA-256 differs"):
        inspect_capture(Path(ref["path"]), "0" * 64)
    assert json.loads(Path(ref["path"]).read_bytes())["evidence_origin"] == "test_double"
