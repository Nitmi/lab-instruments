from __future__ import annotations

import copy
import ctypes
import json
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from lab_instruments.capture import perform, supervise, worker
from lab_instruments.cli import main
from lab_instruments.common import LabError, digest, encode, parse, read, write_new
from lab_instruments.plan import create, validate


@pytest.mark.parametrize("serial", ["", "*", "abc", "abc123\nsn:other", "abc123,config:1"])
def test_serial_is_exact_and_cannot_inject_sdk_options(serial):
    with pytest.raises(LabError):
        create(serial, "logic", [0])


@pytest.mark.parametrize(
    "key,value",
    [
        ("samples", True),
        ("samples", 16385),
        ("samples", -1),
        ("sample_rate_hz", float("nan")),
        ("sample_rate_hz", float("inf")),
        ("sample_rate_hz", 125_000_001),
        ("timeout_ms", 2999),
        ("timeout_ms", 30001),
        ("channels", [0, 0]),
        ("channels", [16]),
        ("channels", [True]),
        ("instrument", "supply"),
        ("authorization_granted", True),
        ("device_id", True),
        ("effects", {}),
        ("sdk_version", "latest"),
        ("settle_ms", False),
    ],
)
def test_plan_rejects_invalid_scope_or_claims(key, value):
    plan = create("210321ABC", "logic", [0, 15])
    plan[key] = value
    with pytest.raises(LabError):
        validate(plan)


def test_no_unknown_fields_or_truthy_effect_values():
    plan = create("210321ABC", "scope", [1, 2])
    plan["arbitrary_command"] = "ignored?"
    with pytest.raises(LabError):
        validate(plan)
    plan.pop("arbitrary_command")
    plan["effects"] = {**plan["effects"], "outputs_commanded": 0}
    with pytest.raises(LabError):
        validate(plan)


def test_static_host_commands_do_not_load_sdk_or_start_process(tmp_path, monkeypatch, capsys):
    def forbidden(*args, **kwargs):
        pytest.fail("Host command touched native runtime, process or network")

    monkeypatch.setattr(ctypes, "CDLL", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    assert main(["doctor", "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert not result["hardware_access"] and not result["data"]["sdk_loaded"]
    output = tmp_path / "scope-plan.json"
    args = [
        "capture",
        "plan",
        "--serial",
        "210321ABC",
        "--instrument",
        "scope",
        "--channels",
        "1",
        "2",
        "--output",
        str(output),
        "--json",
    ]
    assert main(args) == 0
    ref = json.loads(capsys.readouterr().out)["data"]["output"]
    before = output.read_bytes()
    assert main(["capture", "inspect-plan", str(output), "--sha256", ref["sha256"]]) == 0
    assert not json.loads(capsys.readouterr().out)["executables_started"]
    assert main(args) == 2
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "OUTPUT_EXISTS"
    assert output.read_bytes() == before


def test_sdk_doctor_does_not_load_file(tmp_path, monkeypatch, capsys):
    sdk = tmp_path / "not-a-library.dll"
    sdk.write_bytes(b"static test file")
    monkeypatch.setattr(ctypes, "CDLL", lambda *args, **kwargs: pytest.fail("loaded DLL"))
    assert main(["doctor", "--sdk", str(sdk), "--sdk-sha256", digest(sdk.read_bytes())]) == 0
    data = json.loads(capsys.readouterr().out)["data"]
    assert data["status"] == "sdk_file_inspected_not_loaded" and not data["native_sdk_verified"]


@pytest.mark.parametrize(
    "raw",
    [
        b'{"a":1,"a":2}',
        b'{"a":NaN}',
        b'{"a":1e999}',
        b'{"a":' + b"[" * 25 + b"0" + b"]" * 25 + b"}",
    ],
)
def test_bad_json(raw):
    with pytest.raises(LabError):
        parse(raw)


def test_input_hash_and_output_guards(tmp_path):
    path = tmp_path / "input.json"
    path.write_bytes(b"{}\n")
    with pytest.raises(LabError, match="SHA-256 differs"):
        read(path, expected="0" * 64)
    with pytest.raises(LabError, match="byte limit"):
        read(path, maximum=1)
    for filename in ("NUL.json", "CON", "COM1.txt", "name:stream", "file."):
        with pytest.raises(LabError):
            write_new(tmp_path / filename, b"{}")
    with pytest.raises(OSError):
        write_new(tmp_path / "absent" / "output.json", b"{}")


def inputs(tmp_path):
    plan = tmp_path / "plan.json"
    request = create("210321ABC", "logic", [0], timeout_ms=3000)
    plan_ref = write_new(plan, encode(request))
    sdk = tmp_path / "dummy.dll"
    sdk.write_bytes(b"not a vendor runtime; tests never load it")
    return plan, plan_ref["sha256"], sdk, digest(sdk.read_bytes())


def test_changed_plan_and_existing_directory_never_start_worker(tmp_path, monkeypatch):
    args = inputs(tmp_path)
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: pytest.fail("started worker"))
    with pytest.raises(LabError, match="SHA-256 differs"):
        supervise(args[0], "0" * 64, args[2], args[3], tmp_path / "not-created")
    with pytest.raises(LabError, match="output exists"):
        supervise(*args, tmp_path)
    assert not (tmp_path / "not-created").exists()


def test_watchdog_kills_only_its_worker_and_keeps_cleanup_unknown(tmp_path, monkeypatch):
    args = inputs(tmp_path)
    original = subprocess.Popen
    processes = []

    def sleeping_test_worker(*argv, **kwargs):
        # Deliberately replace the fixed worker with a harmless Python sleeper.
        process = original([sys.executable, "-I", "-c", "import time; time.sleep(30)"], **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(subprocess, "Popen", sleeping_test_worker)
    result, ref = supervise(*args, tmp_path / "run")
    assert not result["ok"] and result["error"]["code"] == "WORKER_TIMEOUT"
    assert result["cleanup_confirmed"] is False and result["retry_count"] == 0
    assert len(processes) == 1 and processes[0].poll() is not None
    assert Path(ref["path"]).exists() and (tmp_path / "run" / "plan.json").exists()


def test_worker_failure_with_no_evidence_is_not_a_success(tmp_path, monkeypatch):
    args = inputs(tmp_path)
    original = subprocess.Popen

    def failed_test_worker(*argv, **kwargs):
        return original([sys.executable, "-I", "-c", "raise SystemExit(4)"], **kwargs)

    monkeypatch.setattr(subprocess, "Popen", failed_test_worker)
    result, _ = supervise(*args, tmp_path / "run")
    assert not result["ok"] and result["error"]["code"] == "WORKER_FAILED"
    assert not result["cleanup_confirmed"]


def test_schema_contract_is_not_shared_mutable_state():
    first = create("210321ABC", "scope", [1])
    second = copy.deepcopy(first)
    second["effects"]["outputs_commanded"] = True
    assert create("210321ABC", "scope", [1])["effects"]["outputs_commanded"] is False


@pytest.mark.parametrize("exit_code", [0, 1])
def test_supervisor_retains_capture_but_rejects_abnormal_exit(
    tmp_path, monkeypatch, sdk, exit_code
):
    args = inputs(tmp_path)

    class CompletedDoubleWorker:
        pid = 99999

        def __init__(self, argv, **kwargs):
            assert argv[1:4] == ["-I", "-m", "lab_instruments._worker"]
            assert "shell" not in kwargs
            directory = Path(argv[-1])
            plan = parse((directory / "plan.json").read_bytes())
            perform(plan, sdk, directory, {}, {})

        def wait(self, timeout):
            return exit_code

        def poll(self):
            return exit_code

    monkeypatch.setattr(subprocess, "Popen", CompletedDoubleWorker)
    result, _ = supervise(*args, tmp_path / "run")
    assert result["ok"] is (exit_code == 0)
    assert result["cleanup_confirmed"] is True
    assert result["hardware_access"] is False
    if exit_code:
        assert result["error"]["code"] == "WORKER_EXIT_FAILED"


def test_cancellation_terminates_owned_worker_once(tmp_path, monkeypatch):
    args = inputs(tmp_path)

    class InterruptedDoubleWorker:
        pid = 99998
        returncode = None
        kills = 0

        def __init__(self, *args, **kwargs):
            pass

        def wait(self, timeout):
            if self.returncode is None:
                raise KeyboardInterrupt
            return self.returncode

        def poll(self):
            return self.returncode

        def kill(self):
            self.kills += 1
            self.returncode = -9

    process = InterruptedDoubleWorker()
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: process)
    result, _ = supervise(*args, tmp_path / "run")
    assert result["error"]["code"] == "CANCELLED" and not result["cleanup_confirmed"]
    assert process.kills == 1 and not result["ok"]


def test_worker_sdk_failure_keeps_structured_reason(tmp_path, monkeypatch):
    args = inputs(tmp_path)
    directory = tmp_path / "run"
    directory.mkdir()

    def unavailable(*args, **kwargs):
        raise LabError("SDK_UNAVAILABLE", "synthetic missing vendor dependency")

    monkeypatch.setattr("lab_instruments.capture.Dwf.load", unavailable)
    assert worker(*args, directory) == 2
    result = parse((directory / "worker-error.json").read_bytes())
    assert result["error"]["code"] == "SDK_UNAVAILABLE"
    assert not result["cleanup_confirmed"]
    assert b'"stage":"worker.failed"' in (directory / "journal.jsonl").read_bytes()
