from __future__ import annotations

import json
import socket
import subprocess

import pytest
from bus_fixtures import bus_capture, i2c_signal, spi_signal
from check_fixtures import fixture
from logic_fixtures import capture, save, uart_signal

from lab_instruments import logic_checks as checks
from lab_instruments.cli import main
from lab_instruments.common import LabError, digest, encode
from lab_instruments.dwf import Dwf


def inputs(tmp_path, data=None, spec=None):
    data0, spec0 = fixture()
    data = data if data is not None else data0
    spec = spec if spec is not None else spec0
    path, sha = save(tmp_path, data)
    spec_path, spec_sha = save(tmp_path, spec, "spec.json")
    return spec_path, spec_sha, path, sha


def selected(index):
    _, spec = fixture()
    spec["checks"] = [spec["checks"][index]]
    return spec


def test_all_protocol_checks_use_original_capture_and_exact_expectations(tmp_path):
    args = inputs(tmp_path)
    result = checks.evaluate(*args)
    assert result["ok"] and result["status"] == "passed" and result["complete"]
    assert result["summary"] == {"checks": 4, "passed": 4, "failed": 0}
    assert result["capture"]["sha256"] == args[3] and result["spec"]["sha256"] == args[1]
    assert result["evidence_origin"] == "test_double"
    assert all(
        result[k] is False
        for k in (
            "hardware_access",
            "executables_started",
            "physical_device_verified",
            "physical_timing_verified",
            "calibration_verified",
            "protocol_timing_compliance_verified",
            "endpoint_identity_verified",
            "authorization_granted",
        )
    )
    assert [c["id"] for c in result["checks"]] == ["console", "pwm", "sensor", "spi"]
    for check in result["checks"]:
        assert check["native_sha256"] == digest(encode(check["native"]))
        assert check["native"]["capture"] == result["capture"]
        assert all(a["status"] == "passed" for a in check["assertions"])
    assert digest(args[0].read_bytes()) == args[1] and digest(args[2].read_bytes()) == args[3]


@pytest.mark.parametrize("index", range(4))
def test_mismatch_is_failed_and_other_checks_still_evaluated(tmp_path, index):
    _, spec = fixture()
    expect = spec["checks"][index]["expect"]
    if index == 0:
        expect["bytes_hex"] = "4f4b00"  # Exact whole stream, not prefix/substring.
    elif index == 1:
        expect["minimum_cycles"] = 13
    elif index == 2:
        expect["transfers"][0]["address"] = 0x49
    else:
        expect["transfers"][0]["mosi"] = [0x97, 0]
    result = checks.evaluate(*inputs(tmp_path, spec=spec))
    assert not result["ok"] and result["complete"]
    assert result["summary"] == {"checks": 4, "passed": 3, "failed": 1}
    assert result["checks"][index]["status"] == "failed"
    assert result["checks"][-1]["status"] == ("failed" if index == 3 else "passed")


@pytest.mark.parametrize("kind,index", [("uart", 0), ("i2c", 2), ("spi", 3)])
def test_exact_payload_rejects_extra_messages_transactions_or_words(tmp_path, kind, index):
    spec = selected(index)
    if kind == "uart":
        data = capture(uart_signal(b"OKOK"))
    elif kind == "i2c":
        signals = i2c_signal()
        signals = {2: signals[0] + signals[0], 3: signals[1] + signals[1]}
        data = bus_capture(signals)
    else:
        data = bus_capture(spi_signal([0x96, 0, 1], [0x35, 0xFF, 2], channels=(4, 5, 6, 7)))
    result = checks.evaluate(*inputs(tmp_path, data, spec))
    assert not result["ok"] and result["checks"][0]["native"]["ok"]


def test_pwm_each_cycle_cannot_hide_outliers_behind_a_good_mean(tmp_path):
    spec = selected(1)
    spec["checks"][0]["configuration"]["channel"] = 0
    expect = spec["checks"][0]["expect"]
    expect.update(
        minimum_cycles=2,
        frequency_hz={"minimum": 900, "maximum": 900},
        duty_cycle={"minimum": 0.25, "maximum": 0.5},
    )
    levels = [0] * 2 + [1] * 3 + [0] * 5 + [1] * 4 + [0] * 6 + [1] * 3 + [0] * 4
    result = checks.evaluate(*inputs(tmp_path, capture(levels, 8000), spec))
    check = result["checks"][0]
    assert check["native"]["analysis"]["statistics"]["frequency_hz"]["mean"] == 900
    limits = next(a for a in check["assertions"] if a["name"] == "all_cycles_frequency_hz")
    assert limits["status"] == "failed" and len(limits["outliers"]) == 2
    assert [r["value"] for r in limits["outliers"]] == [1000, 800]
    assert all(r["raw_span"] for r in limits["outliers"])


@pytest.mark.parametrize("change", ["address", "direction", "ack", "data", "boundary"])
def test_i2c_compares_ack_and_repeated_start_semantics(tmp_path, change):
    spec = selected(2)
    expected = spec["checks"][0]["expect"]["transfers"]
    if change == "address":
        expected[0]["address"] = 0x49
    elif change == "direction":
        expected[0]["direction"] = "read"
    elif change == "ack":
        expected[1]["acks"][-1] = True
    elif change == "data":
        expected[1]["data"][-1] = 0x35
    else:
        expected[0]["end_condition"] = "stop"
        expected[1]["start_condition"] = "start"
    result = checks.evaluate(*inputs(tmp_path, spec=spec))
    assert not result["ok"] and result["checks"][0]["status"] == "failed"


@pytest.mark.parametrize("index", range(4))
def test_failed_source_cannot_pass_matching_expectations(tmp_path, index):
    data, spec = fixture()
    spec["checks"] = [spec["checks"][index]]
    data.update(ok=False, status="failed", error={"code": "CLEANUP_INCOMPLETE"})
    data["cleanup"]["complete"] = False
    result = checks.evaluate(*inputs(tmp_path, data, spec))
    check = result["checks"][0]
    assert not result["ok"] and check["status"] == "invalid_evidence"
    assert check["native"]["status"] == "source_failed"
    assert check["assertions"][1]["status"] == "not_evaluated"


@pytest.mark.parametrize("index", range(4))
def test_incomplete_or_missing_signal_never_matches_partial_expected_data(tmp_path, index):
    data, spec = fixture()
    spec["checks"] = [spec["checks"][index]]
    # Keep a structurally valid capture but use constant idle/active levels.
    n = data["plan"]["samples"]
    levels = {
        0: [1] * n,
        1: [0] * n,
        2: [1] * n,
        3: [1] * n,
        4: [0] * n,
        5: [1] * n,
        6: [0] * n,
        7: [0] * n,
    }
    result = checks.evaluate(*inputs(tmp_path, bus_capture(levels), spec))
    assert not result["ok"] and result["checks"][0]["status"] == "invalid_evidence"
    assert result["checks"][0]["assertions"][1]["status"] == "not_evaluated"


@pytest.mark.parametrize(
    "mutation",
    [
        "schema",
        "empty",
        "too-many",
        "duplicate-id",
        "unknown-field",
        "command",
        "kind",
        "missing-config",
        "extra-config",
        "bool-channel",
        "duplicate-channel",
        "bad-mode",
        "bad-width",
        "null-directions",
        "uppercase-hex",
        "odd-hex",
        "empty-hex",
        "too-wide-uart",
        "reversed-range",
        "nan-range",
        "bool-cycles",
        "zero-cycles",
        "extra-direction",
        "unequal-directions",
        "bool-word",
        "negative-word",
        "big-word",
        "ack-type",
        "ack-length",
        "nack-then-data",
        "bad-address",
        "unclosed-bus",
        "bad-start",
        "no-transfers",
    ],
)
def test_contract_malformed_or_unsupported_choices_rejected_before_capture_read(
    tmp_path, monkeypatch, mutation
):
    _, spec = fixture()
    c = spec["checks"]
    if mutation == "schema":
        spec["schema_version"] = "next"
    elif mutation == "empty":
        spec["checks"] = []
    elif mutation == "too-many":
        spec["checks"] = [c[0]] * 9
    elif mutation == "duplicate-id":
        c[1]["id"] = c[0]["id"]
    elif mutation == "unknown-field":
        spec["metadata"] = {}
    elif mutation == "command":
        c[0]["command"] = "arbitrary"
    elif mutation == "kind":
        c[0]["kind"] = "eval"
    elif mutation == "missing-config":
        c[0]["configuration"].pop("baud")
    elif mutation == "extra-config":
        c[0]["configuration"]["auto_baud"] = True
    elif mutation == "bool-channel":
        c[0]["configuration"]["channel"] = True
    elif mutation == "duplicate-channel":
        c[2]["configuration"]["sda"] = 2
    elif mutation == "bad-mode":
        c[3]["configuration"]["mode"] = 4
    elif mutation == "bad-width":
        c[3]["configuration"]["word_bits"] = 0
    elif mutation == "null-directions":
        c[3]["configuration"].update(mosi=None, miso=None)
    elif mutation in ("uppercase-hex", "odd-hex", "empty-hex"):
        c[0]["expect"]["bytes_hex"] = {"uppercase-hex": "4F4B", "odd-hex": "4", "empty-hex": ""}[
            mutation
        ]
    elif mutation == "too-wide-uart":
        c[0]["configuration"]["data_bits"] = 5
    elif mutation == "reversed-range":
        c[1]["expect"]["frequency_hz"] = {"minimum": 2, "maximum": 1}
    elif mutation == "nan-range":
        c[1]["expect"]["duty_cycle"]["minimum"] = "NaN"
    elif mutation == "bool-cycles":
        c[1]["expect"]["minimum_cycles"] = True
    elif mutation == "zero-cycles":
        c[1]["expect"]["minimum_cycles"] = 0
    elif mutation == "extra-direction":
        c[3]["expect"]["transfers"][0]["auto"] = []
    elif mutation == "unequal-directions":
        c[3]["expect"]["transfers"][0]["miso"] = [1]
    elif mutation in ("bool-word", "negative-word", "big-word"):
        c[3]["expect"]["transfers"][0]["mosi"][0] = {
            "bool-word": True,
            "negative-word": -1,
            "big-word": 256,
        }[mutation]
    elif mutation == "ack-type":
        c[2]["expect"]["transfers"][0]["acks"][0] = 1
    elif mutation == "ack-length":
        c[2]["expect"]["transfers"][0]["acks"] = [True]
    elif mutation == "nack-then-data":
        c[2]["expect"]["transfers"][0]["acks"][0] = False
    elif mutation == "bad-address":
        c[2]["expect"]["transfers"][0]["address"] = 0x78
    elif mutation == "unclosed-bus":
        c[2]["expect"]["transfers"][-1]["end_condition"] = "repeated_start"
    elif mutation == "bad-start":
        c[2]["expect"]["transfers"][0]["start_condition"] = "repeated_start"
    else:
        c[2]["expect"]["transfers"] = []
    spec_path, spec_sha = save(tmp_path, spec, "spec.json")
    original = checks.read
    reads = []

    def read(path, *a, **kw):
        reads.append(path)
        assert path == spec_path
        return original(path, *a, **kw)

    monkeypatch.setattr(checks, "read", read)
    with pytest.raises(LabError):
        checks.evaluate(spec_path, spec_sha, tmp_path / "absent-capture.json", "0" * 64)
    assert reads == [spec_path]


def test_specs_and_original_capture_hashes_are_both_required(tmp_path):
    spec_path, spec_sha, path, sha = inputs(tmp_path)
    for expected_spec, expected_capture in [("0" * 64, sha), (spec_sha, "0" * 64)]:
        with pytest.raises(LabError) as error:
            checks.evaluate(spec_path, expected_spec, path, expected_capture)
        assert error.value.code == "HASH_MISMATCH"


def test_one_snapshot_and_no_sdk_network_process_or_native_report_input(tmp_path, monkeypatch):
    args = inputs(tmp_path)
    reads = []
    original = checks.read

    def read(path, *a, **kw):
        reads.append(path)
        return original(path, *a, **kw)

    def forbidden(*a, **kw):
        pytest.fail("offline checks attempted external access")

    monkeypatch.setattr(checks, "read", read)
    monkeypatch.setattr(Dwf, "load", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    result = checks.evaluate(*args)
    assert result["ok"] and reads == [args[0], args[2]]
    native_path, native_sha = save(tmp_path, result["checks"][0]["native"], "forged-native.json")
    with pytest.raises(LabError):
        checks.evaluate(args[0], args[1], native_path, native_sha)


def test_cli_saves_failed_report_without_overwrite_and_uses_exit_3(tmp_path, capsys):
    data, spec = fixture()
    spec["checks"][0]["expect"]["bytes_hex"] = "00"
    sp, sh, cp, ch = inputs(tmp_path, data, spec)
    output = tmp_path / "report.json"
    args = [
        "logic",
        "check",
        "--spec",
        str(sp),
        "--spec-sha256",
        sh,
        "--capture",
        str(cp),
        "--capture-sha256",
        ch,
        "--output",
        str(output),
        "--json",
    ]
    assert main(args) == 3
    result = json.loads(capsys.readouterr().out)
    assert not result["ok"] and result["data"]["report"]["summary"]["passed"] == 3
    assert result["data"]["output"]["sha256"] == digest(output.read_bytes())
    assert main(args) == 2
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "OUTPUT_EXISTS"


def test_output_limit_rejects_cli_envelope_before_report_creation(tmp_path, monkeypatch, capsys):
    sp, sh, cp, ch = inputs(tmp_path)
    result = checks.evaluate(sp, sh, cp, ch)
    # Make the report itself fit while its enclosing CLI object cannot.
    import lab_instruments.common as common

    monkeypatch.setattr(common, "MAX_JSON_BYTES", len(encode(result)) + 1)
    output = tmp_path / "oversized.json"
    assert (
        main(
            [
                "logic",
                "check",
                "--spec",
                str(sp),
                "--spec-sha256",
                sh,
                "--capture",
                str(cp),
                "--capture-sha256",
                ch,
                "--output",
                str(output),
            ]
        )
        == 2
    )
    assert not output.exists()
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "LIMIT_EXCEEDED"


@pytest.mark.parametrize("name", ["", "a/b", "1first", "中文", "a" * 49, True, None])
def test_invalid_check_names_rejected(name):
    _, spec = fixture()
    spec["checks"][0]["id"] = name
    with pytest.raises(LabError):
        checks.validate(spec)


def test_expected_address_nack_can_pass_an_explicit_no_target_assertion(tmp_path):
    spec = selected(2)
    spec["checks"][0]["configuration"].update(scl=0, sda=1)
    spec["checks"][0]["expect"]["transfers"] = [
        {
            "address": 0x48,
            "direction": "write",
            "start_condition": "start",
            "end_condition": "stop",
            "data": [],
            "acks": [False],
        }
    ]
    data = bus_capture(i2c_signal([{"address": 0x48, "acks": [False]}]))
    result = checks.evaluate(*inputs(tmp_path, data, spec))
    assert result["ok"] and result["checks"][0]["native"]["analysis"]["nack_count"] == 1
    assert not result["endpoint_identity_verified"]


def test_spi_32_bit_single_direction_uses_integer_exact_comparison(tmp_path):
    spec = selected(3)
    config = spec["checks"][0]["configuration"]
    config.update(mosi=None, mode=3, word_bits=32, bit_order="lsb", cs_active="high")
    words = [0, 0x80000001, 0xFFFFFFFF]
    spec["checks"][0]["expect"]["transfers"] = [{"miso": words}]
    signals = spi_signal(
        [0] * 3, words, mode=3, word_bits=32, order="lsb", active="high", channels=(4, 5, 6, 7)
    )
    signals.pop(6)
    args = inputs(tmp_path, bus_capture(signals), spec)
    result = checks.evaluate(*args)
    assert result["ok"]
    assert result["checks"][0]["assertions"][1]["actual"] == [{"miso": words}]
    assert encode(result) == encode(checks.evaluate(*args))


@pytest.mark.parametrize("kind", ["i2c", "spi"])
def test_expected_record_caps_are_not_silently_truncated(kind):
    spec = selected(2 if kind == "i2c" else 3)
    check = spec["checks"][0]
    if kind == "i2c":
        check["expect"]["transfers"] = [
            {
                "address": 0x48,
                "direction": "write",
                "start_condition": "start",
                "end_condition": "stop",
                "data": [0] * 511,
                "acks": [True] * 512,
            }
        ]
    else:
        check["expect"]["transfers"] = [{"mosi": [0] * 512, "miso": [0] * 512}]
    with pytest.raises(LabError) as error:
        checks.validate(spec)
    assert error.value.code == "LIMIT_EXCEEDED"


def test_duplicate_json_fields_and_oversized_spec_stop_before_capture(tmp_path):
    path = tmp_path / "spec.json"
    for raw in (b'{"schema_version":"bad","schema_version":"bad","checks":[]}', b" " * 65537):
        path.write_bytes(raw)
        with pytest.raises(LabError):
            checks.evaluate(path, digest(raw), tmp_path / "absent.json", "0" * 64)


def test_repository_example_is_a_valid_passing_spec_on_its_illustrative_fixture(tmp_path):
    from pathlib import Path

    spec = json.loads(
        (Path(__file__).resolve().parents[1] / "examples/logic-checks.json").read_bytes()
    )
    result = checks.evaluate(*inputs(tmp_path, spec=spec))
    assert result["ok"] and len(result["checks"]) == 4
