from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__, sdl, sdl_plan, sds, sds_plan
from .capture import inspect_capture, load_plan, supervise
from .common import LabError, encode, read, write_new
from .dwf import Dwf
from .plan import MODEL, SDK_VERSION, create


def parser():
    root = argparse.ArgumentParser(description="Bounded AD3, SDL1020X-E and SDS804X HD sessions.")
    root.add_argument("--version", action="version", version="lab-instruments " + __version__)
    commands = root.add_subparsers(dest="command", required=True)
    doctor = commands.add_parser("doctor", help="host-only static readiness; never loads SDK")
    doctor.add_argument("--sdk", type=Path)
    doctor.add_argument("--sdk-sha256")
    doctor.add_argument("--json", action="store_true")
    devices = commands.add_parser("devices", help="explicit SDK USB enumeration, no device open")
    devices.add_argument("--sdk", type=Path, required=True)
    devices.add_argument("--sdk-sha256", required=True)
    devices.add_argument("--json", action="store_true")
    capture = commands.add_parser("capture")
    operations = capture.add_subparsers(dest="operation", required=True)
    plan = operations.add_parser("plan", help="host-only acquisition request; no SDK needed")
    plan.add_argument("--serial", required=True)
    plan.add_argument("--instrument", choices=("scope", "logic"), required=True)
    plan.add_argument("--channels", nargs="+", type=int, required=True)
    plan.add_argument("--sample-rate", dest="sample_rate_hz", type=float, default=1_000_000)
    plan.add_argument("--samples", type=int, default=4096)
    plan.add_argument("--range-v", type=float, default=10.0)
    plan.add_argument("--timeout-ms", type=int, default=15000)
    plan.add_argument("--output", type=Path, required=True)
    plan.add_argument("--json", action="store_true")
    check = operations.add_parser("inspect-plan")
    check.add_argument("input", type=Path)
    check.add_argument("--sha256", required=True)
    check.add_argument("--json", action="store_true")
    run = operations.add_parser("run")
    run.add_argument("--plan", type=Path, required=True)
    run.add_argument("--sha256", required=True)
    run.add_argument("--sdk", type=Path, required=True)
    run.add_argument("--sdk-sha256", required=True)
    run.add_argument("--output", type=Path, required=True)
    run.add_argument("--json", action="store_true")
    inspect = commands.add_parser("inspect", help="hash-bound saved acquisition; no SDK access")
    inspect.add_argument("input", type=Path)
    inspect.add_argument("--sha256", required=True)
    inspect.add_argument("--json", action="store_true")
    load = commands.add_parser(
        "load", help="SDL1020X-E: offline plan or bounded exact-identity session"
    )
    load_ops = load.add_subparsers(dest="operation", required=True)
    load_plan = load_ops.add_parser("plan", help="host-only plan, no TCP connection")
    load_plan.add_argument("--host", required=True)
    load_plan.add_argument("--port", type=int, default=5025)
    load_plan.add_argument("--serial")
    load_plan.add_argument("--firmware-version", dest="firmware")
    load_plan.add_argument(
        "--action", choices=("identify", "observe", "cc", "off"), default="observe"
    )
    load_plan.add_argument("--current-a", type=float)
    load_plan.add_argument("--min-voltage-v", type=float, default=0.0)
    load_plan.add_argument("--max-voltage-v", type=float, default=24.0)
    load_plan.add_argument("--max-current-a", type=float, default=1.0)
    load_plan.add_argument("--max-power-w", type=float, default=20.0)
    load_plan.add_argument("--samples", type=int, default=5)
    load_plan.add_argument("--interval-ms", type=int, default=200)
    load_plan.add_argument("--io-timeout-ms", type=int, default=1000)
    load_plan.add_argument("--timeout-ms", type=int, default=15000)
    load_plan.add_argument(
        "--test-double", action="store_true", help="only 127.0.0.1; synthetic evidence"
    )
    load_plan.add_argument("--output", type=Path, required=True)
    load_plan.add_argument("--json", action="store_true")
    for operation in ("inspect-plan", "inspect"):
        check = load_ops.add_parser(operation)
        check.add_argument("input", type=Path)
        check.add_argument("--sha256", required=True)
        check.add_argument("--json", action="store_true")
    load_run = load_ops.add_parser(
        "run", help="one TCP session; no confirmations/reconnects/retries"
    )
    load_run.add_argument("--plan", type=Path, required=True)
    load_run.add_argument("--sha256", required=True)
    load_run.add_argument("--output", type=Path, required=True)
    load_run.add_argument("--json", action="store_true")
    scope = commands.add_parser(
        "scope", help="SDS804X HD: exact-identity WORD waveform acquisition"
    )
    scope_ops = scope.add_subparsers(dest="operation", required=True)
    scope_plan = scope_ops.add_parser("plan", help="offline plan; no network access")
    scope_plan.add_argument("--host", required=True)
    scope_plan.add_argument("--port", type=int, default=5025)
    scope_plan.add_argument("--serial")
    scope_plan.add_argument("--firmware-version", dest="firmware")
    scope_plan.add_argument(
        "--action", choices=("identify", "stopped", "single"), default="stopped"
    )
    scope_plan.add_argument("--channels", type=int, nargs="+", default=[1])
    scope_plan.add_argument("--points", type=int, default=4096)
    scope_plan.add_argument("--io-timeout-ms", type=int, default=2000)
    scope_plan.add_argument("--timeout-ms", type=int, default=15000)
    scope_plan.add_argument(
        "--test-double", action="store_true", help="127.0.0.1 only; synthetic evidence"
    )
    scope_plan.add_argument("--output", type=Path, required=True)
    scope_plan.add_argument("--json", action="store_true")
    for operation in ("inspect-plan", "inspect"):
        check = scope_ops.add_parser(operation)
        check.add_argument("input", type=Path)
        check.add_argument("--sha256", required=True)
        check.add_argument("--json", action="store_true")
    scope_run = scope_ops.add_parser(
        "run", help="one owned worker/socket; no reconnects or retries"
    )
    scope_run.add_argument("--plan", type=Path, required=True)
    scope_run.add_argument("--sha256", required=True)
    scope_run.add_argument("--output", type=Path, required=True)
    scope_run.add_argument("--json", action="store_true")
    return root


def main(argv=None):
    args = parser().parse_args(argv)
    result = {
        "schema_version": "lab-instruments.result.v1",
        "hardware_access": False,
        "executables_started": False,
        "authorization_granted": False,
        "physical_device_verified": False,
        "outputs_commanded": False,
    }
    code = 0
    try:
        if args.command == "doctor":
            data = {
                "status": "sdk_required",
                "model": MODEL,
                "sdk_version_required": SDK_VERSION,
                "sdk_loaded": False,
                "device_enumeration_performed": False,
                "native_sdk_verified": False,
                "physical_device_verified": False,
            }
            if args.sdk is not None:
                _, data["sdk_file"] = read(
                    args.sdk, maximum=64 * 1024 * 1024, expected=args.sdk_sha256
                )
                data["status"] = "sdk_file_inspected_not_loaded"
        elif args.command == "devices":
            sdk, ref = Dwf.load(args.sdk, args.sdk_sha256)
            result["hardware_access"] = True
            data = {"devices": sdk.enumerate(), "sdk": ref, "sdk_version": sdk.version}
        elif args.command == "inspect":
            data = inspect_capture(args.input, args.sha256)
        elif args.command == "scope":
            if args.operation == "plan":
                keys = (
                    "host",
                    "port",
                    "serial",
                    "firmware",
                    "action",
                    "channels",
                    "points",
                    "io_timeout_ms",
                    "timeout_ms",
                    "test_double",
                )
                plan = sds_plan.create(**{key: getattr(args, key) for key in keys})
                data = {"plan": plan, "output": write_new(args.output, encode(plan))}
            elif args.operation == "inspect-plan":
                plan, _, ref = sds.load_plan(args.input, args.sha256)
                data = {"plan": plan, "input": ref}
            elif args.operation == "inspect":
                data = sds.inspect(args.input, args.sha256)
            else:
                captured, ref = sds.run(args.plan, args.sha256, args.output)
                data = {"session": captured, "output": ref}
                result["hardware_access"] = captured["hardware_access"]
                result["executables_started"] = True
                code = 0 if captured["ok"] else 3
        elif args.command == "load":
            if args.operation == "plan":
                keys = (
                    "host",
                    "serial",
                    "firmware",
                    "action",
                    "port",
                    "current_a",
                    "min_voltage_v",
                    "max_voltage_v",
                    "max_current_a",
                    "max_power_w",
                    "samples",
                    "interval_ms",
                    "io_timeout_ms",
                    "timeout_ms",
                    "test_double",
                )
                plan = sdl_plan.create(**{key: getattr(args, key) for key in keys})
                data = {"plan": plan, "output": write_new(args.output, encode(plan))}
            elif args.operation == "inspect-plan":
                plan, _, ref = sdl.load_plan(args.input, args.sha256)
                data = {"plan": plan, "input": ref}
            elif args.operation == "inspect":
                data = sdl.inspect(args.input, args.sha256)
            else:
                captured, ref = sdl.run(args.plan, args.sha256, args.output)
                data = {"session": captured, "output": ref}
                result["hardware_access"] = captured["hardware_access"]
                result["outputs_commanded"] = (
                    captured["input_enable_attempted"] and not captured["plan"]["test_double"]
                )
                code = 0 if captured["ok"] else 3
        elif args.operation == "plan":
            plan = create(
                **{
                    key: getattr(args, key)
                    for key in (
                        "serial",
                        "instrument",
                        "channels",
                        "sample_rate_hz",
                        "samples",
                        "range_v",
                        "timeout_ms",
                    )
                }
            )
            data = {"plan": plan, "output": write_new(args.output, encode(plan))}
        elif args.operation == "inspect-plan":
            plan, _, ref = load_plan(args.input, args.sha256)
            data = {"plan": plan, "input": ref}
        else:
            captured, ref = supervise(
                args.plan, args.sha256, args.sdk, args.sdk_sha256, args.output
            )
            data = {"supervisor": captured, "output": ref}
            result["hardware_access"] = captured["hardware_access"]
            result["executables_started"] = True
            code = 0 if captured["ok"] else 3
        result.update(ok=code == 0, data=data)
    except (LabError, OSError, ValueError, KeyError, TypeError, RecursionError) as error:
        if getattr(error, "code", "") == "WORKER_FAILED":
            result["executables_started"] = True
            result["hardware_access"] = None
        result.update(
            ok=False,
            error={
                "code": getattr(error, "code", "HOST_OR_INPUT_ERROR"),
                "message": str(error)[:512],
            },
        )
        code = 2
    sys.stdout.write(encode(result).decode("ascii"))
    return code
