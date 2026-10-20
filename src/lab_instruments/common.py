from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
from pathlib import Path

MAX_JSON_BYTES = 1024 * 1024
MAX_SDK_BYTES = 64 * 1024 * 1024
SHA = re.compile(r"[a-f0-9]{64}\Z")
SERIAL = re.compile(r"[A-Za-z0-9]{6,31}\Z")


class LabError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def require(condition, message, code="INVALID_INPUT"):
    if not condition:
        raise LabError(code, message)


def number(value, name, low, high):
    require(
        type(value) in (int, float) and math.isfinite(value) and low <= value <= high,
        f"invalid {name}",
    )
    return value


def integer(value, name, low, high):
    require(type(value) is int and low <= value <= high, f"invalid {name}")
    return value


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def checked(path: Path):
    absolute = Path(os.path.abspath(path))
    require(not str(absolute).startswith(("\\\\", "//")), "network/device paths are unsupported")
    for part in (*reversed(absolute.parents), absolute):
        info = part.lstat()
        require(
            not stat.S_ISLNK(info.st_mode) and not (getattr(info, "st_file_attributes", 0) & 0x400),
            "links/reparse points are unsupported",
        )
    return absolute


def read(path, maximum=MAX_JSON_BYTES, expected=None):
    path = checked(Path(path))
    require(stat.S_ISREG(path.stat().st_mode), "input must be a regular file")
    with path.open("rb") as stream:
        require(stat.S_ISREG(os.fstat(stream.fileno()).st_mode), "input must be a regular file")
        raw = stream.read(maximum + 1)
    require(len(raw) <= maximum, "input exceeds byte limit", "LIMIT_EXCEEDED")
    actual = digest(raw)
    if expected is not None:
        require(
            isinstance(expected, str) and SHA.fullmatch(expected) is not None,
            "expected lowercase SHA-256",
        )
        require(actual == expected, "input SHA-256 differs", "HASH_MISMATCH")
    return raw, {"path": str(path), "sha256": actual, "size_bytes": len(raw)}


def encode(value):
    raw = (
        json.dumps(value, sort_keys=True, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
        + "\n"
    ).encode("ascii")
    require(len(raw) <= MAX_JSON_BYTES, "output exceeds byte limit", "LIMIT_EXCEEDED")
    return raw


def parse(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result, "duplicate JSON field")
            result[key] = value
        return result

    def bad_constant(value):
        raise LabError("INVALID_INPUT", "nonfinite JSON value")

    result = json.loads(raw.decode("utf-8"), object_pairs_hook=unique, parse_constant=bad_constant)
    require(isinstance(result, dict), "JSON root must be an object")
    pending = [(result, 0)]
    while pending:
        item, depth = pending.pop()
        require(depth <= 24, "JSON depth exceeds limit")
        if isinstance(item, dict):
            pending.extend((value, depth + 1) for value in item.values())
        elif isinstance(item, list):
            pending.extend((value, depth + 1) for value in item)
        elif isinstance(item, float):
            require(math.isfinite(item), "nonfinite JSON value")
    return result


def new_path(path):
    path = Path(path)
    parent = checked(path.parent)
    require(parent.is_dir(), "output parent must be a directory")
    name = path.name
    require(
        name not in ("", ".", "..")
        and not name.endswith((" ", "."))
        and not any(ord(c) < 32 or c in '<>:"/\\|?*' for c in name),
        "invalid output name",
    )
    reserved = {"CON", "PRN", "AUX", "NUL"} | {
        prefix + str(i) for prefix in ("COM", "LPT") for i in range(1, 10)
    }
    require(name.split(".")[0].upper() not in reserved, "reserved output name")
    target = parent / name
    require(not target.exists() and not target.is_symlink(), "output exists", "OUTPUT_EXISTS")
    return target


def write_new(path, raw):
    target = new_path(path)
    require(len(raw) <= MAX_JSON_BYTES, "output exceeds byte limit", "LIMIT_EXCEEDED")
    with target.open("xb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    return {"path": str(target), "sha256": digest(raw), "size_bytes": len(raw)}
