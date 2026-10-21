"""Offline SDS804X HD plans; a hash binds inputs, never grants permission."""

from __future__ import annotations

import ipaddress

from .common import encode, integer, require
from .sdl_plan import SERIAL, VERSION

MODEL = "SDS804X HD"
MANUAL_URL = (
    "https://www.siglent.com/wp-content/uploads/2024/09/SDS800XHD_Series_ProgrammingGuide_EN11G.pdf"
)
MANUAL_SHA = "9063254852074c8476e93f434d20ca43fc8e926167c13d410a9f21e68bdc43cf"


def create(
    host,
    serial=None,
    firmware=None,
    *,
    action="stopped",
    port=5025,
    channels=None,
    points=4096,
    io_timeout_ms=2000,
    timeout_ms=15000,
    test_double=False,
):
    value = {
        "schema_version": "lab-instruments.scope-plan.v1",
        "model": MODEL,
        "serial": serial,
        "firmware_version": firmware,
        "endpoint": {"host": host, "port": port},
        "action": action,
        "channels": [1] if channels is None else channels,
        "points": points,
        "io_timeout_ms": io_timeout_ms,
        "timeout_ms": timeout_ms,
        "cleanup_timeout_ms": 3000,
        "test_double": test_double,
        "effects": {
            "tcp_connect": True,
            "identity_query": True,
            "waveform_transfer_configuration": action != "identify",
            "single_trigger": action == "single",
            "timeout_stop": action == "single",
            "end_stopped": action != "identify",
            "settings_restored": False,
            "analog_configuration_changed": False,
            "outputs_enabled": False,
        },
        "manual_sha256": MANUAL_SHA,
        "authorization_granted": False,
        "hardware_identity_verified": False,
        "retries": 0,
    }
    validate(value)
    return value


def validate(value):
    require(isinstance(value, dict), "scope plan must be an object")
    keys = {
        "schema_version",
        "model",
        "serial",
        "firmware_version",
        "endpoint",
        "action",
        "channels",
        "points",
        "io_timeout_ms",
        "timeout_ms",
        "cleanup_timeout_ms",
        "test_double",
        "effects",
        "manual_sha256",
        "authorization_granted",
        "hardware_identity_verified",
        "retries",
    }
    require(set(value) == keys, "unknown/missing scope plan fields")
    require(
        value["schema_version"] == "lab-instruments.scope-plan.v1" and value["model"] == MODEL,
        "unsupported scope model/schema",
    )
    endpoint = value["endpoint"]
    require(isinstance(endpoint, dict) and set(endpoint) == {"host", "port"}, "invalid endpoint")
    ip = ipaddress.IPv4Address(endpoint["host"])
    require(
        str(ip) == endpoint["host"]
        and not (
            ip.is_multicast or ip.is_unspecified or ip.is_reserved or str(ip).endswith(".255")
        ),
        "unicast literal IPv4 required",
    )
    require(type(value["test_double"]) is bool, "invalid test-double flag")
    require(
        str(ip) == "127.0.0.1" if value["test_double"] else not ip.is_loopback,
        "test doubles require 127.0.0.1; physical sessions cannot use loopback",
    )
    integer(endpoint["port"], "TCP port", 1, 65535)
    require(value["action"] in ("identify", "stopped", "single"), "unsupported scope action")
    if value["action"] == "identify":
        require(
            value["serial"] is None and value["firmware_version"] is None,
            "identify cannot assert a preverified identity",
        )
    else:
        require(
            isinstance(value["serial"], str) and SERIAL.fullmatch(value["serial"]),
            "exact serial required",
        )
        require(
            isinstance(value["firmware_version"], str)
            and VERSION.fullmatch(value["firmware_version"]),
            "exact firmware version required",
        )
    channels = value["channels"]
    require(isinstance(channels, list) and 1 <= len(channels) <= 4, "select 1-4 channels")
    for channel in channels:
        integer(channel, "channel", 1, 4)
    require(channels == sorted(set(channels)), "channels must be unique and ascending")
    integer(value["points"], "points per channel", 16, 16384)
    integer(value["io_timeout_ms"], "I/O timeout", 100, 5000)
    integer(value["timeout_ms"], "session timeout", 5000, 60000)
    require(
        type(value["cleanup_timeout_ms"]) is int and value["cleanup_timeout_ms"] == 3000,
        "unsupported cleanup budget",
    )
    expected_effects = {
        "tcp_connect": True,
        "identity_query": True,
        "waveform_transfer_configuration": value["action"] != "identify",
        "single_trigger": value["action"] == "single",
        "timeout_stop": value["action"] == "single",
        "end_stopped": value["action"] != "identify",
        "settings_restored": False,
        "analog_configuration_changed": False,
        "outputs_enabled": False,
    }
    require(encode(value["effects"]) == encode(expected_effects), "scope effects differ")
    require(
        value["manual_sha256"] == MANUAL_SHA
        and value["authorization_granted"] is False
        and value["hardware_identity_verified"] is False
        and type(value["retries"]) is int
        and value["retries"] == 0,
        "plan cannot assert permission or enable retries",
    )
