"""SDS804X HD acquisition and offline replay of hash-bound native wire evidence."""

from __future__ import annotations

import math
import subprocess
import sys
import time
from pathlib import Path

from .capture import now
from .common import LabError, digest, encode, new_path, parse, read, require, write_new
from .lease import Lease
from .scpi import numeric
from .sds_plan import MODEL, validate
from .sds_wave import decode
from .sds_wire import Tcp, allowed, split_block


def load_plan(path, expected):
    raw, ref = read(path, maximum=32768, expected=expected)
    value = parse(raw)
    validate(value)
    return value, raw, ref


def identity(text):
    fields = text.split(",")
    require(
        len(fields) == 4 and all(f and f == f.strip() for f in fields),
        "invalid scope IDN",
        "PROTOCOL_ERROR",
    )
    require(
        fields[0].lower() == "siglent technologies" and fields[1] == MODEL,
        "scope manufacturer/model mismatch",
        "IDENTITY_MISMATCH",
    )
    return dict(zip(("manufacturer", "model", "serial", "firmware_version"), fields, strict=True))


def count(text):
    value = numeric(text)
    require(
        value == int(value) and 0 <= value <= 1e12, "invalid acquisition count", "PROTOCOL_ERROR"
    )
    return int(value)


def settings(c, channels):
    result = {}
    for ch in channels:
        values = {
            key: c.query(f":CHAN{ch}:{key}?")
            for key in ("SWIT", "COUP", "SCAL", "OFFS", "PROB", "UNIT")
        }
        require(
            values["SWIT"].upper() in ("ON", "1") and values["COUP"].upper() in ("AC", "DC"),
            "selected channels must already be enabled and AC/DC coupled",
            "PRECONDITION_FAILED",
        )
        require(numeric(values["SCAL"]) > 0, "invalid channel scale", "PROTOCOL_ERROR")
        numeric(values["OFFS"])
        require(
            numeric(values["PROB"]) > 0 and values["UNIT"].upper() == "V",
            "only voltage channels with a numeric probe factor are supported",
            "PRECONDITION_FAILED",
        )
        result[str(ch)] = values
    return result


def stopped(c):
    require(
        c.query(":TRIG:STAT?").upper() == "STOP", "scope must be stopped", "PRECONDITION_FAILED"
    )


def acquire(c, plan, result, save, *, replay=False):
    actual = identity(c.query("*IDN?"))
    result["identity"] = actual
    if plan["action"] == "identify":
        return
    require(
        actual["serial"] == plan["serial"]
        and actual["firmware_version"] == plan["firmware_version"],
        "scope serial/firmware mismatch",
        "IDENTITY_MISMATCH",
    )
    result["instrument_identity_matched"] = True
    stopped(c)
    require(
        c.query(":ACQ:MODE?").upper() == "YT"
        and c.query(":ACQ:TYPE?").upper() in ("NORM", "NORMAL")
        and c.query(":ACQ:SEQ?").upper() in ("OFF", "0"),
        "only normal YT non-sequence acquisition is supported",
        "PRECONDITION_FAILED",
    )
    initial_settings = settings(c, plan["channels"])
    result["channel_settings"] = initial_settings
    baseline = count(c.query(":ACQ:NUMAC?"))
    result["baseline_acquisition_count"] = baseline
    if plan["action"] == "single":
        # Set before send: a failed transmission may already have armed the instrument.
        result["single_attempted"] = True
        c.send(":TRIG:MODE SING")
        require(
            c.query(":TRIG:MODE?").upper() in ("SING", "SINGLE"),
            "single trigger readback differs",
            "READBACK_MISMATCH",
        )
        armed_seen = False
        for _ in range(400):
            state = c.query(":TRIG:STAT?").upper()
            require(
                state in ("ARM", "READY", "AUTO", "TRIG'D", "STOP", "ROLL"),
                "invalid trigger status",
                "PROTOCOL_ERROR",
            )
            current = count(c.query(":ACQ:NUMAC?"))
            armed_seen |= state in ("ARM", "READY", "TRIG'D")
            if state == "STOP":
                require(
                    current > 0 and (armed_seen or current != baseline),
                    "no distinct new acquisition evidence after SINGLE",
                    "FRESHNESS_UNCONFIRMED",
                )
                result["freshness"] = {
                    "basis": "single_and_stop_with_activity_or_counter_change",
                    "armed_seen": armed_seen,
                    "acquisition_count": current,
                }
                break
            if not replay:
                time.sleep(0.05)
        else:
            raise LabError("TIMEOUT", "single acquisition did not stop within poll budget")
    else:
        result["freshness"] = {"basis": "existing_stopped_waveform_age_unknown"}
    c.send(":WAV:WIDT WORD")
    c.send(":WAV:BYT LSB")
    c.send(":WAV:STAR 0")
    c.send(":WAV:INT 1")
    c.send(f":WAV:POIN {plan['points']}")
    for query, expected in (
        (":WAV:WIDT?", "WORD"),
        (":WAV:BYT?", "LSB"),
        (":WAV:STAR?", "0"),
        (":WAV:INT?", "1"),
        (":WAV:POIN?", str(plan["points"])),
    ):
        require(
            c.query(query).upper() == expected,
            "waveform transfer readback differs",
            "READBACK_MISMATCH",
        )
    require(
        count(c.query(":WAV:MAXP?")) >= plan["points"],
        "requested points exceed one transfer",
        "CAPABILITY_UNAVAILABLE",
    )
    result["waveforms"] = []
    capture_count = count(c.query(":ACQ:NUMAC?"))
    for ch in plan["channels"]:
        stopped(c)
        c.send(f":WAV:SOUR C{ch}")
        require(
            c.query(":WAV:SOUR?") == f"C{ch}",
            "waveform source readback differs",
            "READBACK_MISMATCH",
        )
        before = c.block(":WAV:PRE?", 346)
        payload = c.block(":WAV:DATA?", plan["points"] * 2)
        after = c.block(":WAV:PRE?", 346)
        require(before == after, "descriptor changed during waveform transfer", "CAPTURE_CHANGED")
        meta, summary, csv = decode(before, payload, ch, plan["points"])
        setting = initial_settings[str(ch)]
        require(
            all(
                math.isclose(numeric(setting[key]), expected, rel_tol=1e-5, abs_tol=1e-8)
                for key, expected in (
                    ("PROB", meta["probe_factor"]),
                    ("SCAL", meta["scale_without_probe_v"] * meta["probe_factor"]),
                    ("OFFS", meta["offset_without_probe_v"] * meta["probe_factor"]),
                )
            )
            and setting["COUP"].upper() == {0: "DC", 1: "AC", 2: "GND"}[meta["coupling_code"]],
            "descriptor scale/probe/coupling differs from channel readback",
            "CAPTURE_CHANGED",
        )
        files = {
            name: save(f"c{ch}-{name}.{extension}", raw)
            for name, extension, raw in (
                ("preamble", "bin", before),
                ("words", "bin", payload),
                ("samples", "csv", csv),
            )
        }
        result["waveforms"].append(
            {"channel": ch, "metadata": meta, "summary": summary, "files": files}
        )
        stopped(c)
        require(
            count(c.query(":ACQ:NUMAC?")) == capture_count,
            "acquisition count changed during transfer",
            "CAPTURE_CHANGED",
        )
    require(
        settings(c, plan["channels"]) == initial_settings,
        "channel settings changed during capture",
        "CAPTURE_CHANGED",
    )
    # Same frozen acquisition was requested; independent cross-channel timing is not calibrated.
    result["cross_channel_alignment_verified"] = False
    stopped(c)


def perform(plan, directory):
    validate(plan)
    started = time.monotonic()
    result = {
        "schema_version": "lab-instruments.scope-result.v1",
        "plan": plan,
        "plan_sha256": digest(encode(plan)),
        "started_at": now(),
        "ok": False,
        "evidence_origin": "test_double" if plan["test_double"] else "scpi_tcp_session",
        "hardware_access": False,
        "physical_device_verified": False,
        "dut_identity_verified": False,
        "calibration_verified": False,
        "exclusive_instrument_ownership_verified": False,
        "authorization_granted": False,
        "outputs_commanded": False,
        "instrument_identity_matched": False,
        "single_attempted": False,
        "cleanup": {
            "socket_closed": False,
            "local_lease_released": False,
            "stop_attempted": False,
            "stopped_confirmed": False,
            "settings_restored": False,
            "errors": [],
        },
    }
    c, lease = None, None
    write_new(directory / "transcript.jsonl", b"")

    def save(name, raw):
        ref = write_new(directory / name, raw)
        return {"name": name, "sha256": ref["sha256"], "size_bytes": ref["size_bytes"]}

    try:
        lease = Lease("SDS-" + (plan["serial"] or plan["endpoint"]["host"]))
        c = Tcp(
            plan["endpoint"], plan["io_timeout_ms"], started + plan["timeout_ms"] / 1000, directory
        )
        c.connect()
        acquire(c, plan, result, save)
        result["cleanup"]["stopped_confirmed"] = plan["action"] != "identify"
        result["ok"] = True
    except (LabError, OSError, ValueError, KeyboardInterrupt) as error:
        result["error"] = {
            "code": "CANCELLED"
            if isinstance(error, KeyboardInterrupt)
            else "TIMEOUT"
            if isinstance(error, TimeoutError)
            else getattr(error, "code", "HOST_OR_TRANSPORT_ERROR"),
            "message": str(error)[:512],
        }
    finally:
        cleanup = result["cleanup"]
        if c:
            result["hardware_access"] = c.attempted and not plan["test_double"]
            if result["single_attempted"] and not cleanup["stopped_confirmed"]:
                if c.synchronized:
                    c.deadline = time.monotonic() + plan["cleanup_timeout_ms"] / 1000
                    cleanup["stop_attempted"] = True
                    try:
                        c.send(":TRIG:STOP")
                        stopped(c)
                        cleanup["stopped_confirmed"] = True
                    except (LabError, OSError, ValueError) as error:
                        cleanup["errors"].append(str(error)[:512])
                else:
                    cleanup["errors"].append("desynchronized stream; no STOP or readback sent")
            try:
                c.close()
                cleanup["socket_closed"] = True
            except OSError as error:
                cleanup["errors"].append(str(error)[:512])
        else:
            cleanup["socket_closed"] = True
        if lease:
            lease.close()
        cleanup["local_lease_released"] = True
        cleanup["complete"] = (
            cleanup["socket_closed"]
            and cleanup["local_lease_released"]
            and not cleanup["errors"]
            and (not result["single_attempted"] or cleanup["stopped_confirmed"])
        )
        if not cleanup["complete"]:
            result["ok"] = False
    result["status"] = "completed" if result["ok"] else "failed"
    result["finished_at"] = now()
    result["host_elapsed_ms"] = round((time.monotonic() - started) * 1000, 3)
    _, ref = read(directory / "transcript.jsonl")
    result["transcript"] = {"sha256": ref["sha256"], "size_bytes": ref["size_bytes"]}
    write_new(directory / "scope-result.json", encode(result))
    return result


def run(path, expected, output):
    plan, raw, ref = load_plan(path, expected)
    directory = new_path(output)
    directory.mkdir()
    write_new(directory / "plan.json", raw)
    # A killed worker provides no instrument cleanup guarantee. Never reconnect to repair it.
    with (
        (directory / "worker.stdout").open("xb") as stdout,
        (directory / "worker.stderr").open("xb") as stderr,
    ):
        process = subprocess.Popen(
            [
                sys.executable,
                "-I",
                "-m",
                "lab_instruments._scope_worker",
                str(directory),
                digest(raw),
            ],
            stdout=stdout,
            stderr=stderr,
        )
        try:
            code = process.wait(timeout=plan["timeout_ms"] / 1000 + 8)
        except (subprocess.TimeoutExpired, KeyboardInterrupt):
            process.kill()
            process.wait(timeout=5)
            code = -1
    result_path = directory / "scope-result.json"
    if code != 0 or not result_path.exists():
        raise LabError(
            "WORKER_FAILED",
            f"scope worker exit {code}; partial evidence preserved; target state unknown",
        )
    result_raw, result_ref = read(result_path)
    return parse(result_raw), {"plan_file": ref, "result_file": result_ref}


class Replay:
    def __init__(self, directory, records, reader=read):
        self.directory, self.records, self.index = directory, records, 0
        self.read = reader

    def exchange(self, command, kind="line", maximum=512):
        require(self.index < len(self.records), "missing wire transaction")
        record = self.records[self.index]
        index = self.index
        self.index += 1
        allowed(command, kind)
        require(
            record["complete"] is True and record["command"] == command and record["kind"] == kind,
            "wire command/order differs",
        )
        data = {}
        for key in ("tx", "rx"):
            name = f"wire-{index:04d}-{key}.bin"
            require(record[key] == name, "invalid wire evidence name")
            raw, _ = self.read(self.directory / name, expected=record[key + "_sha256"])
            require(len(raw) == record[key + "_bytes"], "wire evidence size differs")
            data[key] = raw
        require(data["tx"] == (command + "\n").encode("ascii"), "TX bytes differ")
        if kind == "block":
            return split_block(data["rx"], maximum)
        if kind == "write":
            require(not data["rx"], "write unexpectedly has a reply")
            return None
        raw = data["rx"]
        require(
            len(raw) <= maximum and raw.endswith(b"\n") and b"\n" not in raw[:-1],
            "invalid saved SCPI line",
        )
        return raw[:-1].removesuffix(b"\r").decode("ascii")

    query = Tcp.query
    send = Tcp.send
    block = Tcp.block


def inspect(path, expected):
    raw, ref = read(path, expected=expected)
    result = parse(raw)
    return inspect_snapshot(result, ref, Path(path).parent)


def inspect_snapshot(result, ref, directory, *, reader=read, retained=None):
    """Replay explicitly supplied saved evidence; optionally retain verified raw waveforms."""
    require(
        result["schema_version"] == "lab-instruments.scope-result.v1", "unsupported scope result"
    )
    plan = result["plan"]
    validate(plan)
    require(result["plan_sha256"] == digest(encode(plan)), "embedded plan hash differs")
    require(
        result["evidence_origin"] == ("test_double" if plan["test_double"] else "scpi_tcp_session")
        and result["authorization_granted"] is False
        and result["physical_device_verified"] is False
        and result["dut_identity_verified"] is False
        and result["calibration_verified"] is False
        and result["exclusive_instrument_ownership_verified"] is False
        and result["outputs_commanded"] is False,
        "invalid evidence boundary",
    )
    plan_raw, _ = reader(directory / "plan.json")
    require(parse(plan_raw) == plan, "saved plan differs")
    transcript, _ = reader(directory / "transcript.jsonl", expected=result["transcript"]["sha256"])
    require(len(transcript) == result["transcript"]["size_bytes"], "transcript size differs")
    records = [parse(line) for line in transcript.splitlines()]
    require(len(records) <= 800, "too many scope wire records")
    # Even failed sessions bind every existing partial wire artifact.
    for index, record in enumerate(records):
        for key in ("tx", "rx"):
            require(record[key] == f"wire-{index:04d}-{key}.bin", "invalid wire file name")
            partial, _ = reader(directory / record[key], expected=record[key + "_sha256"])
            require(len(partial) == record[key + "_bytes"], "partial wire size differs")
    require(
        type(result["ok"]) is bool
        and result["status"] == ("completed" if result["ok"] else "failed"),
        "scope result status differs",
    )
    if result["ok"]:
        require(
            result["hardware_access"] is (not plan["test_double"]),
            "invalid hardware access boundary",
        )
        saved = {}

        def verify_file(name, payload):
            original, file_ref = reader(directory / name)
            require(original == payload, "derived waveform evidence differs from raw wire")
            if retained is not None and name.endswith(("-preamble.bin", "-words.bin")):
                retained[name] = (original, file_ref)
            return {"name": name, "sha256": file_ref["sha256"], "size_bytes": len(original)}

        replay = Replay(directory, records, reader)
        acquire(replay, plan, saved, verify_file, replay=True)
        require(replay.index == len(records), "unexpected extra wire transactions")
        require(
            all(result.get(key) == value for key, value in saved.items()),
            "saved interpretation differs from raw wire replay",
        )
        require(
            result["cleanup"]["complete"] is True
            and not result["cleanup"]["errors"]
            and result["cleanup"]["socket_closed"] is True
            and result["cleanup"]["local_lease_released"] is True,
            "scope cleanup incomplete",
        )
        if plan["action"] != "identify":
            require(result["cleanup"]["stopped_confirmed"] is True, "stopped readback missing")
    return {
        "input": ref,
        "status": result["status"],
        "source_ok": result["ok"],
        "evidence_origin": result["evidence_origin"],
        "hardware_access": False,
        "scope": "saved_scope_evidence_integrity",
        "wire_transactions": len(records),
        "physical_device_verified": False,
    }
