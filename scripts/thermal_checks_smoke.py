"""Installed saved thermal assertions; reuses the existing saved capture."""

from __future__ import annotations

import copy
import hashlib
import json
import xml.etree.ElementTree as ET


def verify(run, cli, root):
    def sha(path):
        return hashlib.sha256(path.read_bytes()).hexdigest()

    capture = root / "thermal-capture-import/thermal-result.json"
    before = {p: sha(p) for p in capture.parent.rglob("*") if p.is_file()}
    contract = {
        "schema_version": "lab-instruments.thermal-check-spec.v1",
        "checks": [
            {
                "id": "cpu",
                "roi": {"x": 10, "y": 10, "width": 20, "height": 20},
                "window": {"start": 0, "end_exclusive": 3},
                "maximum_invalid_pixels": 0,
                "expect": {
                    "mean_c": {"minimum": 26, "maximum": 28},
                    "mean_delta_c": {"minimum": 0.19, "maximum": 0.21},
                },
            }
        ],
    }
    cases = []
    for name in ("passed", "failed", "invalid", "mixed"):
        spec = copy.deepcopy(contract)
        if name == "failed":
            spec["checks"][0]["expect"]["mean_c"] = {"minimum": 50, "maximum": 60}
        if name == "invalid":
            spec["checks"][0]["window"]["end_exclusive"] = 4
        if name == "mixed":
            wrong = copy.deepcopy(spec["checks"][0])
            wrong.update(id="wrong", expect={"max_c": {"minimum": 50, "maximum": 60}})
            spec["checks"].append(wrong)
        sp, rp, xp = [
            root / ("thermal-check-" + name + suffix)
            for suffix in ("-spec.json", "-report.json", ".xml")
        ]
        sp.write_text(json.dumps(spec), encoding="utf-8")
        args = [
            "--spec",
            str(sp),
            "--spec-sha256",
            sha(sp),
            "--capture",
            str(capture),
            "--capture-sha256",
            sha(capture),
            "--json",
        ]
        expected = 0 if name == "passed" else 3
        result = json.loads(
            run(
                "thermal-check-" + name,
                [*cli, "thermal", "check", *args, "--output", str(rp)],
                expected,
            )
        )
        assert result["data"]["report"]["ok"] is (expected == 0)
        result = json.loads(
            run(
                "thermal-verify-" + name,
                [*cli, "thermal", "inspect-check-report", str(rp), "--sha256", sha(rp), *args],
            )
        )
        assert result["data"]["verification"]["verdict"]["ok"] is (expected == 0)
        result = json.loads(
            run(
                "thermal-junit-" + name,
                [
                    *cli,
                    "thermal",
                    "junit",
                    str(rp),
                    "--sha256",
                    sha(rp),
                    *args,
                    "--output",
                    str(xp),
                ],
                expected,
            )
        )
        counts = result["data"]["junit"]
        assert counts["skipped"] == 0
        assert counts["errors"] == (1 if name == "invalid" else 0)
        assert counts["failures"] == (1 if name in ("failed", "mixed") else 0)
        assert ET.fromstring(xp.read_bytes()).attrib["tests"] == str(counts["tests"])
        cases.append({"name": name, "exit_code": expected, "junit": counts})
    # A coherently rehashed edited report still needs exact native regeneration.
    rp = root / "thermal-check-passed-report.json"
    edited = json.loads(rp.read_bytes())
    edited["checks"][0]["frames"][0]["roi"]["max_c"] = 100
    tampered = root / "thermal-check-tampered-report.json"
    tampered.write_text(json.dumps(edited), encoding="utf-8")
    sp = root / "thermal-check-passed-spec.json"
    result = json.loads(
        run(
            "thermal-check-tamper",
            [
                *cli,
                "thermal",
                "inspect-check-report",
                str(tampered),
                "--sha256",
                sha(tampered),
                "--spec",
                str(sp),
                "--spec-sha256",
                sha(sp),
                "--capture",
                str(capture),
                "--capture-sha256",
                sha(capture),
                "--json",
            ],
            2,
        )
    )
    assert result["error"]["code"] == "REPORT_MISMATCH"
    assert before == {p: sha(p) for p in before}
    result = {
        "ok": True,
        "scenarios": cases,
        "hardware_access": False,
        "new_acquisitions_performed": False,
    }
    (root / "thermal-checks-smoke.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result
