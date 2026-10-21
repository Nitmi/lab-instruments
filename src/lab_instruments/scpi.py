"""One bounded, non-reconnecting SCPI TCP stream; no public raw-command CLI."""

from __future__ import annotations

import base64
import math
import re
import socket
import time

from .capture import now
from .common import require

QUERIES = frozenset(
    {
        "*IDN?",
        "*OPC?",
        ":SOUR:INP:STAT?",
        ":SOUR:SHOR:STAT?",
        ":SOUR:FUNC?",
        "EXT:MODE?",
        ":SOUR:EXT:INPUT:STAT?",
        "SYST:SENS:STAT?",
        ":SOUR:CURR:LEV:IMM?",
        ":SOUR:CURR:IRANG?",
        ":SOUR:CURR:VRANG?",
        "MEAS:VOLT:DC?",
        "MEAS:CURR:DC?",
        "MEAS:POW:DC?",
    }
)
WRITES = frozenset(
    {
        ":SOUR:FUNC CURRENT",
        ":SOUR:CURR:IRANG 5",
        ":SOUR:CURR:VRANG 36",
        ":SOUR:INP:STAT ON",
        ":SOUR:INP:STAT OFF",
    }
)


def numeric(text):
    require(
        isinstance(text, str)
        and re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][+-]?\d+)?", text),
        "invalid numeric SCPI response",
        "PROTOCOL_ERROR",
    )
    value = float(text)
    require(
        math.isfinite(value) and abs(value) < 1e30,
        "nonfinite/overload SCPI response",
        "PROTOCOL_ERROR",
    )
    return value


def boolean(text):
    require(text in ("0", "1", "ON", "OFF"), "invalid boolean SCPI response", "PROTOCOL_ERROR")
    return text in ("1", "ON")


class Tcp:
    def __init__(self, endpoint, io_timeout_ms, deadline, journal):
        self.endpoint, self.deadline = endpoint, deadline
        self.io_seconds = io_timeout_ms / 1000
        self.journal = journal
        self.socket = None
        self.synchronized = True
        self.network_attempted = False
        self.closed = False
        self.bytes = 0
        self.transactions = 0

    def event(self, kind, data=b"", **details):
        self.journal(
            {
                "kind": kind,
                "recorded_at": now(),
                "monotonic_s": time.monotonic(),
                "raw_base64": base64.b64encode(data).decode("ascii"),
                "bytes": len(data),
                **details,
            }
        )

    def budget(self, stop):
        remaining = stop - time.monotonic()
        require(remaining > 0, "SCPI deadline exceeded", "TIMEOUT")
        self.socket.settimeout(remaining)

    def connect(self):
        self.network_attempted = True
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        stop = min(self.deadline, time.monotonic() + self.io_seconds)
        self.event("connect.begin", endpoint=self.endpoint)
        self.budget(stop)
        self.socket.connect((self.endpoint["host"], self.endpoint["port"]))
        self.event("connect.complete")

    def send(self, command, *, query=False, cleanup=False):
        require(self.socket is not None, "socket is not connected", "TRANSPORT_ERROR")
        require(
            self.transactions < 1000 and self.bytes < 262144,
            "SCPI transcript limit",
            "LIMIT_EXCEEDED",
        )
        if cleanup:
            require(command == ":SOUR:INP:STAT OFF", "only OFF is allowed for cleanup")
        else:
            require(self.synchronized, "SCPI stream is desynchronized", "PROTOCOL_ERROR")
        if query:
            require(command in QUERIES, "unsupported SCPI query")
        else:
            require(
                command in WRITES or re.fullmatch(r":SOUR:CURR:LEV:IMM [0-5]\.\d{3}", command),
                "unsupported SCPI write",
            )
        stop = min(self.deadline, time.monotonic() + self.io_seconds)
        data = (("\n" if cleanup and not self.synchronized else "") + command + "\n").encode(
            "ascii"
        )
        self.transactions += 1
        self.event("command.begin", command=command, cleanup=cleanup)
        was_synchronized = self.synchronized
        self.synchronized = False
        try:
            while data:
                self.budget(stop)
                count = self.socket.send(data)
                require(count > 0, "socket closed during send", "TRANSPORT_ERROR")
                self.bytes += count
                self.event("tx", data[:count])
                data = data[count:]
            if not query:
                self.synchronized = was_synchronized
                return None
            received = bytearray()
            while not received.endswith(b"\n"):
                self.budget(stop)
                block = self.socket.recv(513 - len(received))
                require(block, "socket closed before response terminator", "TRANSPORT_ERROR")
                self.event("rx", block)
                self.bytes += len(block)
                received.extend(block)
                require(len(received) <= 512, "SCPI response exceeds 512 bytes", "PROTOCOL_ERROR")
                require(b"\n" not in received[:-1], "extra SCPI response lines", "PROTOCOL_ERROR")
            text = bytes(received[:-1]).removesuffix(b"\r").decode("ascii")
            require(
                text and all(32 <= ord(c) <= 126 for c in text),
                "invalid SCPI response characters",
                "PROTOCOL_ERROR",
            )
            self.synchronized = True
            self.event("response.complete", command=command, response=text)
            return text
        except BaseException:
            self.event("transaction.failed", command=command)
            raise

    def query(self, command):
        return self.send(command, query=True)

    def close(self):
        if self.socket is not None:
            self.socket.close()
            self.socket = None
        self.closed = True
        self.event("socket.closed")
