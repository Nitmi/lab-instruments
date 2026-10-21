"""Installed thermal CLI checks. Native helper runs ONLY its explicit test backend."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path


def verify(run, cli, root, helper=None):
    # Generate fixtures using the source implementation in a separate child; the
    # installed CLI itself imports only its wheel, never the source checkout.
    source = Path(__file__).resolve().parents[1]
    serial, software = "0102030405060708", "0102030405060000"
    records = []
    helper_sha = hashlib.sha256(helper.read_bytes()).hexdigest() if helper else None
    for action in ("devices", "identify", "capture"):
        label = "thermal-" + action
        path = root / (label + ".json")
        args = [
            *cli,
            "thermal",
            "plan",
            "--action",
            action,
            "--test-double",
            "--output",
            str(path),
            "--json",
        ]
        if action != "devices":
            args += ["--link", "SYNTHETIC-PT3"]
        if action == "capture":
            args += [
                "--serial-hex",
                serial,
                "--software-hex",
                software,
                "--buffers",
                "3",
                "--roi",
                "cpu",
                "10",
                "10",
                "20",
                "20",
            ]
        created = json.loads(run(label + "-plan", args))
        assert created["ok"] and not created["hardware_access"]
        sha = hashlib.sha256(path.read_bytes()).hexdigest()
        run(
            label + "-inspect-plan",
            [*cli, "thermal", "inspect-plan", str(path), "--sha256", sha, "--json"],
        )
        if helper:
            directory = root / (label + "-run")
            native = json.loads(
                run(
                    label + "-run",
                    [
                        *cli,
                        "thermal",
                        "run",
                        "--plan",
                        str(path),
                        "--plan-sha256",
                        sha,
                        "--helper",
                        str(helper),
                        "--helper-sha256",
                        helper_sha,
                        "--output",
                        str(directory),
                        "--json",
                    ],
                )
            )
            assert native["ok"] and not native["hardware_access"] and native["executables_started"]
            session = native["data"]["session"]
            assert session["cleanup_complete"] and session["local_lease_released"]
            assert session["analysis"]["source_origin"] == "test_double"
            if action == "capture":
                assert session["analysis"]["trend"]["unique_frames"] == 3
                assert (
                    abs(session["analysis"]["trend"]["roi_changes"][0]["mean_delta_c"] - 0.2) < 1e-9
                )
            result_path = directory / "thermal-result.json"
            run(
                label + "-inspect-run",
                [
                    *cli,
                    "thermal",
                    "inspect",
                    str(result_path),
                    "--sha256",
                    hashlib.sha256(result_path.read_bytes()).hexdigest(),
                    "--json",
                ],
            )
            saved = directory / "native/source.json"
        else:
            directory = root / (label + "-fixture")
            directory.mkdir()
            code = (
                "import importlib.util,json,pathlib;"
                "spec=importlib.util.spec_from_file_location('thermal_fixture',"
                f"{str(source / 'tests/thermal_double.py')!r});"
                "m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);"
                f"m.source(pathlib.Path({str(directory)!r}),json.loads(pathlib.Path({str(path)!r}).read_bytes()))"
            )
            run(label + "-fixture", [cli[0], "-I", "-c", code])
            saved = directory / "source.json"
        imported_dir = root / (label + "-import")
        imported = json.loads(
            run(
                label + "-import",
                [
                    *cli,
                    "thermal",
                    "import",
                    "--input",
                    str(saved),
                    "--sha256",
                    hashlib.sha256(saved.read_bytes()).hexdigest(),
                    "--plan",
                    str(path),
                    "--plan-sha256",
                    sha,
                    "--output",
                    str(imported_dir),
                    "--json",
                ],
            )
        )
        assert (
            imported["ok"]
            and not imported["hardware_access"]
            and not imported["executables_started"]
        )
        imported_result = imported_dir / "thermal-result.json"
        run(
            label + "-inspect-import",
            [
                *cli,
                "thermal",
                "inspect",
                str(imported_result),
                "--sha256",
                hashlib.sha256(imported_result.read_bytes()).hexdigest(),
                "--json",
            ],
        )
        records.append(
            {
                "action": action,
                "native_test_backend_exercised": helper is not None,
                "source_sha256": hashlib.sha256(saved.read_bytes()).hexdigest(),
                "hardware_access": False,
            }
        )
    if helper:
        wrong_plan = root / "thermal-wrong-identity.json"
        wrong_args = [
            *cli,
            "thermal",
            "plan",
            "--test-double",
            "--link",
            "SYNTHETIC-PT3",
            "--serial-hex",
            "0000000000000000",
            "--software-hex",
            software,
            "--output",
            str(wrong_plan),
            "--json",
        ]
        run("thermal-wrong-identity-plan", wrong_args)
        rejected_dir = root / "thermal-wrong-identity-run"
        rejected = json.loads(
            run(
                "thermal-wrong-identity-run",
                [
                    *cli,
                    "thermal",
                    "run",
                    "--plan",
                    str(wrong_plan),
                    "--plan-sha256",
                    hashlib.sha256(wrong_plan.read_bytes()).hexdigest(),
                    "--helper",
                    str(helper),
                    "--helper-sha256",
                    helper_sha,
                    "--output",
                    str(rejected_dir),
                    "--json",
                ],
                3,
            )
        )
        assert not rejected["ok"] and not (rejected_dir / "native/warmup.y16").exists()
        assert rejected["data"]["session"]["native_exit_code"] == 3
        rejected_result = rejected_dir / "thermal-result.json"
        run(
            "thermal-wrong-identity-inspect",
            [
                *cli,
                "thermal",
                "inspect",
                str(rejected_result),
                "--sha256",
                hashlib.sha256(rejected_result.read_bytes()).hexdigest(),
                "--json",
            ],
        )
        path = root / "thermal-capture.json"
        sha = hashlib.sha256(path.read_bytes()).hexdigest()
        previous = os.environ.get("LAB_PT3_TEST_FAULT")
        for fault in ("agc", "ffc"):
            os.environ["LAB_PT3_TEST_FAULT"] = fault
            try:
                # package_smoke snapshots env: run passes the explicit test fault
                # as an environment override, never as a real-camera option.
                result = json.loads(
                    run(
                        "thermal-fault-" + fault,
                        [
                            *cli,
                            "thermal",
                            "run",
                            "--plan",
                            str(path),
                            "--plan-sha256",
                            sha,
                            "--helper",
                            str(helper),
                            "--helper-sha256",
                            helper_sha,
                            "--output",
                            str(root / ("thermal-fault-" + fault)),
                            "--json",
                        ],
                        3,
                        extra_env={"LAB_PT3_TEST_FAULT": fault},
                    )
                )
                assert not result["ok"] and not result["hardware_access"]
                assert result["data"]["session"]["cleanup_complete"]
            finally:
                if previous is None:
                    os.environ.pop("LAB_PT3_TEST_FAULT", None)
                else:
                    os.environ["LAB_PT3_TEST_FAULT"] = previous
    result = {
        "ok": True,
        "hardware_access": False,
        "sessions": records,
        "native_test_sessions": 6 if helper else 0,
        "saved_imports": 3,
    }
    (root / "thermal-smoke.json").write_text(json.dumps(result, indent=2) + "\n")
    return result
