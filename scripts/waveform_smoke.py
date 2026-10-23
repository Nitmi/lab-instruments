"""Installed raw-voltage checks/recomputation/JUnit with synthetic saved evidence."""

from __future__ import annotations

import hashlib
import json
import xml.etree.ElementTree as ET
from pathlib import Path


def verify(run, cli, root):
    tests = Path(__file__).resolve().parents[1] / "tests"
    producer = (
        f"import sys,pathlib;sys.path.insert(0,{str(tests)!r});"
        "from waveform_fixtures import capture,spec;from lab_instruments.common import encode;"
        f"root=pathlib.Path({str(root)!r});"
        "(root/'wave-capture.json').write_bytes(encode(capture()));"
        "s=spec();(root/'wave-passed.json').write_bytes(encode(s));"
        "s['checks'][0]['expect']['mean_v']={'minimum':4,'maximum':5};"
        "(root/'wave-failed.json').write_bytes(encode(s));"
        "s=spec();s['checks'][0]['window']['end_exclusive']=17;"
        "(root/'wave-invalid.json').write_bytes(encode(s))"
    )
    run("wave-fixtures", [*cli[:2], "-c", producer])

    def sha(path):
        return hashlib.sha256(path.read_bytes()).hexdigest()

    capture = root / "wave-capture.json"
    inputs_before = {p.name: sha(p) for p in root.glob("wave-*.json")}
    for scenario in ("passed", "failed", "invalid"):
        spec = root / f"wave-{scenario}.json"
        report = root / f"wave-{scenario}-report.json"
        inputs = [
            "--spec",
            str(spec),
            "--spec-sha256",
            sha(spec),
            "--capture",
            str(capture),
            "--capture-sha256",
            sha(capture),
            "--json",
        ]
        checked = json.loads(
            run(
                "wave-check-" + scenario,
                [*cli, "waveform", "check", *inputs, "--output", str(report)],
                0 if scenario == "passed" else 3,
            )
        )
        assert checked["data"]["report"]["checks"][0]["status"] == (
            "invalid_evidence" if scenario == "invalid" else scenario
        )
        viewed = json.loads(
            run(
                "wave-recheck-" + scenario,
                [
                    *cli,
                    "waveform",
                    "inspect-check-report",
                    str(report),
                    "--sha256",
                    sha(report),
                    *inputs,
                ],
            )
        )
        assert viewed["ok"] and viewed["data"]["verification"]["verdict"]["ok"] is (
            scenario == "passed"
        )
        output = root / f"wave-{scenario}.xml"
        exported = json.loads(
            run(
                "wave-junit-" + scenario,
                [
                    *cli,
                    "waveform",
                    "junit",
                    str(report),
                    "--sha256",
                    sha(report),
                    *inputs,
                    "--output",
                    str(output),
                ],
                0 if scenario == "passed" else 3,
            )
        )
        counts = exported["data"]["junit"]
        assert counts == {
            "tests": 1,
            "failures": int(scenario == "failed"),
            "errors": int(scenario == "invalid"),
            "skipped": 0,
        }
        assert int(ET.fromstring(output.read_bytes()).attrib["tests"]) == 1
        if scenario == "passed":
            for field in ("mean_v", "rms_v"):
                changed = json.loads(report.read_bytes())
                changed["checks"][0]["metrics"][field] += 0.1
                edited = root / f"wave-edited-{field}.json"
                edited.write_text(json.dumps(changed), encoding="utf-8")
                refused = json.loads(
                    run(
                        "wave-reject-" + field,
                        [
                            *cli,
                            "waveform",
                            "inspect-check-report",
                            str(edited),
                            "--sha256",
                            sha(edited),
                            *inputs,
                        ],
                        2,
                    )
                )
                assert refused["error"]["code"] == "REPORT_MISMATCH"
    # Use one actual saved native SDS result from this installed smoke's owned double.
    scope = root / "scope-1-stopped-ok-run" / "scope-result.json"
    assert scope.is_file()
    spec = root / "wave-sds.json"
    spec.write_text(
        json.dumps(
            {
                "schema_version": "lab-instruments.waveform-check-spec.v1",
                "source_kind": "sds804x",
                "checks": [
                    {
                        "id": "sds",
                        "channel": 1,
                        "window": {"start": 0, "end_exclusive": 16},
                        "expect": {"mean_v": {"minimum": -100, "maximum": 100}},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    result = json.loads(
        run(
            "wave-sds-check",
            [
                *cli,
                "waveform",
                "check",
                "--spec",
                str(spec),
                "--spec-sha256",
                sha(spec),
                "--capture",
                str(scope),
                "--capture-sha256",
                sha(scope),
                "--output",
                str(root / "wave-sds-report.json"),
                "--json",
            ],
        )
    )
    report = result["data"]["report"]
    assert report["ok"] and report["source"]["evidence_origin"] == "test_double"
    assert report["checks"][0]["raw_span"] == {"start": 0, "end_exclusive": 32}
    assert all(sha(root / p) == h for p, h in inputs_before.items())
    return {"ok": True, "synthetic_only": True, "hardware_access": False}
