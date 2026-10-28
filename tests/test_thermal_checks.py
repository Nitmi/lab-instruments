from __future__ import annotations

import copy
import shutil
import socket
import struct
import subprocess
import xml.etree.ElementTree as ET

import pytest
from thermal_double import SERIAL, SOFTWARE, frame, source

from lab_instruments import __version__, cli, thermal, thermal_plan
from lab_instruments import thermal_checks as checks
from lab_instruments.common import LabError, digest, encode, parse, write_new


def spec():
    return {
        "schema_version": checks.SPEC,
        "checks": [
            {
                "id": "cpu",
                "roi": {"x": 20, "y": 10, "width": 4, "height": 4},
                "window": {"start": 0, "end_exclusive": 3},
                "maximum_invalid_pixels": 0,
                "expect": {
                    "mean_c": {"minimum": 26, "maximum": 29},
                    "max_c": {"minimum": 27, "maximum": 30},
                    "mean_delta_c": {"minimum": 0.19, "maximum": 0.21},
                    "hotspot_within": {"x": 21, "y": 11, "width": 1, "height": 1},
                },
            }
        ],
    }


def hot(index, raw):
    raw = bytearray(raw)
    struct.pack_into("<H", raw, 2 * (11 * 160 + 21), 30100 + index * 10)
    return bytes(raw)


def saved(root, *, mutation=hot, buffers=3, action="capture"):
    root.mkdir()
    native = root / "original"
    native.mkdir()
    plan = thermal_plan.create(
        "SYNTHETIC-PT3",
        SERIAL if action == "capture" else None,
        SOFTWARE if action == "capture" else None,
        buffers=buffers,
        action=action,
        test_double=True,
    )
    original = source(native, plan, mutation=mutation)
    pp = root / "plan.json"
    write_new(pp, encode(plan))
    _, ref = thermal.import_source(
        original, digest(original.read_bytes()), pp, digest(pp.read_bytes()), root / "capture"
    )
    return root / "capture/thermal-result.json", ref["sha256"]


@pytest.fixture
def inputs(tmp_path):
    capture, sha = saved(tmp_path / "saved")
    contract = tmp_path / "spec.json"
    write_new(contract, encode(spec()))
    return contract, digest(contract.read_bytes()), capture, sha


def test_metrics_native_recomputation_raw_spans_and_no_hardware(inputs, monkeypatch):
    def forbidden(*a, **kw):
        pytest.fail("offline check must not use socket, subprocess or acquisition")

    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(thermal, "run", forbidden)
    observed, reader = [], checks.read

    def record(path, *a, **kw):
        observed.append(str(path))
        return reader(path, *a, **kw)

    monkeypatch.setattr(checks, "read", record)
    report = checks.evaluate(*inputs)
    assert report["ok"] and report["runner_version"] == __version__
    assert len(observed) == len(set(observed))
    check = report["checks"][0]
    assert check["frames"][0]["roi"]["max_c"] == pytest.approx(27.85)
    assert check["frames"][0]["roi"]["mean_c"] == pytest.approx(26.9125)
    assert check["frames"][0]["roi"]["p95_c"] == pytest.approx(27.85)
    assert check["frames"][0]["raw_roi_span"] == {
        "first_byte": 3240,
        "row_stride_bytes": 320,
        "row_bytes": 8,
        "rows": 4,
    }
    assert check["trend"]["roi_changes"][0]["mean_delta_c"] == pytest.approx(0.2)
    assert check["trend"]["roi_changes"][0]["elapsed_sensor_s"] == 0.24
    assert report["source"]["evidence_origin"] == "test_double"
    assert report["source"]["analysis"]["frames"][0]["controls"]["resolution"] == 1
    assert all(report[k] is False for k in checks.FLAGS)


@pytest.mark.parametrize(
    "kind",
    ["failed", "window", "no-pixels", "invalid-pixel", "mask", "ffc", "duplicate", "failed-source"],
)
def test_failures_stay_failed_or_invalid_and_junit_never_skips(tmp_path, kind):
    def mutation(i, raw):
        if kind == "ffc":
            return frame(i, ffc_ms=500 + i)
        if kind == "duplicate":
            return frame(0)
        if kind == "failed-source":
            return frame(i, status=32)
        raw = bytearray(hot(i, raw))
        if kind == "no-pixels":
            for y in range(10, 14):
                for x in range(20, 24):
                    struct.pack_into("<H", raw, 2 * (y * 160 + x), 0)
        elif kind == "invalid-pixel" or (kind == "mask" and i):
            struct.pack_into("<H", raw, 2 * (10 * 160 + 20), 0)
        return bytes(raw)

    cap, sha = saved(tmp_path / "saved", mutation=mutation)
    value = spec()
    if kind == "failed":
        value["checks"][0]["expect"]["max_c"] = {"minimum": 50, "maximum": 60}
    if kind == "window":
        value["checks"][0]["window"]["end_exclusive"] = 4
    if kind == "mask":
        value["checks"][0]["maximum_invalid_pixels"] = 1
    sp, rp = tmp_path / "spec.json", tmp_path / "report.json"
    write_new(sp, encode(value))
    args = sp, digest(sp.read_bytes()), cap, sha
    report = checks.evaluate(*args)
    expected = "failed" if kind == "failed" else "invalid_evidence"
    assert report["checks"][0]["status"] == expected and not report["ok"]
    if kind == "failed-source":
        assert report["source"]["source_error"]["code"] == "FRAME_NOT_READY"
        assert report["checks"][0]["frames"] == []
        assert report["source"]["source_cleanup"]["native"]["cleanup_complete"] is True
    write_new(rp, encode(report))
    raw, verification, counts = checks.junit_report(rp, digest(rp.read_bytes()), *args)
    suite = ET.fromstring(raw)
    assert not verification["verdict"]["ok"] and counts["skipped"] == 0
    assert counts["failures" if kind == "failed" else "errors"] == 1
    assert len(suite.findall("testcase/failure" if kind == "failed" else "testcase/error")) == 1


@pytest.mark.parametrize(
    "mutate",
    [
        lambda s: s.update(extra=0),
        lambda s: s.update(checks=[]),
        lambda s: s["checks"].append(copy.deepcopy(s["checks"][0])),
        lambda s: s["checks"][0].update(maximum_invalid_pixels=16),
        lambda s: s["checks"][0].update(maximum_invalid_pixels=True),
        lambda s: s["checks"][0]["roi"].update(x=-1),
        lambda s: s["checks"][0]["roi"].update(width=161),
        lambda s: s["checks"][0]["window"].update(end_exclusive=0),
        lambda s: s["checks"][0]["window"].update(end_exclusive=9),
        lambda s: s["checks"][0].update(expect={}),
        lambda s: s["checks"][0].update(expect={"junction_c": {"minimum": 0, "maximum": 50}}),
        lambda s: s["checks"][0]["expect"]["mean_c"].update(minimum=float("nan")),
        lambda s: s["checks"][0]["expect"]["mean_c"].update(minimum=True),
        lambda s: s["checks"][0]["expect"]["hotspot_within"].update(x=0),
    ],
)
def test_spec_bounds_and_unsupported_semantics(mutate):
    value = spec()
    mutate(value)
    with pytest.raises(LabError):
        checks.validate(value)


@pytest.mark.parametrize("tamper", ["metric", "source", "verdict", "flag", "inventory", "version"])
def test_rehashing_does_not_make_tampered_report_valid(inputs, tmp_path, tamper):
    report = checks.evaluate(*inputs)
    if tamper == "metric":
        report["checks"][0]["frames"][0]["roi"]["max_c"] = 50
    if tamper == "source":
        report["source"]["evidence_origin"] = "real_device"
    if tamper == "verdict":
        report["summary"]["failed"] = 1
    if tamper == "flag":
        report["calibration_verified"] = True
    if tamper == "inventory":
        report["input_files"].pop()
    if tamper == "version":
        report["runner_version"] = "99.0.0"
    path = tmp_path / "report.json"
    write_new(path, encode(report))
    with pytest.raises(LabError):
        checks.inspect_report(path, digest(path.read_bytes()), *inputs)


def test_explicit_relocation_and_binding(inputs, tmp_path):
    report = checks.evaluate(*inputs)
    # These path labels must never be opened.
    report["spec"]["path"] = "Z:/untrusted/absent.json"
    report["capture"]["path"] = "Z:/untrusted/absent-result.json"
    rp = tmp_path / "report.json"
    write_new(rp, encode(report))
    moved = tmp_path / "moved"
    shutil.copytree(inputs[2].parent, moved)
    _, verification = checks.inspect_report(
        rp, digest(rp.read_bytes()), inputs[0], inputs[1], moved / "thermal-result.json", inputs[3]
    )
    assert verification["ok"] and verification["verdict"]["ok"]
    assert not verification["recorded_paths_followed"]
    with pytest.raises(LabError, match="SHA-256"):
        checks.evaluate(inputs[0], "0" * 64, *inputs[2:])


@pytest.mark.parametrize("tamper", ["raw", "csv", "control", "extra", "symlink"])
def test_source_corruption_cannot_be_scored(inputs, tamper):
    directory = inputs[2].parent
    if tamper == "extra":
        write_new(directory / "extra.txt", b"x")
    elif tamper == "symlink":
        try:
            (directory / "link").symlink_to(directory / "plan.json")
        except OSError:
            pytest.skip("symlink creation unavailable")
    else:
        name = {
            "raw": "native/frame-0000.y16",
            "csv": "frame-0000.csv",
            "control": "native/before-resolution.bin",
        }[tamper]
        path = directory / name
        path.write_bytes(path.read_bytes() + b"x")
    with pytest.raises((LabError, OSError)):
        checks.evaluate(*inputs)


def test_cli_preserves_false_verdict_and_exclusive_outputs(inputs, tmp_path, capsys):
    sp = inputs[0]
    value = spec()
    value["checks"][0]["expect"]["max_c"] = {"minimum": 50, "maximum": 60}
    sp.write_bytes(encode(value))
    rp, xp = tmp_path / "report.json", tmp_path / "checks.xml"
    args = [
        "--spec",
        str(sp),
        "--spec-sha256",
        digest(sp.read_bytes()),
        "--capture",
        str(inputs[2]),
        "--capture-sha256",
        inputs[3],
        "--json",
    ]
    assert cli.main(["thermal", "check", *args, "--output", str(rp)]) == 3
    assert parse(capsys.readouterr().out.encode())["data"]["report"]["summary"]["failed"] == 1
    assert (
        cli.main(
            ["thermal", "inspect-check-report", str(rp), "--sha256", digest(rp.read_bytes()), *args]
        )
        == 0
    )
    assert not parse(capsys.readouterr().out.encode())["data"]["verification"]["verdict"]["ok"]
    assert (
        cli.main(
            [
                "thermal",
                "junit",
                str(rp),
                "--sha256",
                digest(rp.read_bytes()),
                *args,
                "--output",
                str(xp),
            ]
        )
        == 3
    )
    assert parse(capsys.readouterr().out.encode())["data"]["junit"]["failures"] == 1
    assert cli.main(["thermal", "check", *args, "--output", str(rp)]) == 2
    assert parse(capsys.readouterr().out.encode())["error"]["code"] == "OUTPUT_EXISTS"


def test_no_csv_mode_reuses_native_values():
    from thermal_double import control_bytes

    from lab_instruments.thermal_frame import analyze

    args = frame(), control_bytes(), [{"name": "all", "x": 0, "y": 0, "width": 160, "height": 120}]
    normal, csv = analyze(*args)
    no_csv, empty = analyze(*args, include_csv=False)
    assert normal == no_csv and csv and empty == b""


def test_missing_trend_does_not_invalidate_absolute_temperature_check(tmp_path):
    cap, sha = saved(tmp_path / "saved", mutation=lambda i, raw: frame(0))
    value = spec()
    check = value["checks"][0]
    check["expect"] = {"mean_c": {"minimum": 26, "maximum": 27}}
    sp = tmp_path / "spec.json"
    write_new(sp, encode(value))
    report = checks.evaluate(sp, digest(sp.read_bytes()), cap, sha)
    assert report["ok"]
    assert report["checks"][0]["trend"]["duplicate_buffers"] == 2
    assert not report["checks"][0]["trend"]["temperature_trend_supported"]


def test_success_report_requires_native_cleanup_even_after_rehash(inputs):
    cap = inputs[2]
    native = cap.parent / "native/native.json"
    value = parse(native.read_bytes())
    value["cleanup_complete"] = False
    native.write_bytes(encode(value))
    # Rebind inventories coherently; native evaluation still rejects success.
    source_path = cap.parent / "native/source.json"
    value = parse(source_path.read_bytes())
    value["files"]["native.json"] = {
        "sha256": digest(native.read_bytes()),
        "size_bytes": native.stat().st_size,
    }
    source_path.write_bytes(encode(value))
    result = parse(cap.read_bytes())
    result["artifacts"] = thermal.inventory(cap.parent)
    cap.write_bytes(encode(result))
    with pytest.raises(LabError, match="cleanup"):
        checks.evaluate(inputs[0], inputs[1], cap, digest(cap.read_bytes()))


def test_snapshot_byte_file_limits_and_no_reopen(tmp_path, monkeypatch):
    path = tmp_path / "one"
    path.write_bytes(b"one")
    snap = checks.Snapshot()
    assert snap.read(path)[0] == b"one"
    path.write_bytes(b"two")
    assert snap.read(path, expected=digest(b"one"))[0] == b"one"
    with pytest.raises(LabError, match="SHA-256"):
        snap.read(path, expected=digest(b"two"))
    snap.size = 16 * 1024 * 1024
    with pytest.raises(LabError, match="budget"):
        snap.read(tmp_path / "unopened")
    snap.size = 0
    snap.files.update({tmp_path / str(i): (b"", {}) for i in range(101)})
    with pytest.raises(LabError, match="file limit"):
        snap.read(tmp_path / "unopened")


def test_cli_rejects_output_inside_original_capture(inputs, capsys):
    output = inputs[2].parent / "report.json"
    assert (
        cli.main(
            [
                "thermal",
                "check",
                "--spec",
                str(inputs[0]),
                "--spec-sha256",
                inputs[1],
                "--capture",
                str(inputs[2]),
                "--capture-sha256",
                inputs[3],
                "--output",
                str(output),
                "--json",
            ]
        )
        == 2
    )
    assert "outside" in parse(capsys.readouterr().out.encode())["error"]["message"]
    assert not output.exists()


def test_failed_source_keeps_unknown_cleanup_and_never_scores_partial_frames(inputs):
    cap = inputs[2]
    native = cap.parent / "native/native.json"
    value = parse(native.read_bytes())
    value.update(ok=False, cleanup_complete=False, error="synthetic cleanup unknown")
    native.write_bytes(encode(value))
    result = parse(cap.read_bytes())
    result.update(
        ok=False, status="failed", error={"code": "NATIVE_INCOMPLETE", "message": "cleanup unknown"}
    )
    result["artifacts"] = thermal.inventory(cap.parent)
    cap.write_bytes(encode(result))
    report = checks.evaluate(inputs[0], inputs[1], cap, digest(cap.read_bytes()))
    assert not report["ok"]
    assert report["source"]["source_cleanup"]["native"]["cleanup_complete"] is False
    assert report["source"]["source_error"]["code"] == "NATIVE_INCOMPLETE"
    assert report["checks"][0]["status"] == "invalid_evidence"
    assert report["checks"][0]["frames"] == []
