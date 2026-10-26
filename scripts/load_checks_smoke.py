"""Installed offline load assertions; reuse prior owned-double captures, never reacquire."""

from __future__ import annotations

import copy
import hashlib
import json
import xml.etree.ElementTree as ET


def verify(run, cli, root):
    def sha(path):
        return hashlib.sha256(path.read_bytes()).hexdigest()

    source = root / "load-2-cc-run/load-result.json"
    contract = {
        "schema_version": "lab-instruments.load-check-spec.v1",
        "checks": [
            {
                "id": "rail",
                "phase": "loaded",
                "window": {"start": 0, "end_exclusive": 1},
                "expect": {"mean_voltage_v": {"minimum": 5, "maximum": 5.01}, "input_on": True},
            }
        ],
    }
    all_inputs = {
        p: sha(p)
        for folder in (root / "load-2-cc-run", root / "load-4-cc-run", root / "load-5-cc-run")
        for p in folder.iterdir()
        if p.is_file()
    }
    outcomes = []
    for scenario in ("passed", "failed", "invalid", "mixed", "cleanup", "timeout"):
        spec = copy.deepcopy(contract)
        capture = source
        if scenario == "failed":
            spec["checks"][0]["expect"]["mean_voltage_v"] = {"minimum": 6, "maximum": 7}
        if scenario == "invalid":
            spec["checks"][0]["window"]["end_exclusive"] = 2
        if scenario == "mixed":
            failed = copy.deepcopy(spec["checks"][0])
            failed.update(id="wrong-state", expect={"input_on": False})
            spec["checks"].append(failed)
        if scenario in ("cleanup", "timeout"):
            capture = (
                root
                / ("load-4-cc-run" if scenario == "cleanup" else "load-5-cc-run")
                / "load-result.json"
            )
        sp, rp, xp = [
            root / ("load-check-" + scenario + suffix)
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
        checked = json.loads(
            run(
                "load-check-" + scenario,
                [*cli, "load", "check", *args, "--output", str(rp)],
                0 if scenario == "passed" else 3,
            )
        )
        assert checked["data"]["report"]["ok"] is (scenario == "passed")
        bound = [str(rp), "--sha256", sha(rp), *args]
        verified = json.loads(
            run("load-recompute-" + scenario, [*cli, "load", "inspect-check-report", *bound])
        )
        assert verified["ok"] and verified["data"]["verification"]["verdict"]["ok"] is (
            scenario == "passed"
        )
        exported = json.loads(
            run(
                "load-junit-" + scenario,
                [*cli, "load", "junit", *bound, "--output", str(xp)],
                0 if scenario == "passed" else 3,
            )
        )
        counts = exported["data"]["junit"]
        assert counts == dict(
            tests=2 if scenario == "mixed" else 1,
            failures=int(scenario in ("failed", "mixed")),
            errors=int(scenario in ("invalid", "cleanup", "timeout")),
            skipped=0,
        )
        assert int(ET.fromstring(xp.read_bytes()).attrib["tests"]) == counts["tests"]
        assert all(
            exported[k] is False
            for k in (
                "hardware_access",
                "executables_started",
                "outputs_commanded",
                "authorization_granted",
            )
        )
        outcomes.append(dict(scenario=scenario, junit=counts, report_sha256=sha(rp)))
        if scenario == "passed":
            edited = json.loads(rp.read_bytes())
            edited["checks"][0]["metrics"]["mean_voltage_v"] += 0.1
            ep = root / "load-check-rehashed-edit.json"
            ep.write_text(json.dumps(edited), encoding="utf-8")
            rejected = json.loads(
                run(
                    "load-reject-rehashed-metric",
                    [*cli, "load", "inspect-check-report", str(ep), "--sha256", sha(ep), *args],
                    2,
                )
            )
            assert rejected["error"]["code"] == "REPORT_MISMATCH"
    assert all(sha(path) == digest for path, digest in all_inputs.items())
    return dict(
        ok=True,
        scenarios=outcomes,
        original_sources_unchanged=True,
        new_sessions_started=False,
        hardware_access=False,
    )
