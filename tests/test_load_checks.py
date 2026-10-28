from __future__ import annotations

import base64
import copy
import ctypes
import json
import shutil
import socket
import subprocess
import xml.etree.ElementTree as ET
from collections import Counter

import pytest
from load_fixtures import acquire, spec

from lab_instruments import cli, load_checks, sdl, waveform
from lab_instruments.common import LabError, digest, encode


@pytest.fixture(scope="module")
def saved(tmp_path_factory):
    root = tmp_path_factory.mktemp("native-load-sources")
    return {
        scenario: acquire(root / scenario, scenario)
        for scenario in ("cc", "observe", "cleanup", "timeout", "identify")
    }


def source_files(root, saved, scenario="cc"):
    for name, raw in saved[scenario].items():
        (root / name).write_bytes(raw)
    return root / "load-result.json"


def inputs(root, saved, contract=None, scenario="cc"):
    capture = source_files(root, saved, scenario)
    contract = contract or spec()
    sp = root / "spec.json"
    sp.write_bytes(encode(contract))
    return sp, digest(sp.read_bytes()), capture, digest(capture.read_bytes())


def forbid(*args, **kwargs):
    raise AssertionError("offline evaluation used network/SDK/process/lease")


def block_io(monkeypatch):
    for target, name in (
        (socket, "socket"),
        (subprocess, "run"),
        (subprocess, "Popen"),
        (ctypes, "CDLL"),
        (sdl.Lease, "__init__"),
    ):
        monkeypatch.setattr(target, name, forbid)


def rebind(root, result, events=None):
    if events is not None:
        raw = b"".join(encode(e) for e in events)
        (root / "transcript.jsonl").write_bytes(raw)
        result["transcript"] = {"sha256": digest(raw), "size_bytes": len(raw)}
    path = root / "load-result.json"
    path.write_bytes(encode(result))
    return path, digest(path.read_bytes())


def test_success_replays_original_commands_metrics_windows_and_reads_once(
    tmp_path, saved, monkeypatch
):
    args = inputs(tmp_path, saved)
    block_io(monkeypatch)
    original, calls = load_checks.read, Counter()

    def counted(path, **kwargs):
        calls[str(path)] += 1
        return original(path, **kwargs)

    monkeypatch.setattr(load_checks, "read", counted)
    report = load_checks.evaluate(*args)
    assert report["ok"] and report["summary"] == {
        "checks": 1,
        "passed": 1,
        "failed": 0,
        "invalid_evidence": 0,
    }
    first = report["checks"][0]
    assert len(first["metrics"]) == 9 and first["metrics"]["mean_voltage_v"] == 5.001
    assert first["metrics"]["minimum_current_a"] == 0.1
    assert (
        first["metrics"]["maximum_power_w"] == 0.5
    )  # Never multiply the non-simultaneous V/I readings.
    assert first["metrics"]["mean_power_w"] != 5.001 * 0.1
    assert len(first["measurements"]) == 2
    raw = (tmp_path / "transcript.jsonl").read_bytes()
    for sample in first["measurements"]:
        assert sample["phase"] == "loaded" and sample["input_on"] is True
        assert len(sample["raw_queries"]) == 3
        for query in sample["raw_queries"]:
            span = query["transcript_span"]
            assert digest(raw[span["start"] : span["end_exclusive"]]) == span["sha256"]
            assert base64.b64decode(query["rx_base64"]).endswith(b"\r\n")
    assert report["source"]["source_success_replayed"]
    assert report["source"]["recorded_cleanup"]["input_off_confirmed"]
    assert report["source"]["time_semantics"] == load_checks.TIME
    assert report["source"]["evidence_origin"] == "test_double"
    assert all(report[k] is False for k in load_checks.FLAGS)
    assert len(report["input_files"]) == 3
    assert len(calls) == 4 and all(n == 1 for n in calls.values())


@pytest.mark.parametrize("phase,scenario", [("before_load", "cc"), ("observe", "observe")])
def test_baseline_and_observe_use_only_explicit_phase(tmp_path, saved, phase, scenario):
    contract = spec(phase=phase, count=1)
    if phase == "before_load":
        contract["checks"][0]["expect"] = {
            "input_on": False,
            "mean_current_a": {"minimum": 0, "maximum": 0},
        }
    report = load_checks.evaluate(*inputs(tmp_path, saved, contract, scenario))
    assert report["ok"]
    assert report["checks"][0]["measurements"][0]["phase"] == phase
    if phase == "observe":
        assert not report["source"]["recorded_cleanup"]["off_attempted"]
        assert report["source"]["recorded_cleanup"]["instrument_state"] == "not_established"


@pytest.mark.parametrize("scenario", ["cleanup", "timeout"])
def test_failed_sources_preserve_errors_and_cleanup_without_scoring_partial_values(
    tmp_path, saved, scenario, monkeypatch
):
    args = inputs(tmp_path, saved, scenario=scenario)
    block_io(monkeypatch)
    report = load_checks.evaluate(*args)
    assert not report["ok"] and report["summary"]["invalid_evidence"] == 1
    check = report["checks"][0]
    assert (
        check["reason"] == "source_failed"
        and check["metrics"] is None
        and check["measurements"] == []
    )
    assert not report["source"]["source_success_replayed"]
    if scenario == "cleanup":
        assert len(report["source"]["recorded_partial_measurements"]) == 2
    assert report["source"]["source_error"]["code"] in ("CLEANUP_INCOMPLETE", "TIMEOUT")
    assert not report["source"]["recorded_cleanup"]["input_off_confirmed"]
    assert report["source"]["recorded_cleanup"]["instrument_state"] == "unknown"
    rp = tmp_path / "report.json"
    rp.write_bytes(encode(report))
    raw, verification, counts = load_checks.junit_report(rp, digest(rp.read_bytes()), *args)
    assert verification["ok"] and verification["verdict"]["ok"] is False
    assert counts == {"tests": 1, "errors": 1, "failures": 0, "skipped": 0}
    assert ET.fromstring(raw).find("testcase/error") is not None


@pytest.mark.parametrize("mode", ["phase", "window", "identify"])
def test_missing_measurements_are_errors_not_clamped_or_skipped(tmp_path, saved, mode):
    contract = spec(
        phase="observe" if mode == "phase" else "loaded", count=3 if mode == "window" else 2
    )
    report = load_checks.evaluate(
        *inputs(tmp_path, saved, contract, "identify" if mode == "identify" else "cc")
    )
    assert report["checks"][0]["status"] == "invalid_evidence" and not report["ok"]


@pytest.mark.parametrize("mode", ["voltage", "input-state"])
def test_real_assertion_failure_is_junit_failure(tmp_path, saved, mode):
    contract = spec()
    contract["checks"][0]["expect"] = (
        {"mean_voltage_v": {"minimum": 5.1, "maximum": 5.2}}
        if mode == "voltage"
        else {"input_on": False}
    )
    args = inputs(tmp_path, saved, contract)
    report = load_checks.evaluate(*args)
    assert report["checks"][0]["status"] == "failed" and report["summary"]["failed"] == 1
    rp = tmp_path / "report.json"
    rp.write_bytes(encode(report))
    xml, _, counts = load_checks.junit_report(rp, digest(rp.read_bytes()), *args)
    assert counts["failures"] == 1 and counts["errors"] == 0
    assert ET.fromstring(xml).find("testcase/failure") is not None


@pytest.mark.parametrize(
    "mode",
    [
        "phase",
        "index",
        "input",
        "settings",
        "cleanup",
        "claim",
        "window",
        "origin",
        "measurements",
        "plan",
    ],
)
def test_rehashed_source_metadata_cannot_override_raw_evidence(tmp_path, saved, mode):
    sp, sh, cp, _ = inputs(tmp_path, saved)
    result = json.loads(cp.read_bytes())
    if mode == "phase":
        result["measurements"][0]["phase"] = "observe"
    elif mode == "index":
        result["measurements"][0]["index"] = True
    elif mode == "input":
        result["measurements"][0]["input_on"] = False
    elif mode == "settings":
        result["settings"]["current_a"] = 0.2
    elif mode == "cleanup":
        result["cleanup"]["socket_closed"] = False
    elif mode == "claim":
        result["dut_identity_verified"] = True
    elif mode == "window":
        result["measurements"][0]["host_started_monotonic_s"] += 60
    elif mode == "origin":
        result["evidence_origin"] = "physical"
    elif mode == "measurements":
        result["measurements"][0]["voltage_v"] = 5.0
    else:
        (tmp_path / "plan.json").write_bytes(encode({**result["plan"], "samples": 3}))
    cp, ch = rebind(tmp_path, result)
    with pytest.raises(LabError):
        load_checks.evaluate(sp, sh, cp, ch)


@pytest.mark.parametrize(
    "mode",
    [
        "off-tx",
        "preflight",
        "extra-write",
        "raw-response",
        "byte-count",
        "unknown",
        "clock",
        "response-order",
    ],
)
def test_rehashed_transcript_corruption_or_wrong_command_sequence_rejected(tmp_path, saved, mode):
    sp, sh, cp, _ = inputs(tmp_path, saved)
    result = json.loads(cp.read_bytes())
    events = [
        json.loads(line) for line in (tmp_path / "transcript.jsonl").read_bytes().splitlines()
    ]
    off = next(
        i
        for i, e in enumerate(events)
        if e["kind"] == "command.begin" and e["command"] == ":SOUR:INP:STAT OFF"
    )
    if mode == "off-tx":
        del events[off + 1]
    elif mode == "preflight":
        start = next(
            i
            for i, e in enumerate(events)
            if e["kind"] == "command.begin" and e["command"] == ":SOUR:SHOR:STAT?"
        )
        end = next(i for i in range(start + 1, len(events)) if events[i]["kind"] == "command.begin")
        del events[start:end]
    elif mode == "extra-write":
        # Same valid OFF pair, injected before the final cleanup command.
        events[off:off] = copy.deepcopy(events[off : off + 2])
    elif mode == "raw-response":
        event = next(e for e in events if e["kind"] == "rx")
        event["raw_base64"] = base64.b64encode(b"changed\r\n").decode()
        event["bytes"] = 9
    elif mode == "byte-count":
        next(e for e in events if e["kind"] == "tx")["bytes"] += 1
    elif mode == "unknown":
        events[1]["kind"] = "endpoint_fallback"
    elif mode == "clock":
        events[1]["monotonic_s"] = 0
    else:
        index = next(i for i, e in enumerate(events) if e["kind"] == "response.complete")
        events.insert(index + 1, copy.deepcopy(events[index]))
    cp, ch = rebind(tmp_path, result, events)
    with pytest.raises(LabError):
        load_checks.evaluate(sp, sh, cp, ch)


@pytest.mark.parametrize("name", ["plan.json", "transcript.jsonl"])
def test_fixed_sibling_required_and_missing_never_reacquires(tmp_path, saved, name, monkeypatch):
    args = inputs(tmp_path, saved)
    (tmp_path / name).unlink()
    block_io(monkeypatch)
    with pytest.raises(OSError):
        load_checks.evaluate(*args)


@pytest.mark.parametrize(
    "mode",
    [
        "empty",
        "unknown",
        "bool",
        "float-window",
        "overflow",
        "reversed",
        "bounds",
        "phase",
        "duplicate",
        "count",
        "state",
        "nan",
        "extra",
    ],
)
def test_invalid_spec_fails_before_source_access(tmp_path, monkeypatch, mode):
    contract = spec()
    check = contract["checks"][0]
    if mode == "empty":
        check["expect"] = {}
    elif mode == "unknown":
        check["expect"]["energy_j"] = {"minimum": 0, "maximum": 1}
    elif mode == "bool":
        check["expect"]["mean_current_a"]["minimum"] = True
    elif mode == "float-window":
        check["window"]["start"] = 0.0
    elif mode == "overflow":
        check["window"]["end_exclusive"] = 51
    elif mode == "reversed":
        check["window"]["end_exclusive"] = 0
    elif mode == "bounds":
        check["expect"]["mean_current_a"] = {"minimum": 1, "maximum": 0}
    elif mode == "phase":
        check["phase"] = "auto"
    elif mode == "duplicate":
        contract["checks"].append(copy.deepcopy(check))
    elif mode == "count":
        contract["checks"] *= 9
    elif mode == "state":
        check["expect"]["input_on"] = 1
    elif mode == "extra":
        contract["run"] = "SOUR ON"
    else:
        check["expect"]["mean_current_a"]["maximum"] = float("nan")
    monkeypatch.setattr(load_checks, "source", forbid)
    with pytest.raises(LabError):
        if mode == "nan":
            load_checks.validate(contract)
        else:
            sp = tmp_path / "bad.json"
            sp.write_bytes(encode(contract))
            load_checks.evaluate(sp, digest(sp.read_bytes()), tmp_path / "absent", "0" * 64)


@pytest.mark.parametrize(
    "field", ["metrics", "verdict", "span", "claim", "source", "bool", "producer"]
)
def test_rehashed_report_needs_full_recomputation(tmp_path, saved, field):
    args = inputs(tmp_path, saved)
    report = load_checks.evaluate(*args)
    if field == "metrics":
        report["checks"][0]["metrics"]["mean_voltage_v"] += 0.01
    elif field == "verdict":
        report["ok"] = False
    elif field == "span":
        report["checks"][0]["measurements"][0]["raw_queries"][0]["transcript_span"]["start"] += 1
    elif field == "claim":
        report["calibration_verified"] = True
    elif field == "source":
        report["source"]["recorded_cleanup"]["off_transmitted"] = False
    elif field == "bool":
        report["complete"] = 1
    else:
        report["runner_version"] = "999.0.0"
    rp = tmp_path / "edited.json"
    rp.write_bytes(encode(report))
    with pytest.raises(LabError):
        load_checks.inspect_report(rp, digest(rp.read_bytes()), *args)


def test_relocation_read_once_and_all_eight_checks_keep_full_junit(tmp_path, saved, monkeypatch):
    contract = spec()
    checks = []
    for i in range(8):
        check = copy.deepcopy(contract["checks"][0])
        check["id"] = "check" + str(i)
        if i % 3 == 1:
            check["expect"]["input_on"] = False
        if i % 3 == 2:
            check["phase"] = "observe"
        checks.append(check)
    contract["checks"] = checks
    args = inputs(tmp_path, saved, contract)
    report = load_checks.evaluate(*args)
    rp = tmp_path / "report.json"
    rp.write_bytes(encode(report))
    moved = tmp_path / "relocated"
    moved.mkdir()
    for name in ("spec.json", "load-result.json", "plan.json", "transcript.jsonl"):
        shutil.move(tmp_path / name, moved / name)
    calls, original = Counter(), load_checks.read

    def counted(path, **kwargs):
        calls[str(path)] += 1
        return original(path, **kwargs)

    monkeypatch.setattr(load_checks, "read", counted)
    block_io(monkeypatch)
    xml, verification, counts = load_checks.junit_report(
        rp,
        digest(rp.read_bytes()),
        moved / "spec.json",
        args[1],
        moved / "load-result.json",
        args[3],
    )
    assert counts == {"tests": 8, "failures": 3, "errors": 2, "skipped": 0}
    assert (
        verification["bindings"]["spec"]["path_label_differs"]
        and verification["bindings"]["capture"]["path_label_differs"]
    )
    assert len(ET.fromstring(xml).findall("testcase")) == 8
    assert len(calls) == 5 and all(n == 1 for n in calls.values())


def test_cli_exports_failure_and_error_without_prompt_or_hardware(
    tmp_path, saved, monkeypatch, capsys
):
    contract = spec(phase="observe")
    sp, sh, cp, ch = inputs(tmp_path, saved, contract)
    block_io(monkeypatch)
    report, xml = tmp_path / "new-report.json", tmp_path / "new.xml"
    args = [
        "--spec",
        str(sp),
        "--spec-sha256",
        sh,
        "--capture",
        str(cp),
        "--capture-sha256",
        ch,
        "--json",
    ]
    assert cli.main(["load", "check", *args, "--output", str(report)]) == 3
    checked = json.loads(capsys.readouterr().out)
    assert not checked["ok"] and checked["data"]["report"]["summary"]["invalid_evidence"] == 1
    bound = [str(report), "--sha256", digest(report.read_bytes())]
    assert cli.main(["load", "inspect-check-report", *bound, *args]) == 0
    assert json.loads(capsys.readouterr().out)["data"]["verification"]["verdict"]["ok"] is False
    assert cli.main(["load", "junit", *bound, *args, "--output", str(xml)]) == 3
    exported = json.loads(capsys.readouterr().out)
    assert exported["data"]["junit"]["errors"] == 1
    assert all(
        exported[k] is False
        for k in (
            "hardware_access",
            "outputs_commanded",
            "executables_started",
            "authorization_granted",
        )
    )
    monkeypatch.setattr(load_checks, "evaluate", forbid)
    assert cli.main(["load", "check", *args, "--output", str(report)]) == 2
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "OUTPUT_EXISTS"


def test_spec_and_source_byte_limits_and_snapshot_reuse(tmp_path, saved, monkeypatch):
    sp, _, cp, ch = inputs(tmp_path, saved)
    sp.write_bytes(b" " * 65537)
    with pytest.raises(LabError, match="byte limit"):
        load_checks.evaluate(sp, digest(sp.read_bytes()), cp, ch)
    sp.write_bytes(encode(spec()))
    cp.write_bytes(b" " * (1024 * 1024 + 1))
    with pytest.raises(LabError, match="byte limit"):
        load_checks.evaluate(sp, digest(sp.read_bytes()), cp, digest(cp.read_bytes()))
    path = tmp_path / "snapshot"
    path.write_bytes(b"one")
    snapshot = load_checks.Snapshot()
    assert snapshot.read(path)[0] == b"one"
    path.write_bytes(b"two")
    monkeypatch.setattr(load_checks, "read", forbid)
    assert snapshot.read(path, expected=digest(b"one"))[0] == b"one"
    with pytest.raises(LabError, match="SHA-256"):
        snapshot.read(path, expected=digest(b"two"))


def test_old_waveform_09_remains_verifiable_without_relabelling(tmp_path):
    from waveform_fixtures import capture, spec

    sp, cp = tmp_path / "spec.json", tmp_path / "capture.json"
    sp.write_bytes(encode(spec()))
    cp.write_bytes(encode(capture()))
    args = sp, digest(sp.read_bytes()), cp, digest(cp.read_bytes())
    report = waveform.evaluate(*args)
    report["runner_version"] = "0.9.0"
    rp = tmp_path / "report.json"
    rp.write_bytes(encode(report))
    original, verified = waveform.inspect_report(rp, digest(rp.read_bytes()), *args)
    assert original["runner_version"] == verified["producer_runner_version"] == "0.9.0"
    assert verified["verifier_version"] == waveform.__version__


def test_eight_checks_over_maximum_native_window_keep_all_samples(tmp_path):
    directory = tmp_path / "native-max"
    acquire(directory, count=50)
    contract = spec(count=50)
    contract["checks"] = [
        {**copy.deepcopy(contract["checks"][0]), "id": "full" + str(i)} for i in range(8)
    ]
    sp = tmp_path / "max.json"
    sp.write_bytes(encode(contract))
    cp = directory / "load-result.json"
    report = load_checks.evaluate(sp, digest(sp.read_bytes()), cp, digest(cp.read_bytes()))
    assert report["ok"] and report["summary"]["passed"] == 8
    assert all(len(check["measurements"]) == 50 for check in report["checks"])
    assert len(encode(report)) <= 1024 * 1024


@pytest.mark.parametrize("kind", ["duplicate-spec", "wrong-sha", "truncated-transcript"])
def test_invalid_saved_bytes_do_not_create_a_report(tmp_path, saved, kind, capsys):
    sp, sh, cp, ch = inputs(tmp_path, saved)
    if kind == "duplicate-spec":
        sp.write_bytes(b'{"schema_version":"x","schema_version":"x"}')
        sh = digest(sp.read_bytes())
    elif kind == "wrong-sha":
        ch = "0" * 64
    else:
        raw = (tmp_path / "transcript.jsonl").read_bytes()[:-1]
        (tmp_path / "transcript.jsonl").write_bytes(raw)
        result = json.loads(cp.read_bytes())
        result["transcript"] = {"sha256": digest(raw), "size_bytes": len(raw)}
        cp, ch = rebind(tmp_path, result)
    output = tmp_path / "absent-report.json"
    assert (
        cli.main(
            [
                "load",
                "check",
                "--spec",
                str(sp),
                "--spec-sha256",
                sh,
                "--capture",
                str(cp),
                "--capture-sha256",
                ch,
                "--output",
                str(output),
                "--json",
            ]
        )
        == 2
    )
    assert not output.exists()
    assert not json.loads(capsys.readouterr().out)["ok"]
