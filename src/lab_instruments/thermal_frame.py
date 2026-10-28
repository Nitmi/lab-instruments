"""Lepton 3.5 telemetry and TLinear interpretation, never infer units from Y16."""

from __future__ import annotations

import math
import struct

from .common import digest, require

# Fixed GET subset: unit name, Windows extension selector, returned bytes.
# RAD2 starts at register 31*4. Its GUID is pt1-lep-rad20000.
CONTROLS = {
    "serial": ("sys", 3, 8),
    "part": ("oem", 8, 32),
    "software": ("oem", 9, 8),
    "agc": ("agc", 1, 4),
    "radiometry": ("rad", 5, 4),
    "tlinear": ("rad2", 18, 4),
    "resolution": ("rad2", 19, 4),
    "auto_resolution": ("rad2", 20, 4),
    "flux": ("rad2", 17, 16),
    "telemetry": ("sys", 7, 4),
    "location": ("sys", 8, 4),
    "ffc_status": ("sys", 18, 4),
    "gain": ("sys", 19, 4),
    "shutter": ("sys", 15, 4),
}


def state(raw):
    require(set(raw) == set(CONTROLS), "missing thermal control readbacks")
    for name, (_, _, size) in CONTROLS.items():
        require(len(raw[name]) == size, "invalid control length: " + name)
    values = {key: struct.unpack("<I", data)[0] for key, data in raw.items() if len(data) == 4}
    require(
        values["agc"] == 0 and values["radiometry"] == 1 and values["tlinear"] == 1,
        "AGC off and radiometry/TLinear on are required",
        "RADIOMETRY_UNCONFIRMED",
    )
    require(
        values["resolution"] in (0, 1) and values["auto_resolution"] == 0,
        "fixed TLinear resolution required",
        "RADIOMETRY_UNCONFIRMED",
    )
    require(
        values["telemetry"] == 1
        and values["location"] == 1
        and values["ffc_status"] == 0
        and values["gain"] in (0, 1, 2),
        "footer telemetry and ready FFC status required",
        "RADIOMETRY_UNCONFIRMED",
    )
    require(values["shutter"] in (0, 1), "shutter is closed/unknown", "FRAME_NOT_READY")
    flux = struct.unpack("<8H", raw["flux"])
    require(
        all(82 <= flux[i] <= 8192 for i in (0, 2, 4)) and flux[6] + flux[2] <= 8192,
        "invalid radiometry flux parameters",
    )
    values["radiometric_parameters"] = {
        "scene_emissivity": flux[0] / 8192,
        "background_c": flux[1] / 100 - 273.15,
        "window_transmission": flux[2] / 8192,
        "window_c": flux[3] / 100 - 273.15,
        "atmosphere_transmission": flux[4] / 8192,
        "atmosphere_c": flux[5] / 100 - 273.15,
        "window_reflection": flux[6] / 8192,
        "reflection_c": flux[7] / 100 - 273.15,
    }
    return values


def telemetry(raw):
    require(len(raw) == 160 * 122 * 2, "160x122 tightly packed Y16 required", "FRAME_FORMAT_ERROR")
    words = struct.unpack_from("<320H", raw, 160 * 120 * 2)
    require(words[0] not in (0, 65535), "invalid telemetry revision", "FRAME_FORMAT_ERROR")
    # Multi-word telemetry values are least-significant word first in the UVC LE profile.
    status = words[3] | (words[4] << 16)
    values = {
        "revision": words[0],
        "uptime_ms": words[1] | (words[2] << 16),
        "frame_counter": words[20] | (words[21] << 16),
        "status_bits": status,
        "ffc_state": (status >> 4) & 3,
        "ffc_desired": bool(status & 8),
        "agc_on": bool(status & (1 << 12)),
        "shutter_lockout": bool(status & (1 << 15)),
        "overtemperature_imminent": bool(status & (1 << 20)),
        "fpa_c": words[24] / 100 - 273.15,
        "housing_c": words[26] / 100 - 273.15,
        "last_ffc_ms": words[30] | (words[31] << 16),
        "gain": words[165],
        "effective_gain": words[166],
        "gain_change_desired": words[167],
        "tlinear": words[208],
        "resolution": words[209],
    }
    require(
        values["ffc_state"] == 3
        and not any(
            values[k]
            for k in (
                "ffc_desired",
                "agc_on",
                "shutter_lockout",
                "overtemperature_imminent",
                "gain_change_desired",
            )
        ),
        "frame is not in a stable radiometric/FFC state",
        "FRAME_NOT_READY",
    )
    require(
        values["tlinear"] == 1
        and values["resolution"] in (0, 1)
        and values["gain"] in (0, 1, 2)
        and values["effective_gain"] in (0, 1),
        "frame radiometric telemetry is unsupported",
        "RADIOMETRY_UNCONFIRMED",
    )
    return values


def analyze(raw, controls, rois, *, include_csv=True):
    values = state(controls)
    telem = telemetry(raw)
    require(
        values["resolution"] == telem["resolution"]
        and values["gain"] == telem["gain"]
        and (telem["gain"] == 2 or telem["effective_gain"] == telem["gain"]),
        "telemetry/control readbacks disagree",
        "CAPTURE_CHANGED",
    )
    codes = struct.unpack_from("<19200H", raw)
    divisor = 100 if values["resolution"] == 1 else 10
    temperatures = [code / divisor - 273.15 for code in codes]
    maximum = 140 if telem["effective_gain"] == 0 else 400
    valid = [
        code not in (0, 65535) and -10 <= temperature <= maximum
        for code, temperature in zip(codes, temperatures, strict=True)
    ]
    summaries = []
    for roi in rois:
        indices = [
            y * 160 + x
            for y in range(roi["y"], roi["y"] + roi["height"])
            for x in range(roi["x"], roi["x"] + roi["width"])
        ]
        accepted = [i for i in indices if valid[i]]
        require(accepted, "ROI has no usable temperature pixels", "NO_VALID_PIXELS")
        ordered = sorted(temperatures[i] for i in accepted)
        hot = max(accepted, key=temperatures.__getitem__)
        summaries.append(
            {
                "name": roi["name"],
                "valid_pixels": len(accepted),
                "invalid_or_out_of_range_pixels": len(indices) - len(accepted),
                "min_c": ordered[0],
                "max_c": ordered[-1],
                "mean_c": math.fsum(ordered) / len(ordered),
                "p95_c": ordered[math.ceil(0.95 * len(ordered)) - 1],
                "hotspot": {"x": hot % 160, "y": hot // 160},
                "small_roi": len(indices) < 10,
                "valid_mask_sha256": digest(bytes(int(valid[i]) for i in indices)),
            }
        )
    csv = (
        "x,y,tlinear_code,apparent_surface_c,valid\n"
        + "".join(
            f"{i % 160},{i // 160},{code},{temp:.6f},{int(ok)}\n"
            for i, (code, temp, ok) in enumerate(zip(codes, temperatures, valid, strict=True))
        )
        if include_csv
        else ""
    )
    return {"telemetry": telem, "controls": values, "rois": summaries}, csv.encode("ascii")


def trends(frames):
    unique, duplicate = [], 0
    for frame in frames:
        if (
            unique
            and frame["telemetry"]["frame_counter"] == unique[-1]["telemetry"]["frame_counter"]
        ):
            require(
                frame["raw_sha256"] == unique[-1]["raw_sha256"],
                "duplicate counter has different bytes",
                "CAPTURE_CHANGED",
            )
            duplicate += 1
            continue
        if unique:
            prev, current = unique[-1]["telemetry"], frame["telemetry"]
            require(
                current["frame_counter"] > prev["frame_counter"]
                and current["uptime_ms"] > prev["uptime_ms"],
                "camera counter reset/wrap/reversal is unsupported",
                "CAPTURE_CHANGED",
            )
        unique.append(frame)
    compatible = (
        len(unique) > 1
        and len(
            {
                (
                    f["telemetry"]["last_ffc_ms"],
                    f["telemetry"]["effective_gain"],
                    f["telemetry"]["resolution"],
                )
                for f in unique
            }
        )
        == 1
    )
    result = {
        "unique_frames": len(unique),
        "duplicate_buffers": duplicate,
        "temperature_trend_supported": compatible,
        "roi_changes": [],
    }
    compatible = (
        compatible
        and len({tuple(roi["valid_mask_sha256"] for roi in f["rois"]) for f in unique}) == 1
    )
    result["temperature_trend_supported"] = compatible
    if compatible:
        elapsed = (
            unique[-1]["telemetry"]["uptime_ms"] - unique[0]["telemetry"]["uptime_ms"]
        ) / 1000
        for before, after in zip(unique[0]["rois"], unique[-1]["rois"], strict=True):
            result["roi_changes"].append(
                {
                    "name": before["name"],
                    "elapsed_sensor_s": elapsed,
                    "mean_delta_c": after["mean_c"] - before["mean_c"],
                    "max_delta_c": after["max_c"] - before["max_c"],
                }
            )
    return result
