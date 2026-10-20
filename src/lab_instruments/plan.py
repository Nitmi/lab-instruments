from __future__ import annotations

from .common import SERIAL, integer, number, require

MODEL = "Digilent Analog Discovery 3"
DEVICE_ID = 10
SDK_VERSION = "3.25.1"
EFFECTS = {
    "sdk_device_open_and_initialization": True,
    "input_configuration": True,
    "input_acquisition": True,
    "owned_handle_close": True,
    "outputs_commanded": False,
    "supply_control": False,
    "dut_reset_or_flash": False,
}


def create(
    serial,
    instrument,
    channels,
    sample_rate_hz=1_000_000,
    samples=4096,
    range_v=10.0,
    timeout_ms=15000,
):
    value = {
        "schema_version": "lab-instruments.capture-plan.v1",
        "model": MODEL,
        "device_id": DEVICE_ID,
        "sdk_version": SDK_VERSION,
        "serial": serial,
        "instrument": instrument,
        "channels": channels,
        "sample_rate_hz": sample_rate_hz,
        "samples": samples,
        "range_v": range_v if instrument == "scope" else None,
        "timeout_ms": timeout_ms,
        "settle_ms": 2000 if instrument == "scope" else 0,
        "trigger": "immediate",
        "effects": EFFECTS.copy(),
        "authorization_granted": False,
        "hardware_identity_verified": False,
    }
    validate(value)
    return value


def validate(value):
    require(isinstance(value, dict), "invalid plan")
    require(
        set(value)
        == {
            "schema_version",
            "model",
            "device_id",
            "sdk_version",
            "serial",
            "instrument",
            "channels",
            "sample_rate_hz",
            "samples",
            "range_v",
            "timeout_ms",
            "settle_ms",
            "trigger",
            "effects",
            "authorization_granted",
            "hardware_identity_verified",
        },
        "unexpected/missing plan fields",
    )
    require(
        value["schema_version"] == "lab-instruments.capture-plan.v1"
        and value["model"] == MODEL
        and type(value["device_id"]) is int
        and value["device_id"] == DEVICE_ID
        and value["sdk_version"] == SDK_VERSION,
        "unsupported model, SDK or schema",
    )
    require(
        isinstance(value["serial"], str) and SERIAL.fullmatch(value["serial"]) is not None,
        "serial must be an exact alphanumeric SDK serial",
    )
    require(value["instrument"] in ("scope", "logic"), "unsupported instrument")
    channels = value["channels"]
    require(
        isinstance(channels, list)
        and 1 <= len(channels) <= 16
        and all(type(c) is int for c in channels)
        and len(set(channels)) == len(channels),
        "invalid channel list",
    )
    scope = value["instrument"] == "scope"
    require(set(channels) <= ({1, 2} if scope else set(range(16))), "channel outside model")
    integer(value["samples"], "samples", 16, 16384)
    number(value["sample_rate_hz"], "sample rate", 1000, 125_000_000)
    integer(value["timeout_ms"], "timeout", 3000, 30000)
    require(
        type(value["settle_ms"]) is int and value["settle_ms"] == (2000 if scope else 0),
        "invalid settling time",
    )
    if scope:
        number(value["range_v"], "full-scale range", 1, 50)
    else:
        require(value["range_v"] is None, "logic has no analog range")
    require(value["trigger"] == "immediate", "unsupported trigger")
    require(
        value["effects"] == EFFECTS and all(type(value["effects"][key]) is bool for key in EFFECTS),
        "effect declaration differs",
    )
    require(
        value["authorization_granted"] is False and value["hardware_identity_verified"] is False,
        "plan cannot assert authorization",
    )
