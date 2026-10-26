"""Bounded saved SDL measurements, raw command replay and verifiable assertions."""

from __future__ import annotations

import base64
import math
import re
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

from . import __version__, sdl
from .common import MAX_JSON_BYTES, digest, encode, integer, number, parse, read, require
from .logic_reports import bound_reference
from .scpi import QUERIES, WRITES, boolean, numeric

SPEC = "lab-instruments.load-check-spec.v1"
REPORT = "lab-instruments.load-check-report.v1"
PRODUCERS = ("0.10.0",)
QUANTITIES = ("voltage_v", "current_a", "power_w")
METRICS = tuple(
    f"{stat}_{quantity}" for quantity in QUANTITIES for stat in ("minimum", "maximum", "mean")
)
TIME = "recorded_host_query_windows_not_simultaneous_instrument_samples"
FLAGS = {
    name: False
    for name in (
        "hardware_access",
        "executables_started",
        "outputs_commanded",
        "authorization_granted",
        "physical_device_verified",
        "dut_identity_verified",
        "calibration_verified",
        "physical_timing_verified",
        "simultaneous_measurements_verified",
        "energy_measured",
        "report_authenticated",
        "recorded_paths_followed",
        "instrument_state_current_verified",
    )
}


def fields(value, keys, label):
    require(isinstance(value, dict) and set(value) == set(keys), f"invalid {label} fields")


def validate(spec):
    fields(spec, ("schema_version", "checks"), "load check spec")
    require(spec["schema_version"] == SPEC, "unsupported load check spec")
    checks = spec["checks"]
    require(isinstance(checks, list) and 1 <= len(checks) <= 8, "expected 1..8 checks")
    seen = set()
    for check in checks:
        fields(check, ("id", "phase", "window", "expect"), "load check")
        name = check["id"]
        require(
            type(name) is str and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", name),
            "invalid check id",
        )
        require(name not in seen, "duplicate check id")
        seen.add(name)
        require(check["phase"] in ("before_load", "loaded", "observe"), "unsupported load phase")
        window = check["window"]
        fields(window, ("start", "end_exclusive"), "sample window")
        integer(window["start"], "window start", 0, 49)
        integer(window["end_exclusive"], "window end", window["start"] + 1, 50)
        expect = check["expect"]
        require(
            isinstance(expect, dict) and bool(expect) and set(expect) <= {*METRICS, "input_on"},
            "unsupported/empty expectations",
        )
        for metric, bounds in expect.items():
            if metric == "input_on":
                require(type(bounds) is bool, "input state expectation must be boolean")
            else:
                fields(bounds, ("minimum", "maximum"), "metric bounds")
                number(bounds["minimum"], "metric minimum", 0, 1e30)
                number(bounds["maximum"], "metric maximum", bounds["minimum"], 1e30)


class Snapshot:
    """Exactly result, fixed sibling plan and transcript; each read once."""

    def __init__(self):
        self.files = {}

    def read(self, path, maximum=MAX_JSON_BYTES, expected=None):
        path = Path(path).absolute()
        if path not in self.files:
            require(len(self.files) < 3, "unexpected load source file")
            self.files[path] = read(path, maximum=maximum, expected=expected)
        raw, ref = self.files[path]
        require(len(raw) <= maximum, "source exceeds byte limit", "LIMIT_EXCEEDED")
        if expected is not None:
            require(ref["sha256"] == expected, "source SHA-256 differs", "HASH_MISMATCH")
        return raw, ref


def timestamp(value):
    require(type(value) is str and len(value) <= 64, "invalid recorded time")
    parsed = datetime.fromisoformat(value)
    require(parsed.utcoffset() is not None, "recorded time must include offset")
    return parsed


def records(raw):
    """Validate even partial journal framing; retain original byte spans."""
    lines = raw.splitlines(keepends=True)
    require(len(lines) <= 4000, "too many transcript records", "LIMIT_EXCEEDED")
    result, offset, previous = [], 0, 0
    for line in lines:
        require(line.endswith(b"\n"), "truncated transcript record")
        event = parse(line)
        kind = event.get("kind")
        extra = {
            "connect.begin": {"endpoint"},
            "connect.complete": set(),
            "command.begin": {"command", "cleanup"},
            "tx": set(),
            "rx": set(),
            "response.complete": {"command", "response"},
            "transaction.failed": {"command"},
            "socket.closed": set(),
        }
        require(type(kind) is str and kind in extra, "unknown transcript event")
        fields(
            event,
            {"kind", "recorded_at", "monotonic_s", "raw_base64", "bytes"} | extra[kind],
            "transcript event",
        )
        timestamp(event["recorded_at"])
        number(event["monotonic_s"], "recorded monotonic time", previous, 1e15)
        previous = event["monotonic_s"]
        integer(event["bytes"], "wire byte count", 0, 262144)
        require(type(event["raw_base64"]) is str, "invalid wire bytes")
        payload = base64.b64decode(event["raw_base64"], validate=True)
        require(len(payload) == event["bytes"], "wire byte count differs")
        if kind not in ("tx", "rx"):
            require(not payload, "metadata event contains wire bytes")
        else:
            require(bool(payload), "empty wire fragment")
        result.append((event, payload, offset, offset + len(line)))
        offset += len(line)
    return result


class Replay:
    """Finite, exact successful command stream; no socket, SDK or clocks used."""

    def __init__(self, raw, rows, plan):
        require(len(rows) >= 3, "incomplete successful journal")
        require(
            rows[0][0]["kind"] == "connect.begin" and rows[0][0]["endpoint"] == plan["endpoint"],
            "endpoint differs",
        )
        require(
            rows[1][0]["kind"] == "connect.complete" and rows[-1][0]["kind"] == "socket.closed",
            "connection lifecycle differs",
        )
        self.items, self.used = [], 0
        index = 2
        while index < len(rows) - 1:
            event, _, start, _ = rows[index]
            require(event["kind"] == "command.begin", "expected command begin")
            command, cleanup = event["command"], event["cleanup"]
            require(type(command) is str and type(cleanup) is bool, "invalid command metadata")
            require(
                command in QUERIES
                or command in WRITES
                or re.fullmatch(r":SOUR:CURR:LEV:IMM [0-5]\.\d{3}", command),
                "unsupported saved command",
            )
            require(not cleanup or command == ":SOUR:INP:STAT OFF", "invalid cleanup command")
            tx, rx, response = bytearray(), bytearray(), None
            first = index
            index += 1
            while index < len(rows) - 1 and rows[index][0]["kind"] not in (
                "command.begin",
                "socket.closed",
            ):
                part, data, _, _ = rows[index]
                require(response is None, "events after completed response")
                if part["kind"] == "tx":
                    require(not rx, "TX after RX")
                    tx.extend(data)
                elif part["kind"] == "rx":
                    require(command in QUERIES, "RX for write-only command")
                    rx.extend(data)
                    require(len(rx) <= 512, "saved response exceeds limit")
                else:
                    require(
                        part["kind"] == "response.complete" and part["command"] == command,
                        "failed/unknown successful transaction",
                    )
                    response = part["response"]
                    require(
                        type(response) is str
                        and bool(response)
                        and all(32 <= ord(c) <= 126 for c in response),
                        "invalid saved response",
                    )
                    require(
                        rx.endswith(b"\n")
                        and b"\n" not in rx[:-1]
                        and bytes(rx[:-1]).removesuffix(b"\r").decode("ascii") == response,
                        "raw response differs",
                    )
                index += 1
            require(tx == (command + "\n").encode("ascii"), "raw command differs")
            require((response is not None) is (command in QUERIES), "missing/unexpected response")
            stop = rows[index - 1][3]
            self.items.append(
                dict(
                    command=command,
                    cleanup=cleanup,
                    response=response,
                    begin=rows[first][0]["monotonic_s"],
                    end=rows[index - 1][0]["monotonic_s"],
                    wire=dict(
                        transaction=len(self.items),
                        command=command,
                        transcript_span=dict(
                            start=start, end_exclusive=stop, sha256=digest(raw[start:stop])
                        ),
                        tx_sha256=digest(tx),
                        rx_base64=base64.b64encode(rx).decode("ascii"),
                    ),
                )
            )

    def take(self, command, cleanup=False):
        require(self.used < len(self.items), "missing planned transaction")
        item = self.items[self.used]
        self.used += 1
        require(
            item["command"] == command and item["cleanup"] is cleanup,
            "saved command sequence differs from plan",
        )
        return item

    def query(self, command):
        return self.take(command)["response"]

    def send(self, command):
        require(self.take(command)["response"] is None, "unexpected write response")


def checked_sample(sample, wire, plan, phase, index, state):
    keys = {
        "index",
        "phase",
        "host_started_monotonic_s",
        "host_finished_monotonic_s",
        "recorded_at",
        *QUANTITIES,
    }
    fields(sample, keys | ({"input_on"} if phase != "before_load" else set()), "measurement")
    require(
        type(sample["index"]) is int and sample["index"] == index and sample["phase"] == phase,
        "sample phase/index differs",
    )
    start = number(sample["host_started_monotonic_s"], "sample start", 0, 1e15)
    stop = number(sample["host_finished_monotonic_s"], "sample finish", start, 1e15)
    timestamp(sample["recorded_at"])
    if phase != "before_load":
        require(sample["input_on"] is state, "input-state metadata differs")
    provenance = []
    for quantity, command in zip(
        QUANTITIES, ("MEAS:VOLT:DC?", "MEAS:CURR:DC?", "MEAS:POW:DC?"), strict=True
    ):
        item = wire.take(command)
        value = numeric(item["response"])
        number(sample[quantity], "measurement", 0, 1e30)
        require(value == sample[quantity], "measurement differs from raw query")
        require(start <= item["begin"] <= item["end"] <= stop, "recorded query window differs")
        provenance.append(item["wire"])
    sdl.check_limits(sample, plan, loaded=phase == "loaded")
    return {**sample, "input_on": state, "raw_queries": provenance}


def replay_success(result, raw, rows):
    plan = result["plan"]
    wire = Replay(raw, rows, plan)
    observed = sdl.identity(wire.query("*IDN?"))
    require(encode(observed) == encode(result["identity"]), "identity differs")
    action = plan["action"]
    phases = {"before_load": [], "loaded": [], "observe": []}
    if action != "identify":
        require(
            observed["serial"] == plan["serial"]
            and observed["firmware_version"] == plan["firmware_version"]
            and result["instrument_identity_matched"] is True,
            "exact identity differs",
        )
    else:
        require(
            result["instrument_identity_matched"] is False, "identify cannot assert identity match"
        )
    require(
        result["configuration_attempted"] is (action == "cc")
        and result["input_enable_attempted"] is (action == "cc"),
        "recorded effects differ",
    )
    if action == "cc":
        preflight = result["preflight"]
        fields(
            preflight,
            (
                "input_on",
                "short_on",
                "external_mode",
                "external_input_on",
                "remote_sense_on",
                "baseline",
            ),
            "preflight",
        )
        states = {
            "input_on": boolean(wire.query(":SOUR:INP:STAT?")),
            "short_on": boolean(wire.query(":SOUR:SHOR:STAT?")),
            "external_mode": wire.query("EXT:MODE?"),
            "external_input_on": boolean(wire.query(":SOUR:EXT:INPUT:STAT?")),
            "remote_sense_on": boolean(wire.query("SYST:SENS:STAT?")),
        }
        require(
            encode(states) == encode({k: v for k, v in preflight.items() if k != "baseline"}),
            "preflight differs",
        )
        require(
            not states["input_on"]
            and not states["short_on"]
            and states["external_mode"] == "INT"
            and not states["external_input_on"]
            and not states["remote_sense_on"],
            "unsafe saved preflight",
        )
        phases["before_load"].append(
            checked_sample(preflight["baseline"], wire, plan, "before_load", -1, False)
        )
        require(
            encode(sdl.configure(wire, plan)) == encode(result["settings"]), "saved settings differ"
        )
        wire.send(":SOUR:INP:STAT ON")
        require(boolean(wire.query(":SOUR:INP:STAT?")), "input ON was not reported")
    if action in ("cc", "observe"):
        phase = "loaded" if action == "cc" else "observe"
        require(len(result["measurements"]) == plan["samples"], "sample count differs")
        for index, sample in enumerate(result["measurements"]):
            state = boolean(wire.query(":SOUR:INP:STAT?"))
            if action == "cc":
                require(
                    state and wire.query(":SOUR:FUNC?") == "CURRENT",
                    "loaded state/function differs",
                )
            phases[phase].append(checked_sample(sample, wire, plan, phase, index, state))
    else:
        require(result["measurements"] == [], "unexpected measurement phase")
    cleanup = result["cleanup"]
    owned = action in ("cc", "off")
    require(
        cleanup["off_attempted"] is owned
        and cleanup["off_transmitted"] is owned
        and cleanup["input_off_confirmed"] is owned,
        "recorded OFF evidence differs",
    )
    if owned:
        wire.take(":SOUR:INP:STAT OFF", cleanup=True)
        require(
            wire.query("*OPC?") == "1" and not boolean(wire.query(":SOUR:INP:STAT?")),
            "OFF completion/readback differs",
        )
    require(
        cleanup["instrument_state"] == ("input_off_reported" if owned else "not_established"),
        "cleanup state differs",
    )
    require(wire.used == len(wire.items), "unexpected trailing command")
    return phases


def source(path, expected):
    snapshot = Snapshot()
    raw, ref = snapshot.read(path, expected=expected)
    result = parse(raw)
    inspected = sdl.inspect_snapshot(result, ref, Path(ref["path"]).parent, reader=snapshot.read)
    plan_raw, _ = snapshot.read(Path(ref["path"]).parent / "plan.json", maximum=32768)
    require(encode(parse(plan_raw)) == encode(result["plan"]), "saved plan differs from result")
    for name in (
        "physical_device_verified",
        "exclusive_instrument_ownership_verified",
        "dut_identity_verified",
        "calibration_verified",
        "authorization_granted",
    ):
        require(result[name] is False, "unsupported source verification claim")
    require(
        type(result["hardware_access"]) is bool
        and (not result["plan"]["test_double"] or not result["hardware_access"]),
        "invalid recorded access flag",
    )
    for name in (
        "instrument_identity_matched",
        "input_enable_attempted",
        "configuration_attempted",
        "ok",
    ):
        require(type(result[name]) is bool, "invalid source boolean")
    require(
        result["time_semantics"] == "host_query_windows_not_simultaneous_instrument_samples",
        "source time semantics differ",
    )
    require(
        isinstance(result["measurements"], list) and len(result["measurements"]) <= 50,
        "invalid measurements",
    )
    cleanup = result["cleanup"]
    require(isinstance(cleanup, dict), "invalid cleanup")
    for name in (
        "off_attempted",
        "off_transmitted",
        "input_off_confirmed",
        "socket_closed",
        "local_lease_released",
        "complete",
    ):
        require(type(cleanup[name]) is bool, "invalid cleanup boolean")
    require(
        isinstance(cleanup["errors"], list)
        and len(cleanup["errors"]) <= 16
        and all(type(e) is str and len(e) <= 1024 for e in cleanup["errors"]),
        "invalid cleanup errors",
    )
    transcript, _ = snapshot.read(
        Path(ref["path"]).parent / "transcript.jsonl", expected=result["transcript"]["sha256"]
    )
    rows = records(transcript)
    if result["ok"]:
        require(
            cleanup["socket_closed"]
            and cleanup["local_lease_released"]
            and cleanup["complete"]
            and not cleanup["errors"]
            and "error" not in result,
            "incomplete successful source",
        )
        phases = replay_success(result, transcript, rows)
    else:
        error = result["error"]
        fields(error, ("code", "message"), "source error")
        require(
            type(error["code"]) is str
            and 0 < len(error["code"]) <= 512
            and type(error["message"]) is str
            and len(error["message"]) <= 512,
            "invalid source error",
        )
        phases = {"before_load": [], "loaded": [], "observe": []}
    info = dict(
        source_status=result["status"],
        source_ok=inspected["source_ok"],
        source_success_replayed=result["ok"],
        source_error=result.get("error"),
        recorded_cleanup=cleanup,
        recorded_identity=result.get("identity"),
        evidence_origin=result["evidence_origin"],
        action=result["plan"]["action"],
        recorded_effects={
            k: result[k]
            for k in ("hardware_access", "input_enable_attempted", "configuration_attempted")
        },
        time_semantics=TIME,
        source_measurements_simultaneous=False,
        failed_source_measurements_evaluated=False,
        recorded_partial_measurements=result["measurements"] if not result["ok"] else [],
    )
    return phases, info, ref, snapshot


def evaluate(spec_path, spec_sha256, capture_path, capture_sha256):
    raw, spec_ref = read(spec_path, maximum=65536, expected=spec_sha256)
    spec = parse(raw)
    validate(spec)
    phases, info, capture_ref, snapshot = source(capture_path, capture_sha256)
    return evaluate_snapshot(spec, spec_ref, phases, info, capture_ref, snapshot)


def evaluate_snapshot(
    spec, spec_ref, phases, info, capture_ref, snapshot, *, version=__version__, labels=None
):
    checks = []
    for check in spec["checks"]:
        samples = phases[check["phase"]]
        start, stop = check["window"]["start"], check["window"]["end_exclusive"]
        reason = (
            "source_failed"
            if not info["source_ok"]
            else "phase_unavailable"
            if not samples
            else "window_unavailable"
            if stop > len(samples)
            else None
        )
        if reason:
            checks.append(
                {
                    **check,
                    "status": "invalid_evidence",
                    "ok": False,
                    "reason": reason,
                    "metrics": None,
                    "assertions": [],
                    "measurements": [],
                }
            )
            continue
        selected = samples[start:stop]
        metrics = {}
        for quantity in QUANTITIES:
            values = [s[quantity] for s in selected]
            metrics.update(
                {
                    "minimum_" + quantity: min(values),
                    "maximum_" + quantity: max(values),
                    "mean_" + quantity: math.fsum(values) / len(values),
                }
            )
        assertions = []
        for metric, expected in sorted(check["expect"].items()):
            actual = [s["input_on"] for s in selected] if metric == "input_on" else metrics[metric]
            ok = (
                all(v is expected for v in actual)
                if metric == "input_on"
                else expected["minimum"] <= actual <= expected["maximum"]
            )
            assertions.append(dict(metric=metric, expected=expected, actual=actual, ok=ok))
        ok = all(a["ok"] for a in assertions)
        checks.append(
            {
                **check,
                "status": "passed" if ok else "failed",
                "ok": ok,
                "metrics": metrics,
                "assertions": assertions,
                "measurements": selected,
            }
        )
    ok = all(c["ok"] for c in checks)
    report = dict(
        schema_version=REPORT,
        runner_version=version,
        scope="host_only_saved_load_evaluation",
        spec=spec_ref,
        capture=capture_ref,
        source=info,
        input_files=[
            dict(
                name="capture" if str(p) == capture_ref["path"] else p.name,
                sha256=ref["sha256"],
                size_bytes=ref["size_bytes"],
            )
            for p, (_, ref) in snapshot.files.items()
        ],
        checks=checks,
        ok=ok,
        status="passed" if ok else "failed",
        complete=True,
        summary={
            "checks": len(checks),
            **{
                s: sum(c["status"] == s for c in checks)
                for s in ("passed", "failed", "invalid_evidence")
            },
        },
        **FLAGS,
    )
    if labels is not None:
        report["spec"], report["capture"] = labels
    encode(report)
    return report


def inspect_report(
    report_path, report_sha256, spec_path, spec_sha256, capture_path, capture_sha256
):
    raw, ref = read(report_path, expected=report_sha256)
    report = parse(raw)
    require(
        report.get("schema_version") == REPORT and report.get("runner_version") in PRODUCERS,
        "unsupported load check producer",
    )
    raw_spec, spec_ref = read(spec_path, maximum=65536, expected=spec_sha256)
    spec = parse(raw_spec)
    validate(spec)
    phases, info, capture_ref, snapshot = source(capture_path, capture_sha256)
    bindings = {
        "spec": bound_reference(report.get("spec"), spec_ref),
        "capture": bound_reference(report.get("capture"), capture_ref),
    }
    expected = evaluate_snapshot(
        spec,
        spec_ref,
        phases,
        info,
        capture_ref,
        snapshot,
        version=report["runner_version"],
        labels=(report["spec"], report["capture"]),
    )
    require(
        encode(report) == encode(expected),
        "load report differs from recomputation",
        "REPORT_MISMATCH",
    )
    verification = dict(
        schema_version="lab-instruments.load-check-verification.v1",
        ok=True,
        status="verified",
        complete=True,
        report=ref,
        bindings=bindings,
        producer_runner_version=report["runner_version"],
        verifier_version=__version__,
        verdict={k: report[k] for k in ("ok", "status", "summary")},
        scope="host_only_saved_load_verification",
        **FLAGS,
    )
    return report, verification


def junit_report(*args):
    report, verification = inspect_report(*args)
    counts = dict(
        tests=len(report["checks"]),
        failures=report["summary"]["failed"],
        errors=report["summary"]["invalid_evidence"],
        skipped=0,
    )
    suite = ET.Element(
        "testsuite", name="lab-instruments.load", **{k: str(v) for k, v in counts.items()}
    )
    props = ET.SubElement(suite, "properties")
    for name, value in sorted(
        {
            **FLAGS,
            "report_sha256": verification["report"]["sha256"],
            "capture_sha256": report["capture"]["sha256"],
            "spec_sha256": report["spec"]["sha256"],
            "producer_runner_version": report["runner_version"],
            "verifier_version": __version__,
            "time_semantics": TIME,
            "source_status": report["source"]["source_status"],
            "evidence_origin": report["source"]["evidence_origin"],
        }.items()
    ):
        ET.SubElement(
            props, "property", name=name, value=str(value).lower() if type(value) is bool else value
        )
    for check in report["checks"]:
        case = ET.SubElement(suite, "testcase", name=check["id"], classname="lab_instruments.load")
        if not check["ok"]:
            tag = "error" if check["status"] == "invalid_evidence" else "failure"
            ET.SubElement(case, tag, message=check["status"]).text = encode(check).decode("ascii")
    raw = ET.tostring(suite, encoding="utf-8", xml_declaration=True) + b"\n"
    require(len(raw) <= MAX_JSON_BYTES, "JUnit exceeds byte limit", "LIMIT_EXCEEDED")
    return raw, verification, counts
