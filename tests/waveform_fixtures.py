"""Explicit synthetic AD3 voltage data and independent voltage bounds."""

from __future__ import annotations

import base64
import struct

from lab_instruments.common import digest
from lab_instruments.plan import create


def capture(values=None):
    channels = values or {1: [2.0, 4.0] * 8, 2: [-3.0, 3.0] * 8}
    count = len(next(iter(channels.values())))
    plan = create("SYNTHETIC123", "scope", list(channels), samples=count, sample_rate_hz=1_000_000)
    raw = struct.pack(
        "<" + "d" * (count * len(channels)),
        *(sample for samples in channels.values() for sample in samples),
    )
    return {
        "schema_version": "lab-instruments.capture.v1",
        "operation": "capture.single",
        "plan": plan,
        "model": plan["model"],
        "serial": plan["serial"],
        "instrument": "scope",
        "sdk_version": plan["sdk_version"],
        "effects": plan["effects"],
        "evidence_origin": "test_double",
        "hardware_access": False,
        "outputs_commanded": False,
        "sample_zero_utc_verified": False,
        "calibration_verified": False,
        "dut_identity_verified": False,
        "physical_device_verified": False,
        "plan_file": {"path": "DO_NOT_OPEN_PLAN"},
        "sdk_file": {"path": "DO_NOT_LOAD_SDK"},
        "settings": {
            "sample_rate_hz": 1_000_000,
            "samples": count,
            "channel_scales": {str(c): {"range_v": 10.0, "offset_v": 0.0} for c in channels},
            "rate_policy": "at_most_requested",
            "trigger": "immediate",
        },
        "payload": {
            "raw_base64": base64.b64encode(raw).decode(),
            "sha256": digest(raw),
            "length": len(raw),
            "channels": list(channels),
            "samples_per_channel": count,
            "encoding": "f64le_channel_major",
            "time_semantics": "sample_index_over_actual_rate_no_verified_utc_origin",
        },
        "ok": True,
        "status": "captured",
        "cleanup": {
            "complete": True,
            "input_stopped": True,
            "owned_handle_closed": True,
            "errors": [],
        },
    }


def spec(kind="ad3"):
    return {
        "schema_version": "lab-instruments.waveform-check-spec.v1",
        "source_kind": kind,
        "checks": [
            {
                "id": "rail",
                "channel": 1,
                "window": {"start": 0, "end_exclusive": 16},
                "expect": {
                    "minimum_v": {"minimum": 2, "maximum": 2},
                    "maximum_v": {"minimum": 4, "maximum": 4},
                    "mean_v": {"minimum": 3, "maximum": 3},
                    "peak_to_peak_v": {"minimum": 2, "maximum": 2},
                    "rms_v": {"minimum": 3.16, "maximum": 3.17},
                    "ac_rms_v": {"minimum": 1, "maximum": 1},
                },
            }
        ],
    }
