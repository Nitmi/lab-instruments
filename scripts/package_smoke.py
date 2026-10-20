"""Installed CLI and C ABI double checks only; never loads the vendor SDK."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dist-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    wheels = list(args.dist_dir.glob("*.whl"))
    assert len(wheels) == 1
    wheel = wheels[0].resolve()
    project = Path(__file__).resolve().parents[1]
    version = tomllib.loads((project / "pyproject.toml").read_text())["project"]["version"]
    root = args.output_dir.absolute()
    root.mkdir()
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    environment.pop("PYTHONHOME", None)
    commands = []

    def run(label, command, expected=0):
        completed = subprocess.run(
            command,
            capture_output=True,
            encoding="utf-8",
            check=False,
            cwd=root,
            env=environment,
            timeout=60,
        )
        (root / (label + ".stdout")).write_text(completed.stdout, encoding="utf-8")
        (root / (label + ".stderr")).write_text(completed.stderr, encoding="utf-8")
        commands.append(
            {
                "label": label,
                "argv": list(map(str, command)),
                "exit_code": completed.returncode,
                "expected_exit_code": expected,
            }
        )
        assert completed.returncode == expected, (label, completed.stderr, completed.stdout)
        return completed.stdout

    uv = shutil.which("uv")
    assert uv
    venv = root / "venv"
    run("venv", [uv, "venv", "--python", sys.executable, str(venv)])
    python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    launcher = venv / ("Scripts/lab-instruments.exe" if os.name == "nt" else "bin/lab-instruments")
    run(
        "install",
        [uv, "pip", "install", "--python", str(python), "--no-index", "--no-deps", str(wheel)],
    )
    cli = [str(python), "-I", "-m", "lab_instruments"]
    identity = json.loads(
        run(
            "identity",
            [
                str(python),
                "-I",
                "-c",
                "import lab_instruments,json;"
                "print(json.dumps({'version':lab_instruments.__version__,"
                "'path':lab_instruments.__file__}))",
            ],
        )
    )
    assert identity["version"] == version and Path(identity["path"]).is_relative_to(venv)
    assert run("version", [str(launcher), "--version"]).strip() == "lab-instruments " + version
    doctor = json.loads(run("doctor", [*cli, "doctor", "--json"]))
    assert not doctor["hardware_access"] and not doctor["data"]["sdk_loaded"]
    assert doctor["data"]["status"] == "sdk_required"
    plan = root / "scope-plan.json"
    plan_args = [
        *cli,
        "capture",
        "plan",
        "--serial",
        "SYNTHETIC123",
        "--instrument",
        "scope",
        "--channels",
        "1",
        "2",
        "--output",
        str(plan),
        "--json",
    ]
    created = json.loads(run("plan", plan_args))
    sha = created["data"]["output"]["sha256"]
    assert hashlib.sha256(plan.read_bytes()).hexdigest() == sha
    run("inspect-plan", [*cli, "capture", "inspect-plan", str(plan), "--sha256", sha, "--json"])
    wrong = json.loads(
        run(
            "wrong-hash",
            [*cli, "capture", "inspect-plan", str(plan), "--sha256", "0" * 64, "--json"],
            2,
        )
    )
    assert wrong["error"]["code"] == "HASH_MISMATCH"
    assert json.loads(run("overwrite", plan_args, 2))["error"]["code"] == "OUTPUT_EXISTS"
    # Copy only our C ABI double classes/imports; omit pytest fixtures/dependencies.
    tree = ast.parse((project / "tests/conftest.py").read_text())
    tree.body = [
        node
        for node in tree.body
        if isinstance(node, (ast.ClassDef, ast.Import, ast.ImportFrom))
        and not (
            isinstance(node, ast.Import) and any(alias.name == "pytest" for alias in node.names)
        )
    ]
    (root / "test_double.py").write_text(ast.unparse(tree) + "\n", encoding="utf-8")
    code = (
        "import importlib.util,pathlib;"
        f"spec=importlib.util.spec_from_file_location('lab_double',str(pathlib.Path({str(root)!r})/'test_double.py'));"
        "m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);"
        "from lab_instruments.capture import perform,inspect_capture;"
        "from lab_instruments.plan import create;"
        "import lab_instruments.capture as cap;cap.time.sleep=lambda _:None;"
        f"base=pathlib.Path({str(root)!r});"
        "[(lambda target,instrument: (target.mkdir(),"
        "perform(create('210321ABC',instrument,[1] if instrument=='scope' else [0],samples=16),"
        "m.SyntheticDwf(m.FakeLibrary()),target,{},{})))(base/instrument,instrument)"
        " for instrument in ('scope','logic')]"
    )
    run("double-captures", [str(python), "-I", "-c", code])
    for instrument in ("scope", "logic"):
        path = root / instrument / "capture.json"
        value = json.loads(path.read_bytes())
        assert value["ok"] and value["evidence_origin"] == "test_double"
        assert not value["hardware_access"] and value["cleanup"]["complete"]
        sha = hashlib.sha256(path.read_bytes()).hexdigest()
        inspected = json.loads(
            run("inspect-" + instrument, [*cli, "inspect", str(path), "--sha256", sha, "--json"])
        )
        assert not inspected["hardware_access"]
    verified = 0
    with zipfile.ZipFile(wheel) as archive:
        for name in archive.namelist():
            if name.startswith("lab_instruments/") and not name.endswith("/"):
                assert (Path(identity["path"]).parent.parent / name).read_bytes() == archive.read(
                    name
                )
                verified += 1
    result = {
        "ok": True,
        "wheel_sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
        "identity": identity,
        "package_files_verified": verified,
        "commands": commands,
        "hardware_access": False,
        "native_sdk_verified": False,
        "physical_device_verified": False,
    }
    (root / "package-smoke.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "ok": True,
                "commands": len(commands),
                "package_files_verified": verified,
                "hardware_access": False,
            }
        )
    )


if __name__ == "__main__":
    main()
