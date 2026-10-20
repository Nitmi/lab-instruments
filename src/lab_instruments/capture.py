from __future__ import annotations

import base64
import struct
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from .common import LabError, digest, encode, new_path, parse, read, require, write_new
from .dwf import Dwf
from .plan import validate


def now():
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def load_plan(path, sha256):
    raw, ref = read(path, maximum=32768, expected=sha256)
    plan = parse(raw)
    validate(plan)
    return plan, raw, ref


def perform(plan, sdk, directory: Path, plan_ref, sdk_ref, *, journal=None):
    """One acquisition, one handle, no retries; injected SDKs are test-only."""
    validate(plan)
    started = time.monotonic()
    result = {
        "schema_version": "lab-instruments.capture.v1",
        "operation": "capture.single",
        "model": plan["model"],
        "serial": plan["serial"],
        "instrument": plan["instrument"],
        "evidence_origin": sdk.evidence_origin,
        "plan": plan,
        "plan_file": plan_ref,
        "sdk_file": sdk_ref,
        "sdk_version": sdk.version,
        "started_at": now(),
        "effects": plan["effects"],
        "hardware_access": False,
        "outputs_commanded": False,
        "sample_zero_utc_verified": False,
        "calibration_verified": False,
        "dut_identity_verified": False,
        "physical_device_verified": False,
        "cleanup": {"input_stopped": False, "owned_handle_closed": False, "errors": []},
        "ok": False,
    }
    configured = False
    journal = journal or (lambda stage: None)
    try:
        journal("device.open.begin")
        result["selection"] = sdk.open(plan["serial"])
        journal("device.open.completed")
        result["settings"] = sdk.configure(plan)
        configured = True
        journal("input.configured")
        time.sleep(plan["settle_ms"] / 1000)
        raw, encoding = sdk.acquire(plan, started + plan["timeout_ms"] / 1000)
        result["payload"] = {
            "encoding": encoding,
            "channels": plan["channels"],
            "samples_per_channel": plan["samples"],
            "length": len(raw),
            "sha256": digest(raw),
            "raw_base64": base64.b64encode(raw).decode("ascii"),
            "time_semantics": "sample_index_over_actual_rate_no_verified_utc_origin",
        }
        journal("input.captured")
        result["ok"] = True
    except (LabError, OSError, ValueError, UnicodeError) as error:
        result["error"] = {
            "code": getattr(error, "code", "CAPTURE_FAILED"),
            "message": str(error)[:512],
        }
    finally:
        if sdk.handle:
            if configured:
                try:
                    sdk.stop(plan["instrument"])
                    result["cleanup"]["input_stopped"] = True
                    journal("input.stopped")
                except (LabError, OSError) as error:
                    result["cleanup"]["errors"].append(str(error)[:512])
            try:
                sdk.close()
                result["cleanup"]["owned_handle_closed"] = True
                journal("device.closed")
            except (LabError, OSError) as error:
                result["cleanup"]["errors"].append(str(error)[:512])
        result["hardware_access"] = sdk.hardware_access
        result["finished_at"] = now()
        result["sdk_call_count"] = len(sdk.calls)
        result["cleanup"]["complete"] = (
            not sdk.handle
            and (not configured or result["cleanup"]["input_stopped"])
            and not result["cleanup"]["errors"]
        )
        if not result["cleanup"]["complete"]:
            result["ok"] = False
            result.setdefault(
                "error",
                {"code": "CLEANUP_INCOMPLETE", "message": "instrument cleanup was not confirmed"},
            )
    result["status"] = "captured" if result["ok"] else "failed"
    result["sdk_calls"] = sdk.calls
    ref = write_new(directory / "capture.json", encode(result))
    return result, ref


def worker(plan_path, plan_sha, sdk_path, sdk_sha, directory):
    plan, _, plan_ref = load_plan(plan_path, plan_sha)
    journal_path = directory / "journal.jsonl"
    with journal_path.open("xb") as stream:

        def journal(stage):
            stream.write(encode({"stage": stage, "recorded_at": now()}))
            stream.flush()

        journal("sdk.load.begin")
        try:
            sdk, sdk_ref = Dwf.load(sdk_path, sdk_sha)
            journal("sdk.load.completed")
            result, _ = perform(plan, sdk, directory, plan_ref, sdk_ref, journal=journal)
        except (LabError, OSError, ValueError, KeyError, TypeError) as error:
            write_new(
                directory / "worker-error.json",
                encode(
                    {
                        "ok": False,
                        "error": {
                            "code": getattr(error, "code", "WORKER_FAILED"),
                            "message": str(error)[:512],
                        },
                        "cleanup_confirmed": False,
                        "hardware_access_possible": True,
                    }
                ),
            )
            journal("worker.failed")
            return 2
    return 0 if result["ok"] else 3


def supervise(plan_path, plan_sha, sdk_path, sdk_sha, output):
    plan, raw, plan_ref = load_plan(plan_path, plan_sha)
    _, sdk_ref = read(sdk_path, maximum=64 * 1024 * 1024, expected=sdk_sha)
    require(Path(sdk_path).is_absolute(), "SDK path must be absolute")
    directory = new_path(output)
    directory.mkdir()
    input_copy = write_new(directory / "plan.json", raw)
    write_new(directory / "request.json", encode({"plan": plan_ref, "sdk": sdk_ref}))
    arguments = [
        sys.executable,
        "-I",
        "-m",
        "lab_instruments._worker",
        "--plan",
        input_copy["path"],
        "--sha256",
        plan_sha,
        "--sdk",
        sdk_ref["path"],
        "--sdk-sha256",
        sdk_sha,
        "--output",
        str(directory),
    ]
    result = {
        "schema_version": "lab-instruments.supervisor.v1",
        "operation": "capture.run",
        "plan": plan_ref,
        "sdk": sdk_ref,
        "output_directory": str(directory),
        "started_at": now(),
        "hardware_access": False,
        "executables_started": False,
        "outputs_commanded": False,
        "physical_device_verified": False,
        "ok": False,
        "cleanup_confirmed": False,
        "retry_count": 0,
    }
    try:
        # Only our fixed worker command is executed. No shell or user-provided argv.
        process = subprocess.Popen(
            arguments,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        result["executables_started"] = True
        result["hardware_access"] = True  # Conservative once the worker may have started.
        result["worker_pid"] = process.pid
        try:
            result["worker_exit_code"] = process.wait(timeout=plan["timeout_ms"] / 1000)
        except (subprocess.TimeoutExpired, KeyboardInterrupt) as error:
            result["error"] = {
                "code": "CANCELLED" if isinstance(error, KeyboardInterrupt) else "WORKER_TIMEOUT",
                "message": "worker interrupted; instrument cleanup is unknown",
            }
        finally:
            if process.poll() is None:
                try:
                    process.kill()
                    process.wait(timeout=10)
                except (OSError, subprocess.TimeoutExpired) as error:
                    result["termination_error"] = str(error)[:512]
                result["worker_exit_code"] = process.poll()
        if "error" not in result:
            capture_path = directory / "capture.json"
            if capture_path.exists():
                capture_raw, capture_ref = read(capture_path)
                capture = parse(capture_raw)
                result["capture"] = capture_ref
                result["ok"] = capture["ok"] is True and result["worker_exit_code"] == 0
                result["cleanup_confirmed"] = capture["cleanup"]["complete"] is True
                result["hardware_access"] = capture["hardware_access"]
                if not result["ok"]:
                    result["error"] = capture.get(
                        "error",
                        {"code": "WORKER_EXIT_FAILED", "message": "worker exited abnormally"},
                    )
            else:
                failure_path = directory / "worker-error.json"
                if failure_path.exists():
                    failure_raw, failure_ref = read(failure_path)
                    result["worker_error"] = failure_ref
                    result["error"] = parse(failure_raw)["error"]
                else:
                    result["error"] = {
                        "code": "WORKER_FAILED",
                        "message": "worker produced no capture evidence; inspect journal",
                    }
    except (OSError, ValueError, KeyError, TypeError) as error:
        result["ok"] = False
        result["cleanup_confirmed"] = False
        result["error"] = {"code": "HOST_OR_WORKER_ERROR", "message": str(error)[:512]}
    result["finished_at"] = now()
    ref = write_new(directory / "supervisor.json", encode(result))
    return result, ref


def inspect_capture(path, sha256):
    raw, ref = read(path, expected=sha256)
    result = parse(raw)
    require(result.get("schema_version") == "lab-instruments.capture.v1", "invalid capture schema")
    validate(result.get("plan"))
    require(
        type(result.get("ok")) is bool
        and result.get("status") == ("captured" if result["ok"] else "failed"),
        "invalid capture status",
    )
    require(
        result.get("evidence_origin") in ("physical_instrument", "test_double"),
        "invalid evidence origin",
    )
    require(
        result.get("model") == result["plan"]["model"]
        and result.get("serial") == result["plan"]["serial"]
        and result.get("instrument") == result["plan"]["instrument"],
        "capture selection differs from plan",
    )
    require(isinstance(result.get("cleanup"), dict), "missing cleanup")
    require(
        result.get("sdk_version") == result["plan"]["sdk_version"]
        and result.get("effects") == result["plan"]["effects"],
        "capture SDK or effects differ from plan",
    )
    for field in (
        "outputs_commanded",
        "sample_zero_utc_verified",
        "calibration_verified",
        "dut_identity_verified",
        "physical_device_verified",
    ):
        require(result.get(field) is False, "unsupported capture verification claim")
    require(
        not result["ok"] or result["cleanup"].get("complete") is True,
        "successful capture has incomplete cleanup",
    )
    payload = result.get("payload")
    summary = {
        "capture": ref,
        "ok": result.get("ok"),
        "status": result.get("status"),
        "evidence_origin": result.get("evidence_origin"),
        "hardware_access": False,
        "executables_started": False,
        "source_files_rechecked": False,
        "physical_device_verified": False,
        "cleanup": result.get("cleanup"),
    }
    if payload is not None:
        require(isinstance(payload, dict), "invalid capture payload")
        decoded = base64.b64decode(payload["raw_base64"], validate=True)
        require(
            digest(decoded) == payload["sha256"] and len(decoded) == payload["length"],
            "payload digest or length differs",
        )
        plan = result["plan"]
        count = plan["samples"]
        require(
            payload["samples_per_channel"] == count and payload["channels"] == plan["channels"],
            "payload channels or count differ",
        )
        require(
            all(type(channel) is int for channel in payload["channels"]),
            "invalid payload channel type",
        )
        if plan["instrument"] == "scope":
            require(
                payload["encoding"] == "f64le_channel_major"
                and len(decoded) == count * len(plan["channels"]) * 8,
                "scope data size differs",
            )
            samples = struct.unpack("<" + "d" * (len(decoded) // 8), decoded)
            for value in samples:
                require(abs(value) <= 1e100, "invalid/nonfinite voltage sample")
            summary["channel_statistics"] = {
                str(ch): {
                    "minimum_v": min(samples[index * count : (index + 1) * count]),
                    "maximum_v": max(samples[index * count : (index + 1) * count]),
                }
                for index, ch in enumerate(plan["channels"])
            }
        else:
            require(
                payload["encoding"] == "u16le_dio0_first" and len(decoded) == count * 2,
                "digital data size differs",
            )
            words = struct.unpack("<" + "H" * count, decoded)
            summary["digital_transitions"] = {
                str(ch): sum(((a ^ b) >> ch) & 1 for a, b in zip(words, words[1:], strict=False))
                for ch in plan["channels"]
            }
        summary["payload_sha256"] = payload["sha256"]
        summary["payload_length"] = len(decoded)
    require(
        result.get("ok") is not True or payload is not None, "successful capture has no payload"
    )
    return summary
