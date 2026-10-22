"""Offline, sample-qualified I2C/SPI views of one saved logic capture."""

from __future__ import annotations

import base64

from .common import integer, require
from .logic import MAX_RECORDS, finish, load_channels, span


class Records:
    def __init__(self):
        self.count = 0

    def add(self):
        self.count += 1
        require(self.count <= MAX_RECORDS, "bus analysis record limit", "LIMIT_EXCEEDED")

    def issue(self, target, code, index):
        self.add()
        target.append({"code": code, "sample_index": index})


def _base(base, mapping):
    return {
        **base,
        "channel_map": mapping,
        "electrical_signal_integrity_verified": False,
        "protocol_timing_compliance_verified": False,
        "endpoint_identity_verified": False,
    }


def i2c(path, sha256, scl, sda, *, min_phase_samples=2):
    integer(min_phase_samples, "minimum phase samples", 2, 64)
    signals, base = load_channels(path, sha256, [scl, sda])
    clock, data = signals[scl], signals[sda]
    base = _base(base, {"scl": scl, "sda": sda})
    records = Records()
    transfers, issues = [], []
    current = None
    pending = []
    high_start = 0 if clock[0] else None
    last_edge = 0
    if (clock[0], data[0]) != (1, 1):
        records.issue(issues, "capture_starts_busy", 0)

    def end(index, condition):
        nonlocal current, pending
        # A STOP/repeated START needs SCL high. The final rise after a
        # completed ACK may be delimiter setup, not an additional data bit.
        setup = (
            condition in ("stop", "repeated_start")
            and current["bytes"]
            and len(pending) == 1
            and pending[0][0] == high_start
            and pending[0][1] == (condition == "repeated_start")
        )
        current["boundary_setup_clock"] = (
            {"sample_index": pending[0][0], "level": pending[0][1]} if setup else None
        )
        if pending and not setup:
            records.issue(current["errors"], "partial_byte_or_ack", pending[0][0])
            current["partial_bits"] = [{"sample_index": i, "value": v} for i, v in pending]
        if not current["bytes"]:
            records.issue(current["errors"], "missing_address", index)
        if condition == "capture_end":
            records.issue(current["errors"], "missing_stop_or_repeated_start", index)
        current.update(
            end_condition=condition,
            end_sample_exclusive=index if condition == "capture_end" else index + 1,
            raw_span=span(
                current["start_sample"], index if condition == "capture_end" else index + 1
            ),
        )
        current["valid"] = not current["errors"]
        transfers.append(current)
        current, pending = None, []

    for i in range(1, len(clock)):
        clk_change = clock[i] != clock[i - 1]
        data_change = data[i] != data[i - 1]
        if clk_change and data_change:
            records.issue(current["errors"] if current else issues, "simultaneous_scl_sda_edge", i)
        if data_change and clock[i] == clock[i - 1] == 1:
            condition = "start" if data[i] == 0 else "stop"
            if i - high_start < min_phase_samples:
                records.issue(
                    current["errors"] if current else issues, "undersampled_delimiter_phase", i
                )
            if condition == "start":
                repeated = current is not None
                if repeated:
                    end(i, "repeated_start")
                records.add()
                current = {
                    "start_sample": i,
                    "start_time_s": i / base["sample_rate_hz"],
                    "start_condition": "repeated_start" if repeated else "start",
                    "address": None,
                    "direction": None,
                    "bytes": [],
                    "errors": [],
                }
                pending = []
            elif current:
                end(i, "stop")
            else:
                records.issue(issues, "stop_without_start", i)
        if not clk_change:
            continue
        if (
            current
            and not current["bytes"]
            and not pending
            and i - current["start_sample"] < min_phase_samples
        ):
            records.issue(current["errors"], "undersampled_start_hold", i)
        if current and i - last_edge < min_phase_samples:
            records.issue(current["errors"], "undersampled_clock_phase", i)
        last_edge = i
        if clock[i] == 1:
            high_start = i
            if current:
                pending.append((i, data[i]))
                if len(pending) == 9:
                    records.add()
                    value = sum(bit << (7 - j) for j, (_, bit) in enumerate(pending[:8]))
                    address_byte = not current["bytes"]
                    ack = pending[8][1] == 0
                    if address_byte:
                        current["address"] = value >> 1
                        current["direction"] = "read" if value & 1 else "write"
                        if not 0x08 <= value >> 1 <= 0x77:
                            records.issue(
                                current["errors"],
                                "unsupported_reserved_or_10bit_address",
                                pending[0][0],
                            )
                    elif current["bytes"][-1]["ack"] is False:
                        records.issue(current["errors"], "data_after_nack", pending[0][0])
                    current["bytes"].append(
                        {
                            "role": "address" if address_byte else "data",
                            "value": value,
                            "ack": ack,
                            "ack_receiver_role": "target"
                            if address_byte or current["direction"] == "write"
                            else "controller",
                            "sample_indices": [index for index, _ in pending],
                            "raw_span": span(pending[0][0], i + 1),
                        }
                    )
                    pending = []
            else:
                records.issue(issues, "clock_without_observed_start", i)
        elif high_start is not None:
            high_start = None
    if current:
        end(len(clock), "capture_end")
    # A repeated START only closes a segment. A complete capture must also
    # contain the final STOP; partial final segments invalidate the whole view.
    valid = bool(transfers) and not issues and all(t["valid"] for t in transfers)
    analysis = {
        "kind": "i2c",
        "profile": "i2c-7bit-sampled-v1",
        "min_phase_samples": min_phase_samples,
        "data_sampling": "scl_rising_edge_first_high_sample",
        "address_range": "ordinary_7bit_0x08_to_0x77",
        "status": "decoded"
        if valid
        else ("no_transfers" if not transfers and not issues else "invalid"),
        "transfers": transfers,
        "issues": issues,
        "nack_count": sum(not b["ack"] for t in transfers for b in t["bytes"]),
        "nack_is_protocol_observation_not_application_success": True,
        "clock_stretch_cause_verified": False,
        "arbitration_verified": False,
    }
    return finish(base, analysis)


def spi(
    path,
    sha256,
    sck,
    cs,
    *,
    mosi=None,
    miso=None,
    mode=0,
    word_bits=8,
    bit_order="msb",
    cs_active="low",
    min_phase_samples=2,
):
    integer(mode, "SPI mode", 0, 3)
    integer(word_bits, "word bits", 1, 32)
    integer(min_phase_samples, "minimum phase samples", 2, 64)
    require(bit_order in ("msb", "lsb"), "invalid SPI bit order")
    require(cs_active in ("low", "high"), "invalid chip-select polarity")
    directions = {k: v for k, v in (("mosi", mosi), ("miso", miso)) if v is not None}
    require(bool(directions), "select at least one SPI data channel")
    channels = [sck, cs, *directions.values()]
    signals, base = load_channels(path, sha256, channels)
    clock = signals[sck]
    selected = [level == (cs_active == "high") for level in signals[cs]]
    base = _base(base, {"sck": sck, "cs": cs, **directions})
    idle, phase = mode >> 1, mode & 1
    records = Records()
    transfers, issues = [], []
    current = None
    edge_count, last_edge = 0, 0
    sampled = []
    if selected[0]:
        records.issue(issues, "capture_starts_selected", 0)

    def close(index, capture_end=False):
        nonlocal current, sampled
        if edge_count % (2 * word_bits):
            records.issue(current["errors"], "partial_word_or_clock_cycle", index)
        if not current["words"]:
            records.issue(current["errors"], "no_complete_words", index)
        if capture_end:
            records.issue(current["errors"], "missing_cs_deassertion", index)
        elif index - last_edge < min_phase_samples:
            records.issue(current["errors"], "undersampled_cs_hold", index)
        if clock[index - 1] != idle:
            records.issue(current["errors"], "clock_not_idle_at_cs_end", index)
        current.update(
            end_sample_exclusive=index if capture_end else index + 1,
            end_condition="capture_end" if capture_end else "cs_deasserted",
            raw_span=span(current["start_sample"], index if capture_end else index + 1),
            clock_edges=edge_count,
            partial_sample_indices=[sample[0] for sample in sampled],
        )
        current["valid"] = not current["errors"]
        transfers.append(current)
        current, sampled = None, []

    for i in range(1, len(clock)):
        clk_change = clock[i] != clock[i - 1]
        cs_change = selected[i] != selected[i - 1]
        if cs_change:
            if selected[i]:
                records.add()
                current = {
                    "start_sample": i,
                    "start_time_s": i / base["sample_rate_hz"],
                    "words": [],
                    "errors": [],
                }
                edge_count, last_edge, sampled = 0, i, []
                if clock[i - 1] != idle or clock[i] != idle:
                    records.issue(current["errors"], "clock_not_idle_at_cs_start", i)
            if clk_change:
                records.issue(
                    current["errors"] if current else issues, "simultaneous_cs_clock_edge", i
                )
            if not selected[i]:
                if current:
                    close(i)
                else:
                    records.issue(issues, "cs_end_without_observed_start", i)
            continue
        if not current or not selected[i] or not clk_change:
            continue
        if i - last_edge < min_phase_samples:
            records.issue(current["errors"], "undersampled_clock_phase", i)
        last_edge = i
        leading = clock[i] != idle
        if leading != (edge_count % 2 == 0):
            records.issue(current["errors"], "unexpected_clock_edge", i)
        edge_count += 1
        sample_edge = leading if phase == 0 else not leading
        if sample_edge:
            values = {}
            for name, channel in directions.items():
                bits = signals[channel]
                values[name] = bits[i]
                if bits[i - 1] != bits[i] or (i + 1 < len(bits) and bits[i + 1] != bits[i]):
                    records.issue(current["errors"], "data_changes_near_sampling_edge_" + name, i)
            sampled.append((i, values))
        if edge_count % (2 * word_bits) == 0:
            records.add()
            values = {
                name: sum(
                    values[name] << (word_bits - 1 - j if bit_order == "msb" else j)
                    for j, (_, values) in enumerate(sampled)
                )
                for name in directions
            }
            current["words"].append(
                {
                    "values": values,
                    "sample_indices": [index for index, _ in sampled],
                    "raw_span": span(sampled[0][0], i + 1),
                }
            )
            sampled = []
    if current:
        close(len(clock), capture_end=True)
    valid = bool(transfers) and not issues and all(t["valid"] for t in transfers)
    analysis = {
        "kind": "spi",
        "profile": "spi-cs-framed-sampled-v1",
        "mode": mode,
        "cpol": idle,
        "cpha": phase,
        "word_bits": word_bits,
        "bit_order": bit_order,
        "cs_active": cs_active,
        "min_phase_samples": min_phase_samples,
        "sample_edge": "leading" if phase == 0 else "trailing",
        "status": "decoded"
        if valid
        else ("no_transfers" if not transfers and not issues else "invalid"),
        "transfers": transfers,
        "issues": issues,
        "bytes_base64": (
            {
                name: [
                    base64.b64encode(bytes(w["values"][name] for w in t["words"])).decode("ascii")
                    for t in transfers
                ]
                for name in directions
            }
            if valid and word_bits == 8
            else None
        ),
        "mode_detected": False,
    }
    return finish(base, analysis)
