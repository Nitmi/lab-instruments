"""Installed CLI checks with an explicit loopback SDL protocol double."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path


def verify(run, cli, root):
    source = Path(__file__).resolve().parents[1] / "tests/sdl_double.py"
    spec = importlib.util.spec_from_file_location("sdl_protocol_double", source)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    records = []
    # Independent synthetic serials avoid competing for another test's lease.
    serial = "SYNTHETIC" + hashlib.sha256(str(root).encode()).hexdigest()[:12].upper()
    for action, fault, expected in (
        ("identify", None, 0),
        ("observe", None, 0),
        ("cc", None, 0),
        ("off", None, 0),
        ("cc", "off-stuck", 3),
        ("cc", module.stalled_loaded_measurement, 3),
    ):
        name = f"load-{len(records)}-{action}"
        with module.SdlDouble(fault, serial=serial) as server:
            plan_path = root / (name + ".json")
            arguments = [
                *cli,
                "load",
                "plan",
                "--host",
                "127.0.0.1",
                "--port",
                str(server.port),
                "--action",
                action,
                "--test-double",
                "--samples",
                "1",
                "--interval-ms",
                "200",
                "--io-timeout-ms",
                "100",
                "--output",
                str(plan_path),
                "--json",
            ]
            if action != "identify":
                arguments.extend(["--serial", serial, "--firmware-version", "1.1.1.23R4"])
            if action == "cc":
                arguments.extend(["--current-a", "0.100"])
            plan = json.loads(run(name + "-plan", arguments))
            assert plan["ok"] and not plan["hardware_access"]
            expected_hash = hashlib.sha256(plan_path.read_bytes()).hexdigest()
            json.loads(
                run(
                    name + "-inspect-plan",
                    [
                        *cli,
                        "load",
                        "inspect-plan",
                        str(plan_path),
                        "--sha256",
                        expected_hash,
                        "--json",
                    ],
                )
            )
            directory = root / (name + "-run")
            session = json.loads(
                run(
                    name + "-run",
                    [
                        *cli,
                        "load",
                        "run",
                        "--plan",
                        str(plan_path),
                        "--sha256",
                        expected_hash,
                        "--output",
                        str(directory),
                        "--json",
                    ],
                    expected,
                )
            )
            assert session["ok"] is (expected == 0)
            assert not session["hardware_access"] and not session["physical_device_verified"]
            native = session["data"]["session"]
            assert native["evidence_origin"] == "test_double"
            assert not native["physical_device_verified"] and not native["hardware_access"]
            assert server.connections == 1
            result_path = directory / "load-result.json"
            result_hash = hashlib.sha256(result_path.read_bytes()).hexdigest()
            inspected = json.loads(
                run(
                    name + "-inspect",
                    [*cli, "load", "inspect", str(result_path), "--sha256", result_hash, "--json"],
                )
            )
            assert inspected["data"]["source_ok"] is (expected == 0)
            assert not inspected["hardware_access"]
            records.append(
                {
                    "action": action,
                    "fault": str(fault),
                    "status": native["status"],
                    "cleanup": native["cleanup"],
                    "commands": server.commands,
                    "result_sha256": result_hash,
                    "connections": server.connections,
                }
            )
    result = {
        "ok": True,
        "evidence_origin": "test_double",
        "hardware_access": False,
        "physical_device_verified": False,
        "sessions": records,
    }
    (root / "load-smoke.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return result
