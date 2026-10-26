"""Exact-identity bounded SDL1020X-E sessions; no reconnect or command retry."""

from __future__ import annotations

import base64
import math
import time
from pathlib import Path

from .capture import now
from .common import LabError, digest, encode, new_path, parse, read, require, write_new
from .lease import Lease
from .scpi import Tcp, boolean, numeric
from .sdl_plan import MODEL, validate


def load_plan(path, expected):
    raw, ref = read(path, maximum=32768, expected=expected)
    plan = parse(raw)
    validate(plan)
    return plan, raw, ref


def identity(text):
    fields = text.split(",")
    require(
        len(fields) == 4 and all(field and field == field.strip() for field in fields),
        "invalid IDN response",
        "PROTOCOL_ERROR",
    )
    require(
        fields[0].lower() == "siglent technologies" and fields[1] == MODEL,
        "IDN manufacturer/model mismatch",
        "IDENTITY_MISMATCH",
    )
    return dict(zip(("manufacturer", "model", "serial", "firmware_version"), fields, strict=True))


def measurement(connection, plan, phase, index):
    started = time.monotonic()
    voltage = numeric(connection.query("MEAS:VOLT:DC?"))
    current = numeric(connection.query("MEAS:CURR:DC?"))
    power = numeric(connection.query("MEAS:POW:DC?"))
    # Queries are separate instrument readings, not simultaneous ADC samples.
    return {
        "index": index,
        "phase": phase,
        "host_started_monotonic_s": started,
        "host_finished_monotonic_s": time.monotonic(),
        "recorded_at": now(),
        "voltage_v": voltage,
        "current_a": current,
        "power_w": power,
    }


def check_limits(sample, plan, *, loaded=False):
    limits = plan["limits"]
    require(
        limits["min_voltage_v"] <= sample["voltage_v"] <= limits["max_voltage_v"],
        "voltage outside task envelope",
        "MEASUREMENT_LIMIT",
    )
    require(
        0 <= sample["current_a"] <= limits["max_current_a"],
        "current outside task envelope",
        "MEASUREMENT_LIMIT",
    )
    require(
        0 <= sample["power_w"] <= limits["max_power_w"],
        "power outside task envelope",
        "MEASUREMENT_LIMIT",
    )
    if loaded:
        tolerance = max(0.002, plan["current_a"] * 0.02)
        require(
            abs(sample["current_a"] - plan["current_a"]) <= tolerance,
            "measured current differs from CC setpoint",
            "CURRENT_MISMATCH",
        )


def preflight(connection, plan):
    result = {"input_on": boolean(connection.query(":SOUR:INP:STAT?"))}
    require(
        not result["input_on"], "input is already ON; will not take over", "PRECONDITION_FAILED"
    )
    result["short_on"] = boolean(connection.query(":SOUR:SHOR:STAT?"))
    result["external_mode"] = connection.query("EXT:MODE?")
    result["external_input_on"] = boolean(connection.query(":SOUR:EXT:INPUT:STAT?"))
    result["remote_sense_on"] = boolean(connection.query("SYST:SENS:STAT?"))
    require(
        not result["short_on"]
        and result["external_mode"] == "INT"
        and not result["external_input_on"]
        and not result["remote_sense_on"],
        "short/external control/remote sense must already be disabled",
        "PRECONDITION_FAILED",
    )
    baseline = measurement(connection, plan, "before_load", -1)
    check_limits(baseline, plan)
    result["baseline"] = baseline
    return result


def configure(connection, plan):
    for command in (
        ":SOUR:FUNC CURRENT",
        ":SOUR:CURR:IRANG 5",
        ":SOUR:CURR:VRANG 36",
        f":SOUR:CURR:LEV:IMM {plan['current_a']:.3f}",
    ):
        connection.send(command)
        require(
            connection.query("*OPC?") == "1", "configuration did not complete", "READBACK_MISMATCH"
        )
    settings = {
        "function": connection.query(":SOUR:FUNC?"),
        "current_range_a": numeric(connection.query(":SOUR:CURR:IRANG?")),
        "voltage_range_v": numeric(connection.query(":SOUR:CURR:VRANG?")),
        "current_a": numeric(connection.query(":SOUR:CURR:LEV:IMM?")),
    }
    require(
        settings["function"] == "CURRENT"
        and settings["current_range_a"] == 5
        and settings["voltage_range_v"] == 36
        and abs(settings["current_a"] - plan["current_a"]) < 0.0000005,
        "CC settings readback differs",
        "READBACK_MISMATCH",
    )
    # Recheck controls before enabling: no clearing or overriding unsafe state.
    require(
        not boolean(connection.query(":SOUR:INP:STAT?"))
        and not boolean(connection.query(":SOUR:SHOR:STAT?"))
        and connection.query("EXT:MODE?") == "INT"
        and not boolean(connection.query(":SOUR:EXT:INPUT:STAT?"))
        and not boolean(connection.query("SYST:SENS:STAT?")),
        "preconditions changed before input ON",
        "PRECONDITION_FAILED",
    )
    return settings


def perform(plan, directory: Path, *, connector=Tcp):
    validate(plan)
    started = time.monotonic()
    main_deadline = started + plan["timeout_ms"] / 1000
    result = {
        "schema_version": "lab-instruments.load-result.v1",
        "plan": plan,
        "plan_sha256": digest(encode(plan)),
        "evidence_origin": "test_double" if plan["test_double"] else "scpi_tcp_session",
        "hardware_access": False,
        "physical_device_verified": False,
        "instrument_identity_matched": False,
        "exclusive_instrument_ownership_verified": False,
        "dut_identity_verified": False,
        "calibration_verified": False,
        "authorization_granted": False,
        "input_enable_attempted": False,
        "configuration_attempted": False,
        "measurements": [],
        "started_at": now(),
        "time_semantics": "host_query_windows_not_simultaneous_instrument_samples",
        "cleanup": {
            "off_attempted": False,
            "off_transmitted": False,
            "input_off_confirmed": False,
            "socket_closed": False,
            "local_lease_released": False,
            "instrument_state": "not_established",
            "errors": [],
        },
        "ok": False,
    }
    connection = None
    lease = None
    owns_control = False
    journal_error = None
    action = plan["action"]
    with (directory / "transcript.jsonl").open("xb") as transcript:

        def journal(event):
            nonlocal journal_error
            if journal_error is None:
                try:
                    transcript.write(encode(event))
                    transcript.flush()
                except OSError as error:
                    journal_error = str(error)[:512]
                    # The first error aborts the main operation. Subsequent
                    # cleanup communication must still attempt OFF without logging.
                    raise

        try:
            if action != "identify":
                lease = Lease(plan["serial"])
            connection = connector(plan["endpoint"], plan["io_timeout_ms"], main_deadline, journal)
            connection.connect()
            observed = identity(connection.query("*IDN?"))
            result["identity"] = observed
            if action != "identify":
                require(
                    observed["serial"] == plan["serial"]
                    and observed["firmware_version"] == plan["firmware_version"],
                    "IDN serial/firmware mismatch",
                    "IDENTITY_MISMATCH",
                )
                result["instrument_identity_matched"] = True
            if action == "off":
                owns_control = True
            elif action in ("cc", "observe"):
                if action == "cc":
                    result["preflight"] = preflight(connection, plan)
                    owns_control = True
                    result["configuration_attempted"] = True
                    result["settings"] = configure(connection, plan)
                    # Bound from the first byte of ON, including the ON query time.
                    on_started = time.monotonic()
                    active_deadline = on_started + plan["samples"] * plan["interval_ms"] / 1000
                    connection.deadline = min(main_deadline, active_deadline)
                    result["input_enable_attempted"] = True
                    result["input_enable_host_monotonic_s"] = on_started
                    connection.send(":SOUR:INP:STAT ON")
                    require(
                        boolean(connection.query(":SOUR:INP:STAT?")),
                        "input ON was not confirmed",
                        "READBACK_MISMATCH",
                    )
                    sample_start = time.monotonic() + 0.05
                else:
                    sample_start = time.monotonic()
                    active_deadline = main_deadline
                for index in range(plan["samples"]):
                    target = sample_start + index * plan["interval_ms"] / 1000
                    require(
                        target < connection.deadline, "sample schedule exceeds budget", "TIMEOUT"
                    )
                    time.sleep(max(0, target - time.monotonic()))
                    state = boolean(connection.query(":SOUR:INP:STAT?"))
                    if action == "cc":
                        require(
                            state and connection.query(":SOUR:FUNC?") == "CURRENT",
                            "input/function changed during CC run",
                            "READBACK_MISMATCH",
                        )
                    sample = measurement(
                        connection, plan, "loaded" if action == "cc" else "observe", index
                    )
                    sample["input_on"] = state
                    result["measurements"].append(sample)
                    check_limits(sample, plan, loaded=action == "cc")
                if action == "cc":
                    time.sleep(max(0, active_deadline - time.monotonic()))
            result["ok"] = True
        except (LabError, OSError, ValueError, UnicodeError, KeyboardInterrupt) as error:
            result["error"] = {
                "code": "CANCELLED"
                if isinstance(error, KeyboardInterrupt)
                else "TIMEOUT"
                if isinstance(error, TimeoutError)
                else getattr(error, "code", "TRANSPORT_ERROR"),
                "message": str(error)[:512],
            }
        finally:
            if connection is not None:
                result["hardware_access"] = connection.network_attempted and not plan["test_double"]
                if owns_control:
                    cleanup = result["cleanup"]
                    cleanup["instrument_state"] = "unknown"
                    connection.deadline = time.monotonic() + plan["cleanup_timeout_ms"] / 1000
                    cleanup["off_attempted"] = True
                    cleanup["off_attempt_host_monotonic_s"] = time.monotonic()
                    try:
                        connection.send(":SOUR:INP:STAT OFF", cleanup=True)
                        cleanup["off_transmitted"] = True
                        if connection.synchronized:
                            require(
                                connection.query("*OPC?") == "1",
                                "OFF did not complete",
                                "CLEANUP_INCOMPLETE",
                            )
                            require(
                                not boolean(connection.query(":SOUR:INP:STAT?")),
                                "input remains ON",
                                "CLEANUP_INCOMPLETE",
                            )
                            cleanup["input_off_confirmed"] = True
                            cleanup["instrument_state"] = "input_off_reported"
                    except (LabError, OSError, ValueError) as error:
                        cleanup["errors"].append(str(error)[:512])
                try:
                    connection.close()
                    result["cleanup"]["socket_closed"] = connection.closed
                except (LabError, OSError) as error:
                    result["cleanup"]["errors"].append(str(error)[:512])
            else:
                result["cleanup"]["socket_closed"] = True
            try:
                if lease is not None:
                    lease.close()
                result["cleanup"]["local_lease_released"] = True
            except OSError as error:
                result["cleanup"]["errors"].append(str(error)[:512])
            if journal_error:
                result["cleanup"]["errors"].append("evidence journal failed: " + journal_error)
            result["cleanup"]["complete"] = (
                result["cleanup"]["socket_closed"]
                and result["cleanup"]["local_lease_released"]
                and (not owns_control or result["cleanup"]["input_off_confirmed"])
                and not result["cleanup"]["errors"]
            )
            if not result["cleanup"]["complete"]:
                result["ok"] = False
                result.setdefault(
                    "error",
                    {
                        "code": "CLEANUP_INCOMPLETE",
                        "message": "input OFF or host cleanup was not confirmed",
                    },
                )
    result["finished_at"] = now()
    result["host_elapsed_ms"] = round((time.monotonic() - started) * 1000, 3)
    result["status"] = "completed" if result["ok"] else "failed"
    _, transcript_ref = read(directory / "transcript.jsonl")
    result["transcript"] = {k: transcript_ref[k] for k in ("sha256", "size_bytes")}
    write_new(directory / "load-result.json", encode(result))
    return result


def run(path, expected, output):
    plan, raw, ref = load_plan(path, expected)
    directory = new_path(output)
    directory.mkdir()
    write_new(directory / "plan.json", raw)
    result = perform(plan, directory)
    return result, {"plan_file": ref, "result_file": read(directory / "load-result.json")[1]}


def inspect(path, expected):
    raw, ref = read(path, expected=expected)
    result = parse(raw)
    return inspect_snapshot(result, ref, Path(path).parent)


def inspect_snapshot(result, ref, directory, *, reader=read):
    """Inspect caller-snapshotted bytes; recorded paths are never followed."""
    require(result["schema_version"] == "lab-instruments.load-result.v1", "unsupported load result")
    plan = result["plan"]
    validate(plan)
    require(result["plan_sha256"] == digest(encode(plan)), "embedded plan hash differs")
    require(
        result["evidence_origin"] == ("test_double" if plan["test_double"] else "scpi_tcp_session")
        and result["physical_device_verified"] is False
        and result["authorization_granted"] is False,
        "invalid evidence origin/boundary",
    )
    transcript, _ = reader(directory / "transcript.jsonl", expected=result["transcript"]["sha256"])
    require(len(transcript) == result["transcript"]["size_bytes"], "transcript size differs")
    records = [parse(line) for line in transcript.splitlines()]
    require(len(records) <= 4000, "too many transcript records")
    require(result["ok"] is (result["status"] == "completed"), "result status differs")
    if result["ok"]:
        responses = transcript_responses(records)
        require(responses and responses[0][0] == "*IDN?", "missing raw identity query")
        actual_identity = identity(responses[0][1])
        require(actual_identity == result["identity"], "identity differs from transcript")
        if plan["action"] != "identify":
            require(
                actual_identity["serial"] == plan["serial"]
                and actual_identity["firmware_version"] == plan["firmware_version"],
                "saved identity differs from plan",
            )
        require(
            result["cleanup"]["complete"] is True and not result["cleanup"]["errors"],
            "cleanup incomplete",
        )
        if plan["action"] != "identify":
            require(result["instrument_identity_matched"] is True, "identity not matched")
        if plan["action"] in ("cc", "off"):
            require(result["cleanup"]["input_off_confirmed"] is True, "input OFF not confirmed")
            require(
                responses[-2:] == [("*OPC?", "1"), (":SOUR:INP:STAT?", "0")]
                or responses[-2:] == [("*OPC?", "1"), (":SOUR:INP:STAT?", "OFF")],
                "raw OFF completion/readback missing",
            )
        if plan["action"] in ("cc", "observe"):
            native_measurements = [
                (cmd, numeric(text)) for cmd, text in responses if cmd.startswith("MEAS:")
            ]
            saved_samples = [result["preflight"]["baseline"]] if plan["action"] == "cc" else []
            saved_samples += result["measurements"]
            expected_measurements = [
                (cmd, sample[key])
                for sample in saved_samples
                for cmd, key in (
                    ("MEAS:VOLT:DC?", "voltage_v"),
                    ("MEAS:CURR:DC?", "current_a"),
                    ("MEAS:POW:DC?", "power_w"),
                )
            ]
            require(
                native_measurements == expected_measurements,
                "measurements differ from raw SCPI transcript",
            )
            require(len(result["measurements"]) == plan["samples"], "sample count differs")
            for sample in result["measurements"]:
                for key in ("voltage_v", "current_a", "power_w"):
                    require(
                        type(sample[key]) in (int, float) and math.isfinite(sample[key]),
                        "invalid measurement",
                    )
                check_limits(sample, plan, loaded=plan["action"] == "cc")
    return {
        "input": ref,
        "status": result["status"],
        "source_ok": result["ok"],
        "evidence_origin": result["evidence_origin"],
        "transcript_records": len(records),
        "physical_device_verified": False,
        "hardware_access": False,
        "scope": "saved_load_evidence_integrity",
    }


def transcript_responses(records):
    """Reconstruct complete command/reply bytes, not just derived JSON values."""
    responses = []
    command = None
    tx, rx = bytearray(), bytearray()
    cleanup = False
    for event in records:
        raw = base64.b64decode(event["raw_base64"], validate=True)
        require(len(raw) == event["bytes"], "transcript byte count differs")
        kind = event["kind"]
        if kind == "command.begin":
            command = event["command"]
            cleanup = event["cleanup"]
            tx.clear()
            rx.clear()
        elif kind == "tx":
            tx.extend(raw)
        elif kind == "rx":
            rx.extend(raw)
        elif kind == "response.complete":
            require(
                command == event["command"] and tx == (command + "\n").encode("ascii"),
                "transcript command bytes differ",
            )
            require(
                bytes(rx).removesuffix(b"\n").removesuffix(b"\r").decode("ascii")
                == event["response"]
                and rx.endswith(b"\n"),
                "transcript response bytes differ",
            )
            responses.append((command, event["response"]))
        elif kind == "transaction.failed":
            require(False, "successful result contains a failed transaction")
        require(not cleanup or command == ":SOUR:INP:STAT OFF", "unexpected cleanup command")
    return responses
