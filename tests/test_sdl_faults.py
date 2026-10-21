from __future__ import annotations

import copy
import json

import pytest
from sdl_double import SdlDouble
from test_sdl import plan_for

from lab_instruments.common import LabError, digest, encode, parse
from lab_instruments.scpi import Tcp, boolean, numeric
from lab_instruments.sdl import inspect, perform


@pytest.mark.parametrize("bad", ["NaN", "Inf", "1 V", "", "1,2", "9.9E37", "1e999", " 1"])
def test_scpi_numeric_parser_rejects_nonfinite_units_and_ambiguous_responses(bad):
    with pytest.raises(LabError):
        numeric(bad)


@pytest.mark.parametrize("bad", ["2", "true", "ON;*RST", "on", "0.0"])
def test_scpi_boolean_is_exact(bad):
    with pytest.raises(LabError):
        boolean(bad)


@pytest.mark.parametrize("payload", [b"5.0", b"5.0\n1\n", b"X" * 600 + b"\n", b"\xff\n"])
def test_malformed_or_partial_loaded_response_cannot_be_used_for_off_confirmation(
    tmp_path, payload
):
    def fault(server, cmd, response):
        return payload if server.on and cmd == "MEAS:VOLT:DC?" else response

    with SdlDouble(fault) as server:
        result = perform(plan_for(server), tmp_path)
        assert not result["ok"] and result["cleanup"]["off_attempted"]
        assert not result["cleanup"]["input_off_confirmed"]
        assert result["cleanup"]["instrument_state"] == "unknown"
        assert result["cleanup"]["socket_closed"]
        assert server.commands.count(":SOUR:INP:STAT OFF") == 1


def test_cancellation_after_on_attempts_off_and_keeps_failure(tmp_path, monkeypatch):
    from lab_instruments import sdl

    real_sleep = sdl.time.sleep
    interrupted = False

    def once(seconds):
        nonlocal interrupted
        if not interrupted:
            interrupted = True
            raise KeyboardInterrupt
        real_sleep(seconds)

    monkeypatch.setattr(sdl.time, "sleep", once)
    with SdlDouble() as server:
        result = perform(plan_for(server), tmp_path)
        assert result["error"]["code"] == "CANCELLED"
        assert result["cleanup"]["input_off_confirmed"] and not server.on
        assert server.commands.count(":SOUR:INP:STAT OFF") == 1


def test_partial_on_send_failure_is_not_retried_and_off_is_still_attempted(tmp_path):
    class PartialOn(Tcp):
        def send(self, command, **kwargs):
            if command == ":SOUR:INP:STAT ON":
                self.socket.sendall(b":SOUR:INP:STAT O")
                self.synchronized = False
                raise TimeoutError("synthetic partial write")
            return super().send(command, **kwargs)

    with SdlDouble() as server:
        result = perform(plan_for(server), tmp_path, connector=PartialOn)
        assert result["error"]["code"] == "TIMEOUT"
        assert result["input_enable_attempted"]
        assert result["cleanup"]["off_transmitted"]
        assert not result["cleanup"]["input_off_confirmed"]
        assert server.commands.count(":SOUR:INP:STAT OFF") == 1
        assert bytes(server.raw_received).count(b":SOUR:INP:STAT O\n") == 1


def test_raw_command_subset_rejects_arbitrary_reset_before_transmitting(tmp_path):
    with SdlDouble() as server:
        plan = plan_for(server)
        connection = Tcp(plan["endpoint"], 100, 1e20, lambda event: None)
        try:
            connection.connect()
            with pytest.raises(LabError, match="unsupported"):
                connection.send("*RST")
            with pytest.raises(LabError, match="unsupported"):
                connection.query("SYST:ERR?")
            with pytest.raises(LabError, match="only OFF"):
                connection.send(":SOUR:INP:STAT ON", cleanup=True)
        finally:
            connection.close()
        assert not server.commands


@pytest.mark.parametrize(
    "change",
    [
        lambda r: r["measurements"][0].update(voltage_v=5.1),
        lambda r: r["identity"].update(serial="DIFFERENT123"),
        lambda r: r.update(physical_device_verified=True),
        lambda r: r["cleanup"].update(input_off_confirmed=False),
        lambda r: r["measurements"].clear(),
    ],
)
def test_rehashed_derived_report_cannot_hide_raw_evidence_disagreement(tmp_path, change):
    with SdlDouble() as server:
        result = perform(plan_for(server), tmp_path)
    assert result["ok"]
    changed = copy.deepcopy(result)
    change(changed)
    path = tmp_path / "changed-result.json"
    path.write_bytes(encode(changed))
    with pytest.raises(LabError):
        inspect(path, digest(path.read_bytes()))


def test_raw_response_must_agree_with_derived_transcript_response(tmp_path):
    with SdlDouble() as server:
        result = perform(plan_for(server), tmp_path)
    path = tmp_path / "transcript.jsonl"
    records = [parse(line) for line in path.read_bytes().splitlines()]
    event = next(
        e for e in records if e["kind"] == "response.complete" and e["command"] == "MEAS:VOLT:DC?"
    )
    event["response"] = "5.1"
    path.write_bytes(b"".join(encode(e) for e in records))
    result["transcript"] = {"sha256": digest(path.read_bytes()), "size_bytes": path.stat().st_size}
    saved = tmp_path / "changed-result.json"
    saved.write_bytes(encode(result))
    with pytest.raises(LabError, match="response bytes differ"):
        inspect(saved, digest(saved.read_bytes()))


def test_journal_error_does_not_prevent_best_effort_off(tmp_path, monkeypatch):
    import lab_instruments.sdl as sdl

    original = sdl.encode
    failed = False

    def fail_once(value):
        nonlocal failed
        if (
            isinstance(value, dict)
            and value.get("kind") == "command.begin"
            and value.get("command") == "MEAS:VOLT:DC?"
            and server.on
            and not failed
        ):
            failed = True
            raise OSError("synthetic evidence disk error")
        return original(value)

    monkeypatch.setattr(sdl, "encode", fail_once)
    with SdlDouble() as server:
        result = perform(plan_for(server), tmp_path)
        assert not result["ok"] and result["cleanup"]["input_off_confirmed"]
        assert not server.on and not result["cleanup"]["complete"]
        assert any("journal failed" in e for e in result["cleanup"]["errors"])


def test_identify_model_mismatch_has_no_control_side_effect(tmp_path):
    def fault(server, cmd, response):
        return (
            "Siglent Technologies,SDL1030X,SYNTHETIC123,1.1.1.23R4" if cmd == "*IDN?" else response
        )

    with SdlDouble(fault) as server:
        result = perform(plan_for(server), tmp_path)
        assert not result["ok"] and server.commands == ["*IDN?"]


def test_result_and_transcript_are_valid_json_after_loaded_failure(tmp_path):
    def fault(server, cmd, response):
        return "0.0" if server.on and cmd == "MEAS:CURR:DC?" else response

    with SdlDouble(fault) as server:
        perform(plan_for(server), tmp_path)
    result = json.loads((tmp_path / "load-result.json").read_bytes())
    assert result["status"] == "failed" and result["cleanup"]["input_off_confirmed"]
    for line in (tmp_path / "transcript.jsonl").read_bytes().splitlines():
        assert isinstance(json.loads(line), dict)
