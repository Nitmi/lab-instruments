from __future__ import annotations

import json
import socket
import struct

import pytest
from sds_double import SdsDouble, block, preamble

from lab_instruments import sds, sds_plan
from lab_instruments.common import LabError, digest, encode, write_new
from lab_instruments.sds_wave import TIMEBASE, decode
from lab_instruments.sds_wire import Tcp, split_block


def plan(server, *, io_timeout_ms=150, **kwargs):
    return sds_plan.create(
        "127.0.0.1",
        server.serial,
        "1.1.6.5",
        port=server.port,
        points=16,
        test_double=True,
        io_timeout_ms=io_timeout_ms,
        **kwargs,
    )


def perform(directory, value):
    write_new(directory / "plan.json", encode(value))
    return sds.perform(value, directory)


@pytest.mark.parametrize(
    "change",
    [
        {"host": "localhost"},
        {"host": "224.0.0.1"},
        {"host": "0.0.0.0"},
        {"host": "127.0.0.2"},
        {"host": "192.168.1.255"},
        {"host": 3232235777},
        {"serial": ""},
        {"firmware": None},
        {"channels": [True]},
        {"channels": [5]},
        {"channels": [1, 1]},
        {"channels": [2, 1]},
        {"channels": []},
        {"points": True},
        {"points": 0},
        {"points": 16385},
        {"port": False},
        {"io_timeout_ms": 99},
        {"timeout_ms": 60001},
        {"action": "raw"},
        {"action": "identify"},
        {"test_double": "yes"},
    ],
)
def test_plan_invalid(change):
    kwargs = {
        "host": "127.0.0.1",
        "serial": "SYNTHETICSDS123",
        "firmware": "1.1.6.5",
        "test_double": True,
    }
    kwargs.update(change)
    with pytest.raises((LabError, ValueError, TypeError)):
        sds_plan.create(**kwargs)


def test_offline_plan_and_inspect_do_not_network(tmp_path, monkeypatch):
    monkeypatch.setattr(socket, "socket", lambda *a, **k: pytest.fail("unexpected network"))
    value = sds_plan.create("192.168.0.50", "SDS8SYNTHETIC01", "1.1.6.5")
    path = tmp_path / "plan.json"
    write_new(path, encode(value))
    assert sds.load_plan(path, digest(path.read_bytes()))[0] == value
    assert not value["authorization_granted"] and not value["hardware_identity_verified"]
    with pytest.raises(LabError, match="SHA-256 differs"):
        sds.load_plan(path, "0" * 64)


@pytest.mark.parametrize(
    "raw",
    [b"#0abc\n\n", b"#a12", b"#231a" + b"x" * 31, b"#12ab\n", b"#12ab\n\nextra", b"#1999999999"],
)
def test_invalid_blocks(raw):
    with pytest.raises((LabError, ValueError)):
        split_block(raw, 346)


def test_scale_signed_left_aligned_probe_and_time():
    meta, summary, csv = decode(preamble(), struct.pack("<16h", *([-6400, 6400] * 8)), 1, 16)
    assert meta["probe_factor"] == 10
    assert summary["min_v"] == -2.5 and summary["max_v"] == 7.5
    assert summary["mean_v"] == 2.5
    assert summary["first_time_s"] == pytest.approx(-0.003)
    assert len(csv.splitlines()) == 17
    assert TIMEBASE[9] == pytest.approx(200e-9)
    assert len(TIMEBASE) == 39


@pytest.mark.parametrize(
    "offset,fmt,value",
    [
        (32, "h", 0),
        (34, "h", 1),
        (36, "i", 347),
        (60, "i", 1),
        (116, "i", 15),
        (132, "i", 1),
        (136, "i", 2),
        (156, "f", 0),
        (160, "f", float("nan")),
        (164, "f", 0),
        (172, "h", 8),
        (176, "f", 0),
        (180, "d", float("inf")),
        (324, "h", 39),
        (326, "h", 9),
        (328, "f", -1),
        (344, "h", 4),
    ],
)
def test_bad_descriptor(offset, fmt, value):
    raw = bytearray(preamble())
    struct.pack_into("<" + fmt, raw, offset, value)
    with pytest.raises(LabError):
        decode(raw, bytes(32), 1, 16)


@pytest.mark.parametrize("action", ["identify", "stopped", "single"])
def test_success_and_offline_replay(tmp_path, monkeypatch, action):
    with SdsDouble("fragmented") as server:
        if action == "identify":
            value = sds_plan.create("127.0.0.1", action=action, test_double=True, port=server.port)
        else:
            value = plan(server, action=action, channels=[1, 2, 3, 4])
        result = perform(tmp_path, value)
        assert result["ok"], result
        assert server.connections == 1
        assert server.commands.count(":TRIG:MODE SING") == (action == "single")
        assert ":TRIG:STOP" not in server.commands
    monkeypatch.setattr(socket, "socket", lambda *a, **k: pytest.fail("network in inspect"))
    path = tmp_path / "scope-result.json"
    inspected = sds.inspect(path, digest(path.read_bytes()))
    assert inspected["source_ok"] and not inspected["hardware_access"]
    if action == "identify":
        assert server.commands == ["*IDN?"]
    else:
        assert len(result["waveforms"]) == 4
        assert not result["cross_channel_alignment_verified"]


@pytest.mark.parametrize(
    "fault,code",
    [
        ("identity", "IDENTITY_MISMATCH"),
        ("running", "PRECONDITION_FAILED"),
        ("disabled", "PRECONDITION_FAILED"),
        ("sequence", "PRECONDITION_FAILED"),
        ("type", "PRECONDITION_FAILED"),
        ("readback", "READBACK_MISMATCH"),
        ("metadata", "CAPTURE_CHANGED"),
        ("count", "CAPTURE_CHANGED"),
        ("oversize", "LIMIT_EXCEEDED"),
        ("truncated", "TIMEOUT"),
        ("indefinite", "PROTOCOL_ERROR"),
        ("terminator", "PROTOCOL_ERROR"),
        ("stale", "FRESHNESS_UNCONFIRMED"),
        ("mode", "READBACK_MISMATCH"),
        ("unit", "PRECONDITION_FAILED"),
        ("scale", "CAPTURE_CHANGED"),
    ],
)
def test_session_faults_preserve_evidence(tmp_path, fault, code):
    seen = 0

    def inject(server, command, response):
        nonlocal seen
        if fault == "identity" and command == "*IDN?":
            return response.replace(server.serial, "DIFFERENT123")
        if fault == "running" and command == ":TRIG:STAT?":
            return "Auto"
        if fault == "disabled" and command.endswith(":SWIT?"):
            return "OFF"
        if fault == "unit" and command.endswith(":UNIT?"):
            return "A"
        if fault == "scale" and command.endswith(":SCAL?"):
            return "0.5"
        if fault == "sequence" and command == ":ACQ:SEQ?":
            return "ON"
        if fault == "type" and command == ":ACQ:TYPE?":
            return "PEAK"
        if fault == "readback" and command == ":WAV:BYT?":
            return "MSB"
        if command == ":WAV:PRE?":
            seen += 1
            if fault == "metadata" and seen == 2:
                raw = bytearray(preamble())
                struct.pack_into("<f", raw, 328, 1)
                return block(raw)
        if fault == "count" and command == ":ACQ:NUMAC?" and ":WAV:DATA?" in server.commands:
            return str(server.count + 1)
        if command == ":WAV:DATA?":
            return {
                "oversize": b"#9999999999",
                "truncated": b"#232\x00",
                "indefinite": b"#0",
                "terminator": block(bytes(32))[:-2] + b"\r\n",
            }.get(fault, response)
        if fault == "stale" and server.single and command == ":TRIG:STAT?":
            return "Stop"
        if fault == "mode" and command == ":TRIG:MODE?":
            return "AUTO"
        return response

    with SdsDouble(inject) as server:
        result = perform(
            tmp_path, plan(server, action="single" if fault in ("stale", "mode") else "stopped")
        )
        assert not result["ok"] and result["error"]["code"] == code, result
        assert server.connections == 1
        assert server.commands.count(":TRIG:MODE SING") <= 1
        assert server.commands.count(":TRIG:STOP") <= 1
        if fault in ("identity", "running", "disabled", "sequence", "type"):
            assert all(" " not in command for command in server.commands)
    path = tmp_path / "scope-result.json"
    assert not sds.inspect(path, digest(path.read_bytes()))["source_ok"]


def test_desync_after_single_does_not_send_cleanup(tmp_path):
    def stall(server, command, response):
        return None if command == ":TRIG:MODE?" else response

    with SdsDouble(stall) as server:
        result = perform(tmp_path, plan(server, action="single"))
        assert not result["ok"] and not result["cleanup"]["complete"]
        assert not result["cleanup"]["stopped_confirmed"]
        assert ":TRIG:STOP" not in server.commands
        assert server.commands[-1] == ":TRIG:MODE?"


@pytest.fixture(scope="module")
def saved_scope_files(tmp_path_factory):
    directory = tmp_path_factory.mktemp("saved-scope-baseline")
    with SdsDouble() as server:
        # These cases test saved bytes, not a 150 ms transport deadline. Acquire
        # one fully checked baseline using the adapter's normal 2 s I/O budget;
        # each mutation below receives its own immutable-input copy. Dedicated
        # transport/fault cases retain their shorter budgets and live peer.
        result = perform(directory, plan(server, io_timeout_ms=2000))
        assert result["ok"], json.dumps(result, ensure_ascii=True)
    return {path.name: path.read_bytes() for path in directory.iterdir() if path.is_file()}


@pytest.mark.parametrize("tamper", ["csv", "words", "summary", "wire", "extra-command"])
def test_saved_tamper_rejected(tmp_path, tamper, saved_scope_files):
    for name, data in saved_scope_files.items():
        (tmp_path / name).write_bytes(data)
    path = tmp_path / "scope-result.json"
    result = json.loads(path.read_bytes())
    if tamper == "csv":
        (tmp_path / "c1-samples.csv").write_bytes(b"fabricated\n")
    elif tamper == "words":
        (tmp_path / "c1-words.bin").write_bytes(bytes(32))
    elif tamper == "summary":
        result["waveforms"][0]["summary"]["mean_v"] = 999
        path.write_bytes(encode(result))
    elif tamper == "wire":
        (tmp_path / "wire-0000-rx.bin").write_bytes(b"fake\n")
    else:
        rows = (tmp_path / "transcript.jsonl").read_bytes()
        record = json.loads(rows.splitlines()[0])
        record["command"] = "*RST"
        records = encode(record) + b"\n".join(rows.splitlines()[1:]) + b"\n"
        (tmp_path / "transcript.jsonl").write_bytes(records)
        result["transcript"] = {"sha256": digest(records), "size_bytes": len(records)}
        path.write_bytes(encode(result))
    with pytest.raises(LabError):
        sds.inspect(path, digest(path.read_bytes()))


def test_transport_rejects_arbitrary_commands(tmp_path):
    c = Tcp({}, 100, 0, tmp_path)
    for cmd in ("*RST", ":TRIG:RUN", ":TRIG:MODE FTRIG", ":CHAN1:SCAL 5", "*IDN?;*RST"):
        with pytest.raises(LabError):
            c.send(cmd)


def test_maximum_capture_csv_fits_evidence_limit(tmp_path):
    with SdsDouble() as server:
        value = plan(server)
        value["points"] = 16384
        result = perform(tmp_path, value)
        assert result["ok"], result
    path = tmp_path / "scope-result.json"
    assert sds.inspect(path, digest(path.read_bytes()))["source_ok"]


def test_watchdog_kills_once_preserves_unknown_state(tmp_path, monkeypatch):
    calls = []

    class Stuck:
        def __init__(self, args, **kwargs):
            calls.append("spawn")

        def wait(self, timeout):
            if len(calls) == 1:
                raise sds.subprocess.TimeoutExpired("scope worker", timeout)
            calls.append("wait")
            return -1

        def kill(self):
            calls.append("kill")

    monkeypatch.setattr(sds.subprocess, "Popen", Stuck)
    monkeypatch.setattr(socket, "socket", lambda *a, **k: pytest.fail("parent made network call"))
    value = sds_plan.create("192.168.0.50", "SYNTHETICSDS123", "1.1.6.5")
    path = tmp_path / "request.json"
    write_new(path, encode(value))
    with pytest.raises(LabError, match="state unknown"):
        sds.run(path, digest(path.read_bytes()), tmp_path / "run")
    assert calls == ["spawn", "kill", "wait"]
    assert (tmp_path / "run" / "plan.json").exists()
    assert not (tmp_path / "run" / "scope-result.json").exists()
