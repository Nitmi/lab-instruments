"""Saved radiometric ROI assertions; no camera, helper or acquisition path."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path

from . import __version__, thermal
from .common import MAX_JSON_BYTES, LabError, encode, integer, number, parse, read, require
from .logic_reports import bound_reference
from .thermal_frame import CONTROLS, analyze, trends

SPEC = "lab-instruments.thermal-check-spec.v1"
REPORT = "lab-instruments.thermal-check-report.v1"
PRODUCERS = ("0.11.0",)
TEMPERATURES = ("min_c", "max_c", "mean_c", "p95_c")
DELTAS = ("mean_delta_c", "max_delta_c")
TIME = "sensor_uptime_no_verified_utc_or_physical_clock"
FLAGS = {
    key: False
    for key in (
        "hardware_access",
        "executables_started",
        "authorization_granted",
        "physical_device_verified",
        "dut_identity_verified",
        "calibration_verified",
        "radiometric_accuracy_verified",
        "physical_timing_verified",
        "scene_parameters_verified",
        "junction_temperature_verified",
        "report_authenticated",
        "recorded_paths_followed",
    )
}


def fields(value, names, label):
    require(isinstance(value, dict) and set(value) == set(names), f"invalid {label} fields")


def rectangle(value):
    fields(value, ("x", "y", "width", "height"), "rectangle")
    integer(value["x"], "x", 0, 159)
    integer(value["y"], "y", 0, 119)
    integer(value["width"], "width", 1, 160 - value["x"])
    integer(value["height"], "height", 1, 120 - value["y"])


def contains(outer, inner):
    return all(
        outer[k] <= inner[k] and inner[k] + inner[s] <= outer[k] + outer[s]
        for k, s in (("x", "width"), ("y", "height"))
    )


def validate(spec):
    fields(spec, ("schema_version", "checks"), "thermal spec")
    require(spec["schema_version"] == SPEC, "unsupported thermal spec")
    checks = spec["checks"]
    require(isinstance(checks, list) and 1 <= len(checks) <= 8, "expected 1..8 checks")
    seen = set()
    for check in checks:
        fields(check, ("id", "roi", "window", "maximum_invalid_pixels", "expect"), "thermal check")
        name = check["id"]
        require(
            type(name) is str and re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,31}", name),
            "invalid check id",
        )
        require(name not in seen, "duplicate check id")
        seen.add(name)
        roi = check["roi"]
        rectangle(roi)
        integer(
            check["maximum_invalid_pixels"],
            "maximum invalid pixels",
            0,
            roi["width"] * roi["height"] - 1,
        )
        window = check["window"]
        fields(window, ("start", "end_exclusive"), "buffer window")
        integer(window["start"], "window start", 0, 7)
        integer(window["end_exclusive"], "window end", window["start"] + 1, 8)
        expect = check["expect"]
        require(
            isinstance(expect, dict)
            and bool(expect)
            and set(expect) <= {*TEMPERATURES, *DELTAS, "hotspot_within"},
            "unsupported/empty thermal expectations",
        )
        for metric, bounds in expect.items():
            if metric == "hotspot_within":
                rectangle(bounds)
                require(contains(roi, bounds), "hotspot rectangle exceeds ROI")
                continue
            fields(bounds, ("minimum", "maximum"), "metric bounds")
            low, high = (-410, 410) if metric in DELTAS else (-10, 400)
            number(bounds["minimum"], "minimum", low, high)
            number(bounds["maximum"], "maximum", bounds["minimum"], high)


class Snapshot:
    """101 files / 16 MiB, each unique artifact is read once including replay."""

    def __init__(self):
        self.files, self.size = {}, 0

    def read(self, path, maximum=MAX_JSON_BYTES, expected=None):
        path = Path(path).absolute()
        if path not in self.files:
            require(len(self.files) < 101, "thermal file limit", "LIMIT_EXCEEDED")
            remaining = 16 * MAX_JSON_BYTES - self.size
            require(remaining > 0, "thermal input budget", "LIMIT_EXCEEDED")
            raw, ref = read(path, maximum=min(maximum, remaining), expected=expected)
            self.files[path] = raw, ref
            self.size += len(raw)
        raw, ref = self.files[path]
        require(len(raw) <= maximum, "thermal file exceeds limit", "LIMIT_EXCEEDED")
        if expected is not None:
            require(expected == ref["sha256"], "snapshot SHA-256 differs", "HASH_MISMATCH")
        return raw, ref


def source(path, expected):
    snapshot, retained = Snapshot(), {}
    raw, ref = snapshot.read(path, expected=expected)
    value = parse(raw)
    directory = Path(ref["path"]).parent
    thermal.inspect_snapshot(value, ref, directory, reader=snapshot.read, retained=retained)
    require(value["plan"]["action"] == "capture", "thermal check requires capture evidence")
    payloads = retained.get("payloads", {})
    native_raw = snapshot.files.get(directory / "native/native.json")
    native = parse(native_raw[0]) if native_raw is not None else None
    info = {
        "source_ok": value["ok"],
        "source_status": value["status"],
        "source_error": value.get("error"),
        "operation": value["operation"],
        "evidence_origin": "test_double"
        if value["plan"]["test_double"]
        else "windows_media_foundation",
        "source_cleanup": {
            "recorded_parent_cleanup_complete": value.get("cleanup_complete"),
            "recorded_lease_released": value.get("local_lease_released"),
            "native": native,
        },
        "recorded_effects": value["plan"]["effects"],
        "source_hardware_access": value["hardware_access"],
        "source_executables_started": value["executables_started"],
        "context": value["plan"]["context"],
        "analysis": retained.get("analysis"),
        "time_basis": TIME,
        "apparent_surface_temperature_only": True,
        "hotspot_tie_policy": "first_maximum_in_row_major_roi_order",
        "p95_definition": "nearest_rank_ceil_0.95_n_valid_pixels",
        "partial_frames_scored": False,
    }
    return payloads, info, ref, snapshot


def evaluate(spec_path, spec_sha256, capture_path, capture_sha256):
    raw, spec_ref = read(spec_path, maximum=65536, expected=spec_sha256)
    spec = parse(raw)
    validate(spec)
    payloads, info, ref, snapshot = source(capture_path, capture_sha256)
    return evaluate_snapshot(spec, spec_ref, payloads, info, ref, snapshot)


def one_check(check, payloads, info):
    rows, assertions, trend, reason = [], [], None, None
    start, stop = check["window"]["start"], check["window"]["end_exclusive"]
    if not info["source_ok"]:
        reason = "source_failed"
    elif stop > len(info["analysis"]["frames"]):
        reason = "window_unavailable"
    else:
        controls = {key: payloads["before-" + key + ".bin"] for key in CONTROLS}
        frames = []
        for index in range(start, stop):
            native_frame = info["analysis"]["frames"][index]
            try:
                frame, _ = analyze(
                    payloads[f"frame-{index:04d}.y16"],
                    controls,
                    [{"name": check["id"], **check["roi"]}],
                    include_csv=False,
                )
            except LabError as error:
                if error.code != "NO_VALID_PIXELS":
                    raise
                reason = "no_valid_pixels"
                break
            summary = frame["rois"][0]
            row = {
                "buffer_index": index,
                "raw_sha256": native_frame["raw_sha256"],
                "pixel_encoding": "uint16_le_row_major_160x120_with_160x2_footer",
                "raw_roi_span": {
                    "first_byte": 2 * (check["roi"]["y"] * 160 + check["roi"]["x"]),
                    "row_stride_bytes": 320,
                    "row_bytes": 2 * check["roi"]["width"],
                    "rows": check["roi"]["height"],
                },
                "native_timestamp_100ns": native_frame["native_timestamp_100ns"],
                "telemetry": frame["telemetry"],
                "roi": summary,
            }
            rows.append(row)
            frame["raw_sha256"] = native_frame["raw_sha256"]
            frames.append(frame)
            if summary["invalid_or_out_of_range_pixels"] > check["maximum_invalid_pixels"]:
                reason = "invalid_pixel_budget_exceeded"
        if not reason:
            trend = trends(frames)
            if (
                any(key in check["expect"] for key in DELTAS)
                and not trend["temperature_trend_supported"]
            ):
                reason = "trend_unavailable"
        if not reason:
            for metric, bounds in sorted(check["expect"].items()):
                if metric in DELTAS:
                    actual = [trend["roi_changes"][0][metric]]
                elif metric == "hotspot_within":
                    actual = [row["roi"]["hotspot"] for row in rows]
                else:
                    actual = [row["roi"][metric] for row in rows]
                ok = all(
                    contains(bounds, {**v, "width": 1, "height": 1})
                    if metric == "hotspot_within"
                    else bounds["minimum"] <= v <= bounds["maximum"]
                    for v in actual
                )
                assertions.append(dict(metric=metric, expected=bounds, actual=actual, ok=ok))
    ok = not reason and all(a["ok"] for a in assertions)
    return {
        **check,
        "status": "invalid_evidence" if reason else "passed" if ok else "failed",
        "ok": ok,
        "reason": reason,
        "frames": rows,
        "trend": trend,
        "assertions": assertions,
    }


def evaluate_snapshot(
    spec, spec_ref, payloads, info, ref, snapshot, *, version=__version__, labels=None
):
    checks = [one_check(check, payloads, info) for check in spec["checks"]]
    ok = all(c["ok"] for c in checks)
    directory = Path(ref["path"]).parent
    report = dict(
        schema_version=REPORT,
        runner_version=version,
        scope="host_only_saved_thermal_assertions",
        spec=spec_ref,
        capture=ref,
        source=info,
        checks=checks,
        ok=ok,
        status="passed" if ok else "failed",
        complete=True,
        summary={
            "checks": len(checks),
            **{
                s: sum(c["status"] == s for c in checks)
                for s in ("passed", "failed", "invalid_evidence")
            },
        },
        input_files=[
            dict(
                name="capture" if p == Path(ref["path"]) else p.relative_to(directory).as_posix(),
                sha256=r["sha256"],
                size_bytes=r["size_bytes"],
            )
            for p, (_, r) in snapshot.files.items()
        ],
        **FLAGS,
    )
    if labels is not None:
        report["spec"], report["capture"] = labels
    encode(report)
    return report


def inspect_report(
    report_path, report_sha256, spec_path, spec_sha256, capture_path, capture_sha256
):
    raw, ref = read(report_path, expected=report_sha256)
    report = parse(raw)
    require(
        report.get("schema_version") == REPORT and report.get("runner_version") in PRODUCERS,
        "unsupported thermal check producer",
    )
    raw_spec, spec_ref = read(spec_path, maximum=65536, expected=spec_sha256)
    spec = parse(raw_spec)
    validate(spec)
    payloads, info, capture_ref, snapshot = source(capture_path, capture_sha256)
    bindings = {
        "spec": bound_reference(report.get("spec"), spec_ref),
        "capture": bound_reference(report.get("capture"), capture_ref),
    }
    expected = evaluate_snapshot(
        spec,
        spec_ref,
        payloads,
        info,
        capture_ref,
        snapshot,
        version=report["runner_version"],
        labels=(report["spec"], report["capture"]),
    )
    require(
        encode(report) == encode(expected),
        "thermal report differs from recomputation",
        "REPORT_MISMATCH",
    )
    verification = dict(
        schema_version="lab-instruments.thermal-check-verification.v1",
        ok=True,
        status="verified",
        complete=True,
        report=ref,
        bindings=bindings,
        producer_runner_version=report["runner_version"],
        verifier_version=__version__,
        verdict={k: report[k] for k in ("ok", "status", "summary")},
        scope="host_only_saved_thermal_verification",
        **FLAGS,
    )
    return report, verification


def junit_report(*args):
    report, verification = inspect_report(*args)
    counts = dict(
        tests=len(report["checks"]),
        failures=report["summary"]["failed"],
        errors=report["summary"]["invalid_evidence"],
        skipped=0,
    )
    suite = ET.Element(
        "testsuite", name="lab-instruments.thermal", **{k: str(v) for k, v in counts.items()}
    )
    props = ET.SubElement(suite, "properties")
    values = {
        **FLAGS,
        "time_basis": TIME,
        "source_status": report["source"]["source_status"],
        "evidence_origin": report["source"]["evidence_origin"],
        "apparent_surface_temperature_only": True,
        "producer_runner_version": report["runner_version"],
        "verifier_version": __version__,
        **{k + "_sha256": report[k]["sha256"] for k in ("spec", "capture")},
        "report_sha256": verification["report"]["sha256"],
    }
    for name, value in sorted(values.items()):
        ET.SubElement(
            props, "property", name=name, value=str(value).lower() if type(value) is bool else value
        )
    for check in report["checks"]:
        case = ET.SubElement(
            suite, "testcase", name=check["id"], classname="lab_instruments.thermal"
        )
        if not check["ok"]:
            ET.SubElement(
                case,
                "error" if check["status"] == "invalid_evidence" else "failure",
                message=check["reason"] or check["status"],
            ).text = encode(check).decode("ascii")
    raw = ET.tostring(suite, encoding="utf-8", xml_declaration=True) + b"\n"
    require(len(raw) <= MAX_JSON_BYTES, "JUnit exceeds limit", "LIMIT_EXCEEDED")
    return raw, verification, counts
