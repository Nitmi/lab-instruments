"""Saved native records from owned loopback peers, acquired once per scenario."""

from __future__ import annotations

from sdl_double import SdlDouble, stalled_loaded_measurement

from lab_instruments import sdl, sdl_plan
from lab_instruments.common import encode


def spec(*, phase="loaded", count=2):
    return {
        "schema_version": "lab-instruments.load-check-spec.v1",
        "checks": [
            {
                "id": "supply",
                "phase": phase,
                "window": {"start": 0, "end_exclusive": count},
                "expect": {
                    "minimum_voltage_v": {"minimum": 5.001, "maximum": 5.001},
                    "mean_current_a": {"minimum": 0.1, "maximum": 0.1},
                    "maximum_power_w": {"minimum": 0.5, "maximum": 0.5},
                    "input_on": True,
                },
            }
        ],
    }


def acquire(directory, scenario="cc", *, count=2):
    directory.mkdir()
    fault = (
        "off-stuck"
        if scenario == "cleanup"
        else stalled_loaded_measurement
        if scenario == "timeout"
        else None
    )
    action = "observe" if scenario == "observe" else "identify" if scenario == "identify" else "cc"
    with SdlDouble(fault, serial="LOADCHECKS123") as peer:
        if action == "observe":
            peer.on = True
        plan = sdl_plan.create(
            "127.0.0.1",
            None if action == "identify" else peer.serial,
            None if action == "identify" else "1.1.1.23R4",
            action=action,
            current_a=0.1 if action == "cc" else None,
            samples=count,
            interval_ms=100,
            port=peer.port,
            io_timeout_ms=2000 if scenario != "timeout" else 100,
            test_double=True,
        )
        (directory / "plan.json").write_bytes(encode(plan))
        result = sdl.perform(plan, directory)
        assert result["ok"] is (scenario not in ("cleanup", "timeout")), result
        assert peer.connections == 1
    return {p.name: p.read_bytes() for p in directory.iterdir() if p.is_file()}
