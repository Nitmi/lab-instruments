"""EN11G pp.754-759, 838-840: WORD/LSB descriptor and engineering units."""

from __future__ import annotations

import math
import struct

from .common import require

# Use the manual's Python enumeration; the table's index 9 has a printing typo.
TIMEBASE = [200e-12, 500e-12] + [m * 10.0**e for e in range(-9, 3) for m in (1, 2, 5)] + [1000.0]


def descriptor(raw, channel, points):
    require(
        len(raw) == 346 and raw[:8] == b"WAVEDESC" and raw[16:23] == b"WAVEACE",
        "unsupported waveform descriptor",
        "PROTOCOL_ERROR",
    )

    def unpack(offset, fmt):
        return struct.unpack_from("<" + fmt, raw, offset)[0]

    require(
        unpack(32, "h") == 1 and unpack(34, "h") == 0 and unpack(36, "i") == 346,
        "descriptor is not WORD/LSB/346",
        "PROTOCOL_ERROR",
    )
    result = {
        "wave_bytes": unpack(60, "i"),
        "array_points": unpack(116, "i"),
        "first_point": unpack(132, "i"),
        "transfer_interval": unpack(136, "i"),
        "scale_without_probe_v": unpack(156, "f"),
        "offset_without_probe_v": unpack(160, "f"),
        "code_per_div": unpack(164, "f"),
        "adc_bits": unpack(172, "h"),
        "sample_interval_s": unpack(176, "f"),
        "trigger_delay_s": unpack(180, "d"),
        "timebase_index": unpack(324, "h"),
        "coupling_code": unpack(326, "h"),
        "probe_factor": unpack(328, "f"),
        "channel": unpack(344, "h") + 1,
    }
    require(
        all(not isinstance(v, float) or math.isfinite(v) for v in result.values()),
        "nonfinite descriptor",
        "PROTOCOL_ERROR",
    )
    require(
        result["channel"] == channel
        and result["first_point"] == 0
        and result["transfer_interval"] == 1
        and result["wave_bytes"] >= points * 2
        and points <= result["array_points"] <= 100_000_000
        and 8 < result["adc_bits"] <= 16
        and result["coupling_code"] in (0, 1, 2)
        and 0 <= result["timebase_index"] < len(TIMEBASE)
        and 0 < result["sample_interval_s"] <= 1000
        and 0 < result["scale_without_probe_v"] <= 100
        and 0 < result["code_per_div"] <= 32768
        and 0 < result["probe_factor"] <= 100000,
        "unsupported/inconsistent descriptor values",
        "PROTOCOL_ERROR",
    )
    result["timebase_s"] = TIMEBASE[result["timebase_index"]]
    return result


def decode(preamble, payload, channel, points):
    meta = descriptor(preamble, channel, points)
    require(
        len(payload) == points * 2,
        "waveform length differs from requested WORD points",
        "PROTOCOL_ERROR",
    )
    codes = struct.unpack("<" + "h" * points, payload)
    factor = meta["probe_factor"]
    # WORD data is signed and left aligned. Do not shift right by ADC bit count.
    voltage = [
        (
            code * meta["scale_without_probe_v"] / meta["code_per_div"]
            - meta["offset_without_probe_v"]
        )
        * factor
        for code in codes
    ]
    first = meta["trigger_delay_s"] - meta["timebase_s"] * 10 / 2
    times = [first + i * meta["sample_interval_s"] for i in range(points)]
    require(all(math.isfinite(x) for x in (*times, *voltage)), "nonfinite decoded waveform")
    summary = {
        "points": points,
        "min_v": min(voltage),
        "max_v": max(voltage),
        "mean_v": math.fsum(voltage) / points,
        "first_time_s": first,
        "last_time_s": times[-1],
    }
    csv = "index,time_s,voltage_v,adc_code\n" + "".join(
        f"{i},{t:.12g},{v:.12g},{c}\n"
        for i, (t, v, c) in enumerate(zip(times, voltage, codes, strict=True))
    )
    return meta, summary, csv.encode("ascii")
