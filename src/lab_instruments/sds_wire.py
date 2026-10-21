"""Fixed SDS800X HD SCPI subset over one deadline-bounded TCP socket."""

from __future__ import annotations

import re
import socket
import time

from .capture import now
from .common import digest, encode, require

QUERIES = {
    "*IDN?",
    ":TRIG:STAT?",
    ":TRIG:MODE?",
    ":ACQ:NUMAC?",
    ":ACQ:MODE?",
    ":ACQ:TYPE?",
    ":ACQ:SEQ?",
    ":WAV:SOUR?",
    ":WAV:STAR?",
    ":WAV:INT?",
    ":WAV:POIN?",
    ":WAV:WIDT?",
    ":WAV:BYT?",
    ":WAV:MAXP?",
}
QUERIES |= {
    f":CHAN{c}:{q}?" for c in range(1, 5) for q in ("SWIT", "COUP", "SCAL", "OFFS", "PROB", "UNIT")
}
BLOCKS = {":WAV:PRE?", ":WAV:DATA?"}
WRITES = {
    ":TRIG:MODE SING",
    ":TRIG:STOP",
    ":WAV:STAR 0",
    ":WAV:INT 1",
    ":WAV:WIDT WORD",
    ":WAV:BYT LSB",
} | {f":WAV:SOUR C{c}" for c in range(1, 5)}


def allowed(command, kind):
    valid = (
        command in QUERIES
        if kind == "line"
        else command in BLOCKS
        if kind == "block"
        else command in WRITES or re.fullmatch(r":WAV:POIN [1-9][0-9]{0,4}", command)
    )
    require(valid, "unsupported scope SCPI command")


def split_block(raw, maximum):
    require(
        len(raw) >= 3 and raw[:1] == b"#" and raw[1:2] in b"123456789",
        "definite binary block required",
        "PROTOCOL_ERROR",
    )
    digits = int(raw[1:2])
    length = raw[2 : 2 + digits]
    require(len(length) == digits and length.isdigit(), "invalid block length", "PROTOCOL_ERROR")
    count = int(length)
    require(0 < count <= maximum, "binary block exceeds byte budget", "LIMIT_EXCEEDED")
    end = 2 + digits + count
    require(
        len(raw) == end + 2 and raw[end:] == b"\n\n",
        "SDS block must end with two LF bytes",
        "PROTOCOL_ERROR",
    )
    return raw[2 + digits : end]


class Tcp:
    def __init__(self, endpoint, io_ms, deadline, directory):
        self.endpoint, self.io_s, self.deadline = endpoint, io_ms / 1000, deadline
        self.directory = directory
        self.socket = None
        self.synchronized = True
        self.attempted = False
        self.records = []
        self.total_bytes = 0

    def budget(self, stop):
        remaining = stop - time.monotonic()
        require(remaining > 0, "scope TCP deadline exceeded", "TIMEOUT")
        self.socket.settimeout(remaining)

    def connect(self):
        self.attempted = True
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.budget(min(self.deadline, time.monotonic() + self.io_s))
        self.socket.connect((self.endpoint["host"], self.endpoint["port"]))

    def exchange(self, command, kind="line", maximum=512):
        allowed(command, kind)
        require(self.synchronized, "scope stream desynchronized", "PROTOCOL_ERROR")
        require(
            len(self.records) < 800 and self.total_bytes < 512 * 1024,
            "scope transaction budget exceeded",
            "LIMIT_EXCEEDED",
        )
        self.synchronized = False
        stop = min(self.deadline, time.monotonic() + self.io_s)
        index = len(self.records)
        tx_name, rx_name = f"wire-{index:04d}-tx.bin", f"wire-{index:04d}-rx.bin"
        request = (command + "\n").encode("ascii")
        record = {
            "command": command,
            "kind": kind,
            "started_at": now(),
            "tx": tx_name,
            "rx": rx_name,
            "complete": False,
        }
        self.records.append(record)
        sent, received = bytearray(), bytearray()
        # Create evidence before any transmission; partial receives are preserved on disk.
        with (
            (self.directory / tx_name).open("xb") as tx,
            (self.directory / rx_name).open("xb") as rx,
        ):

            def receive(count):
                while count:
                    self.budget(stop)
                    chunk = self.socket.recv(min(count, 65536))
                    require(chunk, "scope disconnected mid-response", "TRANSPORT_ERROR")
                    rx.write(chunk)
                    rx.flush()
                    received.extend(chunk)
                    count -= len(chunk)

            try:
                while len(sent) < len(request):
                    self.budget(stop)
                    count = self.socket.send(request[len(sent) :])
                    require(count > 0, "scope disconnected mid-send", "TRANSPORT_ERROR")
                    chunk = request[len(sent) : len(sent) + count]
                    sent.extend(chunk)
                    tx.write(chunk)
                    tx.flush()
                if kind == "line":
                    while not received.endswith(b"\n"):
                        require(
                            len(received) < maximum,
                            "scope line exceeds byte budget",
                            "LIMIT_EXCEEDED",
                        )
                        receive(1)
                    value = bytes(received[:-1]).removesuffix(b"\r").decode("ascii")
                    require(
                        value and all(32 <= ord(c) <= 126 for c in value),
                        "invalid scope response characters",
                        "PROTOCOL_ERROR",
                    )
                elif kind == "block":
                    receive(2)
                    require(
                        received[:1] == b"#" and received[1:2] in b"123456789",
                        "definite binary block required",
                        "PROTOCOL_ERROR",
                    )
                    digits = int(received[1:2])
                    receive(digits)
                    require(received[2:].isdigit(), "invalid block length", "PROTOCOL_ERROR")
                    count = int(received[2:])
                    require(
                        0 < count <= maximum, "binary block exceeds byte budget", "LIMIT_EXCEEDED"
                    )
                    receive(count + 2)
                    value = split_block(bytes(received), maximum)
                else:
                    value = None
                record["complete"] = True
                self.synchronized = True
                return value
            finally:
                self.total_bytes += len(sent) + len(received)
                for key, raw in (("tx", sent), ("rx", received)):
                    record[key + "_sha256"] = digest(raw)
                    record[key + "_bytes"] = len(raw)
                record["finished_at"] = now()
                # An append failure aborts the main operation, never triggers a retry.
                with (self.directory / "transcript.jsonl").open("ab") as journal:
                    journal.write(encode(record))
                    journal.flush()

    def query(self, command):
        return self.exchange(command)

    def send(self, command):
        return self.exchange(command, "write")

    def block(self, command, maximum):
        return self.exchange(command, "block", maximum)

    def close(self):
        if self.socket is not None:
            self.socket.close()
            self.socket = None
