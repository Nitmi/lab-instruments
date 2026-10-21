"""Cold-installed CLI sessions against an explicitly local SDS protocol double."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path


def verify(run, cli, root):
    tests = Path(__file__).resolve().parents[1] / "tests"
    for name in ("sdl_double", "sds_double"):
        spec = importlib.util.spec_from_file_location(name, tests / (name + ".py"))
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    server_type = sys.modules["sds_double"].SdsDouble
    serial = "SDSFAKE" + hashlib.sha256(str(root).encode()).hexdigest()[:12].upper()
    records = []

    def fault(server, command, response):
        if mode == "truncated" and command == ":WAV:DATA?":
            return b"#232\x00"
        if mode == "stale" and server.single and command == ":TRIG:STAT?":
            return "Stop"
        return response

    for action, mode, expected in (
        ("identify", "ok", 0),
        ("stopped", "ok", 0),
        ("single", "ok", 0),
        ("single", "stale", 3),
        ("single", "truncated", 3),
    ):
        label = f"scope-{len(records)}-{action}-{mode}"
        with server_type(fault, serial=serial) as server:
            plan = root / (label + ".json")
            args = [
                *cli,
                "scope",
                "plan",
                "--host",
                "127.0.0.1",
                "--port",
                str(server.port),
                "--action",
                action,
                "--test-double",
                "--channels",
                "1",
                "2",
                "3",
                "4",
                "--points",
                "16",
                "--io-timeout-ms",
                "150",
                "--output",
                str(plan),
                "--json",
            ]
            if action != "identify":
                args += ["--serial", serial, "--firmware-version", "1.1.6.5"]
            created = json.loads(run(label + "-plan", args))
            assert created["ok"] and not created["hardware_access"]
            sha = hashlib.sha256(plan.read_bytes()).hexdigest()
            checked = json.loads(
                run(
                    label + "-inspect-plan",
                    [*cli, "scope", "inspect-plan", str(plan), "--sha256", sha, "--json"],
                )
            )
            assert checked["ok"] and not checked["executables_started"]
            directory = root / (label + "-run")
            native = json.loads(
                run(
                    label + "-run",
                    [
                        *cli,
                        "scope",
                        "run",
                        "--plan",
                        str(plan),
                        "--sha256",
                        sha,
                        "--output",
                        str(directory),
                        "--json",
                    ],
                    expected,
                )
            )
            assert native["ok"] is (expected == 0) and not native["hardware_access"]
            assert native["executables_started"]
            result = native["data"]["session"]
            assert result["evidence_origin"] == "test_double"
            assert not result["physical_device_verified"]
            assert result["cleanup"]["socket_closed"] and result["cleanup"]["local_lease_released"]
            assert server.connections == 1
            assert server.commands.count(":TRIG:MODE SING") == (action == "single")
            assert server.commands.count(":TRIG:STOP") <= 1
            if mode == "truncated":
                assert not result["cleanup"]["complete"]
                assert not result["cleanup"]["stopped_confirmed"]
                assert ":TRIG:STOP" not in server.commands
            path = directory / "scope-result.json"
            result_sha = hashlib.sha256(path.read_bytes()).hexdigest()
            inspected = json.loads(
                run(
                    label + "-inspect",
                    [*cli, "scope", "inspect", str(path), "--sha256", result_sha, "--json"],
                )
            )
            assert inspected["ok"] and not inspected["hardware_access"]
            assert inspected["data"]["source_ok"] is (expected == 0)
            records.append(
                {
                    "action": action,
                    "fault": mode,
                    "exit_code": expected,
                    "connections": server.connections,
                    "result_sha256": result_sha,
                    "hardware_access": False,
                }
            )
    (root / "scope-smoke.json").write_text(
        json.dumps({"ok": True, "sessions": records}, indent=2) + "\n"
    )
    return {"ok": True, "sessions": len(records)}
