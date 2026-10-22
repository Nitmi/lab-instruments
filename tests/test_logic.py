from __future__ import annotations

import base64
import copy
import json
import random
import socket
import subprocess

import pytest
from logic_fixtures import capture, save, uart_signal

from lab_instruments import logic
from lab_instruments.cli import main
from lab_instruments.common import LabError, digest
from lab_instruments.dwf import Dwf


def test_uart_phase_sweep_against_independent_bit_level_transmitter(tmp_path):
    randomizer = random.Random(74031)
    for i in range(80):
        data_bits = randomizer.randrange(5, 9)
        parity = randomizer.choice(("none", "even", "odd"))
        stop_bits = randomizer.choice((1, 2))
        baud = randomizer.choice((9600, 57600, 115200, 230400))
        rate = baud * randomizer.randrange(8, 25) + randomizer.randrange(baud)
        message = bytes(randomizer.randrange(1 << data_bits) for _ in range(12))
        options = {"data_bits": data_bits, "parity": parity, "stop_bits": stop_bits}
        levels = uart_signal(message, rate=rate, baud=baud, phase=randomizer.random(), **options)
        path, sha = save(tmp_path, capture(levels, rate), f"phase-{i}.json")
        result = logic.uart(path, sha, 0, baud, **options)
        assert result["ok"], (i, options, result)
        assert base64.b64decode(result["analysis"]["bytes_base64"]) == message


@pytest.mark.parametrize(
    "rate,baud,phase", [(1_000_000, 100_000, 0), (2_500_000, 115200, 0.37), (921600, 115200, 0.8)]
)
@pytest.mark.parametrize("channel,inverted", [(0, False), (15, True)])
def test_uart_back_to_back_and_noninteger_sample_phase(
    tmp_path, rate, baud, phase, channel, inverted
):
    message = b"\x00\xff\x55\xaaAgent\r\n"
    levels = uart_signal(message, rate=rate, baud=baud, phase=phase, inverted=inverted)
    path, sha = save(tmp_path, capture(levels, rate, channel=channel, requested_rate=3_000_000))
    result = logic.uart(path, sha, channel, baud, inverted=inverted)
    assert result["ok"] and result["analysis"]["status"] == "decoded"
    assert base64.b64decode(result["analysis"]["bytes_base64"]) == message
    assert result["sample_rate_hz"] == rate
    assert result["evidence_origin"] == "test_double"
    assert not result["hardware_access"] and not result["executables_started"]
    assert not result["physical_device_verified"] and not result["physical_timing_verified"]
    assert not result["source_files_rechecked"]
    for frame in result["analysis"]["frames"]:
        assert frame["valid"] and frame["errors"] == []
        assert frame["raw_span"]["start"] == 2 * (frame["start_sample"] - 1)
        assert frame["raw_span"]["end_exclusive"] <= 2 * len(levels)
    assert digest(path.read_bytes()) == sha


@pytest.mark.parametrize("data_bits", [5, 6, 7, 8])
@pytest.mark.parametrize("parity,stop_bits", [("none", 1), ("even", 2), ("odd", 1)])
def test_uart_word_lengths_parity_and_two_stops(tmp_path, data_bits, parity, stop_bits):
    message = bytes([0, (1 << data_bits) - 1, 0x15])
    args = {"data_bits": data_bits, "parity": parity, "stop_bits": stop_bits}
    path, sha = save(tmp_path, capture(uart_signal(message, **args)))
    result = logic.uart(path, sha, 0, 100_000, **args)
    assert result["ok"], result
    assert base64.b64decode(result["analysis"]["bytes_base64"]) == message


@pytest.mark.parametrize(
    "error",
    [
        "parity_error",
        "framing_error",
        "incomplete_frame",
        "false_start",
        "capture_starts_active",
        "insufficient_idle_before_start",
    ],
)
def test_uart_errors_preserve_records_without_partial_byte_stream(tmp_path, error):
    options = {}
    if error == "parity_error":
        levels = uart_signal([0x55], parity="even", corrupt_parity=True)
        options["parity"] = "even"
    elif error == "framing_error":
        levels = uart_signal([0x55], corrupt_stop=True)
    elif error == "incomplete_frame":
        levels = uart_signal([0x55])[:100]
    elif error == "false_start":
        levels = [1] * 20 + [0] + [1] * 140
    elif error == "capture_starts_active":
        levels = [0] * 50
    else:
        levels = [1] * 2 + [0] * 20 + [1] * 140
    path, sha = save(tmp_path, capture(levels))
    result = logic.uart(path, sha, 0, 100_000, **options)
    assert not result["ok"] and result["analysis"]["bytes_base64"] is None
    errors = {code for f in result["analysis"]["frames"] for code in f["errors"]}
    errors.update(issue["code"] for issue in result["analysis"]["issues"])
    assert error in errors


def test_uart_idle_is_no_data_not_success_and_undersampling_rejected(tmp_path):
    path, sha = save(tmp_path, capture([1] * 64))
    result = logic.uart(path, sha, 0, 100_000)
    assert result["analysis"]["status"] == "no_frames" and not result["ok"]
    with pytest.raises(LabError, match="eight actual samples"):
        logic.uart(path, sha, 0, 125001)


@pytest.mark.parametrize("active_level", ["high", "low"])
def test_pwm_closed_cycles_and_actual_readback_not_request(tmp_path, active_level):
    levels = [0] * 3 + [1] * 2 + [0] * 6 + [1] * 3 + [0] * 5 + [1] * 4 + [0] * 2
    if active_level == "low":
        levels = [1 - bit for bit in levels]
    path, sha = save(tmp_path, capture(levels, 8000, requested_rate=1_000_000))
    result = logic.pwm(path, sha, 0, active_level=active_level)
    analysis = result["analysis"]
    assert result["ok"] and analysis["cycle_count"] == 2
    assert [c["start_sample"] for c in analysis["cycles"]] == [3, 11]
    assert [c["period_samples"] for c in analysis["cycles"]] == [8, 8]
    assert [c["active_samples"] for c in analysis["cycles"]] == [2, 3]
    assert analysis["statistics"]["frequency_hz"] == {
        "minimum": 1000,
        "maximum": 1000,
        "mean": 1000,
    }
    assert analysis["statistics"]["duty_cycle"]["mean"] == 0.3125
    assert analysis["prefix_samples_excluded"] == 3
    assert analysis["suffix_samples_excluded"] == 6
    assert analysis["cycles"][0]["raw_span"] == {"start": 4, "end_exclusive": 24}
    assert not result["physical_timing_verified"] and not result["calibration_verified"]


@pytest.mark.parametrize("levels", [[0] * 32, [1] * 32, [0] * 4 + [1] * 8 + [0] * 20])
def test_pwm_constant_or_one_pulse_has_no_frequency_or_duty_estimate(tmp_path, levels):
    path, sha = save(tmp_path, capture(levels))
    result = logic.pwm(path, sha, 0)
    assert not result["ok"] and result["status"] == "insufficient_cycles"
    assert result["analysis"]["statistics"] is None and result["analysis"]["cycles"] == []


def test_pwm_jitter_uses_per_cycle_period_and_mean_of_frequencies(tmp_path):
    levels = [0] * 2 + [1] * 3 + [0] * 5 + [1] * 4 + [0] * 6 + [1] * 3 + [0] * 4
    path, sha = save(tmp_path, capture(levels, 8000))
    result = logic.pwm(path, sha, 0)
    assert [c["period_samples"] for c in result["analysis"]["cycles"]] == [8, 10]
    assert result["analysis"]["statistics"]["frequency_hz"]["mean"] == 900


@pytest.mark.parametrize("kind", ["uart", "pwm"])
def test_failed_source_remains_failed_while_payload_can_be_diagnosed(tmp_path, kind):
    levels = uart_signal([0x41]) if kind == "uart" else [0, 0, 1, 1, 0, 0] * 5
    path, sha = save(tmp_path, capture(levels, ok=False))
    result = logic.uart(path, sha, 0, 100_000) if kind == "uart" else logic.pwm(path, sha, 0)
    assert not result["ok"] and result["status"] == "source_failed"
    assert (
        result["source_status"] == "failed"
        and result["source_error"]["code"] == "CLEANUP_INCOMPLETE"
    )
    assert result["source_cleanup"]["complete"] is False
    assert result["analysis"]["status"] in ("decoded", "measured")


@pytest.mark.parametrize(
    "mutation",
    [
        "missing-rate",
        "nan-rate",
        "bool-rate",
        "over-rate",
        "huge-rate",
        "missing-settings",
        "count",
        "payload",
        "encoding",
        "time",
        "source-claim",
    ],
)
def test_invalid_saved_data_or_rate_cannot_produce_measurements(tmp_path, mutation):
    value = capture(uart_signal([0x41]))
    if mutation == "missing-rate":
        value["settings"].pop("sample_rate_hz")
    elif mutation in ("nan-rate", "bool-rate", "over-rate", "huge-rate"):
        value["settings"]["sample_rate_hz"] = {
            "nan-rate": "NaN",
            "bool-rate": True,
            "over-rate": 2_000_000,
            "huge-rate": 10**400,
        }[mutation]
    elif mutation == "missing-settings":
        value.pop("settings")
    elif mutation == "count":
        value["settings"]["samples"] += 1
    elif mutation == "payload":
        value["payload"]["raw_base64"] = "AAAA"
    elif mutation == "encoding":
        value["payload"]["encoding"] = "u8"
    elif mutation == "time":
        value["payload"]["time_semantics"] = "UTC"
    else:
        value["physical_device_verified"] = True
    path, sha = save(tmp_path, value)
    with pytest.raises(LabError):
        logic.uart(path, sha, 0, 100_000)


def test_hash_and_unselected_channel_and_scope_rejected(tmp_path):
    value = capture([0, 0, 1, 1] * 8)
    path, sha = save(tmp_path, value)
    with pytest.raises(LabError, match="SHA-256 differs"):
        logic.pwm(path, "0" * 64, 0)
    with pytest.raises(LabError, match="not selected"):
        logic.pwm(path, sha, 1)
    with pytest.raises(LabError, match="DIO channel"):
        logic.pwm(path, sha, True)
    scope = copy.deepcopy(value)
    scope["plan"]["instrument"] = scope["instrument"] = "scope"
    path2, sha2 = save(tmp_path, scope, "wrong-instrument.json")
    with pytest.raises(LabError):
        logic.pwm(path2, sha2, 0)


def test_record_limits_do_not_silently_truncate_to_success(tmp_path):
    path, sha = save(tmp_path, capture([0, 1] * 600))
    with pytest.raises(LabError) as error:
        logic.pwm(path, sha, 0)
    assert error.value.code == "LIMIT_EXCEEDED"


def test_uart_truncated_final_frame_still_counts_toward_record_cap(tmp_path, monkeypatch):
    levels = uart_signal([0x41, 0x42])[:-45]
    path, sha = save(tmp_path, capture(levels))
    monkeypatch.setattr(logic, "MAX_RECORDS", 1)
    with pytest.raises(LabError) as error:
        logic.uart(path, sha, 0, 100_000)
    assert error.value.code == "LIMIT_EXCEEDED"


def test_offline_cli_does_not_load_sdk_open_network_or_spawn_and_writes_once(
    tmp_path, monkeypatch, capsys
):
    def forbidden(*args, **kwargs):
        pytest.fail("offline analysis attempted host/device access")

    monkeypatch.setattr(Dwf, "load", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    path, sha = save(tmp_path, capture(uart_signal([0x41])))
    output = tmp_path / "analysis.json"
    args = [
        "logic",
        "uart",
        str(path),
        "--sha256",
        sha,
        "--channel",
        "0",
        "--baud",
        "100000",
        "--output",
        str(output),
        "--json",
    ]
    assert main(args) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["data"]["output"]["sha256"] == digest(output.read_bytes())
    assert not result["hardware_access"] and not result["executables_started"]
    assert main(args) == 2
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "OUTPUT_EXISTS"
    assert digest(path.read_bytes()) == sha


def test_offline_cli_failed_analysis_still_saves_evidence(tmp_path, capsys):
    path, sha = save(tmp_path, capture([1] * 64))
    output = tmp_path / "no-data.json"
    assert (
        main(
            [
                "logic",
                "uart",
                str(path),
                "--sha256",
                sha,
                "--channel",
                "0",
                "--baud",
                "100000",
                "--output",
                str(output),
            ]
        )
        == 3
    )
    result = json.loads(capsys.readouterr().out)
    assert not result["ok"] and output.is_file()
    assert json.loads(output.read_bytes())["analysis"]["status"] == "no_frames"
