"""Bounded offline voltage assertions from native AD3 or SDS804X raw evidence."""

from __future__ import annotations

import base64
import math
import re
import struct
import xml.etree.ElementTree as ET
from pathlib import Path

from . import __version__, sds
from .capture import inspect_capture_value
from .common import MAX_JSON_BYTES, digest, encode, integer, number, parse, read, require
from .logic_reports import bound_reference
from .sds_wave import descriptor, voltage_samples

SPEC = "lab-instruments.waveform-check-spec.v1"
REPORT = "lab-instruments.waveform-check-report.v1"
METRICS = ("minimum_v", "maximum_v", "mean_v", "peak_to_peak_v", "rms_v", "ac_rms_v")
FLAGS = {
    key: False
    for key in (
        "hardware_access",
        "executables_started",
        "authorization_granted",
        "physical_device_verified",
        "physical_timing_verified",
        "calibration_verified",
        "dut_identity_verified",
        "sample_zero_utc_verified",
        "cross_channel_alignment_verified",
        "clipping_absent_verified",
        "analog_bandwidth_verified",
        "report_authenticated",
        "recorded_paths_followed",
    )
}


def fields(value, names, label):
    require(isinstance(value, dict) and set(value) == set(names), f"invalid {label} fields")


def validate(spec):
    fields(spec, ("schema_version", "source_kind", "checks"), "waveform spec")
    require(spec["schema_version"] == SPEC, "unsupported waveform spec")
    require(spec["source_kind"] in ("ad3", "sds804x"), "unsupported waveform source")
    checks = spec["checks"]
    require(isinstance(checks, list) and 1 <= len(checks) <= 8, "expected 1..8 checks")
    seen = set()
    for check in checks:
        fields(check, ("id", "channel", "window", "expect"), "waveform check")
        name = check["id"]
        require(
            type(name) is str and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", name),
            "invalid check id",
        )
        require(name not in seen, "duplicate check id")
        seen.add(name)
        integer(check["channel"], "channel", 1, 2 if spec["source_kind"] == "ad3" else 4)
        window = check["window"]
        fields(window, ("start", "end_exclusive"), "sample window")
        integer(window["start"], "window start", 0, 16383)
        integer(window["end_exclusive"], "window end", window["start"] + 1, 16384)
        expect = check["expect"]
        require(
            isinstance(expect, dict) and bool(expect) and set(expect) <= set(METRICS),
            "unsupported/empty voltage expectations",
        )
        for metric, bounds in expect.items():
            fields(bounds, ("minimum", "maximum"), "metric bounds")
            low = 0 if metric in ("peak_to_peak_v", "rms_v", "ac_rms_v") else -1e100
            number(bounds["minimum"], "metric minimum", low, 2e100)
            number(bounds["maximum"], "metric maximum", bounds["minimum"], 2e100)


class Snapshot:
    """At most 8 MiB/1,615 files; replay uses the same bytes without reopening."""

    def __init__(self):
        self.files, self.size = {}, 0

    def read(self, path, maximum=MAX_JSON_BYTES, expected=None):
        path = Path(path).absolute()
        if path not in self.files:
            require(len(self.files) < 1615, "waveform file limit exceeded", "LIMIT_EXCEEDED")
            remaining = 8 * MAX_JSON_BYTES - self.size
            require(remaining > 0, "waveform input budget exceeded", "LIMIT_EXCEEDED")
            raw, ref = read(path, maximum=min(maximum, remaining), expected=expected)
            self.size += len(raw)
            self.files[path] = (raw, ref)
        raw, ref = self.files[path]
        require(len(raw) <= maximum, "waveform file exceeds byte limit", "LIMIT_EXCEEDED")
        if expected is not None:
            require(expected == ref["sha256"], "snapshot SHA-256 differs", "HASH_MISMATCH")
        return raw, ref


def source(path, expected, kind):
    snapshot = Snapshot()
    raw, ref = snapshot.read(path, expected=expected)
    value = parse(raw)
    channels = {}
    if kind == "ad3":
        inspected = inspect_capture_value(value, ref)
        require(
            value["instrument"] == "scope" and inspected["ok"],
            "requires a successful AD3 scope capture",
        )
        cleanup = value["cleanup"]
        require(
            cleanup.get("input_stopped") is True
            and cleanup.get("owned_handle_closed") is True
            and cleanup.get("errors") == [],
            "AD3 source cleanup incomplete",
        )
        plan, settings = value["plan"], value["settings"]
        count = plan["samples"]
        require(
            type(settings["samples"]) is int and settings["samples"] == count,
            "AD3 sample count readback differs",
        )
        rate = number(settings["sample_rate_hz"], "actual sample rate", 1, 125_000_000)
        require(
            rate <= plan["sample_rate_hz"] * (1 + 1e-9)
            and settings["trigger"] == "immediate"
            and settings["rate_policy"] == "at_most_requested",
            "AD3 readback differs",
        )
        require(
            value["payload"]["time_semantics"]
            == "sample_index_over_actual_rate_no_verified_utc_origin",
            "AD3 time basis differs",
        )
        payload = base64.b64decode(value["payload"]["raw_base64"], validate=True)
        samples = struct.unpack("<" + "d" * (len(payload) // 8), payload)
        for index, channel in enumerate(plan["channels"]):
            scale = settings["channel_scales"][str(channel)]
            number(scale["range_v"], "readback range", 1, 50)
            number(scale["offset_v"], "readback offset", -1e-9, 1e-9)
            channels[channel] = {
                "values": samples[index * count : (index + 1) * count],
                "stride": 8,
                "offset": index * count * 8,
                "raw": {
                    "sha256": digest(payload),
                    "size_bytes": len(payload),
                    "encoding": "f64le_channel_major",
                },
                "interval_s": 1 / rate,
                "first_time_s": 0.0,
                "conversion": "sdk_voltage_samples",
                "coupling": "DC",
            }
        source_info = {
            "model": value["model"],
            "serial": value["serial"],
            "source_cleanup": cleanup,
            "evidence_origin": value["evidence_origin"],
            "time_basis": "sample_index_over_reported_rate_no_verified_utc_origin",
        }
    else:
        retained = {}
        inspected = sds.inspect_snapshot(
            value, ref, Path(ref["path"]).parent, reader=snapshot.read, retained=retained
        )
        require(
            inspected["source_ok"] and value["plan"]["action"] != "identify",
            "requires a successful SDS waveform acquisition",
        )
        count = value["plan"]["points"]
        for channel in value["plan"]["channels"]:
            preamble, pre_ref = retained[f"c{channel}-preamble.bin"]
            words, word_ref = retained[f"c{channel}-words.bin"]
            meta = descriptor(preamble, channel, count)
            _, samples = voltage_samples(meta, words, count)
            channels[channel] = {
                "values": samples,
                "stride": 2,
                "offset": 0,
                "raw": {
                    "name": f"c{channel}-words.bin",
                    "sha256": word_ref["sha256"],
                    "size_bytes": word_ref["size_bytes"],
                    "encoding": "s16le_left_aligned_adc_words",
                },
                "preamble": {
                    "name": f"c{channel}-preamble.bin",
                    "sha256": pre_ref["sha256"],
                    "size_bytes": pre_ref["size_bytes"],
                },
                "descriptor": meta,
                "interval_s": meta["sample_interval_s"],
                "first_time_s": meta["trigger_delay_s"] - meta["timebase_s"] * 5,
                "conversion": "en11g_probe_aware_word_conversion",
                "coupling": {0: "DC", 1: "AC", 2: "GND"}[meta["coupling_code"]],
            }
        source_info = {
            "identity": value["identity"],
            "source_cleanup": value["cleanup"],
            "source_freshness": value["freshness"],
            "evidence_origin": value["evidence_origin"],
            "time_basis": "descriptor_relative_no_verified_utc_origin",
        }
    return channels, source_info, ref, snapshot


def statistics(values):
    count = len(values)
    mean = math.fsum(values) / count
    low, high = min(values), max(values)
    return {
        "minimum_v": low,
        "maximum_v": high,
        "mean_v": mean,
        "peak_to_peak_v": high - low,
        "rms_v": math.sqrt(math.fsum(x * x for x in values) / count),
        "ac_rms_v": math.sqrt(math.fsum((x - mean) ** 2 for x in values) / count),
    }


def evaluate(spec_path, spec_sha256, capture_path, capture_sha256, *, labels=None):
    raw, spec_ref = read(spec_path, maximum=65536, expected=spec_sha256)
    spec = parse(raw)
    validate(spec)  # Reject malformed specs before reading source data.
    channels, source_info, capture_ref, snapshot = source(
        capture_path, capture_sha256, spec["source_kind"]
    )
    return evaluate_snapshot(
        spec, spec_ref, channels, source_info, capture_ref, snapshot, labels=labels
    )


def evaluate_snapshot(spec, spec_ref, channels, source_info, capture_ref, snapshot, *, labels=None):
    checks = []
    for check in spec["checks"]:
        channel = channels.get(check["channel"])
        start, stop = check["window"]["start"], check["window"]["end_exclusive"]
        reason = (
            "channel_not_captured"
            if channel is None
            else "window_exceeds_capture"
            if stop > len(channel["values"])
            else "requires_dc_coupling"
            if channel["coupling"] != "DC"
            else None
        )
        if reason:
            checks.append(
                {
                    **check,
                    "status": "invalid_evidence",
                    "ok": False,
                    "reason": reason,
                    "metrics": None,
                    "assertions": [],
                }
            )
            continue
        values = channel["values"][start:stop]
        require(
            all(math.isfinite(v) and abs(v) <= 1e100 for v in values),
            "unsupported/nonfinite voltage samples",
        )
        metrics = statistics(values)
        assertions = [
            {
                "metric": name,
                "expected": bounds,
                "actual": metrics[name],
                "ok": bounds["minimum"] <= metrics[name] <= bounds["maximum"],
            }
            for name, bounds in sorted(check["expect"].items())
        ]
        passed = all(a["ok"] for a in assertions)
        checks.append(
            {
                **check,
                "status": "passed" if passed else "failed",
                "ok": passed,
                "metrics": metrics,
                "assertions": assertions,
                "raw_span": {
                    "start": channel["offset"] + start * channel["stride"],
                    "end_exclusive": channel["offset"] + stop * channel["stride"],
                },
                "source_samples": {k: v for k, v in channel.items() if k != "values"},
                "first_time_s": channel["first_time_s"] + start * channel["interval_s"],
                "last_time_s": channel["first_time_s"] + (stop - 1) * channel["interval_s"],
            }
        )
    passed = all(c["ok"] for c in checks)
    report = {
        "schema_version": REPORT,
        "runner_version": __version__,
        "scope": "host_only_saved_waveform_evaluation",
        "spec": spec_ref,
        "capture": capture_ref,
        "source_kind": spec["source_kind"],
        "source": source_info,
        "input_files": [
            {
                "name": "capture" if str(p) == capture_ref["path"] else p.name,
                "sha256": r["sha256"],
                "size_bytes": r["size_bytes"],
            }
            for p, (_, r) in snapshot.files.items()
        ],
        "ok": passed,
        "status": "passed" if passed else "failed",
        "complete": True,
        "checks": checks,
        "summary": {
            "checks": len(checks),
            **{
                status: sum(c["status"] == status for c in checks)
                for status in ("passed", "failed", "invalid_evidence")
            },
        },
        **FLAGS,
    }
    if labels is not None:
        # Paths are labels only; rechecking uses explicit caller-supplied files.
        report["spec"], report["capture"] = labels
    encode(report)
    return report


def inspect_report(
    report_path, report_sha256, spec_path, spec_sha256, capture_path, capture_sha256
):
    raw, ref = read(report_path, expected=report_sha256)
    report = parse(raw)
    require(
        report.get("schema_version") == REPORT and report.get("runner_version") == __version__,
        "unsupported waveform report producer",
    )
    # Validate bindings before regenerating; never open the report's path labels.
    raw_spec, spec_ref = read(spec_path, maximum=65536, expected=spec_sha256)
    spec = parse(raw_spec)
    validate(spec)
    channels, source_info, capture_ref, snapshot = source(
        capture_path, capture_sha256, spec["source_kind"]
    )
    bindings = {
        "spec": bound_reference(report.get("spec"), spec_ref),
        "capture": bound_reference(report.get("capture"), capture_ref),
    }
    regenerated = evaluate_snapshot(
        spec,
        spec_ref,
        channels,
        source_info,
        capture_ref,
        snapshot,
        labels=(report["spec"], report["capture"]),
    )
    require(
        encode(report) == encode(regenerated),
        "waveform report differs from recomputation",
        "REPORT_MISMATCH",
    )
    verification = {
        "schema_version": "lab-instruments.waveform-check-verification.v1",
        "ok": True,
        "status": "verified",
        "report": ref,
        "bindings": bindings,
        "producer_runner_version": report["runner_version"],
        "verifier_version": __version__,
        "verdict": {k: report[k] for k in ("ok", "status", "summary")},
        "scope": "host_only_saved_waveform_verification",
        **FLAGS,
    }
    return report, verification


def junit_report(*args):
    report, verification = inspect_report(*args)
    counts = {
        "tests": len(report["checks"]),
        "failures": report["summary"]["failed"],
        "errors": report["summary"]["invalid_evidence"],
        "skipped": 0,
    }
    suite = ET.Element(
        "testsuite", name="lab-instruments.waveform", **{k: str(v) for k, v in counts.items()}
    )
    props = ET.SubElement(suite, "properties")
    for name, value in sorted(
        {
            **FLAGS,
            "capture_sha256": report["capture"]["sha256"],
            "spec_sha256": report["spec"]["sha256"],
            "report_sha256": verification["report"]["sha256"],
        }.items()
    ):
        ET.SubElement(
            props, "property", name=name, value=str(value).lower() if type(value) is bool else value
        )
    for check in report["checks"]:
        case = ET.SubElement(
            suite, "testcase", name=check["id"], classname="lab_instruments.waveform"
        )
        if not check["ok"]:
            tag = "error" if check["status"] == "invalid_evidence" else "failure"
            ET.SubElement(case, tag, message=check["status"]).text = encode(check).decode("ascii")
    raw = ET.tostring(suite, encoding="utf-8", xml_declaration=True) + b"\n"
    require(len(raw) <= MAX_JSON_BYTES, "JUnit exceeds byte limit", "LIMIT_EXCEEDED")
    return raw, verification, counts
