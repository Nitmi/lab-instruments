"""Independent digital bus transmitters; all saved captures are synthetic."""

from __future__ import annotations

import base64
import struct

from logic_fixtures import capture

from lab_instruments.common import digest
from lab_instruments.plan import create


def bus_capture(signals, rate=1_000_000, *, ok=True):
    lengths = {len(bits) for bits in signals.values()}
    assert len(lengths) == 1
    n = next(iter(lengths))
    result = capture([0] * n, rate, ok=ok)
    channels = list(signals)
    result["plan"] = create("SYNTHETIC123", "logic", channels, samples=n, sample_rate_hz=rate)
    words = [sum(signals[ch][i] << ch for ch in channels) for i in range(n)]
    raw = struct.pack("<" + "H" * n, *words)
    result["payload"].update(
        raw_base64=base64.b64encode(raw).decode("ascii"),
        length=len(raw),
        sha256=digest(raw),
        channels=channels,
    )
    return result


def i2c_signal(segments=None, *, phase=3, scl=0, sda=1, stretch=0):
    segments = segments or [{"address": 0x50, "data": [0x00, 0xA5]}]
    rows = []

    def hold(clock, data, extra=0):
        rows.extend([(clock, data)] * (phase + extra))

    hold(1, 1)
    hold(1, 1)
    hold(1, 0)  # START
    for index, segment in enumerate(segments):
        values = [(segment["address"] << 1) | bool(segment.get("read")), *segment.get("data", [])]
        acks = segment.get("acks", [True] * len(values))
        level = 0
        for value, ack in zip(values, acks, strict=True):
            bits = [(value >> bit) & 1 for bit in range(7, -1, -1)] + [not ack]
            for bit in bits:
                hold(0, level, stretch)
                hold(0, int(bit))
                hold(1, int(bit))
                level = int(bit)
        hold(0, level)
        if index + 1 < len(segments):
            hold(0, 1)
            hold(1, 1)
            hold(1, 0)  # repeated START, no STOP
        else:
            hold(0, 0)
            hold(1, 0)
            hold(1, 1)  # STOP
    hold(1, 1)
    return {scl: [c for c, d in rows], sda: [d for c, d in rows]}


def spi_signal(
    values=None,
    received=None,
    *,
    mode=0,
    word_bits=8,
    order="msb",
    active="low",
    phase=3,
    channels=(0, 1, 2, 3),
):
    values = values if values is not None else [0xA5, 0x00, 0xFF]
    received = received if received is not None else [v ^ ((1 << word_bits) - 1) for v in values]
    idle, cpha = mode >> 1, mode & 1
    selected = int(active == "high")
    rows = []

    def hold(clock, cs, mosi, miso):
        rows.extend([(clock, cs, mosi, miso)] * phase)

    hold(idle, 1 - selected, 0, 0)
    hold(idle, selected, 0, 0)
    for tx, rx in zip(values, received, strict=True):
        indices = range(word_bits - 1, -1, -1) if order == "msb" else range(word_bits)
        for bit in indices:
            out, incoming = (tx >> bit) & 1, (rx >> bit) & 1
            if cpha == 0:
                hold(idle, selected, out, incoming)
                hold(1 - idle, selected, out, incoming)
                hold(idle, selected, out, incoming)
            else:
                hold(1 - idle, selected, out, incoming)
                hold(idle, selected, out, incoming)
    hold(idle, selected, out, incoming)
    hold(idle, 1 - selected, out, incoming)
    hold(idle, 1 - selected, 0, 0)
    return {ch: [row[i] for row in rows] for i, ch in enumerate(channels)}
