"""Evaluate a fixed offline assertion spec against one saved digital capture."""

from __future__ import annotations

import base64
import re

from . import __version__, bus_logic, logic
from .common import digest, encode, integer, number, parse, read, require

SCHEMA = "lab-instruments.logic-check-spec.v1"
ID = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,47}\Z")
HEX = re.compile(r"(?:[0-9a-f]{2}){1,512}\Z")


def fields(value, names, description):
    require(isinstance(value, dict) and set(value) == set(names), "invalid " + description)


def choice(value, choices, description):
    require(type(value) is str and value in choices, "invalid " + description)


def word_list(values, width, description, *, empty=False):
    require(
        isinstance(values, list) and (0 if empty else 1) <= len(values) <= 512,
        "invalid " + description,
    )
    for value in values:
        integer(value, description, 0, (1 << width) - 1)


def validate_configuration(kind, config):
    if kind == "uart":
        fields(
            config,
            ("channel", "baud", "data_bits", "parity", "stop_bits", "inverted"),
            "UART configuration",
        )
        channels = [config["channel"]]
        integer(config["baud"], "baud", 50, 3_000_000)
        integer(config["data_bits"], "data bits", 5, 8)
        choice(config["parity"], ("none", "even", "odd"), "parity")
        integer(config["stop_bits"], "stop bits", 1, 2)
        require(type(config["inverted"]) is bool, "invalid inversion")
    elif kind == "pwm":
        fields(config, ("channel", "active_level"), "PWM configuration")
        channels = [config["channel"]]
        choice(config["active_level"], ("high", "low"), "active level")
    elif kind == "i2c":
        fields(config, ("scl", "sda", "min_phase_samples"), "I2C configuration")
        channels = [config["scl"], config["sda"]]
        integer(config["min_phase_samples"], "phase samples", 2, 64)
    else:
        fields(
            config,
            (
                "sck",
                "cs",
                "mosi",
                "miso",
                "mode",
                "word_bits",
                "bit_order",
                "cs_active",
                "min_phase_samples",
            ),
            "SPI configuration",
        )
        data = [config[key] for key in ("mosi", "miso") if config[key] is not None]
        require(bool(data), "SPI requires a data channel")
        channels = [config["sck"], config["cs"], *data]
        integer(config["mode"], "SPI mode", 0, 3)
        integer(config["word_bits"], "word bits", 1, 32)
        integer(config["min_phase_samples"], "phase samples", 2, 64)
        choice(config["bit_order"], ("msb", "lsb"), "bit order")
        choice(config["cs_active"], ("high", "low"), "CS polarity")
    for channel in channels:
        integer(channel, "DIO channel", 0, 15)
    require(len(channels) == len(set(channels)), "protocol channels must be distinct")
    return channels


def bounds(value, description, low, high):
    fields(value, ("minimum", "maximum"), description)
    for key in value:
        number(value[key], description, low, high)
    require(value["minimum"] <= value["maximum"], "reversed " + description)


def validate_expected(kind, config, expect):
    if kind == "uart":
        fields(expect, ("bytes_hex",), "UART expectation")
        value = expect["bytes_hex"]
        require(
            isinstance(value, str) and HEX.fullmatch(value), "expected nonempty lowercase byte hex"
        )
        require(
            all(v < 1 << config["data_bits"] for v in bytes.fromhex(value)),
            "expected UART byte exceeds configured data width",
        )
        return
    if kind == "pwm":
        fields(expect, ("minimum_cycles", "frequency_hz", "duty_cycle"), "PWM expectation")
        integer(expect["minimum_cycles"], "minimum cycles", 1, 512)
        bounds(expect["frequency_hz"], "frequency bounds", 0, 125_000_000)
        bounds(expect["duty_cycle"], "duty bounds", 0, 1)
        return
    fields(expect, ("transfers",), "bus expectation")
    transfers = expect["transfers"]
    require(isinstance(transfers, list) and 1 <= len(transfers) <= 64, "invalid expected transfers")
    records = len(transfers)
    for index, transfer in enumerate(transfers):
        if kind == "i2c":
            fields(
                transfer,
                ("address", "direction", "start_condition", "end_condition", "data", "acks"),
                "I2C transfer expectation",
            )
            integer(transfer["address"], "7-bit address", 0x08, 0x77)
            choice(transfer["direction"], ("read", "write"), "direction")
            choice(transfer["start_condition"], ("start", "repeated_start"), "start condition")
            choice(transfer["end_condition"], ("stop", "repeated_start"), "end condition")
            start = (
                "repeated_start"
                if index and transfers[index - 1]["end_condition"] == "repeated_start"
                else "start"
            )
            require(transfer["start_condition"] == start, "inconsistent expected I2C boundaries")
            word_list(transfer["data"], 8, "I2C data", empty=True)
            acks = transfer["acks"]
            require(
                isinstance(acks, list)
                and len(acks) == len(transfer["data"]) + 1
                and all(type(v) is bool for v in acks),
                "invalid expected ACK sequence",
            )
            require(all(acks[:-1]), "expected data cannot follow a NACK")
            records += 1 + len(transfer["data"])
        else:
            directions = [key for key in ("mosi", "miso") if config[key] is not None]
            fields(transfer, directions, "SPI direction expectations")
            lengths = set()
            for key in directions:
                word_list(transfer[key], config["word_bits"], "SPI words")
                lengths.add(len(transfer[key]))
            require(len(lengths) == 1, "SPI directions must have equal word counts")
            records += next(iter(lengths))
    if kind == "i2c":
        require(transfers[-1]["end_condition"] == "stop", "expected I2C capture must end with STOP")
    require(records <= 512, "expected bus record limit", "LIMIT_EXCEEDED")


def validate(spec):
    fields(spec, ("schema_version", "checks"), "logic check spec")
    require(spec["schema_version"] == SCHEMA, "unsupported check spec schema")
    checks = spec["checks"]
    require(isinstance(checks, list) and 1 <= len(checks) <= 8, "expected 1..8 checks")
    seen = set()
    for check in checks:
        fields(check, ("id", "kind", "configuration", "expect"), "check fields")
        name = check["id"]
        require(
            isinstance(name, str) and ID.fullmatch(name) and name not in seen,
            "invalid/duplicate check id",
        )
        seen.add(name)
        kind = check["kind"]
        choice(kind, ("uart", "pwm", "i2c", "spi"), "check kind")
        validate_configuration(kind, check["configuration"])
        validate_expected(kind, check["configuration"], check["expect"])


def decode(capture, ref, kind, config):
    channels = validate_configuration(kind, config)
    signals, base = logic.channels_from_capture(capture, ref, channels)
    options = dict(config)
    if kind in ("uart", "pwm"):
        channel = options.pop("channel")
        base.pop("channels")
        base["channel"] = channel
        decoder = logic.uart_samples if kind == "uart" else logic.pwm_samples
        return decoder(signals[channel], base, **options)
    decoder = bus_logic.i2c_samples if kind == "i2c" else bus_logic.spi_samples
    return decoder(signals, base, **options)


def assertion(name, expected, actual):
    return {
        "name": name,
        "expected": expected,
        "actual": actual,
        "status": "passed" if actual == expected else "failed",
    }


def compare(kind, native, expect):
    analysis = native["analysis"]
    if not native["ok"]:
        return [
            {"name": "source_and_decode", "status": "failed", "actual": native["status"]},
            {
                "name": "expectation",
                "status": "not_evaluated",
                "reason": "source or decoding failed",
            },
        ]
    results = [{"name": "source_and_decode", "status": "passed", "actual": native["status"]}]
    if kind == "uart":
        actual = base64.b64decode(analysis["bytes_base64"], validate=True).hex()
        results.append(assertion("bytes_exact", expect["bytes_hex"], actual))
    elif kind == "pwm":
        cycles = analysis["cycles"]
        results.append(
            {
                "name": "minimum_cycles",
                "minimum": expect["minimum_cycles"],
                "actual": len(cycles),
                "status": "passed" if len(cycles) >= expect["minimum_cycles"] else "failed",
            }
        )
        for metric in ("frequency_hz", "duty_cycle"):
            limits = expect[metric]
            outliers = [
                {"cycle_index": index, "value": c[metric], "raw_span": c["raw_span"]}
                for index, c in enumerate(cycles)
                if not limits["minimum"] <= c[metric] <= limits["maximum"]
            ]
            results.append(
                {
                    "name": "all_cycles_" + metric,
                    "bounds": limits,
                    "cycles_checked": len(cycles),
                    "outliers": outliers,
                    "status": "failed" if outliers else "passed",
                }
            )
    else:
        transfers = analysis["transfers"]
        if kind == "i2c":
            actual = [
                {
                    key: t[key]
                    for key in ("address", "direction", "start_condition", "end_condition")
                }
                | {
                    "data": [b["value"] for b in t["bytes"][1:]],
                    "acks": [b["ack"] for b in t["bytes"]],
                }
                for t in transfers
            ]
        else:
            directions = ("mosi", "miso")
            actual = [
                {
                    key: [w["values"][key] for w in t["words"]]
                    for key in directions
                    if key in t["words"][0]["values"]
                }
                for t in transfers
            ]
        results.append(assertion("transfers_exact", expect["transfers"], actual))
    return results


def evaluate(spec_path, spec_sha256, capture_path, capture_sha256):
    raw_spec, spec_ref = read(spec_path, maximum=65536, expected=spec_sha256)
    spec = parse(raw_spec)
    # Reject all invalid contracts before reading any captured data.
    validate(spec)
    raw_capture, capture_ref = read(capture_path, expected=capture_sha256)
    capture = parse(raw_capture)
    checks = []
    for check in spec["checks"]:
        native = decode(capture, capture_ref, check["kind"], check["configuration"])
        assertions = compare(check["kind"], native, check["expect"])
        passed = all(a["status"] == "passed" for a in assertions)
        checks.append(
            {
                **check,
                "native": native,
                "native_sha256": digest(encode(native)),
                "assertions": assertions,
                "ok": passed,
                "status": "passed"
                if passed
                else ("failed" if native["ok"] else "invalid_evidence"),
            }
        )
    passed = all(check["ok"] for check in checks)
    report = {
        "schema_version": "lab-instruments.logic-check-report.v1",
        "runner_version": __version__,
        "scope": "host_only_saved_logic_evaluation",
        "spec": spec_ref,
        "capture": capture_ref,
        "ok": passed,
        "status": "passed" if passed else "failed",
        "complete": True,
        "checks": checks,
        "summary": {
            "checks": len(checks),
            "passed": sum(c["ok"] for c in checks),
            "failed": sum(not c["ok"] for c in checks),
        },
        "evidence_origin": capture["evidence_origin"],
        "hardware_access": False,
        "executables_started": False,
        "authorization_granted": False,
        "physical_device_verified": False,
        "physical_timing_verified": False,
        "calibration_verified": False,
        "protocol_timing_compliance_verified": False,
        "endpoint_identity_verified": False,
    }
    encode(report)  # Enforce the output bound for direct Python callers too.
    return report
