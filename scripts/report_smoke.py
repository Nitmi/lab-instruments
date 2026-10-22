"""Installed saved-report verification/JUnit checks; synthetic inputs only."""

from __future__ import annotations

import hashlib
import json
import xml.etree.ElementTree as ET


def verify(run, cli, root):
    def sha(path):
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def args(
        operation, report="checks-passed-report", spec="checks-spec", capture="checks-capture"
    ):
        rp, sp, cp = (root / (name + ".json") for name in (report, spec, capture))
        return [
            *cli,
            "logic",
            operation,
            str(rp),
            "--sha256",
            sha(rp),
            "--spec",
            str(sp),
            "--spec-sha256",
            sha(sp),
            "--capture",
            str(cp),
            "--capture-sha256",
            sha(cp),
            "--json",
        ]

    scenarios = (
        ("passed", "checks-passed-report", "checks-spec", "checks-capture", 0, 0),
        ("failed", "checks-failed-report", "checks-mismatch", "checks-capture", 1, 0),
        ("source", "checks-source-report", "checks-spec", "checks-failed-source", 0, 4),
    )
    for label, report, spec, capture, failures, errors in scenarios:
        inspected = json.loads(
            run("report-inspect-" + label, args("inspect-check-report", report, spec, capture))
        )
        verified = inspected["data"]["verification"]
        assert inspected["ok"] and verified["ok"] and verified["evidence_origin"] == "test_double"
        assert verified["verdict"]["ok"] is (label == "passed")
        assert not verified["hardware_access"] and not verified["executables_started"]
        output = root / ("junit-" + label + ".xml")
        command = [*args("junit", report, spec, capture), "--output", str(output)]
        exported = json.loads(run("report-junit-" + label, command, 0 if label == "passed" else 3))
        counts = {"tests": 4, "failures": failures, "errors": errors, "skipped": 0}
        assert exported["data"]["junit"] == counts
        assert exported["data"]["output"]["sha256"] == sha(output)
        xml = ET.fromstring(output.read_bytes())
        assert all(xml.attrib[k] == str(v) for k, v in counts.items())
        assert len(xml.findall("testcase/failure")) == failures
        assert len(xml.findall("testcase/error")) == errors
        assert len(xml.findall("testcase")) == 4 and not xml.findall("testcase/skipped")
    assert json.loads(run("report-junit-overwrite", command, 2))["error"]["code"] == "OUTPUT_EXISTS"
    original = root / "checks-passed-report.json"
    report = json.loads(original.read_bytes())
    report["checks"][0]["native"]["analysis"]["bytes_base64"] = "AA=="
    encoded = (
        json.dumps(
            report["checks"][0]["native"], sort_keys=True, ensure_ascii=True, separators=(",", ":")
        )
        + "\n"
    )
    report["checks"][0]["native_sha256"] = hashlib.sha256(encoded.encode("ascii")).hexdigest()
    edited = root / "report-edited.json"
    edited.write_text(json.dumps(report), encoding="utf-8")
    output = root / "report-rejected.xml"
    rejected = json.loads(
        run("report-junit-edited", [*args("junit", "report-edited"), "--output", str(output)], 2)
    )
    assert rejected["error"]["code"] == "REPORT_MISMATCH" and not output.exists()
    wrong = args("inspect-check-report")
    wrong[wrong.index("--sha256") + 1] = "0" * 64
    assert json.loads(run("report-wrong-hash", wrong, 2))["error"]["code"] == "HASH_MISMATCH"
    # Copy the inputs and delete these disposable originals: recorded paths must
    # not be followed during binding verification or XML generation.
    for name in ("checks-spec", "checks-capture"):
        path = root / (name + ".json")
        (root / (name + "-moved.json")).write_bytes(path.read_bytes())
    moved = args("inspect-check-report", spec="checks-spec-moved", capture="checks-capture-moved")
    for name in ("checks-spec", "checks-capture"):
        (root / (name + ".json")).unlink()
    result = json.loads(run("report-relocated", moved))["data"]["verification"]
    assert (
        result["spec_binding"]["path_label_differs"]
        and result["capture_binding"]["path_label_differs"]
    )
    return {"ok": True, "synthetic_only": True, "hardware_access": False}
