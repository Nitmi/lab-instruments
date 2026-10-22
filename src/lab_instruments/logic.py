"""Bounded offline analysis of saved, hash-bound AD3 digital samples."""

from __future__ import annotations

import base64
import math
import struct

from .capture import inspect_capture_value
from .common import integer, number, parse, read, require

MAX_RECORDS = 512


def load(path, sha256, channel):
    signals, base = load_channels(path, sha256, [channel])
    base.pop("channels")
    return signals[channel], {**base, "channel": channel}


def load_channels(path, sha256, channels):
    require(isinstance(channels, list) and 1 <= len(channels) <= 4, "invalid channel selection")
    for channel in channels:
        integer(channel, "DIO channel", 0, 15)
    require(len(channels) == len(set(channels)), "protocol channels must be distinct")
    raw, ref = read(path, expected=sha256)
    capture = parse(raw)
    return channels_from_capture(capture, ref, channels)


def channels_from_capture(capture, ref, channels):
    """Extract from one already-read snapshot; never follow saved file references."""
    require(isinstance(channels, list) and 1 <= len(channels) <= 4, "invalid channel selection")
    for channel in channels:
        integer(channel, "DIO channel", 0, 15)
    require(len(channels) == len(set(channels)), "protocol channels must be distinct")
    summary = inspect_capture_value(capture, ref)
    plan = capture["plan"]
    require(plan["instrument"] == "logic", "analysis requires a logic capture")
    require(set(channels) <= set(plan["channels"]), "channel was not selected in the capture plan")
    payload = capture.get("payload")
    require(isinstance(payload, dict), "capture has no complete digital payload")
    require(
        payload.get("time_semantics") == "sample_index_over_actual_rate_no_verified_utc_origin",
        "unsupported sample time semantics",
    )
    settings = capture.get("settings")
    require(isinstance(settings, dict), "missing saved acquisition readback")
    rate = number(settings.get("sample_rate_hz"), "actual sample rate", 1, 125_000_000)
    require(
        rate <= plan["sample_rate_hz"] * (1 + 1e-9)
        and type(settings.get("samples")) is int
        and settings["samples"] == plan["samples"]
        and settings.get("rate_policy") == "at_most_requested"
        and settings.get("trigger") == "immediate"
        and settings.get("channel_scales") == {},
        "saved readback differs from logic plan",
    )
    decoded = base64.b64decode(payload["raw_base64"], validate=True)
    words = struct.unpack("<" + "H" * plan["samples"], decoded)
    signals = {channel: [(word >> channel) & 1 for word in words] for channel in channels}
    base = {
        "schema_version": "lab-instruments.logic-analysis.v1",
        "capture": ref,
        "payload_sha256": summary["payload_sha256"],
        "evidence_origin": capture["evidence_origin"],
        "source_ok": capture["ok"],
        "source_status": capture["status"],
        "source_error": capture.get("error"),
        "source_cleanup": capture["cleanup"],
        "channels": channels,
        "samples": len(words),
        "sample_rate_hz": rate,
        "sample_rate_source": "saved_sdk_readback_not_independently_measured",
        "sample_interval_s": 1 / rate,
        "time_domain": "sample_relative_no_verified_utc_origin",
        "raw_span_unit": "decoded_u16le_payload_bytes_end_exclusive",
        "hardware_access": False,
        "executables_started": False,
        "source_files_rechecked": False,
        "physical_device_verified": False,
        "physical_timing_verified": False,
        "calibration_verified": False,
        "sample_zero_utc_verified": False,
    }
    return signals, base


def span(start, end):
    return {"start": 2 * max(0, start - 1), "end_exclusive": 2 * end}


def finish(base, analysis):
    return {
        **base,
        "ok": base["source_ok"] and analysis["status"] in ("decoded", "measured"),
        "status": analysis["status"] if base["source_ok"] else "source_failed",
        "analysis": analysis,
    }


def uart(path, sha256, channel, baud, *, data_bits=8, parity="none", stop_bits=1, inverted=False):
    bits, base = load(path, sha256, channel)
    return uart_samples(
        bits, base, baud, data_bits=data_bits, parity=parity, stop_bits=stop_bits, inverted=inverted
    )


def uart_samples(bits, base, baud, *, data_bits=8, parity="none", stop_bits=1, inverted=False):
    integer(baud, "baud", 50, 3_000_000)
    integer(data_bits, "data bits", 5, 8)
    require(parity in ("none", "even", "odd"), "unsupported parity")
    integer(stop_bits, "stop bits", 1, 2)
    require(type(inverted) is bool, "invalid inversion")
    if inverted:
        bits = [1 - bit for bit in bits]
    step = base["sample_rate_hz"] / baud
    require(step >= 8, "UART needs at least eight actual samples per configured bit")
    frame_bits = 1 + data_bits + (parity != "none") + stop_bits
    frames, issues = [], []
    if bits[0] == 0:
        issues.append({"code": "capture_starts_active", "sample_index": 0})
    # An edge lies between i-1 and i. Sample from that interval's midpoint,
    # rounded to the nearest sample; never infer the baud from the data.
    idle_run = 1 if bits[0] else 0
    available = 0
    for i in range(1, len(bits)):
        previous_idle = idle_run
        idle_run = idle_run + 1 if bits[i] else 0
        if not (bits[i - 1] == 1 and bits[i] == 0 and i >= available):
            continue
        if previous_idle < math.floor(step):
            issues.append({"code": "insufficient_idle_before_start", "sample_index": i})
            require(len(issues) + len(frames) <= MAX_RECORDS, "UART record limit", "LIMIT_EXCEEDED")
            continue
        centers = [math.floor(i + (bit + 0.5) * step) for bit in range(frame_bits)]
        # Conservative right edge: require the latest possible full stop end
        # to fit the sample window, not just the final stop midpoint.
        end = math.ceil(i + frame_bits * step)
        if end > len(bits):
            frames.append(
                {
                    "start_sample": i,
                    "end_sample_exclusive": len(bits),
                    "raw_span": span(i, len(bits)),
                    "value": None,
                    "valid": False,
                    "errors": ["incomplete_frame"],
                }
            )
            break
        sampled = [bits[index] for index in centers]
        errors = []
        value = None
        if sampled[0] != 0:
            errors.append("false_start")
            available = centers[0] + 1
        else:
            value = sum(sampled[bit + 1] << bit for bit in range(data_bits))
            if parity != "none":
                total = sum(sampled[1 : data_bits + 2]) % 2
                if total != (parity == "odd"):
                    errors.append("parity_error")
            if any(bit != 1 for bit in sampled[-stop_bits:]):
                errors.append("framing_error")
            # Resume after the last stop midpoint, allowing back-to-back
            # frames at noninteger sample/baud ratios without skipping edges.
            available = centers[-1] + 1
        frames.append(
            {
                "start_sample": i,
                "end_sample_exclusive": end,
                "start_time_s": (i - 0.5) / base["sample_rate_hz"],
                "center_samples": centers,
                "raw_span": span(i, end),
                "value": value,
                "valid": not errors,
                "errors": errors,
            }
        )
        require(len(issues) + len(frames) <= MAX_RECORDS, "UART record limit", "LIMIT_EXCEEDED")
    require(len(issues) + len(frames) <= MAX_RECORDS, "UART record limit", "LIMIT_EXCEEDED")
    complete = bool(frames) and not issues and all(frame["valid"] for frame in frames)
    analysis = {
        "kind": "uart",
        "profile": "uart-fixed-sampling-v1",
        "baud": baud,
        "data_bits": data_bits,
        "parity": parity,
        "stop_bits": stop_bits,
        "inverted": inverted,
        "samples_per_bit": step,
        "edge_convention": "midpoint_between_previous_and_first_active_sample",
        "sampling_policy": "nearest_sample_bit_centers_ties_to_later_sample",
        "baud_detected": False,
        "electrical_signal_integrity_verified": False,
        "status": "decoded"
        if complete
        else ("no_frames" if not frames and not issues else "invalid"),
        "frames": frames,
        "issues": issues,
        "valid_frames": sum(frame["valid"] for frame in frames),
        "byte_stream_complete": complete,
        # Never silently join the good bytes across a corrupt/partial frame.
        "bytes_base64": (
            base64.b64encode(bytes(frame["value"] for frame in frames)).decode("ascii")
            if complete
            else None
        ),
    }
    return finish(base, analysis)


def statistics(values):
    return {"minimum": min(values), "maximum": max(values), "mean": math.fsum(values) / len(values)}


def pwm(path, sha256, channel, *, active_level="high"):
    bits, base = load(path, sha256, channel)
    return pwm_samples(bits, base, active_level=active_level)


def pwm_samples(bits, base, *, active_level="high"):
    require(active_level in ("high", "low"), "unsupported active level")
    active = [bit == (active_level == "high") for bit in bits]
    starts = [i for i in range(1, len(bits)) if not active[i - 1] and active[i]]
    cycles = []
    for start, end in zip(starts, starts[1:], strict=False):
        fall = next(i for i in range(start + 1, end) if not active[i])
        period, width = end - start, fall - start
        rate = base["sample_rate_hz"]
        cycles.append(
            {
                "start_sample": start,
                "inactive_sample": fall,
                "next_start_sample": end,
                "raw_span": span(start, end + 1),
                "period_samples": period,
                "active_samples": width,
                "inactive_samples": period - width,
                "period_s": period / rate,
                "active_s": width / rate,
                "inactive_s": (period - width) / rate,
                "frequency_hz": rate / period,
                "duty_cycle": width / period,
            }
        )
        require(len(cycles) <= MAX_RECORDS, "PWM cycle limit", "LIMIT_EXCEEDED")
    measured = bool(cycles)
    analysis = {
        "kind": "pwm",
        "profile": "pwm-closed-cycles-v1",
        "active_level": active_level,
        "edge_convention": "first_sample_of_new_level",
        "timing_resolution_s": 1 / base["sample_rate_hz"],
        "interval_quantization_bound_samples": 1,
        "quantization_bound_excludes_clock_and_electrical_uncertainty": True,
        "status": "measured" if measured else "insufficient_cycles",
        "cycles": cycles,
        "cycle_count": len(cycles),
        "prefix_samples_excluded": starts[0] if measured else len(bits),
        "suffix_samples_excluded": len(bits) - starts[-1] if measured else 0,
        "boundary_cycles_excluded": True,
        "window_active_sample_fraction": sum(active) / len(active),
        "level_observation": (
            "mixed"
            if any(active) and not all(active)
            else ("active" if all(active) else "inactive")
        ),
        "statistics": (
            {
                key: statistics([cycle[key] for cycle in cycles])
                for key in ("period_s", "frequency_hz", "duty_cycle")
            }
            if measured
            else None
        ),
    }
    return finish(base, analysis)
