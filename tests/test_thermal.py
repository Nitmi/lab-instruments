from __future__ import annotations

import copy
import socket
import struct

import pytest
from thermal_double import SERIAL, SOFTWARE, control_bytes, frame, source

from lab_instruments import thermal, thermal_plan
from lab_instruments.common import LabError, digest, encode, write_new
from lab_instruments.thermal_frame import analyze, state, telemetry, trends


def plan(**kwargs):
    return thermal_plan.create("SYNTHETIC-PT3", SERIAL, SOFTWARE, test_double=True, **kwargs)


@pytest.mark.parametrize(
    "change",
    [
        {"link": "first camera"},
        {"link": "SYNTHETIC-PT3\n"},
        {"serial": "bad"},
        {"software": "0102"},
        {"buffers": 0},
        {"buffers": True},
        {"buffers": 9},
        {"timeout_ms": 4999},
        {"distance_m": -1},
        {"ambient_c": float("nan")},
        {"rois": []},
        {"rois": [{"name": "cpu", "x": True, "y": 0, "width": 1, "height": 1}]},
        {"rois": [{"name": "cpu", "x": 159, "y": 119, "width": 2, "height": 1}]},
    ],
)
def test_plan_rejects_invalid(change):
    values = {"link": "SYNTHETIC-PT3", "serial": SERIAL, "software": SOFTWARE, "test_double": True}
    values.update(change)
    with pytest.raises(LabError):
        thermal_plan.create(**values)


def test_physical_link_exact_and_effects():
    value = thermal_plan.create(r"\\?\usb#vid_1e4e&pid_0100&mi_00#EXACT#{GUID}", SERIAL, SOFTWARE)
    assert value["effects"]["enumerate_camera_interfaces"]
    assert not value["effects"]["explicit_uvc_set_or_ffc"]
    assert not value["authorization_granted"]
    value["effects"]["explicit_uvc_set_or_ffc"] = True
    with pytest.raises(LabError):
        thermal_plan.validate(value)


@pytest.mark.parametrize("resolution,code", [(1, 30000), (0, 3000)])
def test_temperature_scale_and_roi(resolution, code):
    controls = control_bytes()
    controls["resolution"] = struct.pack("<I", resolution)
    raw = bytearray(frame(code=code, resolution=resolution))
    struct.pack_into("<H", raw, 2 * (10 * 160 + 20), code + 100)
    roi = [{"name": "cpu", "x": 20, "y": 10, "width": 4, "height": 4}]
    value, csv = analyze(raw, controls, roi)
    summary = value["rois"][0]
    assert summary["min_c"] == pytest.approx(26.85)
    assert summary["max_c"] == pytest.approx(27.85 if resolution else 36.85)
    assert summary["hotspot"] == {"x": 20, "y": 10}
    assert summary["valid_pixels"] == 16
    assert len(csv.splitlines()) == 19201
    assert value["controls"]["radiometric_parameters"]["scene_emissivity"] == 1


@pytest.mark.parametrize(
    "name,value",
    [
        ("agc", 1),
        ("tlinear", 0),
        ("radiometry", 0),
        ("resolution", 2),
        ("auto_resolution", 1),
        ("telemetry", 0),
        ("location", 0),
        ("ffc_status", 1),
        ("gain", 9),
    ],
)
def test_controls_gate_temperature(name, value):
    controls = control_bytes()
    controls[name] = struct.pack("<I", value)
    with pytest.raises(LabError):
        state(controls)


@pytest.mark.parametrize("status", [0, 16, 32, 56, 48 | (1 << 12), 48 | (1 << 15), 48 | (1 << 20)])
def test_frame_not_ready(status):
    with pytest.raises(LabError):
        telemetry(frame(status=status))


def test_invalid_pixels_and_empty_roi():
    raw = bytearray(frame())
    struct.pack_into("<HH", raw, 0, 0, 65535)
    value, _ = analyze(
        raw, control_bytes(), [{"name": "all", "x": 0, "y": 0, "width": 160, "height": 120}]
    )
    assert value["rois"][0]["invalid_or_out_of_range_pixels"] == 2
    with pytest.raises(LabError, match="no usable"):
        analyze(raw, control_bytes(), [{"name": "empty", "x": 0, "y": 0, "width": 2, "height": 1}])


def test_frame_layout_and_status_control_disagreement():
    for raw in (frame()[:-1], bytes(160 * 120 * 2), bytes(160 * 121 * 2)):
        with pytest.raises(LabError):
            telemetry(raw)
    with pytest.raises(LabError, match="disagree"):
        analyze(frame(resolution=0), control_bytes(), plan()["rois"])


def test_sensor_counter_trends_and_duplicates():
    frames = []
    for i in range(3):
        raw = frame(i, code=30000 + 10 * i)
        value, _ = analyze(raw, control_bytes(), plan()["rois"])
        value["raw_sha256"] = digest(raw)
        frames.append(value)
    value = trends([frames[0], frames[0], *frames[1:]])
    assert value["unique_frames"] == 3 and value["duplicate_buffers"] == 1
    assert value["roi_changes"][0]["mean_delta_c"] == pytest.approx(0.2)
    assert value["roi_changes"][0]["elapsed_sensor_s"] == pytest.approx(0.24)
    changed = copy.deepcopy(frames)
    changed[-1]["telemetry"]["last_ffc_ms"] += 1
    assert not trends(changed)["temperature_trend_supported"]
    changed = copy.deepcopy(frames)
    changed[-1]["rois"][0]["valid_mask_sha256"] = "0" * 64
    assert not trends(changed)["temperature_trend_supported"]
    with pytest.raises(LabError):
        trends([frames[1], frames[0]])
    changed = copy.deepcopy(frames[0])
    changed["raw_sha256"] = "0" * 64
    with pytest.raises(LabError):
        trends([frames[0], changed])


@pytest.mark.parametrize("action", ["capture", "identify", "devices"])
def test_import_and_reinspect_never_start_process_or_hardware(tmp_path, monkeypatch, action):
    value = (
        plan()
        if action == "capture"
        else thermal_plan.create(
            "SYNTHETIC-PT3" if action == "identify" else None, action=action, test_double=True
        )
    )
    raw_dir = tmp_path / "source"
    raw_dir.mkdir()
    source_path = source(raw_dir, value)
    plan_path = tmp_path / "plan.json"
    write_new(plan_path, encode(value))
    monkeypatch.setattr(socket, "socket", lambda *a, **k: pytest.fail("network in offline import"))
    monkeypatch.setattr(
        thermal.subprocess, "Popen", lambda *a, **k: pytest.fail("process in offline import")
    )
    result, _ = thermal.import_source(
        source_path,
        digest(source_path.read_bytes()),
        plan_path,
        digest(plan_path.read_bytes()),
        tmp_path / "evaluation",
    )
    assert result["ok"], result
    assert not result["hardware_access"] and not result["executables_started"]
    path = tmp_path / "evaluation/thermal-result.json"
    assert thermal.inspect(path, digest(path.read_bytes()))["source_ok"]
    if action == "capture":
        assert result["analysis"]["trend"]["unique_frames"] == 3


@pytest.mark.parametrize(
    "tamper", ["source", "csv", "analysis", "inventory", "identity", "failed-frame"]
)
def test_import_hashes_and_saved_tampering(tmp_path, tamper):
    value = plan()
    native = tmp_path / "source"
    native.mkdir()
    path = source(
        native,
        value,
        mutation=(lambda i, raw: frame(i, status=32)) if tamper == "failed-frame" else None,
    )
    plan_path = tmp_path / "plan.json"
    write_new(plan_path, encode(value))
    if tamper == "source":
        (native / "frame-0000.y16").write_bytes(bytes(39040))
        with pytest.raises(LabError, match="SHA-256 differs"):
            thermal.import_source(
                path,
                digest(path.read_bytes()),
                plan_path,
                digest(plan_path.read_bytes()),
                tmp_path / "result",
            )
        assert not (tmp_path / "result").exists()
        return
    result, _ = thermal.import_source(
        path,
        digest(path.read_bytes()),
        plan_path,
        digest(plan_path.read_bytes()),
        tmp_path / "result",
    )
    saved = tmp_path / "result/thermal-result.json"
    if tamper == "failed-frame":
        assert (
            not result["ok"] and not thermal.inspect(saved, digest(saved.read_bytes()))["source_ok"]
        )
        return
    assert result["ok"]
    if tamper == "csv":
        (tmp_path / "result/frame-0000.csv").write_bytes(b"fake")
    elif tamper == "analysis":
        result["analysis"]["frames"][0]["rois"][0]["mean_c"] = 999
    elif tamper == "inventory":
        del result["artifacts"]["frame-0000.csv"]
    else:
        (tmp_path / "result/native/identity-serial.bin").write_bytes(bytes(8))
    saved.write_bytes(encode(result))
    with pytest.raises(LabError):
        thermal.inspect(saved, digest(saved.read_bytes()))


def test_bad_helper_hash_rejected_before_process_and_output(tmp_path, monkeypatch):
    helper = tmp_path / "fake.exe"
    helper.write_bytes(b"MZsynthetic")
    path = tmp_path / "plan.json"
    write_new(path, encode(plan()))
    monkeypatch.setattr(
        thermal.subprocess, "Popen", lambda *a, **k: pytest.fail("started bad helper")
    )
    with pytest.raises(LabError, match="SHA-256 differs"):
        thermal.run(path, digest(path.read_bytes()), helper, "0" * 64, tmp_path / "run")
    assert not (tmp_path / "run").exists()


def test_native_timeout_preserves_unknown_cleanup(tmp_path, monkeypatch):
    calls = []

    class Stuck:
        def __init__(self, *args, **kwargs):
            calls.append("spawn")

        def wait(self, timeout):
            if len(calls) == 1:
                raise thermal.subprocess.TimeoutExpired("synthetic helper", timeout)
            calls.append("wait")
            return -1

        def kill(self):
            calls.append("kill")

    monkeypatch.setattr(thermal.subprocess, "Popen", Stuck)
    helper = tmp_path / "fake.exe"
    helper.write_bytes(b"MZsynthetic")
    path = tmp_path / "plan.json"
    write_new(path, encode(plan()))
    result, _ = thermal.run(
        path, digest(path.read_bytes()), helper, digest(helper.read_bytes()), tmp_path / "run"
    )
    assert calls == ["spawn", "kill", "wait"]
    assert not result["ok"] and not result["cleanup_complete"]
    assert result["error"]["code"] == "NATIVE_TIMEOUT" and result["local_lease_released"]
    saved = tmp_path / "run/thermal-result.json"
    assert not thermal.inspect(saved, digest(saved.read_bytes()))["source_ok"]
