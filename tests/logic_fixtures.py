"""Synthetic saved samples, not SDK or physical capture evidence."""

from __future__ import annotations

import base64
import math
import struct

from lab_instruments.common import digest, encode
from lab_instruments.plan import create


def capture(levels, rate=1_000_000, *, channel=0, requested_rate=None, ok=True):
    levels = list(levels)
    assert 16 <= len(levels) <= 16384
    plan = create(
        "SYNTHETIC123",
        "logic",
        [channel],
        samples=len(levels),
        sample_rate_hz=requested_rate or rate,
    )
    # Exercise channel extraction while unrelated DIO bits keep changing.
    words = [
        (level << channel) | ((i % 2) << ((channel + 1) % 16)) for i, level in enumerate(levels)
    ]
    raw = struct.pack("<" + "H" * len(words), *words)
    return {
        "schema_version": "lab-instruments.capture.v1",
        "operation": "capture.single",
        "plan": plan,
        "model": plan["model"],
        "serial": plan["serial"],
        "instrument": "logic",
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
            "sample_rate_hz": rate,
            "samples": len(levels),
            "channel_scales": {},
            "rate_policy": "at_most_requested",
            "trigger": "immediate",
        },
        "payload": {
            "raw_base64": base64.b64encode(raw).decode("ascii"),
            "sha256": digest(raw),
            "length": len(raw),
            "channels": [channel],
            "samples_per_channel": len(words),
            "encoding": "u16le_dio0_first",
            "time_semantics": "sample_index_over_actual_rate_no_verified_utc_origin",
        },
        "ok": ok,
        "status": "captured" if ok else "failed",
        "cleanup": {
            "complete": ok,
            "input_stopped": ok,
            "owned_handle_closed": ok,
            "errors": [] if ok else ["synthetic cleanup failure"],
        },
        **({} if ok else {"error": {"code": "CLEANUP_INCOMPLETE", "message": "synthetic failure"}}),
    }


def save(tmp_path, value, name="capture.json"):
    path = tmp_path / name
    path.write_bytes(encode(value))
    return path, digest(path.read_bytes())


def uart_signal(
    values,
    *,
    rate=1_000_000,
    baud=100_000,
    data_bits=8,
    parity="none",
    stop_bits=1,
    inverted=False,
    phase=0.0,
    corrupt_parity=False,
    corrupt_stop=False,
):
    # Independent bit-level transmitter, then uniformly sample continuous time.
    line = [1, 1]
    for value in values:
        data = [(value >> i) & 1 for i in range(data_bits)]
        line += [0] + data
        if parity != "none":
            bit = sum(data) % 2 ^ (parity == "odd")
            line.append(bit ^ corrupt_parity)
        line += [0 if corrupt_stop else 1] * stop_bits
    line += [1, 1]
    count = math.ceil(len(line) * rate / baud)
    levels = [line[min(len(line) - 1, math.floor((i + phase) * baud / rate))] for i in range(count)]
    return [level ^ inverted for level in levels]
