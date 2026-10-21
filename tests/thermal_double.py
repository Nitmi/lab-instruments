"""Synthetic saved Y16/control evidence, not a physical temperature reference."""

from __future__ import annotations

import struct

from lab_instruments.common import encode, write_new
from lab_instruments.thermal import bundle
from lab_instruments.thermal_frame import CONTROLS

SERIAL = "0102030405060708"
SOFTWARE = "0102030405060000"


def control_bytes():
    values = {name: bytes(size) for name, (_, _, size) in CONTROLS.items()}
    values["serial"] = bytes.fromhex(SERIAL)
    values["software"] = bytes.fromhex(SOFTWARE)
    values["part"] = b"500-0771-01" + bytes(21)
    values["flux"] = struct.pack("<8H", 8192, 30000, 8192, 30000, 8192, 30000, 0, 30000)
    for key in ("radiometry", "tlinear", "resolution", "telemetry", "location"):
        values[key] = struct.pack("<I", 1)
    return values


def frame(index=0, *, code=30000, resolution=1, status=48, gain=0, ffc_ms=500):
    words = [code] * 19200 + [0] * 320
    base = 19200
    for offset, value in (
        (0, 0x0900),
        (1, (1000 + 120 * index) & 65535),
        (2, (1000 + 120 * index) >> 16),
        (3, status & 65535),
        (4, status >> 16),
        (20, (100 + index) & 65535),
        (21, (100 + index) >> 16),
        (24, 30000),
        (26, 30000),
        (30, ffc_ms & 65535),
        (31, ffc_ms >> 16),
        (165, gain),
        (166, min(gain, 1)),
        (208, 1),
        (209, resolution),
    ):
        words[base + offset] = value
    return struct.pack("<19520H", *words)


def source(directory, plan, *, mutation=None):
    data = control_bytes()
    manifest = {
        "schema_version": "lab-instruments.pt3-native.v1",
        "ok": True,
        "origin": "test_double",
        "error": "",
        "capture_started": plan["action"] == "capture",
        "cleanup_complete": True,
        "link": plan["link"] or "",
        "frames": [],
        "devices": ["SYNTHETIC-PT3"],
    }
    if plan["action"] != "devices":
        for key in ("serial", "software", "part"):
            write_new(directory / ("identity-" + key + ".bin"), data[key])
    if plan["action"] == "capture":
        for phase in ("before", "after"):
            for key, value in data.items():
                write_new(directory / f"{phase}-{key}.bin", value)
        write_new(directory / "warmup.y16", frame())
        for i in range(plan["buffers"]):
            name = f"frame-{i:04d}.y16"
            raw = frame(i, code=30000 + i * 10)
            if mutation:
                raw = mutation(i, raw)
            write_new(directory / name, raw)
            manifest["frames"].append({"name": name, "timestamp_100ns": i * 1200000})
    write_new(directory / "native.json", encode(manifest))
    bundle(directory, plan)
    return directory / "source.json"
