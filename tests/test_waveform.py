from __future__ import annotations

import copy
import ctypes
import json
import math
import shutil
import socket
import struct
import subprocess
import xml.etree.ElementTree as ET
from collections import Counter

import pytest
from sds_double import SdsDouble, block, preamble
from waveform_fixtures import capture, spec

from lab_instruments import cli, sds, sds_plan, waveform
from lab_instruments.common import LabError, digest, encode


def save(root, name, value):
    path = root / name
    path.write_bytes(encode(value))
    return path, digest(path.read_bytes())


def evaluate(root, contract=None, source=None):
    sp, sh = save(root, "spec.json", contract or spec())
    cp, ch = save(root, "capture.json", source or capture())
    return waveform.evaluate(sp, sh, cp, ch), (sp, sh, cp, ch)


def forbid(*args, **kwargs):
    raise AssertionError("offline operation attempted hardware/network/subprocess/SDK")


def block_io(monkeypatch):
    monkeypatch.setattr(socket, "socket", forbid)
    monkeypatch.setattr(subprocess, "run", forbid)
    monkeypatch.setattr(subprocess, "Popen", forbid)
    monkeypatch.setattr(ctypes, "CDLL", forbid)


def test_ad3_exact_statistics_windows_spans_and_read_once(tmp_path, monkeypatch):
    block_io(monkeypatch)
    calls = Counter()
    original = waveform.read

    def counted(path, **kwargs):
        calls[str(path)] += 1
        return original(path, **kwargs)

    monkeypatch.setattr(waveform, "read", counted)
    contract = spec()
    second = copy.deepcopy(contract["checks"][0])
    second.update(
        id="negative",
        channel=2,
        window={"start": 1, "end_exclusive": 2},
        expect={"mean_v": {"minimum": 3, "maximum": 3}},
    )
    contract["checks"].append(second)
    report, _ = evaluate(tmp_path, contract)
    assert report["ok"] and report["summary"] == {
        "checks": 2,
        "passed": 2,
        "failed": 0,
        "invalid_evidence": 0,
    }
    first, second = report["checks"]
    assert first["metrics"]["rms_v"] == pytest.approx(math.sqrt(10))
    assert first["metrics"]["ac_rms_v"] == 1
    assert first["raw_span"] == {"start": 0, "end_exclusive": 128}
    assert second["raw_span"] == {"start": 136, "end_exclusive": 144}
    assert second["metrics"]["ac_rms_v"] == 0
    assert second["first_time_s"] == second["last_time_s"] == 1e-6
    assert report["source"]["evidence_origin"] == "test_double"
    assert all(report[k] is False for k in waveform.FLAGS)
    assert len(calls) == 2 and all(n == 1 for n in calls.values())


@pytest.mark.parametrize(
    "values,mean,rms,ac",
    [
        ([3.3] * 16, 3.3, 3.3, 0),
        ([-4.0] * 16, -4, 4, 0),
        ([0.0] * 16, 0, 0, 0),
        ([1e100, -1e100] * 8, 0, 1e100, 1e100),
        ([1e12 - 1, 1e12 + 1] * 8, 1e12, None, 1),
    ],
)
def test_known_dc_symmetric_and_large_offset_statistics(values, mean, rms, ac):
    metrics = waveform.statistics(values)
    assert metrics["mean_v"] == pytest.approx(mean)
    if rms is not None:
        assert metrics["rms_v"] == pytest.approx(rms)
    assert metrics["ac_rms_v"] == pytest.approx(ac)
    assert all(math.isfinite(v) for v in metrics.values())


@pytest.mark.parametrize(
    "mutation",
    [
        "empty",
        "unknown",
        "bool-channel",
        "bool-bound",
        "float-window",
        "reversed-window",
        "overflow-window",
        "inverted-bounds",
        "negative-rms",
        "duplicate-id",
        "source",
        "extra-field",
        "too-many",
        "nan",
    ],
)
def test_malformed_spec_rejected_before_source_read(tmp_path, monkeypatch, mutation):
    contract = spec()
    check = contract["checks"][0]
    if mutation == "empty":
        check["expect"] = {}
    elif mutation == "unknown":
        check["expect"]["frequency_hz"] = {"minimum": 1, "maximum": 2}
    elif mutation == "bool-channel":
        check["channel"] = True
    elif mutation == "bool-bound":
        check["expect"]["mean_v"]["minimum"] = True
    elif mutation == "float-window":
        check["window"]["start"] = 0.0
    elif mutation == "reversed-window":
        check["window"]["end_exclusive"] = 0
    elif mutation == "overflow-window":
        check["window"]["end_exclusive"] = 16385
    elif mutation == "inverted-bounds":
        check["expect"]["mean_v"] = {"minimum": 5, "maximum": 4}
    elif mutation == "negative-rms":
        check["expect"]["rms_v"]["minimum"] = -1
    elif mutation == "duplicate-id":
        contract["checks"].append(copy.deepcopy(check))
    elif mutation == "source":
        contract["source_kind"] = "guess"
    elif mutation == "extra-field":
        contract["run"] = "evil"
    elif mutation == "too-many":
        contract["checks"] = [check] * 9
    else:
        check["expect"]["mean_v"]["minimum"] = float("nan")
    monkeypatch.setattr(waveform, "source", forbid)
    with pytest.raises(LabError):
        if mutation == "nan":
            waveform.validate(contract)
        else:
            path, sha = save(tmp_path, "bad.json", contract)
            waveform.evaluate(path, sha, tmp_path / "not-read", "0" * 64)


@pytest.mark.parametrize(
    "mutation",
    ["payload", "cleanup", "rate", "samples", "scale", "clock", "claim", "logic", "failed"],
)
def test_invalid_ad3_source_never_becomes_a_pass(tmp_path, mutation):
    value = capture()
    if mutation == "payload":
        value["payload"]["sha256"] = "0" * 64
    elif mutation == "cleanup":
        value["cleanup"]["input_stopped"] = False
    elif mutation == "rate":
        value["settings"]["sample_rate_hz"] = 2_000_000
    elif mutation == "samples":
        value["settings"]["samples"] = True
    elif mutation == "scale":
        value["settings"]["channel_scales"]["1"]["offset_v"] = 1
    elif mutation == "clock":
        value["payload"]["time_semantics"] = "UTC"
    elif mutation == "claim":
        value["physical_device_verified"] = True
    elif mutation == "logic":
        value["instrument"] = "logic"
    else:
        value.update(ok=False, status="failed")
    with pytest.raises(LabError):
        evaluate(tmp_path, source=value)


@pytest.mark.parametrize("mode", ["failed", "window", "channel"])
def test_failed_assertion_and_unavailable_evidence_are_distinct(tmp_path, mode):
    contract = spec()
    if mode == "failed":
        contract["checks"][0]["expect"]["mean_v"] = {"minimum": 3.2, "maximum": 3.4}
    elif mode == "window":
        contract["checks"][0]["window"]["end_exclusive"] = 17
    else:
        contract["checks"][0]["channel"] = 2
    report, _ = evaluate(tmp_path, contract, capture({1: [2, 4] * 8}))
    assert not report["ok"]
    check = report["checks"][0]
    assert check["status"] == ("failed" if mode == "failed" else "invalid_evidence")
    if mode != "failed":
        assert check["metrics"] is None and check["assertions"] == []


@pytest.fixture(scope="module")
def saved_sds(tmp_path_factory):
    directory = tmp_path_factory.mktemp("waveform-sds")
    with SdsDouble() as peer:
        plan = sds_plan.create(
            "127.0.0.1",
            peer.serial,
            "1.1.6.5",
            port=peer.port,
            points=16,
            test_double=True,
            io_timeout_ms=2000,
        )
        (directory / "plan.json").write_bytes(encode(plan))
        result = sds.perform(plan, directory)
        assert result["ok"], json.dumps(result)
    return {p.name: p.read_bytes() for p in directory.iterdir() if p.is_file()}


def test_sds_reuses_verified_wire_conversion_and_each_file_read_once(
    tmp_path, saved_sds, monkeypatch
):
    for name, raw in saved_sds.items():
        (tmp_path / name).write_bytes(raw)
    block_io(monkeypatch)
    calls = Counter()
    original = waveform.read

    def counted(path, **kwargs):
        calls[str(path)] += 1
        return original(path, **kwargs)

    monkeypatch.setattr(waveform, "read", counted)
    contract = spec("sds804x")
    contract["checks"][0]["expect"] = {"mean_v": {"minimum": -100, "maximum": 100}}
    sp, sh = save(tmp_path, "spec.json", contract)
    cp = tmp_path / "scope-result.json"
    report = waveform.evaluate(sp, sh, cp, digest(cp.read_bytes()))
    assert report["ok"]
    check = report["checks"][0]
    assert check["source_samples"]["descriptor"]["probe_factor"] == 10
    assert check["metrics"]["minimum_v"] == pytest.approx(-23.1)
    assert check["metrics"]["maximum_v"] == pytest.approx(28.0875)
    assert check["metrics"]["mean_v"] == pytest.approx(0.89765625)
    assert check["raw_span"] == {"start": 0, "end_exclusive": 32}
    assert check["first_time_s"] == pytest.approx(-0.003)
    assert report["source"]["source_freshness"]["basis"] == "existing_stopped_waveform_age_unknown"
    assert all(n == 1 for n in calls.values())
    assert report["source"]["evidence_origin"] == "test_double"
    assert all(report[k] is False for k in waveform.FLAGS)


@pytest.mark.parametrize(
    "artifact", ["c1-words.bin", "c1-preamble.bin", "c1-samples.csv", "transcript.jsonl"]
)
def test_sds_tamper_is_rejected(tmp_path, saved_sds, artifact):
    for name, raw in saved_sds.items():
        (tmp_path / name).write_bytes(raw)
    (tmp_path / artifact).write_bytes(b"edited")
    sp, sh = save(tmp_path, "spec.json", spec("sds804x"))
    cp = tmp_path / "scope-result.json"
    with pytest.raises(LabError):
        waveform.evaluate(sp, sh, cp, digest(cp.read_bytes()))


@pytest.mark.parametrize("mode", ["passed", "failed", "invalid"])
def test_recomputation_junit_and_path_relocation(tmp_path, mode, monkeypatch):
    block_io(monkeypatch)
    contract = spec()
    if mode == "failed":
        contract["checks"][0]["expect"]["mean_v"] = {"minimum": 4, "maximum": 5}
    if mode == "invalid":
        contract["checks"][0]["window"]["end_exclusive"] = 17
    report, inputs = evaluate(tmp_path, contract)
    rp, rh = save(tmp_path, "report.json", report)
    relocated = tmp_path / "relocated"
    relocated.mkdir()
    sp, sh, cp, ch = inputs
    shutil.copyfile(sp, relocated / "new-spec.json")
    shutil.copyfile(cp, relocated / "new-capture.json")
    args = (rp, rh, relocated / "new-spec.json", sh, relocated / "new-capture.json", ch)
    raw, verification, counts = waveform.junit_report(*args)
    assert verification["ok"] and verification["verdict"]["ok"] is (mode == "passed")
    assert verification["bindings"]["capture"]["path_label_differs"]
    assert counts == {
        "tests": 1,
        "failures": int(mode == "failed"),
        "errors": int(mode == "invalid"),
        "skipped": 0,
    }
    xml = ET.fromstring(raw)
    assert len(xml.findall("testcase")) == 1 and not xml.findall(".//skipped")


@pytest.mark.parametrize("field", ["metrics", "verdict", "raw-span", "claim", "bool"])
def test_rehashed_report_edits_are_not_trusted(tmp_path, field):
    report, inputs = evaluate(tmp_path)
    if field == "metrics":
        report["checks"][0]["metrics"]["mean_v"] = 3.1
    elif field == "verdict":
        report["ok"] = False
    elif field == "raw-span":
        report["checks"][0]["raw_span"]["start"] = 8
    elif field == "claim":
        report["calibration_verified"] = True
    else:
        report["complete"] = 1
    rp, rh = save(tmp_path, "edited.json", report)
    with pytest.raises(LabError, match="differs from recomputation"):
        waveform.inspect_report(rp, rh, *inputs)


def test_snapshot_aggregate_budget_and_no_reopen(tmp_path, monkeypatch):
    path = tmp_path / "one"
    path.write_bytes(b"one")
    snapshot = waveform.Snapshot()
    assert snapshot.read(path)[0] == b"one"
    path.write_bytes(b"two")
    assert snapshot.read(path)[0] == b"one"
    snapshot.size = 8 * 1024 * 1024
    monkeypatch.setattr(waveform, "read", forbid)
    with pytest.raises(LabError, match="budget exceeded"):
        snapshot.read(tmp_path / "new")


def test_report_recheck_reads_each_explicit_input_once(tmp_path, monkeypatch):
    report, inputs = evaluate(tmp_path)
    path, sha = save(tmp_path, "report.json", report)
    original = waveform.read
    calls = Counter()

    def counted(path, **kwargs):
        calls[str(path)] += 1
        return original(path, **kwargs)

    monkeypatch.setattr(waveform, "read", counted)
    waveform.inspect_report(path, sha, *inputs)
    assert len(calls) == 3 and set(calls.values()) == {1}


def test_sds_ac_coupling_is_qualified_not_mistaken_for_dc(tmp_path, monkeypatch):
    def ac(peer, command, response):
        if command.endswith(":COUP?"):
            return "AC"
        if command == ":WAV:PRE?":
            data = bytearray(preamble(peer.channel, peer.points))
            struct.pack_into("<h", data, 326, 1)
            return block(bytes(data))
        return response

    with SdsDouble(ac) as peer:
        plan = sds_plan.create(
            "127.0.0.1",
            peer.serial,
            "1.1.6.5",
            port=peer.port,
            points=16,
            test_double=True,
            io_timeout_ms=2000,
        )
        (tmp_path / "plan.json").write_bytes(encode(plan))
        result = sds.perform(plan, tmp_path)
        assert result["ok"], result
    block_io(monkeypatch)
    sp, sh = save(tmp_path, "spec.json", spec("sds804x"))
    cp = tmp_path / "scope-result.json"
    report = waveform.evaluate(sp, sh, cp, digest(cp.read_bytes()))
    assert report["checks"][0]["status"] == "invalid_evidence"
    assert report["checks"][0]["reason"] == "requires_dc_coupling"


def test_cli_failure_report_and_exclusive_output(tmp_path, capsys, monkeypatch):
    block_io(monkeypatch)
    contract = spec()
    contract["checks"][0]["expect"]["mean_v"] = {"minimum": 4, "maximum": 5}
    _, inputs = evaluate(tmp_path, contract)
    sp, sh, cp, ch = inputs
    output = tmp_path / "report.json"
    args = [
        "waveform",
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
    assert cli.main(args) == 3
    result = json.loads(capsys.readouterr().out)
    assert not result["ok"] and result["data"]["report"]["summary"]["failed"] == 1
    original = output.read_bytes()
    assert cli.main(args) == 2
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "OUTPUT_EXISTS"
    assert output.read_bytes() == original
