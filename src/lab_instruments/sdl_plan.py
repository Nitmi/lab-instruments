"""Offline typed SDL1020X-E plans. Digests bind bytes, not user permission."""

from __future__ import annotations

import ipaddress
import re

from .common import encode, integer, number, require

MODEL = "SDL1020X-E"
MANUAL_URL = "https://siglentna.com/wp-content/uploads/dlm_uploads/2023/10/SDL1000X_programming_guide_E02B.pdf"
MANUAL_SHA = "bd3ec6896e35b1a59da11f9adef408fb4dd308d0e83724bd740d276ac92d1ba3"
SERIAL = re.compile(r"[A-Za-z0-9-]{6,40}\Z")
VERSION = re.compile(r"[A-Za-z0-9_.-]{1,48}\Z")


def create(
    host,
    serial=None,
    firmware=None,
    *,
    action="observe",
    port=5025,
    current_a=None,
    min_voltage_v=0.0,
    max_voltage_v=24.0,
    max_current_a=1.0,
    max_power_w=20.0,
    samples=5,
    interval_ms=200,
    io_timeout_ms=1000,
    timeout_ms=15000,
    test_double=False,
):
    value = {
        "schema_version": "lab-instruments.load-plan.v1",
        "model": MODEL,
        "serial": serial,
        "firmware_version": firmware,
        "endpoint": {"host": host, "port": port},
        "action": action,
        "current_a": current_a,
        "limits": {
            "min_voltage_v": min_voltage_v,
            "max_voltage_v": max_voltage_v,
            "max_current_a": max_current_a,
            "max_power_w": max_power_w,
        },
        "samples": samples,
        "interval_ms": interval_ms,
        "io_timeout_ms": io_timeout_ms,
        "timeout_ms": timeout_ms,
        "cleanup_timeout_ms": 3000,
        "test_double": test_double,
        "effects": {
            "tcp_connect": True,
            "identity_query": True,
            "measurement_queries": action in ("observe", "cc"),
            "static_cc_configuration": action == "cc",
            "input_enable": action == "cc",
            "input_off": action in ("cc", "off"),
            "configuration_restored": False,
            "reset": False,
            "protection_settings_changed": False,
        },
        "manual_sha256": MANUAL_SHA,
        "authorization_granted": False,
        "hardware_identity_verified": False,
        "retries": 0,
    }
    validate(value)
    return value


def validate(value):
    require(isinstance(value, dict), "load plan must be an object")
    require(
        set(value)
        == {
            "schema_version",
            "model",
            "serial",
            "firmware_version",
            "endpoint",
            "action",
            "current_a",
            "limits",
            "samples",
            "interval_ms",
            "io_timeout_ms",
            "timeout_ms",
            "cleanup_timeout_ms",
            "test_double",
            "effects",
            "manual_sha256",
            "authorization_granted",
            "hardware_identity_verified",
            "retries",
        },
        "unknown/missing load fields",
    )
    require(
        value["schema_version"] == "lab-instruments.load-plan.v1" and value["model"] == MODEL,
        "unsupported load model/schema",
    )
    endpoint = value["endpoint"]
    require(isinstance(endpoint, dict) and set(endpoint) == {"host", "port"}, "invalid endpoint")
    require(isinstance(endpoint["host"], str), "literal IPv4 required")
    ip = ipaddress.IPv4Address(endpoint["host"])
    require(
        str(ip) == endpoint["host"]
        and not (ip.is_multicast or ip.is_unspecified or ip.is_reserved),
        "unicast literal IPv4 required",
    )
    require(type(value["test_double"]) is bool, "invalid test-double flag")
    require(
        (str(ip) == "127.0.0.1") if value["test_double"] else not ip.is_loopback,
        "test doubles require 127.0.0.1; real sessions cannot use loopback",
    )
    integer(endpoint["port"], "TCP port", 1, 65535)
    action = value["action"]
    require(action in ("identify", "observe", "cc", "off"), "unsupported load action")
    if action == "identify":
        require(
            value["serial"] is None and value["firmware_version"] is None,
            "identify has no preverified identity",
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
    limits = value["limits"]
    require(
        isinstance(limits, dict)
        and set(limits) == {"min_voltage_v", "max_voltage_v", "max_current_a", "max_power_w"},
        "invalid load limits",
    )
    number(limits["min_voltage_v"], "minimum voltage", 0, 24)
    number(limits["max_voltage_v"], "maximum voltage", 0.001, 24)
    require(limits["min_voltage_v"] < limits["max_voltage_v"], "voltage window is empty")
    number(limits["max_current_a"], "maximum current", 0.001, 5)
    number(limits["max_power_w"], "maximum power", 0.001, 50)
    if action == "cc":
        current = number(value["current_a"], "CC setpoint", 0.010, 5)
        require(abs(current * 1000 - round(current * 1000)) < 1e-8, "CC setpoint must use whole mA")
        require(
            current <= limits["max_current_a"]
            and current * limits["max_voltage_v"] <= limits["max_power_w"],
            "CC setpoint exceeds task current/power envelope",
        )
    else:
        require(value["current_a"] is None, "only CC accepts a setpoint")
    integer(value["samples"], "sample count", 1, 50)
    integer(value["interval_ms"], "sample interval", 100, 1000)
    require(value["samples"] * value["interval_ms"] <= 30000, "load duration exceeds 30 seconds")
    integer(value["io_timeout_ms"], "I/O timeout", 100, 2000)
    integer(value["timeout_ms"], "main timeout", 5000, 60000)
    require(
        value["timeout_ms"] >= value["samples"] * value["interval_ms"] + 3000,
        "main timeout needs time for preflight and samples",
    )
    require(
        type(value["cleanup_timeout_ms"]) is int and value["cleanup_timeout_ms"] == 3000,
        "unsupported cleanup budget",
    )
    expected_effects = {
        "tcp_connect": True,
        "identity_query": True,
        "measurement_queries": action in ("observe", "cc"),
        "static_cc_configuration": action == "cc",
        "input_enable": action == "cc",
        "input_off": action in ("cc", "off"),
        "configuration_restored": False,
        "reset": False,
        "protection_settings_changed": False,
    }
    require(encode(value["effects"]) == encode(expected_effects), "load effects differ")
    require(
        value["manual_sha256"] == MANUAL_SHA
        and value["authorization_granted"] is False
        and value["hardware_identity_verified"] is False
        and type(value["retries"]) is int
        and value["retries"] == 0,
        "plan cannot assert permission or enable retries",
    )
