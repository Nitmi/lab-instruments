"""Recompute saved logic reports from explicit inputs before inspection/JUnit export."""

from __future__ import annotations

import xml.etree.ElementTree as ET

from . import __version__, logic_checks
from .common import MAX_JSON_BYTES, encode, parse, read, require

# These producers share the exact v1 assertion/decoder semantics. Unknown versions
# require an explicit compatibility review, not a best-effort pass.
PRODUCERS = ("0.7.0", "0.8.0", "0.9.0")
FLAGS = {
    "hardware_access": False,
    "executables_started": False,
    "authorization_granted": False,
    "physical_device_verified": False,
    "physical_timing_verified": False,
    "calibration_verified": False,
    "protocol_timing_compliance_verified": False,
    "endpoint_identity_verified": False,
}


def bound_reference(recorded, supplied):
    logic_checks.fields(recorded, ("path", "sha256", "size_bytes"), "saved input reference")
    path = recorded["path"]
    require(
        type(path) is str and 0 < len(path) <= 4096 and all(ord(c) >= 32 for c in path),
        "invalid recorded path label",
    )
    require(
        recorded["sha256"] == supplied["sha256"]
        and type(recorded["size_bytes"]) is int
        and recorded["size_bytes"] == supplied["size_bytes"],
        "report is bound to different input bytes",
        "REPORT_BINDING_MISMATCH",
    )
    # A recorded path is an untrusted label. Never resolve/open it: relocation is
    # allowed only through the explicitly supplied files and their exact hashes.
    return {
        "recorded": recorded,
        "supplied": supplied,
        "path_label_differs": path != supplied["path"],
    }


def verified_snapshot(
    report_path, report_sha256, spec_path, spec_sha256, capture_path, capture_sha256
):
    raw_report, report_ref = read(report_path, expected=report_sha256)
    report = parse(raw_report)
    require(
        report.get("schema_version") == "lab-instruments.logic-check-report.v1",
        "unsupported report schema",
    )
    version = report.get("runner_version")
    require(type(version) is str and version in PRODUCERS, "unsupported report producer version")
    raw_spec, spec_ref = read(spec_path, maximum=65536, expected=spec_sha256)
    spec = parse(raw_spec)
    logic_checks.validate(spec)  # Reject malformed specs before reading capture bytes.
    spec_binding = bound_reference(report.get("spec"), spec_ref)
    raw_capture, capture_ref = read(capture_path, expected=capture_sha256)
    capture = parse(raw_capture)
    capture_binding = bound_reference(report.get("capture"), capture_ref)
    expected = logic_checks.evaluate_snapshot(
        spec, report["spec"], capture, report["capture"], version
    )
    # Canonical JSON comparison also distinguishes bool/int and int/float. A
    # rehashed edited report, even with self-consistent native hashes, must match
    # the independently regenerated entire result, including every safety flag.
    require(
        encode(report) == encode(expected),
        "saved report differs from recomputation",
        "REPORT_MISMATCH",
    )
    verification = {
        "schema_version": "lab-instruments.logic-check-verification.v1",
        "ok": True,
        "status": "verified",
        "complete": True,
        "scope": "host_only_saved_logic_verification",
        "report": report_ref,
        "producer_runner_version": version,
        "verifier_version": __version__,
        "spec_binding": spec_binding,
        "capture_binding": capture_binding,
        "verdict": {key: report[key] for key in ("ok", "status", "summary")},
        "checks": [{key: c[key] for key in ("id", "kind", "status")} for c in report["checks"]],
        "evidence_origin": report["evidence_origin"],
        "report_authenticated": False,
        "recorded_paths_followed": False,
        **FLAGS,
    }
    encode(verification)
    return report, verification


def inspect_report(*args):
    """Integrity success is distinct from the original assertion verdict."""
    _, verification = verified_snapshot(*args)
    return verification


def junit_report(*args):
    """Return bounded XML only after regenerating and checking the saved report."""
    report, verification = verified_snapshot(*args)
    checks = report["checks"]
    counts = {
        "tests": len(checks),
        "failures": sum(c["status"] == "failed" for c in checks),
        "errors": sum(c["status"] == "invalid_evidence" for c in checks),
        "skipped": 0,
    }
    suite = ET.Element(
        "testsuite", name="lab-instruments.logic", **{k: str(v) for k, v in counts.items()}
    )
    props = ET.SubElement(suite, "properties")
    properties = {
        "report_sha256": verification["report"]["sha256"],
        "spec_sha256": report["spec"]["sha256"],
        "capture_sha256": report["capture"]["sha256"],
        "producer_runner_version": report["runner_version"],
        "verifier_version": __version__,
        "evidence_origin": report["evidence_origin"],
        "scope": "host_only_saved_logic_verification",
        "report_authenticated": False,
        "recorded_paths_followed": False,
        "duration_measured": False,
        **FLAGS,
    }
    for key, value in sorted(properties.items()):
        ET.SubElement(
            props, "property", name=key, value=str(value).lower() if type(value) is bool else value
        )
    for check in checks:
        case = ET.SubElement(
            suite, "testcase", name=check["id"], classname="lab_instruments.logic." + check["kind"]
        )
        if check["status"] == "passed":
            continue
        tag = "error" if check["status"] == "invalid_evidence" else "failure"
        native = check["native"]
        detail = {
            "status": check["status"],
            "assertions": check["assertions"],
            "native_sha256": check["native_sha256"],
            "native_status": native["status"],
            "source_status": native["source_status"],
            "source_error": native["source_error"],
            "source_cleanup": native["source_cleanup"],
        }
        ET.SubElement(case, tag, type=check["status"], message=check["status"]).text = encode(
            detail
        ).decode("ascii")
    raw = ET.tostring(suite, encoding="utf-8", xml_declaration=True) + b"\n"
    require(len(raw) <= MAX_JSON_BYTES, "JUnit exceeds byte limit", "LIMIT_EXCEEDED")
    return raw, verification, counts
