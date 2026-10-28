"""Bounded Windows capture and host-only thermal evidence evaluation."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

from .capture import now
from .common import LabError, checked, digest, encode, new_path, parse, read, require, write_new
from .lease import Lease
from .thermal_frame import CONTROLS, analyze, trends
from .thermal_plan import PART, validate

NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}\Z")


def load_plan(path, expected):
    raw, ref = read(path, maximum=32768, expected=expected)
    plan = parse(raw)
    validate(plan)
    return plan, raw, ref


def reference(path):
    raw, ref = read(path)
    return raw, {"sha256": ref["sha256"], "size_bytes": len(raw)}


def bundle(directory, plan):
    files = {}
    for path in sorted(directory.iterdir()):
        if path.name in ("helper.stdout", "helper.stderr"):
            continue
        require(NAME.fullmatch(path.name), "invalid native evidence name")
        _, files[path.name] = reference(path)
    require(len(files) <= 64, "too many thermal evidence files")
    source = {
        "schema_version": "lab-instruments.thermal-source.v1",
        "plan_sha256": digest(encode(plan)),
        "files": files,
    }
    write_new(directory / "source.json", encode(source))
    return source


def load_source(path, expected, plan, *, reader=read):
    raw, ref = reader(path, expected=expected)
    source = parse(raw)
    require(
        set(source) == {"schema_version", "plan_sha256", "files"}
        and source["schema_version"] == "lab-instruments.thermal-source.v1"
        and source["plan_sha256"] == digest(encode(plan)),
        "thermal source/plan binding differs",
    )
    files = source["files"]
    require(isinstance(files, dict) and len(files) <= 64, "invalid source inventory")
    payloads = {}
    for name, file_ref in files.items():
        require(NAME.fullmatch(name) and name != "source.json", "invalid source file name")
        require(
            isinstance(file_ref, dict) and set(file_ref) == {"sha256", "size_bytes"},
            "invalid file reference",
        )
        data, _ = reader(Path(path).parent / name, expected=file_ref["sha256"])
        require(len(data) == file_ref["size_bytes"], "source file size differs")
        payloads[name] = data
    return source, payloads, ref


def evaluate(plan, payloads, save):
    require("native.json" in payloads, "native completion record missing", "NATIVE_INCOMPLETE")
    native = parse(payloads["native.json"])
    origin = "test_double" if plan["test_double"] else "windows_media_foundation"
    require(
        native["schema_version"] == "lab-instruments.pt3-native.v1"
        and native["origin"] == origin
        and native["link"] == (plan["link"] or "")
        and native["ok"] is True
        and native["cleanup_complete"] is True
        and not native["error"],
        "native capture failed or cleanup is unconfirmed",
        "NATIVE_INCOMPLETE",
    )
    result = {
        "source_origin": origin,
        "identity": None,
        "frames": [],
        "trend": None,
        "apparent_surface_temperature_only": True,
        "radiometric_accuracy_verified": False,
        "physical_device_verified": False,
        "dut_identity_verified": False,
        "calibration_verified": False,
        "authorization_granted": False,
    }
    if plan["action"] == "devices":
        require(
            native["frames"] == [] and native["capture_started"] is False,
            "unexpected capture in devices",
        )
        require(
            isinstance(native["devices"], list)
            and len(native["devices"]) <= 128
            and all(isinstance(link, str) and len(link) <= 512 for link in native["devices"]),
            "invalid interface listing",
        )
        result["devices"] = native["devices"]
        return result
    identity = {}
    for key in ("serial", "software", "part"):
        value = payloads["identity-" + key + ".bin"]
        require(len(value) == CONTROLS[key][2], "invalid identity byte count")
        identity[key] = value.split(b"\0", 1)[0].decode("ascii") if key == "part" else value.hex()
    require(identity["part"] == PART, "Lepton part mismatch", "IDENTITY_MISMATCH")
    result["identity"] = identity
    if plan["action"] == "identify":
        require(
            native["frames"] == [] and native["capture_started"] is False,
            "unexpected capture in identify",
        )
        return result
    require(
        identity["serial"] == plan["lepton_serial_hex"]
        and identity["software"] == plan["lepton_software_hex"],
        "Lepton serial/software mismatch",
        "IDENTITY_MISMATCH",
    )
    require(
        native["capture_started"] is True and len(native["frames"]) == plan["buffers"],
        "UVC buffer count differs",
    )
    controls = {name: payloads["before-" + name + ".bin"] for name in CONTROLS}
    after = {name: payloads["after-" + name + ".bin"] for name in CONTROLS}
    require(controls == after, "radiometry controls changed during capture", "CAPTURE_CHANGED")
    require(
        all(
            controls[key] == payloads["identity-" + key + ".bin"]
            for key in ("serial", "part", "software")
        ),
        "camera identity changed after stream start",
        "IDENTITY_MISMATCH",
    )
    require(len(payloads["warmup.y16"]) == 160 * 122 * 2, "missing/invalid warmup buffer")
    previous_timestamp = None
    for index, frame in enumerate(native["frames"]):
        name = f"frame-{index:04d}.y16"
        require(
            set(frame) == {"name", "timestamp_100ns"} and frame["name"] == name,
            "invalid native frame reference",
        )
        stamp = frame["timestamp_100ns"]
        require(
            type(stamp) is int
            and 0 <= stamp <= 2**63 - 1
            and (previous_timestamp is None or stamp > previous_timestamp),
            "media timestamp is not increasing",
        )
        previous_timestamp = stamp
        value, csv = analyze(payloads[name], controls, plan["rois"])
        value["raw_sha256"] = digest(payloads[name])
        value["native_timestamp_100ns"] = stamp
        value["csv"] = save(f"frame-{index:04d}.csv", csv)
        result["frames"].append(value)
    result["trend"] = trends(result["frames"])
    return result


def inventory(directory):
    files = {}
    for path in sorted(directory.rglob("*")):
        if path == directory / "thermal-result.json":
            continue
        if path.is_file():
            name = path.relative_to(directory).as_posix()
            require(len(files) < 100, "thermal artifact count limit")
            _, files[name] = reference(path)
    return files


def finish(directory, result):
    result["finished_at"] = now()
    result["status"] = "completed" if result["ok"] else "failed"
    result["artifacts"] = inventory(directory)
    ref = write_new(directory / "thermal-result.json", encode(result))
    return result, ref


def start_result(plan, operation):
    return {
        "schema_version": "lab-instruments.thermal-result.v1",
        "plan": plan,
        "plan_sha256": digest(encode(plan)),
        "operation": operation,
        "started_at": now(),
        "scope": "host_only_saved_thermal_evaluation"
        if operation == "import"
        else "bounded_thermal_capture",
        "ok": False,
        "hardware_access": False,
        "executables_started": False,
        "physical_device_verified": False,
        "calibration_verified": False,
        "authorization_granted": False,
        "outputs_commanded": False,
    }


def save_csv(directory):
    def save(name, payload):
        ref = write_new(directory / name, payload)
        return {"name": name, "sha256": ref["sha256"], "size_bytes": ref["size_bytes"]}

    return save


def import_source(path, expected, plan_path, plan_hash, output):
    plan, raw, _ = load_plan(plan_path, plan_hash)
    source, payloads, _ = load_source(path, expected, plan)
    directory = new_path(output)
    directory.mkdir()
    write_new(directory / "plan.json", raw)
    native = directory / "native"
    native.mkdir()
    for name, data in payloads.items():
        write_new(native / name, data)
    write_new(native / "source.json", encode(source))
    result = start_result(plan, "import")
    try:
        result["analysis"] = evaluate(plan, payloads, save_csv(directory))
        result["ok"] = True
    except (LabError, OSError, ValueError, KeyError, TypeError) as error:
        result["error"] = {
            "code": getattr(error, "code", "EVIDENCE_INVALID"),
            "message": str(error)[:512],
        }
    return finish(directory, result)


def run(plan_path, plan_hash, helper, helper_hash, output):
    plan, raw, _ = load_plan(plan_path, plan_hash)
    helper = Path(helper)
    require(helper.is_absolute() and os.name == "nt", "absolute Windows helper path required")
    helper_raw, helper_ref = read(helper, maximum=16 * 1024 * 1024, expected=helper_hash)
    require(helper_raw[:2] == b"MZ", "Windows PE helper required")
    directory = new_path(output)
    directory.mkdir()
    write_new(directory / "plan.json", raw)
    native = directory / "native"
    native.mkdir()
    result = start_result(plan, "run")
    result["helper"] = helper_ref
    result["cleanup_complete"] = False
    lease = None
    process = None
    try:
        lease = Lease("PT3-" + (plan["link"] or "devices"))
        args = [
            str(helper),
            "test_double" if plan["test_double"] else "real",
            plan["action"],
            str(native),
            plan["link"] or "",
            plan["lepton_serial_hex"] or "-",
            plan["lepton_software_hex"] or "-",
            str(plan["buffers"]),
        ]
        with (
            (native / "helper.stdout").open("xb") as stdout,
            (native / "helper.stderr").open("xb") as stderr,
        ):
            process = subprocess.Popen(
                args, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr, cwd=native
            )
            result["executables_started"] = True
            result["hardware_access"] = None if not plan["test_double"] else False
            try:
                result["native_exit_code"] = process.wait(timeout=plan["timeout_ms"] / 1000)
            except (subprocess.TimeoutExpired, KeyboardInterrupt) as error:
                process.kill()
                process.wait(timeout=5)
                raise LabError(
                    "NATIVE_TIMEOUT",
                    "native worker terminated; camera cleanup unknown; partial evidence retained",
                ) from error
        source = bundle(native, plan)
        _, payloads, _ = load_source(native / "source.json", digest(encode(source)), plan)
        native_result = parse(payloads["native.json"]) if "native.json" in payloads else {}
        result["cleanup_complete"] = native_result.get("cleanup_complete") is True
        result["hardware_access"] = not plan["test_double"]
        require(result["native_exit_code"] == 0, "native reader failed; no retry", "NATIVE_FAILED")
        result["analysis"] = evaluate(plan, payloads, save_csv(directory))
        result["ok"] = True
    except (LabError, OSError, ValueError, KeyError, TypeError) as error:
        result["error"] = {
            "code": getattr(error, "code", "HOST_OR_NATIVE_ERROR"),
            "message": str(error)[:512],
        }
    finally:
        if lease:
            lease.close()
        result["local_lease_released"] = True
    return finish(directory, result)


def inspect(path, expected):
    raw, ref = read(path, expected=expected)
    result = parse(raw)
    return inspect_snapshot(result, ref, Path(ref["path"]).parent)


def inspect_snapshot(result, ref, directory, *, reader=read, retained=None):
    """Native recomputation from one caller-owned byte snapshot, never a helper."""
    require(
        result["schema_version"] == "lab-instruments.thermal-result.v1", "invalid thermal result"
    )
    plan = result["plan"]
    validate(plan)
    require(
        result["plan_sha256"] == digest(encode(plan)) and result["operation"] in ("run", "import"),
        "invalid thermal plan/operation binding",
    )
    require(
        result["physical_device_verified"] is False
        and result["calibration_verified"] is False
        and result["authorization_granted"] is False
        and result["outputs_commanded"] is False,
        "invalid evidence boundary",
    )
    artifacts = result["artifacts"]
    require(
        isinstance(artifacts, dict) and len(artifacts) <= 100, "invalid thermal artifact inventory"
    )
    names, pending, count = set(), [directory], 0
    while pending:
        parent = pending.pop()
        with os.scandir(parent) as entries:
            for item in entries:
                count += 1
                require(count <= 128, "thermal directory entry limit", "LIMIT_EXCEEDED")
                entry = checked(Path(item.path))
                if entry.is_dir():
                    require(parent == directory and entry.name == "native", "unexpected directory")
                    pending.append(entry)
                else:
                    require(entry.is_file(), "nonregular thermal evidence")
                    name = entry.relative_to(directory).as_posix()
                    if name != "thermal-result.json":
                        names.add(name)
    require(names == set(artifacts), "thermal artifact inventory differs")
    for name, file_ref in artifacts.items():
        parts = name.split("/")
        require(
            1 <= len(parts) <= 2
            and all(NAME.fullmatch(p) and p not in (".", "..") for p in parts)
            and name != "thermal-result.json",
            "invalid artifact path",
        )
        data, _ = reader(directory / name, expected=file_ref["sha256"])
        require(len(data) == file_ref["size_bytes"], "artifact size differs")
    saved_plan = parse(reader(directory / "plan.json", maximum=32768)[0])
    require(
        saved_plan == plan
        and type(result["ok"]) is bool
        and result["status"] == ("completed" if result["ok"] else "failed"),
        "result status/plan differs",
    )
    if result["ok"]:
        source_path = directory / "native/source.json"
        source_raw, _ = reader(source_path)
        _, payloads, _ = load_source(source_path, digest(source_raw), plan, reader=reader)

        def verify(name, payload):
            original, file_ref = reader(directory / name)
            require(original == payload, "thermal CSV differs from source frame")
            return {"name": name, "sha256": file_ref["sha256"], "size_bytes": len(original)}

        analysis = evaluate(plan, payloads, verify)
        if retained is not None:
            retained.update(analysis=analysis, payloads=payloads)
        require(
            encode(analysis) == encode(result["analysis"]),
            "thermal analysis differs from raw evidence",
        )
        if result["operation"] == "run":
            require(
                result["cleanup_complete"] is True
                and result["local_lease_released"] is True
                and result["executables_started"] is True
                and result["hardware_access"] is (not plan["test_double"]),
                "invalid run cleanup/boundary",
            )
        else:
            require(
                result["hardware_access"] is False and result["executables_started"] is False,
                "offline import cannot assert hardware access",
            )
    return {
        "input": ref,
        "source_ok": result["ok"],
        "status": result["status"],
        "scope": "saved_thermal_evidence_integrity",
        "hardware_access": False,
        "executables_started": False,
        "physical_device_verified": False,
    }
