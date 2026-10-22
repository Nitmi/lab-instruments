from __future__ import annotations

import base64
import json
import random
import socket
import subprocess

import pytest
from bus_fixtures import bus_capture, i2c_signal, spi_signal
from logic_fixtures import save

from lab_instruments import bus_logic, logic
from lab_instruments.cli import main
from lab_instruments.common import LabError, digest
from lab_instruments.dwf import Dwf


def errors(result):
    a = result["analysis"]
    return {e["code"] for e in a["issues"] + [e for t in a["transfers"] for e in t["errors"]]}


@pytest.mark.parametrize("scl,sda,stretch", [(0, 1, 0), (15, 2, 5), (7, 10, 40)])
def test_i2c_known_address_data_and_ack_spans(tmp_path, scl, sda, stretch):
    signals = i2c_signal(scl=scl, sda=sda, stretch=stretch)
    path, sha = save(tmp_path, bus_capture(signals))
    result = bus_logic.i2c(path, sha, scl, sda)
    assert result["ok"], result
    transfer = result["analysis"]["transfers"][0]
    assert transfer["address"] == 0x50 and transfer["direction"] == "write"
    assert [b["value"] for b in transfer["bytes"]] == [0xA0, 0x00, 0xA5]
    assert all(b["ack"] and b["ack_receiver_role"] == "target" for b in transfer["bytes"])
    assert transfer["start_condition"] == "start" and transfer["end_condition"] == "stop"
    assert transfer["boundary_setup_clock"]["level"] == 0
    assert not result["analysis"]["clock_stretch_cause_verified"]
    assert result["channel_map"] == {"scl": scl, "sda": sda}
    for byte in transfer["bytes"]:
        assert len(byte["sample_indices"]) == 9
        assert byte["raw_span"]["start"] == 2 * (byte["sample_indices"][0] - 1)
        assert byte["raw_span"]["end_exclusive"] == 2 * (byte["sample_indices"][-1] + 1)
    assert digest(path.read_bytes()) == sha


def test_i2c_repeated_start_read_and_final_nack_are_observations(tmp_path):
    segments = [
        {"address": 0x48, "data": [2]},
        {"address": 0x48, "read": True, "data": [0x12, 0x34], "acks": [True, True, False]},
    ]
    path, sha = save(tmp_path, bus_capture(i2c_signal(segments)))
    result = bus_logic.i2c(path, sha, 0, 1)
    assert result["ok"], result
    first, second = result["analysis"]["transfers"]
    assert first["end_condition"] == second["start_condition"] == "repeated_start"
    assert first["boundary_setup_clock"]["level"] == 1
    assert second["direction"] == "read" and second["end_condition"] == "stop"
    assert [b["value"] for b in second["bytes"]] == [0x91, 0x12, 0x34]
    assert second["bytes"][-1]["ack"] is False
    assert second["bytes"][-1]["ack_receiver_role"] == "controller"
    assert result["analysis"]["nack_count"] == 1


def test_i2c_address_nack_is_not_silently_converted_into_application_failure_or_success(tmp_path):
    path, sha = save(tmp_path, bus_capture(i2c_signal([{"address": 0x50, "acks": [False]}])))
    result = bus_logic.i2c(path, sha, 0, 1)
    assert result["ok"] and result["analysis"]["nack_count"] == 1
    assert not result["endpoint_identity_verified"]


@pytest.mark.parametrize(
    "case",
    [
        "reserved",
        "10bit",
        "after-nack",
        "truncated",
        "partial",
        "busy",
        "simultaneous",
        "fast",
        "void",
    ],
)
def test_i2c_invalid_or_unsupported_boundaries(tmp_path, case):
    signals = i2c_signal()
    expected = {
        "reserved": "unsupported_reserved_or_10bit_address",
        "10bit": "unsupported_reserved_or_10bit_address",
        "after-nack": "data_after_nack",
        "truncated": "missing_stop_or_repeated_start",
        "partial": "partial_byte_or_ack",
        "busy": "capture_starts_busy",
        "simultaneous": "simultaneous_scl_sda_edge",
        "fast": "undersampled_clock_phase",
        "void": "missing_address",
    }[case]
    if case in ("reserved", "10bit"):
        signals = i2c_signal([{"address": 0 if case == "reserved" else 0x78}])
    elif case == "after-nack":
        signals = i2c_signal([{"address": 0x50, "data": [1], "acks": [False, True]}])
    elif case in ("truncated", "partial"):
        end = -16 if case == "truncated" else 50
        signals = {ch: bits[:end] for ch, bits in signals.items()}
    elif case == "busy":
        signals[1][:2] = [0, 0]
    elif case == "simultaneous":
        i = next(
            i for i in range(12, len(signals[0])) if signals[0][i - 1] == 0 and signals[0][i] == 1
        )
        signals[1][i] = 1 - signals[1][i]
    elif case == "fast":
        signals = i2c_signal(phase=1)
    else:
        signals = {0: [1] * 24, 1: [1] * 8 + [0] * 8 + [1] * 8}
    path, sha = save(tmp_path, bus_capture(signals))
    result = bus_logic.i2c(path, sha, 0, 1)
    assert not result["ok"] and expected in errors(result), result


def test_i2c_multiple_stop_separated_transactions_and_no_traffic(tmp_path):
    left, right = i2c_signal(), i2c_signal([{"address": 0x21, "data": [0xFF]}])
    signals = {ch: left[ch] + right[ch] for ch in left}
    path, sha = save(tmp_path, bus_capture(signals))
    result = bus_logic.i2c(path, sha, 0, 1)
    assert result["ok"] and len(result["analysis"]["transfers"]) == 2
    idle, ish = save(tmp_path, bus_capture({0: [1] * 32, 1: [1] * 32}), "idle.json")
    result = bus_logic.i2c(idle, ish, 0, 1)
    assert not result["ok"] and result["status"] == "no_transfers"


def test_i2c_clock_before_observed_start_is_not_silently_discarded(tmp_path):
    signals = i2c_signal()
    signals = {0: [1] * 3 + [0] * 3 + [1] * 3 + signals[0], 1: [1] * 9 + signals[1]}
    path, sha = save(tmp_path, bus_capture(signals))
    result = bus_logic.i2c(path, sha, 0, 1)
    assert not result["ok"] and "clock_without_observed_start" in errors(result)


def test_i2c_delimiter_and_start_hold_need_sample_resolution(tmp_path):
    signals = i2c_signal()
    # Compress only the observed START hold; all ordinary SCL phases remain long.
    signals = {ch: bits[:7] + bits[9:] for ch, bits in signals.items()}
    path, sha = save(tmp_path, bus_capture(signals))
    result = bus_logic.i2c(path, sha, 0, 1)
    assert not result["ok"] and "undersampled_start_hold" in errors(result)


def test_spi_last_sampled_bit_without_final_trailing_edge_is_partial(tmp_path):
    signals = spi_signal([0xA5])
    last = next(i for i in range(len(signals[0]) - 1, 0, -1) if signals[0][i] != signals[0][i - 1])
    signals = {ch: bits[:last] for ch, bits in signals.items()}
    path, sha = save(tmp_path, bus_capture(signals))
    result = bus_logic.spi(path, sha, 0, 1, mosi=2, mode=0)
    assert not result["ok"] and "partial_word_or_clock_cycle" in errors(result)
    transfer = result["analysis"]["transfers"][0]
    assert len(transfer["partial_sample_indices"]) == 8 and transfer["words"] == []


def test_spi_cli_saves_invalid_evidence_and_requires_a_mode(tmp_path, capsys):
    signals = {ch: bits[:70] for ch, bits in spi_signal().items()}
    path, sha = save(tmp_path, bus_capture(signals))
    output = tmp_path / "partial.json"
    args = ["logic", "spi", str(path), "--sha256", sha, "--sck", "0", "--cs", "1", "--mosi", "2"]
    with pytest.raises(SystemExit):
        main(args)
    capsys.readouterr()
    assert main([*args, "--mode", "0", "--output", str(output)]) == 3
    result = json.loads(capsys.readouterr().out)
    assert result["data"]["output"]["sha256"] == digest(output.read_bytes())
    assert json.loads(output.read_bytes())["analysis"]["bytes_base64"] is None


@pytest.mark.parametrize("mode", range(4))
@pytest.mark.parametrize("order", ["msb", "lsb"])
@pytest.mark.parametrize("active", ["low", "high"])
def test_spi_modes_orders_polarities_and_full_duplex(tmp_path, mode, order, active):
    tx, rx = [0, 0x96, 0xFF], [0xFF, 0x35, 0]
    signals = spi_signal(tx, rx, mode=mode, order=order, active=active, channels=(15, 1, 7, 12))
    path, sha = save(tmp_path, bus_capture(signals))
    result = bus_logic.spi(
        path, sha, 15, 1, mosi=7, miso=12, mode=mode, bit_order=order, cs_active=active
    )
    assert result["ok"], result
    a = result["analysis"]
    assert [w["values"]["mosi"] for w in a["transfers"][0]["words"]] == tx
    assert [w["values"]["miso"] for w in a["transfers"][0]["words"]] == rx
    assert base64.b64decode(a["bytes_base64"]["mosi"][0]) == bytes(tx)
    assert a["sample_edge"] == ("trailing" if mode % 2 else "leading")
    assert a["transfers"][0]["clock_edges"] == 48
    assert not a["mode_detected"] and not result["protocol_timing_compliance_verified"]


@pytest.mark.parametrize("width", [1, 4, 9, 16, 24, 32])
@pytest.mark.parametrize("mode", range(4))
def test_spi_word_width_and_single_direction(tmp_path, width, mode):
    tx = [0, (1 << width) - 1, 0x39 & ((1 << width) - 1)]
    signals = spi_signal(tx, mode=mode, word_bits=width)
    signals.pop(2)
    path, sha = save(tmp_path, bus_capture(signals))
    result = bus_logic.spi(path, sha, 0, 1, miso=3, mode=mode, word_bits=width)
    assert result["ok"], result
    assert [w["values"]["miso"] for w in result["analysis"]["transfers"][0]["words"]] == [
        v ^ ((1 << width) - 1) for v in tx
    ]
    assert result["analysis"]["bytes_base64"] is None


@pytest.mark.parametrize(
    "case", ["busy", "truncated", "partial", "cs-clock", "unstable", "idle", "fast", "hold"]
)
def test_spi_bad_clock_data_or_chip_select_never_passes(tmp_path, case):
    signals = spi_signal()
    expected = {
        "busy": "capture_starts_selected",
        "truncated": "missing_cs_deassertion",
        "partial": "partial_word_or_clock_cycle",
        "cs-clock": "simultaneous_cs_clock_edge",
        "unstable": "data_changes_near_sampling_edge_mosi",
        "idle": "clock_not_idle_at_cs_start",
        "fast": "undersampled_clock_phase",
        "hold": "undersampled_cs_hold",
    }[case]
    if case == "busy":
        signals[1][:3] = [0] * 3
    elif case in ("truncated", "partial"):
        end = -8 if case == "truncated" else 70
        signals = {ch: bits[:end] for ch, bits in signals.items()}
    elif case == "cs-clock":
        signals[0][3:6] = [1] * 3
    elif case == "unstable":
        i = next(i for i in range(1, len(signals[0])) if signals[0][i] > signals[0][i - 1])
        signals[2][i] = 1 - signals[2][i]
    elif case == "idle":
        signals[0][:9] = [1] * 9
    elif case == "fast":
        signals = spi_signal(mode=1, phase=1)
    else:
        last = next(
            i for i in range(len(signals[0]) - 1, 0, -1) if signals[0][i] != signals[0][i - 1]
        )
        for i in range(last + 1, len(signals[1])):
            signals[1][i] = 1
    path, sha = save(tmp_path, bus_capture(signals))
    result = bus_logic.spi(path, sha, 0, 1, mosi=2, miso=3)
    assert not result["ok"] and expected in errors(result), result
    assert result["analysis"]["bytes_base64"] is None


def test_spi_preserves_transactions_and_ignores_unselected_clocks(tmp_path):
    a, b = spi_signal([1], [2]), spi_signal([3], [4])
    signals = {ch: a[ch] + b[ch] for ch in a}
    path, sha = save(tmp_path, bus_capture(signals))
    result = bus_logic.spi(path, sha, 0, 1, mosi=2, miso=3)
    assert result["ok"] and len(result["analysis"]["transfers"]) == 2
    assert [base64.b64decode(v) for v in result["analysis"]["bytes_base64"]["mosi"]] == [
        b"\x01",
        b"\x03",
    ]
    idle, ish = save(tmp_path, bus_capture({0: [0, 1] * 16, 1: [1] * 32, 2: [0] * 32}), "idle.json")
    result = bus_logic.spi(idle, ish, 0, 1, mosi=2)
    assert not result["ok"] and result["status"] == "no_transfers"


@pytest.mark.parametrize("kind", ["i2c", "spi"])
def test_source_failures_and_record_limits_are_preserved(tmp_path, monkeypatch, kind):
    signals = i2c_signal() if kind == "i2c" else spi_signal()
    path, sha = save(tmp_path, bus_capture(signals, ok=False))

    def analyze():
        return (
            bus_logic.i2c(path, sha, 0, 1)
            if kind == "i2c"
            else bus_logic.spi(path, sha, 0, 1, mosi=2)
        )

    result = analyze()
    assert not result["ok"] and result["status"] == "source_failed"
    assert result["analysis"]["status"] == "decoded"
    assert result["source_cleanup"]["complete"] is False
    monkeypatch.setattr(bus_logic, "MAX_RECORDS", 2)
    with pytest.raises(LabError) as error:
        analyze()
    assert error.value.code == "LIMIT_EXCEEDED"


@pytest.mark.parametrize(
    "args",
    [
        {"mosi": None},
        {"mosi": True},
        {"mosi": 2, "mode": True},
        {"mosi": 2, "word_bits": 33},
        {"mosi": 2, "cs_active": "auto"},
        {"mosi": 2, "min_phase_samples": 1},
        {"mosi": 2, "bit_order": "auto"},
        {"mosi": 1},
    ],
)
def test_spi_requires_exact_distinct_channels_and_explicit_valid_settings(tmp_path, args):
    path, sha = save(tmp_path, bus_capture(spi_signal()))
    with pytest.raises(LabError):
        bus_logic.spi(path, sha, 0, 1, **args)


def test_bus_loader_reads_one_capture_and_never_starts_hardware_or_processes(
    tmp_path, monkeypatch, capsys
):
    path, sha = save(tmp_path, bus_capture(i2c_signal()))
    original = logic.read
    reads = []

    def read(*a, **kw):
        reads.append(a[0])
        return original(*a, **kw)

    def forbidden(*a, **kw):
        pytest.fail("offline bus analysis attempted external I/O")

    monkeypatch.setattr(logic, "read", read)
    monkeypatch.setattr(Dwf, "load", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    output = tmp_path / "analysis.json"
    args = [
        "logic",
        "i2c",
        str(path),
        "--sha256",
        sha,
        "--scl",
        "0",
        "--sda",
        "1",
        "--output",
        str(output),
    ]
    assert main(args) == 0
    result = json.loads(capsys.readouterr().out)
    assert reads == [path]
    assert result["data"]["output"]["sha256"] == digest(output.read_bytes())
    assert not result["hardware_access"] and not result["executables_started"]
    assert main(args) == 2
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "OUTPUT_EXISTS"
    assert reads == [path]


def test_bus_hash_unselected_duplicate_and_bool_channels_rejected(tmp_path):
    path, sha = save(tmp_path, bus_capture(i2c_signal()))
    for expected, scl, sda in [("0" * 64, 0, 1), (sha, 0, 15), (sha, 0, 0), (sha, True, 0)]:
        with pytest.raises(LabError):
            bus_logic.i2c(path, expected, scl, sda)


def test_bus_randomized_transmitters(tmp_path):
    randomizer = random.Random(94062)
    for i in range(40):
        address = randomizer.randrange(8, 0x78)
        payload = [randomizer.randrange(256) for _ in range(8)]
        signals = i2c_signal(
            [{"address": address, "data": payload}], phase=randomizer.randrange(2, 7)
        )
        path, sha = save(tmp_path, bus_capture(signals), f"i2c-{i}.json")
        result = bus_logic.i2c(path, sha, 0, 1)
        assert result["ok"], result
        assert [b["value"] for b in result["analysis"]["transfers"][0]["bytes"]] == [
            address << 1,
            *payload,
        ]
        mode = randomizer.randrange(4)
        width = randomizer.randrange(1, 33)
        values = [randomizer.randrange(1 << width) for _ in range(4)]
        order = randomizer.choice(("msb", "lsb"))
        signals = spi_signal(
            values, mode=mode, word_bits=width, order=order, phase=randomizer.randrange(2, 6)
        )
        path, sha = save(tmp_path, bus_capture(signals), f"spi-{i}.json")
        result = bus_logic.spi(path, sha, 0, 1, mosi=2, mode=mode, word_bits=width, bit_order=order)
        assert result["ok"], result
        assert [w["values"]["mosi"] for w in result["analysis"]["transfers"][0]["words"]] == values
