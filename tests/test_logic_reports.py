from __future__ import annotations

import base64
import json
import socket
import subprocess
import xml.etree.ElementTree as ET

import pytest
from check_fixtures import fixture
from logic_fixtures import save

from lab_instruments import logic_checks, logic_reports
from lab_instruments.cli import main
from lab_instruments.common import LabError, digest, encode
from lab_instruments.dwf import Dwf


def inputs(tmp_path, *, verdict="passed", version=None):
    capture, spec = fixture()
    if verdict == "failed":
        spec["checks"][0]["expect"]["bytes_hex"] = "00"
    elif verdict == "invalid_evidence":
        capture.update(ok=False, status="failed", error={"code": "CLEANUP_INCOMPLETE"})
        capture["cleanup"]["complete"] = False
    sp, sh = save(tmp_path, spec, "spec.json")
    cp, ch = save(tmp_path, capture, "capture.json")
    report = logic_checks.evaluate(sp, sh, cp, ch)
    if version:
        report["runner_version"] = version
    rp, rh = save(tmp_path, report, "report.json")
    return (rp, rh, sp, sh, cp, ch), report


def cli_args(args, operation="inspect-check-report"):
    rp, rh, sp, sh, cp, ch = args
    return [
        "logic",
        operation,
        str(rp),
        "--sha256",
        rh,
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


@pytest.mark.parametrize("version", ["0.7.0", "0.8.0"])
@pytest.mark.parametrize("verdict", ["passed", "failed", "invalid_evidence"])
def test_bound_inspection_preserves_verdict_and_origin(tmp_path, version, verdict):
    args, original = inputs(tmp_path, version=version, verdict=verdict)
    result = logic_reports.inspect_report(*args)
    assert result["ok"] and result["status"] == "verified" and result["complete"]
    assert result["producer_runner_version"] == version
    assert result["verdict"] == {key: original[key] for key in ("ok", "status", "summary")}
    assert result["checks"] == [
        {key: c[key] for key in ("id", "kind", "status")} for c in original["checks"]
    ]
    assert result["evidence_origin"] == "test_double"
    assert all(result[k] is False for k in logic_reports.FLAGS)
    assert not result["recorded_paths_followed"] and not result["report_authenticated"]
    assert result["report"]["sha256"] == args[1]


def test_explicit_copies_work_without_following_recorded_paths_and_read_once(tmp_path, monkeypatch):
    args, report = inputs(tmp_path)
    copied = tmp_path / "copied"
    copied.mkdir()
    updated = list(args)
    for index in (0, 2, 4):
        updated[index] = copied / args[index].name
        updated[index].write_bytes(args[index].read_bytes())
        args[index].unlink()
    calls = []
    original_read = logic_reports.read

    def read_once(path, **kwargs):
        assert path not in calls
        calls.append(path)
        raw, ref = original_read(path, **kwargs)
        # Later checks must use these bytes, even if the file then changes.
        path.write_bytes(b"changed after snapshot")
        return raw, ref

    def forbidden(*args, **kwargs):
        raise AssertionError("offline verification must not access devices/processes")

    monkeypatch.setattr(logic_reports, "read", read_once)
    monkeypatch.setattr(Dwf, "load", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    raw, verified, counts = logic_reports.junit_report(*updated)
    assert calls == [updated[i] for i in (0, 2, 4)]
    assert counts == {"tests": 4, "failures": 0, "errors": 0, "skipped": 0}
    assert ET.fromstring(raw).tag == "testsuite"
    for name in ("spec", "capture"):
        binding = verified[name + "_binding"]
        assert binding["path_label_differs"] and binding["recorded"] == report[name]
        assert binding["supplied"]["path"].startswith(str(copied))


@pytest.mark.parametrize(
    "verdict,failures,errors", [("passed", 0, 0), ("failed", 1, 0), ("invalid_evidence", 0, 4)]
)
def test_junit_has_one_case_per_check_and_never_skips_failures(tmp_path, verdict, failures, errors):
    args, report = inputs(tmp_path, verdict=verdict)
    raw, verified, counts = logic_reports.junit_report(*args)
    assert raw == logic_reports.junit_report(*args)[0]
    root = ET.fromstring(raw)
    assert root.attrib == {
        "name": "lab-instruments.logic",
        "tests": "4",
        "failures": str(failures),
        "errors": str(errors),
        "skipped": "0",
    }
    assert counts == {"tests": 4, "failures": failures, "errors": errors, "skipped": 0}
    assert len(root.findall("testcase")) == 4
    assert len(root.findall("testcase/failure")) == failures
    assert len(root.findall("testcase/error")) == errors
    assert not root.findall("testcase/skipped")
    assert [c.attrib["name"] for c in root.findall("testcase")] == [
        c["id"] for c in report["checks"]
    ]
    props = {p.attrib["name"]: p.attrib["value"] for p in root.findall("properties/property")}
    assert props["evidence_origin"] == "test_double"
    assert props["report_sha256"] == args[1] and props["capture_sha256"] == args[5]
    assert all(props[k] == "false" for k in logic_reports.FLAGS)
    assert props["report_authenticated"] == props["duration_measured"] == "false"
    for failed in root.findall("testcase/failure") + root.findall("testcase/error"):
        detail = json.loads(failed.text)
        assert detail["native_sha256"] and detail["source_cleanup"]
        if errors:
            assert detail["source_error"]["code"] == "CLEANUP_INCOMPLETE"
            assert detail["assertions"][1]["status"] == "not_evaluated"
    assert verified["verdict"]["ok"] is (verdict == "passed")


@pytest.mark.parametrize(
    "mutation",
    [
        "overall_ok",
        "overall_status",
        "complete",
        "summary",
        "dropped_check",
        "check_order",
        "check_ok",
        "check_status",
        "expectation",
        "configuration",
        "assertion_actual",
        "assertion_status",
        "native_hash",
        "native_data_rehashed",
        "native_span_rehashed",
        "native_source_rehashed",
        "extra",
        "missing",
        "bool_as_int",
        "int_as_float",
        *logic_reports.FLAGS,
    ],
)
def test_rehashed_report_edits_cannot_survive_recomputation(tmp_path, mutation):
    args, report = inputs(tmp_path)
    first = report["checks"][0]
    if mutation == "overall_ok":
        report["ok"] = False
    elif mutation == "overall_status":
        report["status"] = "failed"
    elif mutation == "complete":
        report["complete"] = False
    elif mutation == "summary":
        report["summary"]["passed"] = 3
    elif mutation == "dropped_check":
        report["checks"].pop()
    elif mutation == "check_order":
        report["checks"].reverse()
    elif mutation == "check_ok":
        first["ok"] = False
    elif mutation == "check_status":
        first["status"] = "failed"
    elif mutation == "expectation":
        first["expect"]["bytes_hex"] = "00"
    elif mutation == "configuration":
        first["configuration"]["baud"] = 200000
    elif mutation == "assertion_actual":
        first["assertions"][1]["actual"] = "00"
    elif mutation == "assertion_status":
        first["assertions"][1]["status"] = "failed"
    elif mutation == "native_hash":
        first["native_sha256"] = "0" * 64
    elif mutation.startswith("native_"):
        native = first["native"]
        if mutation == "native_data_rehashed":
            native["analysis"]["bytes_base64"] = "AA=="
        elif mutation == "native_span_rehashed":
            native["analysis"]["frames"][0]["raw_span"]["start"] += 1
        else:
            native["source_status"] = "failed"
        first["native_sha256"] = digest(encode(native))
    elif mutation == "extra":
        report["unrecognized"] = True
    elif mutation == "missing":
        del report["scope"]
    elif mutation == "bool_as_int":
        report["ok"] = 1
    elif mutation == "int_as_float":
        report["summary"]["checks"] = 4.0
    else:
        report[mutation] = True
    rp, rh = save(tmp_path, report, "edited.json")
    with pytest.raises(LabError) as error:
        logic_reports.junit_report(rp, rh, *args[2:])
    assert error.value.code == "REPORT_MISMATCH"


@pytest.mark.parametrize("index", [0, 2, 4])
def test_all_three_file_hashes_are_mandatory_and_checked(tmp_path, index):
    args, _ = inputs(tmp_path)
    changed = list(args)
    changed[index + 1] = "0" * 64
    with pytest.raises(LabError) as error:
        logic_reports.inspect_report(*changed)
    assert error.value.code == "HASH_MISMATCH"


@pytest.mark.parametrize("source", ["spec", "capture"])
@pytest.mark.parametrize(
    "field", ["sha256", "size_bytes", "extra", "path_control", "path_missing", "bool_size"]
)
def test_input_reference_tampering_is_rejected(tmp_path, source, field):
    args, report = inputs(tmp_path)
    ref = report[source]
    if field == "sha256":
        ref[field] = "0" * 64
    elif field == "size_bytes":
        ref[field] += 1
    elif field == "extra":
        ref["extra"] = True
    elif field == "path_control":
        ref["path"] = "bad\x00path"
    elif field == "path_missing":
        del ref["path"]
    else:
        ref["size_bytes"] = True
    rp, rh = save(tmp_path, report, "edited.json")
    with pytest.raises(LabError):
        logic_reports.inspect_report(rp, rh, *args[2:])


@pytest.mark.parametrize(
    "field,value",
    [
        ("runner_version", "0.6.0"),
        ("runner_version", "0.9.0"),
        ("runner_version", True),
        ("schema_version", "unknown"),
    ],
)
def test_unknown_producers_or_schemas_stop_before_original_inputs(tmp_path, field, value):
    args, report = inputs(tmp_path)
    report[field] = value
    rp, rh = save(tmp_path, report, "edited.json")
    args[2].unlink()
    with pytest.raises(LabError):
        logic_reports.inspect_report(rp, rh, *args[2:])


@pytest.mark.parametrize("changed_input", ["spec", "capture"])
def test_changed_explicit_original_is_not_accepted_even_with_its_new_hash(tmp_path, changed_input):
    args, _ = inputs(tmp_path)
    if changed_input == "spec":
        spec = json.loads(args[2].read_bytes())
        spec["checks"][0]["expect"]["bytes_hex"] = "00"
        sp, sh = save(tmp_path, spec, "changed-spec.json")
        with pytest.raises(LabError) as error:
            logic_reports.inspect_report(*args[:2], sp, sh, *args[4:])
        assert error.value.code == "REPORT_BINDING_MISMATCH"
        return
    capture = json.loads(args[4].read_bytes())
    capture.update(ok=False, status="failed", error={"code": "CLEANUP_INCOMPLETE"})
    capture["cleanup"]["complete"] = False
    cp, ch = save(tmp_path, capture, "changed-capture.json")
    with pytest.raises(LabError) as error:
        logic_reports.inspect_report(*args[:4], cp, ch)
    assert error.value.code == "REPORT_BINDING_MISMATCH"


def test_junit_mixed_data_failure_and_invalid_signal_are_separate(tmp_path):
    capture, spec = fixture()
    # Hold UART idle without changing the other seven captured channels.
    raw = bytearray(base64.b64decode(capture["payload"]["raw_base64"]))
    for index in range(0, len(raw), 2):
        raw[index] |= 1
    capture["payload"].update(raw_base64=base64.b64encode(raw).decode(), sha256=digest(raw))
    spec["checks"][1]["expect"]["minimum_cycles"] = 13
    cp, ch = save(tmp_path, capture, "capture.json")
    sp, sh = save(tmp_path, spec, "spec.json")
    report = logic_checks.evaluate(sp, sh, cp, ch)
    assert [c["status"] for c in report["checks"]] == [
        "invalid_evidence",
        "failed",
        "passed",
        "passed",
    ]
    rp, rh = save(tmp_path, report, "report.json")
    raw, verified, counts = logic_reports.junit_report(rp, rh, sp, sh, cp, ch)
    assert counts == {"tests": 4, "failures": 1, "errors": 1, "skipped": 0}
    assert not verified["verdict"]["ok"]
    root = ET.fromstring(raw)
    assert root.find("testcase[@name='console']/error") is not None
    assert root.find("testcase[@name='pwm']/failure") is not None


def test_json_formatting_is_not_an_assertion_but_input_bytes_stay_bound(tmp_path):
    args, report = inputs(tmp_path)
    raw = json.dumps(report, indent=2).encode()
    args[0].write_bytes(raw)
    assert logic_reports.inspect_report(args[0], digest(raw), *args[2:])["ok"]


def test_duplicate_json_fields_and_oversized_report_are_rejected(tmp_path):
    for raw in (b'{"a":1,"a":2}', b" " * (1024 * 1024 + 1)):
        path = tmp_path / "bad.json"
        path.write_bytes(raw)
        with pytest.raises(LabError):
            logic_reports.inspect_report(path, digest(raw), "absent", "0" * 64, "absent", "0" * 64)


def test_xml_escapes_untrusted_diagnostics_and_retains_source_failure(tmp_path):
    args, _ = inputs(tmp_path, verdict="invalid_evidence")
    capture = json.loads(args[4].read_bytes())
    message = '</error><testcase name="forged"/> & \x01 中文'
    capture["error"]["message"] = message
    cp, ch = save(tmp_path, capture, "xml-capture.json")
    report = logic_checks.evaluate(args[2], args[3], cp, ch)
    rp, rh = save(tmp_path, report, "xml-report.json")
    raw, _, counts = logic_reports.junit_report(rp, rh, args[2], args[3], cp, ch)
    root = ET.fromstring(raw)
    assert counts["errors"] == 4 and len(root.findall("testcase")) == 4
    assert json.loads(root.find("testcase/error").text)["source_error"]["message"] == message
    assert b"&lt;" in raw and b"&amp;" in raw and b"\x01" not in raw


@pytest.mark.parametrize("verdict,code", [("passed", 0), ("failed", 3), ("invalid_evidence", 3)])
def test_cli_inspection_and_export_have_distinct_exit_semantics(tmp_path, capsys, verdict, code):
    args, _ = inputs(tmp_path, verdict=verdict)
    assert main(cli_args(args)) == 0
    inspected = json.loads(capsys.readouterr().out)
    assert inspected["ok"] and inspected["data"]["verification"]["verdict"]["ok"] is (code == 0)
    output = tmp_path / "results.xml"
    command = [*cli_args(args, "junit"), "--output", str(output)]
    assert main(command) == code
    exported = json.loads(capsys.readouterr().out)
    assert exported["ok"] is (code == 0) and exported["data"]["verification"]["ok"]
    assert exported["data"]["output"]["sha256"] == digest(output.read_bytes())
    assert main(command) == 2
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "OUTPUT_EXISTS"


def test_existing_output_is_rejected_before_any_input_read(tmp_path, monkeypatch, capsys):
    args, _ = inputs(tmp_path)
    output = tmp_path / "existing.xml"
    output.write_bytes(b"preserve")

    def forbidden(*args, **kwargs):
        raise AssertionError("must preflight output first")

    monkeypatch.setattr(logic_reports, "read", forbidden)
    assert main([*cli_args(args, "junit"), "--output", str(output)]) == 2
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "OUTPUT_EXISTS"
    assert output.read_bytes() == b"preserve"


def test_bad_report_never_writes_xml(tmp_path, capsys):
    args, report = inputs(tmp_path, verdict="failed")
    report["ok"] = True
    rp, rh = save(tmp_path, report, "edited.json")
    output = tmp_path / "absent.xml"
    assert main([*cli_args((rp, rh, *args[2:]), "junit"), "--output", str(output)]) == 2
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "REPORT_MISMATCH"
    assert not output.exists()


def test_xml_limit_is_enforced_without_truncation(tmp_path, monkeypatch):
    args, _ = inputs(tmp_path)
    monkeypatch.setattr(logic_reports, "MAX_JSON_BYTES", 100)
    with pytest.raises(LabError) as error:
        logic_reports.junit_report(*args)
    assert error.value.code == "LIMIT_EXCEEDED"


def test_cli_envelope_limit_is_checked_before_xml_write(tmp_path, monkeypatch, capsys):
    from lab_instruments import cli

    args, _ = inputs(tmp_path)
    output = tmp_path / "absent.xml"
    actual_encode = cli.encode

    def encode_bounded(value):
        if "data" in value:
            raise LabError("LIMIT_EXCEEDED", "test envelope limit")
        return actual_encode(value)

    monkeypatch.setattr(cli, "encode", encode_bounded)
    assert main([*cli_args(args, "junit"), "--output", str(output)]) == 2
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "LIMIT_EXCEEDED"
    assert not output.exists()
