from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import (
    __version__,
    bus_logic,
    load_checks,
    logic,
    logic_checks,
    logic_reports,
    sdl,
    sdl_plan,
    sds,
    sds_plan,
    thermal,
    thermal_plan,
    waveform,
)
from .capture import inspect_capture, load_plan, supervise
from .common import LabError, digest, encode, new_path, read, write_new
from .dwf import Dwf
from .plan import MODEL, SDK_VERSION, create


def parser():
    root = argparse.ArgumentParser(description="Bounded instrument sessions and offline analysis.")
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
    logic_command = commands.add_parser("logic", help="offline AD3 digital samples; no SDK/device")
    logic_ops = logic_command.add_subparsers(dest="operation", required=True)
    logic_check = logic_ops.add_parser("check", help="offline exact-data and per-cycle assertions")
    logic_check.add_argument("--spec", type=Path, required=True)
    logic_check.add_argument("--spec-sha256", required=True)
    logic_check.add_argument("--capture", type=Path, required=True)
    logic_check.add_argument("--capture-sha256", required=True)
    logic_check.add_argument("--output", type=Path)
    logic_check.add_argument("--json", action="store_true")
    for operation in ("inspect-check-report", "junit"):
        report = logic_ops.add_parser(
            operation, help="recompute a saved check report from original inputs"
        )
        report.add_argument("input", type=Path)
        report.add_argument("--sha256", required=True)
        report.add_argument("--spec", type=Path, required=True)
        report.add_argument("--spec-sha256", required=True)
        report.add_argument("--capture", type=Path, required=True)
        report.add_argument("--capture-sha256", required=True)
        if operation == "junit":
            report.add_argument("--output", type=Path, required=True)
        report.add_argument("--json", action="store_true")
    for operation in ("uart", "pwm", "i2c", "spi"):
        analysis = logic_ops.add_parser(operation)
        analysis.add_argument("input", type=Path)
        analysis.add_argument("--sha256", required=True)
        if operation in ("uart", "pwm"):
            analysis.add_argument("--channel", type=int, required=True)
        analysis.add_argument("--output", type=Path)
        analysis.add_argument("--json", action="store_true")
        if operation == "uart":
            analysis.add_argument("--baud", type=int, required=True)
            analysis.add_argument("--data-bits", type=int, choices=(5, 6, 7, 8), default=8)
            analysis.add_argument("--parity", choices=("none", "even", "odd"), default="none")
            analysis.add_argument("--stop-bits", type=int, choices=(1, 2), default=1)
            analysis.add_argument("--inverted", action="store_true")
        elif operation == "pwm":
            analysis.add_argument("--active-level", choices=("high", "low"), default="high")
        else:
            analysis.add_argument("--min-phase-samples", type=int, default=2)
            if operation == "i2c":
                analysis.add_argument("--scl", type=int, required=True)
                analysis.add_argument("--sda", type=int, required=True)
            else:
                analysis.add_argument("--sck", type=int, required=True)
                analysis.add_argument("--cs", type=int, required=True)
                analysis.add_argument("--mosi", type=int)
                analysis.add_argument("--miso", type=int)
                analysis.add_argument("--mode", type=int, choices=(0, 1, 2, 3), required=True)
                analysis.add_argument("--word-bits", type=int, default=8)
                analysis.add_argument("--bit-order", choices=("msb", "lsb"), default="msb")
                analysis.add_argument("--cs-active", choices=("low", "high"), default="low")
    wave = commands.add_parser("waveform", help="offline raw voltage assertions; no SDK or network")
    wave_ops = wave.add_subparsers(dest="operation", required=True)
    for operation in ("check", "inspect-check-report", "junit"):
        check = wave_ops.add_parser(operation)
        if operation != "check":
            check.add_argument("input", type=Path)
            check.add_argument("--sha256", required=True)
        check.add_argument("--spec", type=Path, required=True)
        check.add_argument("--spec-sha256", required=True)
        check.add_argument("--capture", type=Path, required=True)
        check.add_argument("--capture-sha256", required=True)
        if operation != "inspect-check-report":
            check.add_argument("--output", type=Path, required=operation == "junit")
        check.add_argument("--json", action="store_true")
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
    for operation in ("check", "inspect-check-report", "junit"):
        check = load_ops.add_parser(operation, help="offline saved-measurement assertions")
        if operation != "check":
            check.add_argument("input", type=Path)
            check.add_argument("--sha256", required=True)
        check.add_argument("--spec", type=Path, required=True)
        check.add_argument("--spec-sha256", required=True)
        check.add_argument("--capture", type=Path, required=True)
        check.add_argument("--capture-sha256", required=True)
        if operation != "inspect-check-report":
            check.add_argument("--output", type=Path, required=operation == "junit")
        check.add_argument("--json", action="store_true")
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
    camera = commands.add_parser(
        "thermal", help="PureThermal/Lepton: bounded Y16 capture and offline ROI analysis"
    )
    camera_ops = camera.add_subparsers(dest="operation", required=True)
    camera_plan = camera_ops.add_parser("plan", help="host-only request; no camera discovery")
    camera_plan.add_argument("--link")
    camera_plan.add_argument("--serial-hex", dest="serial")
    camera_plan.add_argument("--software-hex", dest="software")
    camera_plan.add_argument(
        "--action", choices=("devices", "identify", "capture"), default="capture"
    )
    camera_plan.add_argument("--buffers", type=int, default=3)
    camera_plan.add_argument(
        "--roi", action="append", nargs=5, metavar=("NAME", "X", "Y", "WIDTH", "HEIGHT")
    )
    camera_plan.add_argument("--distance-m", type=float)
    camera_plan.add_argument("--ambient-c", type=float)
    camera_plan.add_argument("--timeout-ms", type=int, default=15000)
    camera_plan.add_argument("--test-double", action="store_true")
    camera_plan.add_argument("--output", type=Path, required=True)
    camera_plan.add_argument("--json", action="store_true")
    for operation in ("inspect-plan", "inspect"):
        check = camera_ops.add_parser(operation)
        check.add_argument("input", type=Path)
        check.add_argument("--sha256", required=True)
        check.add_argument("--json", action="store_true")
    camera_run = camera_ops.add_parser(
        "run", help="exact Windows helper; no prompts/retries/driver changes"
    )
    camera_run.add_argument("--helper", type=Path, required=True)
    camera_run.add_argument("--helper-sha256", required=True)
    camera_import = camera_ops.add_parser(
        "import", help="host-only evaluation of hash-bound saved thermal source"
    )
    camera_import.add_argument("--input", type=Path, required=True)
    camera_import.add_argument("--sha256", required=True)
    for command in (camera_run, camera_import):
        command.add_argument("--plan", type=Path, required=True)
        command.add_argument("--plan-sha256", required=True)
        command.add_argument("--output", type=Path, required=True)
        command.add_argument("--json", action="store_true")
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
        elif args.command == "waveform":
            target = new_path(args.output) if getattr(args, "output", None) is not None else None
            inputs = (args.spec, args.spec_sha256, args.capture, args.capture_sha256)
            if args.operation == "check":
                report = waveform.evaluate(*inputs)
                code = 0 if report["ok"] else 3
                data = {"report": report}
                raw = encode(report)
            elif args.operation == "inspect-check-report":
                _, verification = waveform.inspect_report(args.input, args.sha256, *inputs)
                data = {"verification": verification}
            else:
                raw, verification, counts = waveform.junit_report(args.input, args.sha256, *inputs)
                code = 0 if verification["verdict"]["ok"] else 3
                data = {"verification": verification, "junit": counts}
            if target is not None:
                ref = {"path": str(target), "sha256": digest(raw), "size_bytes": len(raw)}
                encode({**result, "ok": code == 0, "data": {**data, "output": ref}})
                data["output"] = write_new(target, raw)
            else:
                encode({**result, "ok": code == 0, "data": data})
        elif args.command == "logic" and args.operation == "check":
            target = new_path(args.output) if args.output is not None else None
            report = logic_checks.evaluate(
                args.spec, args.spec_sha256, args.capture, args.capture_sha256
            )
            data = {"report": report}
            if target is not None:
                raw = encode(report)
                ref = {"path": str(target), "sha256": digest(raw), "size_bytes": len(raw)}
                # Reject an oversized CLI envelope before creating the report.
                encode({**result, "ok": report["ok"], "data": {**data, "output": ref}})
                data["output"] = write_new(target, raw)
            else:
                encode({**result, "ok": report["ok"], "data": data})
            code = 0 if report["ok"] else 3
        elif args.command == "logic" and args.operation in ("inspect-check-report", "junit"):
            target = new_path(args.output) if args.operation == "junit" else None
            inputs = (
                args.input,
                args.sha256,
                args.spec,
                args.spec_sha256,
                args.capture,
                args.capture_sha256,
            )
            if target is None:
                data = {"verification": logic_reports.inspect_report(*inputs)}
                encode({**result, "ok": True, "data": data})
            else:
                raw, verification, counts = logic_reports.junit_report(*inputs)
                code = 0 if verification["verdict"]["ok"] else 3
                data = {"verification": verification, "junit": counts}
                ref = {"path": str(target), "sha256": digest(raw), "size_bytes": len(raw)}
                encode({**result, "ok": code == 0, "data": {**data, "output": ref}})
                data["output"] = write_new(target, raw)
        elif args.command == "logic":
            if args.output is not None:
                new_path(args.output)
            if args.operation == "uart":
                analysis = logic.uart(
                    args.input,
                    args.sha256,
                    args.channel,
                    args.baud,
                    data_bits=args.data_bits,
                    parity=args.parity,
                    stop_bits=args.stop_bits,
                    inverted=args.inverted,
                )
            elif args.operation == "pwm":
                analysis = logic.pwm(
                    args.input, args.sha256, args.channel, active_level=args.active_level
                )
            elif args.operation == "i2c":
                analysis = bus_logic.i2c(
                    args.input,
                    args.sha256,
                    args.scl,
                    args.sda,
                    min_phase_samples=args.min_phase_samples,
                )
            else:
                analysis = bus_logic.spi(
                    args.input,
                    args.sha256,
                    args.sck,
                    args.cs,
                    mosi=args.mosi,
                    miso=args.miso,
                    mode=args.mode,
                    word_bits=args.word_bits,
                    bit_order=args.bit_order,
                    cs_active=args.cs_active,
                    min_phase_samples=args.min_phase_samples,
                )
            data = {"analysis": analysis}
            if args.output is not None:
                data["output"] = write_new(args.output, encode(analysis))
            code = 0 if analysis["ok"] else 3
        elif args.command == "thermal":
            if args.operation == "plan":
                keys = (
                    "link",
                    "serial",
                    "software",
                    "action",
                    "buffers",
                    "distance_m",
                    "ambient_c",
                    "timeout_ms",
                    "test_double",
                )
                rois = (
                    None
                    if args.roi is None
                    else [
                        dict(
                            zip(
                                ("name", "x", "y", "width", "height"),
                                [roi[0], *map(int, roi[1:])],
                                strict=True,
                            )
                        )
                        for roi in args.roi
                    ]
                )
                plan = thermal_plan.create(**{key: getattr(args, key) for key in keys}, rois=rois)
                data = {"plan": plan, "output": write_new(args.output, encode(plan))}
            elif args.operation == "inspect-plan":
                plan, _, ref = thermal.load_plan(args.input, args.sha256)
                data = {"plan": plan, "input": ref}
            elif args.operation == "inspect":
                data = thermal.inspect(args.input, args.sha256)
            else:
                captured, ref = (
                    thermal.run(
                        args.plan, args.plan_sha256, args.helper, args.helper_sha256, args.output
                    )
                    if args.operation == "run"
                    else thermal.import_source(
                        args.input, args.sha256, args.plan, args.plan_sha256, args.output
                    )
                )
                data = {"session": captured, "output": ref}
                result["hardware_access"] = captured["hardware_access"]
                result["executables_started"] = captured["executables_started"]
                code = 0 if captured["ok"] else 3
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
            if args.operation in ("check", "inspect-check-report", "junit"):
                target = (
                    new_path(args.output) if getattr(args, "output", None) is not None else None
                )
                inputs = (args.spec, args.spec_sha256, args.capture, args.capture_sha256)
                if args.operation == "check":
                    report = load_checks.evaluate(*inputs)
                    code = 0 if report["ok"] else 3
                    data, raw = {"report": report}, encode(report)
                elif args.operation == "inspect-check-report":
                    _, verification = load_checks.inspect_report(args.input, args.sha256, *inputs)
                    data = {"verification": verification}
                else:
                    raw, verification, counts = load_checks.junit_report(
                        args.input, args.sha256, *inputs
                    )
                    code = 0 if verification["verdict"]["ok"] else 3
                    data = {"verification": verification, "junit": counts}
                if target is not None:
                    ref = {"path": str(target), "sha256": digest(raw), "size_bytes": len(raw)}
                    encode({**result, "ok": code == 0, "data": {**data, "output": ref}})
                    data["output"] = write_new(target, raw)
                else:
                    encode({**result, "ok": code == 0, "data": data})
            elif args.operation == "plan":
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
