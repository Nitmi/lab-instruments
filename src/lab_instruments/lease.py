"""Cooperative local-process lease, not an instrument-side VISA/network lock."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from .common import LabError, checked, digest


class Lease:
    def __init__(self, serial):
        self.stream = None
        directory = checked(Path(tempfile.gettempdir())) / "lab-instruments-load-leases"
        directory.mkdir(exist_ok=True)
        checked(directory)
        path = directory / (digest(serial.encode("ascii")) + ".lock")
        if path.exists():
            checked(path)
        self.stream = path.open("a+b")
        try:
            if path.stat().st_size == 0:
                self.stream.write(b"0")
                self.stream.flush()
            self.stream.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            self.stream.close()
            self.stream = None
            raise LabError("INSTRUMENT_BUSY", "another local session owns this serial") from error

    def close(self):
        if self.stream:
            self.stream.close()
            self.stream = None
