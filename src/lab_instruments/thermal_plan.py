"""Offline PureThermal 3 / radiometric Lepton 3.5 request validation."""

from __future__ import annotations

import re

from .common import encode, integer, number, require

LINK = re.compile(r"\\\\\?\\usb#vid_1e4e&pid_0100[^\r\n\x00]{1,480}\Z", re.IGNORECASE)
HEX8 = re.compile(r"[0-9a-f]{16}\Z")
MODEL = "PureThermal 3 + Lepton 3.5"
PART = "500-0771-01"
PROFILE = "pt-y16-160x122-footer-lepton35-rev400"


def create(
    link=None,
    serial=None,
    software=None,
    *,
    action="capture",
    buffers=3,
    rois=None,
    distance_m=None,
    ambient_c=None,
    timeout_ms=15000,
    test_double=False,
):
    value = {
        "schema_version": "lab-instruments.thermal-plan.v1",
        "model": MODEL,
        "profile": PROFILE,
        "link": link,
        "lepton_serial_hex": serial,
        "lepton_software_hex": software,
        "lepton_part": PART,
        "action": action,
        "buffers": buffers,
        "rois": rois
        if rois is not None
        else [{"name": "frame", "x": 0, "y": 0, "width": 160, "height": 120}],
        "context": {
            "distance_m": distance_m,
            "ambient_c": ambient_c,
            "user_context_verified": False,
            "calibration_verified": False,
        },
        "timeout_ms": timeout_ms,
        "test_double": test_double,
        "effects": {
            "enumerate_camera_interfaces": True,
            "open_exact_camera": action != "devices",
            "uvc_get_controls": action != "devices",
            "start_y16_stream": action == "capture",
            "firmware_may_change_agc_telemetry_format": action == "capture",
            "explicit_uvc_set_or_ffc": False,
            "settings_restored": False,
        },
        "authorization_granted": False,
        "hardware_identity_verified": False,
        "retries": 0,
    }
    validate(value)
    return value


def validate(value):
    require(
        isinstance(value, dict)
        and set(value)
        == {
            "schema_version",
            "model",
            "profile",
            "link",
            "lepton_serial_hex",
            "lepton_software_hex",
            "lepton_part",
            "action",
            "buffers",
            "rois",
            "context",
            "timeout_ms",
            "test_double",
            "effects",
            "authorization_granted",
            "hardware_identity_verified",
            "retries",
        },
        "invalid thermal plan fields",
    )
    require(
        value["schema_version"] == "lab-instruments.thermal-plan.v1"
        and value["model"] == MODEL
        and value["profile"] == PROFILE
        and value["lepton_part"] == PART,
        "unsupported thermal model/profile",
    )
    require(value["action"] in ("devices", "identify", "capture"), "unsupported thermal action")
    require(type(value["test_double"]) is bool, "invalid test-double flag")
    if value["action"] == "devices":
        require(value["link"] is None, "devices has no selected interface")
    else:
        require(
            isinstance(value["link"], str)
            and (
                value["link"] == "SYNTHETIC-PT3"
                if value["test_double"]
                else LINK.fullmatch(value["link"])
            ),
            "exact PureThermal symbolic link required",
        )
    for name in ("lepton_serial_hex", "lepton_software_hex"):
        if value["action"] == "capture":
            require(
                isinstance(value[name], str) and HEX8.fullmatch(value[name]),
                "exact Lepton identity required",
            )
        else:
            require(value[name] is None, "discovery cannot assert verified identity")
    integer(value["buffers"], "UVC buffer count", 1, 8)
    integer(value["timeout_ms"], "total capture timeout", 5000, 60000)
    rois = value["rois"]
    require(isinstance(rois, list) and 1 <= len(rois) <= 8, "select 1-8 ROIs")
    names = set()
    for roi in rois:
        require(
            isinstance(roi, dict) and set(roi) == {"name", "x", "y", "width", "height"},
            "invalid ROI",
        )
        require(
            isinstance(roi["name"], str)
            and re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,31}", roi["name"])
            and roi["name"] not in names,
            "invalid/duplicate ROI name",
        )
        names.add(roi["name"])
        for key, low, high in (("x", 0, 159), ("y", 0, 119), ("width", 1, 160), ("height", 1, 120)):
            integer(roi[key], "ROI " + key, low, high)
        require(
            roi["x"] + roi["width"] <= 160 and roi["y"] + roi["height"] <= 120, "ROI exceeds frame"
        )
    context = value["context"]
    require(
        isinstance(context, dict)
        and set(context)
        == {"distance_m", "ambient_c", "user_context_verified", "calibration_verified"},
        "invalid context",
    )
    if context["distance_m"] is not None:
        number(context["distance_m"], "distance", 0.01, 100)
    if context["ambient_c"] is not None:
        number(context["ambient_c"], "ambient temperature", -40, 80)
    require(
        context["user_context_verified"] is False and context["calibration_verified"] is False,
        "context cannot assert calibration",
    )
    effects = {
        "enumerate_camera_interfaces": True,
        "open_exact_camera": value["action"] != "devices",
        "uvc_get_controls": value["action"] != "devices",
        "start_y16_stream": value["action"] == "capture",
        "firmware_may_change_agc_telemetry_format": value["action"] == "capture",
        "explicit_uvc_set_or_ffc": False,
        "settings_restored": False,
    }
    require(encode(effects) == encode(value["effects"]), "thermal effects differ")
    require(
        value["authorization_granted"] is False
        and value["hardware_identity_verified"] is False
        and type(value["retries"]) is int
        and value["retries"] == 0,
        "invalid permission/retry claims",
    )
