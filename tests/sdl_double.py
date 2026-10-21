"""Local TCP protocol double. No instrument, VISA library or physical I/O."""

from __future__ import annotations

import socket
import threading
import time


class SdlDouble:
    def __init__(self, fault=None, *, serial="SYNTHETIC123"):
        self.fault = fault
        self.serial = serial
        self.commands = []
        self.raw_received = bytearray()
        self.errors = []
        self.connections = 0
        self.on = False
        self.current = 0.1
        self.function = "CURRENT"
        self.server = socket.socket()
        self.server.bind(("127.0.0.1", 0))
        self.server.listen(1)
        self.port = self.server.getsockname()[1]
        self.stop = threading.Event()
        self.ready = threading.Event()
        self.thread = threading.Thread(target=self.serve, daemon=True)

    def __enter__(self):
        self.thread.start()
        self.ready.wait(2)
        return self

    def __exit__(self, *_):
        self.stop.set()
        self.server.close()
        self.thread.join(3)
        assert not self.thread.is_alive(), "test double did not stop"
        assert not self.errors, self.errors

    def response(self, command):
        if command == "*IDN?":
            return f"Siglent Technologies,SDL1020X-E,{self.serial},1.1.1.23R4"
        if command == ":SOUR:INP:STAT ON":
            self.on = True
        elif command == ":SOUR:INP:STAT OFF":
            if self.fault != "off-stuck":
                self.on = False
        elif command.startswith(":SOUR:CURR:LEV:IMM "):
            self.current = float(command.split()[-1])
        elif command == ":SOUR:FUNC CURRENT":
            self.function = "CURRENT"
        responses = {
            "*OPC?": "1",
            ":SOUR:INP:STAT?": "1" if self.on else "0",
            ":SOUR:SHOR:STAT?": "0",
            "EXT:MODE?": "INT",
            ":SOUR:EXT:INPUT:STAT?": "0",
            "SYST:SENS:STAT?": "0",
            ":SOUR:FUNC?": self.function,
            ":SOUR:CURR:IRANG?": "5",
            ":SOUR:CURR:VRANG?": "36",
            ":SOUR:CURR:LEV:IMM?": f"{self.current:.3f}",
            "MEAS:VOLT:DC?": "5.001",
            "MEAS:CURR:DC?": f"{self.current if self.on else 0:.6f}",
            "MEAS:POW:DC?": f"{5 * self.current if self.on else 0:.6f}",
        }
        return responses.get(command)

    def serve(self):
        self.ready.set()
        client = None
        try:
            self.server.settimeout(0.1)
            while not self.stop.is_set():
                try:
                    client, _ = self.server.accept()
                    break
                except TimeoutError:
                    pass
            if client is None:
                return
            self.connections += 1
            client.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            client.settimeout(0.1)
            pending = bytearray()
            while not self.stop.is_set():
                try:
                    block = client.recv(4096)
                except TimeoutError:
                    continue
                if not block:
                    break
                self.raw_received.extend(block)
                pending.extend(block)
                while b"\n" in pending:
                    raw, _, pending = pending.partition(b"\n")
                    if not raw:
                        continue
                    command = raw.decode("ascii")
                    self.commands.append(command)
                    response = self.response(command)
                    if callable(self.fault):
                        response = self.fault(self, command, response)
                    if response is not None:
                        encoded = (
                            response
                            if isinstance(response, bytes)
                            else (response + "\r\n").encode("ascii")
                        )
                        if self.fault == "fragmented":
                            for byte in encoded:
                                client.sendall(bytes([byte]))
                        else:
                            client.sendall(encoded)
        except (ConnectionError, OSError) as error:
            if not self.stop.is_set() and not isinstance(
                error, (ConnectionResetError, BrokenPipeError)
            ):
                self.errors.append(str(error))
        finally:
            if client:
                client.close()


def stalled_loaded_measurement(server, command, response):
    if server.on and command == "MEAS:VOLT:DC?":
        return None
    return response


def delayed_response(server, command, response):
    if server.on and command == "MEAS:VOLT:DC?":
        time.sleep(0.2)
    return response
