from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__
from .capture import inspect_capture, load_plan, supervise
from .common import LabError, encode, read, write_new
from .dwf import Dwf
from .plan import MODEL, SDK_VERSION, create


def parser():
    root = argparse.ArgumentParser(description="Bounded Digilent AD3 input acquisition.")
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
