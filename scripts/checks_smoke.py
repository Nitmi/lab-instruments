"""Installed offline assertion checks; all inputs are explicitly synthetic."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


def verify(run, cli, root):
    tests = Path(__file__).resolve().parents[1] / "tests"
    producer = (
        f"import sys,pathlib;sys.path.insert(0,{str(tests)!r});"
        "from check_fixtures import fixture;from logic_fixtures import save;"
        f"p=pathlib.Path({str(root)!r});"
        "data,spec=fixture();save(p,data,'checks-capture.json');"
        "save(p,spec,'checks-spec.json');"
        "spec['checks'][0]['expect']['bytes_hex']='00';"
        "save(p,spec,'checks-mismatch.json');"
        "data.update(ok=False,status='failed',error={'code':'CLEANUP_INCOMPLETE'});"
        "data['cleanup']['complete']=False;"
        "save(p,data,'checks-failed-source.json');"
        "spec['checks'][0]['arbitrary_command']='not allowed';"
        "save(p,spec,'checks-invalid-spec.json')"
    )
    run("checks-fixtures", [*cli[:2], "-c", producer])
    names = (
        "checks-capture",
        "checks-spec",
        "checks-mismatch",
        "checks-failed-source",
        "checks-invalid-spec",
    )
    hashes = {
        name: hashlib.sha256((root / (name + ".json")).read_bytes()).hexdigest() for name in names
    }

    def args(spec="checks-spec", source="checks-capture"):
        return [
            *cli,
            "logic",
            "check",
            "--spec",
            str(root / (spec + ".json")),
            "--spec-sha256",
            hashes[spec],
            "--capture",
            str(root / (source + ".json")),
            "--capture-sha256",
            hashes[source],
            "--json",
        ]

    command = [*args(), "--output", str(root / "checks-passed-report.json")]
    passed = json.loads(run("checks-passed", command))
    report = passed["data"]["report"]
    assert passed["ok"] and report["status"] == "passed" and report["complete"]
    assert report["summary"] == {"checks": 4, "passed": 4, "failed": 0}
    assert report["evidence_origin"] == "test_double"
    for check in report["checks"]:
        assert check["ok"] and check["native"]["ok"]
        assert check["native"]["capture"]["sha256"] == hashes["checks-capture"]
    assert all(
        report[k] is False
        for k in (
            "hardware_access",
            "executables_started",
            "physical_device_verified",
            "physical_timing_verified",
            "protocol_timing_compliance_verified",
            "authorization_granted",
        )
    )
    report_path = root / "checks-passed-report.json"
    assert (
        hashlib.sha256(report_path.read_bytes()).hexdigest() == passed["data"]["output"]["sha256"]
    )
    assert json.loads(run("checks-overwrite", command, 2))["error"]["code"] == "OUTPUT_EXISTS"
    failed = json.loads(
        run(
            "checks-mismatch",
            [*args("checks-mismatch"), "--output", str(root / "checks-failed-report.json")],
            3,
        )
    )
    assert failed["data"]["report"]["summary"] == {"checks": 4, "passed": 3, "failed": 1}
    failed_source = json.loads(run("checks-source-failed", args(source="checks-failed-source"), 3))
    assert all(c["status"] == "invalid_evidence" for c in failed_source["data"]["report"]["checks"])
    wrong = args()
    wrong[wrong.index("--capture-sha256") + 1] = "0" * 64
    assert json.loads(run("checks-wrong-hash", wrong, 2))["error"]["code"] == "HASH_MISMATCH"
    invalid = args("checks-invalid-spec")
    invalid[invalid.index("--capture") + 1] = str(root / "absent-capture.json")
    bad = json.loads(run("checks-invalid-spec", invalid, 2))
    assert bad["error"]["code"] == "INVALID_INPUT"
    for name, sha in hashes.items():
        assert hashlib.sha256((root / (name + ".json")).read_bytes()).hexdigest() == sha
    return {"ok": True, "synthetic_only": True, "hardware_access": False}
