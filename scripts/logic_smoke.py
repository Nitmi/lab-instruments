"""Installed offline UART/PWM checks using explicitly synthetic saved samples."""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path


def verify(run, cli, root):
    fixture = Path(__file__).resolve().parents[1] / "tests/logic_fixtures.py"
    producer = (
        "import importlib.util,pathlib;"
        f"s=importlib.util.spec_from_file_location('logic_fixture',{str(fixture)!r});"
        "m=importlib.util.module_from_spec(s);s.loader.exec_module(m);"
        f"p=pathlib.Path({str(root)!r});"
        "m.save(p,m.capture(m.uart_signal(b'Agent\\x00\\xff',rate=2500000,baud=115200),"
        "2500000,requested_rate=3000000),'uart-capture.json');"
        "m.save(p,m.capture([0]*3+[1]*2+[0]*6+[1]*3+[0]*5+[1]*4+[0]*2,"
        "8000,requested_rate=1000000),'pwm-capture.json');"
        "m.save(p,m.capture([1]*64),'idle-capture.json');"
        "m.save(p,m.capture(m.uart_signal([65]),ok=False),'failed-capture.json')"
    )
    run("logic-fixtures", [*cli[:2], "-c", producer])
    references = {}
    for name in ("uart", "pwm", "idle", "failed"):
        path = root / (name + "-capture.json")
        references[name] = (path, hashlib.sha256(path.read_bytes()).hexdigest())

    def args(name, kind):
        path, sha = references[name]
        return [*cli, "logic", kind, str(path), "--sha256", sha, "--channel", "0", "--json"]

    uart_args = [*args("uart", "uart"), "--baud", "115200", "--output", str(root / "uart.json")]
    uart = json.loads(run("logic-uart", uart_args))
    decoded = uart["data"]["analysis"]
    assert uart["ok"] and base64.b64decode(decoded["analysis"]["bytes_base64"]) == b"Agent\x00\xff"
    assert decoded["sample_rate_hz"] == 2_500_000
    pwm = json.loads(run("logic-pwm", [*args("pwm", "pwm"), "--output", str(root / "pwm.json")]))
    measured = pwm["data"]["analysis"]
    assert pwm["ok"] and measured["analysis"]["cycle_count"] == 2
    assert measured["analysis"]["statistics"]["frequency_hz"]["mean"] == 1000
    assert measured["analysis"]["statistics"]["duty_cycle"]["mean"] == 0.3125
    for result in (decoded, measured):
        assert result["evidence_origin"] == "test_double"
        assert all(
            result[k] is False
            for k in (
                "hardware_access",
                "executables_started",
                "physical_device_verified",
                "physical_timing_verified",
                "calibration_verified",
            )
        )
    assert json.loads(run("logic-overwrite", uart_args, 2))["error"]["code"] == "OUTPUT_EXISTS"
    idle = json.loads(run("logic-no-frames", [*args("idle", "uart"), "--baud", "100000"], 3))
    assert not idle["ok"] and idle["data"]["analysis"]["status"] == "no_frames"
    failed = json.loads(
        run("logic-source-failed", [*args("failed", "uart"), "--baud", "100000"], 3)
    )
    assert failed["data"]["analysis"]["status"] == "source_failed"
    wrong = args("uart", "uart")
    wrong[wrong.index("--sha256") + 1] = "0" * 64
    assert (
        json.loads(run("logic-wrong-hash", [*wrong, "--baud", "115200"], 2))["error"]["code"]
        == "HASH_MISMATCH"
    )
    for path, sha in references.values():
        assert hashlib.sha256(path.read_bytes()).hexdigest() == sha
    for label in ("uart", "pwm"):
        report = root / (label + ".json")
        assert json.loads(report.read_bytes())["capture"]["sha256"] == references[label][1]
    return {"ok": True, "hardware_access": False, "synthetic_only": True}
