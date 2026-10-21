"""Synthetic SDS804X HD TCP protocol peer. Not a vendor simulator or real scope."""

from __future__ import annotations

import struct

from sdl_double import SdlDouble


def block(payload):
    size = str(len(payload)).encode("ascii")
    return b"#" + str(len(size)).encode("ascii") + size + payload + b"\n\n"


def preamble(channel=1, points=16):
    raw = bytearray(346)
    raw[:8] = b"WAVEDESC"
    raw[16:23] = b"WAVEACE"
    raw[76:87] = b"Siglent SDS"
    for offset, fmt, value in (
        (32, "h", 1),
        (34, "h", 0),
        (36, "i", 346),
        (60, "i", points * 2),
        (116, "i", points),
        (132, "i", 0),
        (136, "i", 1),
        (156, "f", 0.5),
        (160, "f", -0.25),
        (164, "f", 6400),
        (172, "h", 12),
        (176, "f", 1e-6),
        (180, "d", 2e-3),
        (324, "h", 20),
        (326, "h", 0),
        (328, "f", 10),
        (344, "h", channel - 1),
    ):
        struct.pack_into("<" + fmt, raw, offset, value)
    return bytes(raw)


class SdsDouble(SdlDouble):
    def __init__(self, fault=None, *, serial="SYNTHETICSDS123"):
        super().__init__(fault, serial=serial)
        self.points = 16
        self.channel = 1
        self.count = 10
        self.armed_polls = 0
        self.armed = False
        self.width = "BYTE"
        self.order = "MSB"
        self.single = False

    def response(self, command):
        if command == "*IDN?":
            return f"Siglent Technologies,SDS804X HD,{self.serial},1.1.6.5"
        if command == ":TRIG:MODE SING":
            self.armed = self.single = True
        elif command == ":TRIG:STOP":
            self.armed = False
        elif command == ":TRIG:STAT?":
            if self.armed:
                self.armed_polls += 1
                if self.armed_polls <= 1:
                    return "Ready"
                self.armed = False
                self.count += 1
            return "Stop"
        elif command.startswith(":WAV:POIN "):
            self.points = int(command.split()[-1])
        elif command.startswith(":WAV:SOUR C"):
            self.channel = int(command[-1])
        elif command == ":WAV:WIDT WORD":
            self.width = "WORD"
        elif command == ":WAV:BYT LSB":
            self.order = "LSB"
        elif command == ":WAV:PRE?":
            return block(preamble(self.channel, self.points))
        elif command == ":WAV:DATA?":
            values = (-32768, -6400, 0, 6400, 32752)
            return block(
                struct.pack("<" + "h" * self.points, *(values[i % 5] for i in range(self.points)))
            )
        elif command.startswith(":CHAN"):
            return {
                "SWIT?": "ON",
                "COUP?": "DC",
                "SCAL?": "5",
                "OFFS?": "-2.5",
                "PROB?": "10",
                "UNIT?": "V",
            }.get(command.split(":")[-1])
        return {
            ":TRIG:MODE?": "SINGle" if self.single else "AUTO",
            ":ACQ:NUMAC?": str(self.count),
            ":ACQ:MODE?": "YT",
            ":ACQ:TYPE?": "NORMal",
            ":ACQ:SEQ?": "OFF",
            ":WAV:WIDT?": self.width,
            ":WAV:BYT?": self.order,
            ":WAV:STAR?": "0",
            ":WAV:INT?": "1",
            ":WAV:POIN?": str(self.points),
            ":WAV:MAXP?": "1000000",
            ":WAV:SOUR?": f"C{self.channel}",
        }.get(command)
