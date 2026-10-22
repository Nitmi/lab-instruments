from __future__ import annotations

import copy
import json
import socket
import subprocess

import pytest
from sdl_double import SdlDouble, client_transmissions, stalled_loaded_measurement

from lab_instruments.cli import main
from lab_instruments.common import LabError, digest, encode, read, write_new
from lab_instruments.lease import Lease
from lab_instruments.sdl import inspect, perform, run
from lab_instruments.sdl_plan import create, validate


def plan_for(server, **changes):
    return create(
        "127.0.0.1",
        "SYNTHETIC123",
        "1.1.1.23R4",
        port=server.port,
        action="cc",
        current_a=0.1,
        samples=2,
        interval_ms=100,
        io_timeout_ms=100,
        test_double=True,
        **changes,
    )


@pytest.mark.parametrize("fault", [None, "fragmented"])
def test_actual_tcp_cc_fixed_subset_off_readback_and_saved_inspection(tmp_path, fault):
    with SdlDouble(fault) as server:
        result = perform(plan_for(server), tmp_path)
        assert result["ok"], result
        assert len(result["measurements"]) == 2
        assert result["input_enable_attempted"] and not server.on
        assert result["cleanup"]["input_off_confirmed"]
        assert result["evidence_origin"] == "test_double" and not result["hardware_access"]
        assert server.commands.count(":SOUR:INP:STAT ON") == 1
        assert server.commands.count(":SOUR:INP:STAT OFF") == 1
        assert server.connections == 1
        assert all(not c.startswith(("*RST", "*CLS", ":SOUR:SHOR ")) for c in server.commands)
    saved = tmp_path / "load-result.json"
    value = inspect(saved, digest(saved.read_bytes()))
    assert value["source_ok"] and not value["physical_device_verified"]


@pytest.mark.parametrize("field,value", [("serial", "DIFFERENT123"), ("firmware_version", "1.0")])
def test_identity_mismatch_never_mutates_even_for_cleanup(tmp_path, field, value):
    with SdlDouble() as server:
        plan = plan_for(server)
        plan[field] = value
        result = perform(plan, tmp_path)
        assert not result["ok"] and result["error"]["code"] == "IDENTITY_MISMATCH"
        assert server.commands == ["*IDN?"]
        assert not result["cleanup"]["off_attempted"]


@pytest.mark.parametrize(
    "command,bad",
    [
        (":SOUR:SHOR:STAT?", "1"),
        ("EXT:MODE?", "EXTI"),
        (":SOUR:EXT:INPUT:STAT?", "1"),
        ("SYST:SENS:STAT?", "1"),
        (":SOUR:INP:STAT?", "1"),
    ],
)
def test_unsafe_preconditions_reject_without_configuration(tmp_path, command, bad):
    def change(server, cmd, response):
        return bad if cmd == command else response

    with SdlDouble(change) as server:
        result = perform(plan_for(server), tmp_path)
        assert not result["ok"] and result["error"]["code"] == "PRECONDITION_FAILED"
        assert not result["configuration_attempted"] and not result["cleanup"]["off_attempted"]
        assert all(cmd.endswith("?") for cmd in server.commands)


def test_setpoint_readback_mismatch_does_not_enable_input(tmp_path):
    def change(server, command, response):
        return "0.200" if command == ":SOUR:CURR:LEV:IMM?" else response

    with SdlDouble(change) as server:
        result = perform(plan_for(server), tmp_path)
        assert result["error"]["code"] == "READBACK_MISMATCH"
        assert ":SOUR:INP:STAT ON" not in server.commands
        assert server.commands.count(":SOUR:INP:STAT OFF") == 1
        assert result["cleanup"]["input_off_confirmed"]


@pytest.mark.parametrize(
    "command,bad,error",
    [
        ("MEAS:VOLT:DC?", "30", "MEASUREMENT_LIMIT"),
        ("MEAS:CURR:DC?", "1.01", "MEASUREMENT_LIMIT"),
        ("MEAS:POW:DC?", "21", "MEASUREMENT_LIMIT"),
        ("MEAS:CURR:DC?", "0.05", "CURRENT_MISMATCH"),
        ("MEAS:CURR:DC?", "NaN", "PROTOCOL_ERROR"),
        ("MEAS:CURR:DC?", "9.9E37", "PROTOCOL_ERROR"),
    ],
)
def test_loaded_bad_measurement_aborts_and_turns_off(tmp_path, command, bad, error):
    def change(server, cmd, response):
        return bad if server.on and cmd == command else response

    with SdlDouble(change) as server:
        result = perform(plan_for(server), tmp_path)
        assert result["error"]["code"] == error, result
        assert result["cleanup"]["input_off_confirmed"] and not server.on
        assert server.commands.count(":SOUR:INP:STAT ON") == 1
        assert server.commands.count(":SOUR:INP:STAT OFF") == 1


def test_pending_response_timeout_sends_off_once_but_cannot_claim_confirmation(tmp_path):
    with SdlDouble(stalled_loaded_measurement) as server:
        result = perform(plan_for(server), tmp_path)
        assert not result["ok"]
        assert result["cleanup"]["off_transmitted"]
        assert not result["cleanup"]["input_off_confirmed"]
        assert result["cleanup"]["instrument_state"] == "unknown"
        assert not result["cleanup"]["complete"]
        transmitted = client_transmissions(tmp_path)
        assert transmitted.count(b":SOUR:INP:STAT OFF\n") == 1
        assert not transmitted.split(b":SOUR:INP:STAT OFF\n", 1)[1]


def test_off_stuck_keeps_failure_and_partial_measurements(tmp_path):
    with SdlDouble("off-stuck") as server:
        result = perform(plan_for(server), tmp_path)
        assert not result["ok"] and result["error"]["code"] == "CLEANUP_INCOMPLETE"
        assert len(result["measurements"]) == 2 and server.on
    saved = tmp_path / "load-result.json"
    assert inspect(saved, digest(saved.read_bytes()))["source_ok"] is False


def test_observe_sends_only_queries_and_does_not_turn_off_existing_load(tmp_path):
    with SdlDouble() as server:
        server.on = True
        plan = plan_for(server)
        plan["action"] = "observe"
        plan["current_a"] = None
        plan["effects"].update(static_cc_configuration=False, input_enable=False, input_off=False)
        result = perform(plan, tmp_path)
        assert result["ok"] and server.on
        assert all(command.endswith("?") for command in server.commands)
        assert not result["cleanup"]["off_attempted"]


def test_explicit_off_can_stop_an_already_active_owned_instrument(tmp_path):
    with SdlDouble() as server:
        server.on = True
        plan = create(
            "127.0.0.1",
            "SYNTHETIC123",
            "1.1.1.23R4",
            action="off",
            port=server.port,
            test_double=True,
        )
        result = perform(plan, tmp_path)
        assert result["ok"] and not server.on
        assert server.commands == ["*IDN?", ":SOUR:INP:STAT OFF", "*OPC?", ":SOUR:INP:STAT?"]


def test_identify_has_one_query_no_enable(tmp_path):
    with SdlDouble() as server:
        result = perform(
            create("127.0.0.1", action="identify", port=server.port, test_double=True), tmp_path
        )
        assert result["ok"] and not result["instrument_identity_matched"]
        assert server.commands == ["*IDN?"]


@pytest.mark.parametrize(
    "change",
    [
        {"host": "localhost"},
        {"host": "0.0.0.0"},
        {"host": "224.0.0.1"},
        {"serial": "SYNTHETIC123\n*RST"},
        {"firmware": "1,2"},
        {"test_double": False},
        {"current_a": float("nan")},
        {"current_a": 6},
        {"current_a": 0.0001},
        {"current_a": 0.1001},
        {"samples": True},
        {"samples": 51},
        {"port": True},
        {"max_voltage_v": 25},
        {"max_power_w": 51},
        {"max_current_a": 6},
        {"timeout_ms": 1},
        {"io_timeout_ms": 99},
    ],
)
def test_invalid_plans_are_host_only(change):
    values = dict(
        host="127.0.0.1",
        serial="SYNTHETIC123",
        firmware="1.1.1.23R4",
        action="cc",
        current_a=0.1,
        test_double=True,
    )
    values.update(change)
    with pytest.raises((LabError, ValueError)):
        create(**values)


def test_claims_and_unknown_fields_rejected():
    plan = create("192.0.2.10", "SYNTHETIC123", "1.1.1.23R4")
    for key, value in (
        ("authorization_granted", True),
        ("hardware_identity_verified", True),
        ("retries", 1),
        ("arbitrary_command", "*RST"),
        ("effects", {}),
    ):
        changed = copy.deepcopy(plan)
        changed[key] = value
        with pytest.raises(LabError):
            validate(changed)


def test_static_cli_does_not_touch_network_or_start_process(tmp_path, monkeypatch, capsys):
    def forbidden(*args, **kwargs):
        raise AssertionError("offline operation reached network/process")

    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    path = tmp_path / "plan.json"
    assert (
        main(
            [
                "load",
                "plan",
                "--host",
                "192.0.2.10",
                "--serial",
                "SYNTHETIC123",
                "--firmware-version",
                "1.1.1.23R4",
                "--output",
                str(path),
                "--json",
            ]
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out)
    assert not result["hardware_access"] and not result["executables_started"]
    assert (
        main(["load", "inspect-plan", str(path), "--sha256", digest(path.read_bytes()), "--json"])
        == 0
    )
    capsys.readouterr()
    assert (
        main(
            [
                "load",
                "run",
                "--plan",
                str(path),
                "--sha256",
                "0" * 64,
                "--output",
                str(tmp_path / "reject"),
                "--json",
            ]
        )
        == 2
    )
    assert not (tmp_path / "reject").exists()


def test_busy_local_lease_prevents_any_tcp_connection(tmp_path):
    with SdlDouble() as server:
        lease = Lease("SYNTHETIC123")
        try:
            result = perform(plan_for(server), tmp_path)
        finally:
            lease.close()
        assert result["error"]["code"] == "INSTRUMENT_BUSY"
        assert server.connections == 0


def test_tampered_transcript_rejected(tmp_path):
    with SdlDouble() as server:
        result = perform(plan_for(server), tmp_path)
    assert result["ok"]
    saved = tmp_path / "load-result.json"
    (tmp_path / "transcript.jsonl").write_bytes(b"{}\n")
    with pytest.raises(LabError, match="SHA-256"):
        inspect(saved, digest(saved.read_bytes()))


def test_existing_output_is_rejected_before_connect(tmp_path, monkeypatch):
    path = tmp_path / "plan.json"
    write_new(path, encode(create("192.0.2.10", "SYNTHETIC123", "1.1.1.23R4")))
    target = tmp_path / "already"
    target.mkdir()

    def forbidden(*args, **kwargs):
        raise AssertionError("opened network")

    monkeypatch.setattr(socket, "socket", forbidden)
    with pytest.raises(LabError, match="exists"):
        run(path, read(path)[1]["sha256"], target)
