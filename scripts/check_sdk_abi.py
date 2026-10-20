"""Compare ABI declarations to a pinned pydwf wheel without executing its code."""

from __future__ import annotations

import argparse
import ast
import ctypes as c
import hashlib
import json
import zipfile
from pathlib import Path

from lab_instruments.dwf import SIGNATURES

REFERENCE_SHA256 = "aefd97cfb8d315d2c4daf623682ae9e235a4f7483618c01e3f9362588663f9f2"


def check(path):
    with Path(path).open("rb") as stream:
        raw = stream.read(8 * 1024 * 1024 + 1)
    assert len(raw) <= 8 * 1024 * 1024
    assert hashlib.sha256(raw).hexdigest() == REFERENCE_SHA256
    member = "pydwf/core/auxiliary/dwf_function_signatures.py"
    with zipfile.ZipFile(path) as archive:
        assert len([item for item in archive.infolist() if item.filename == member]) == 1
        assert archive.getinfo(member).file_size < 200000
        tree = ast.parse(archive.read(member).decode())
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "dwf_function_signatures"
    )
    expression = next(node.value for node in function.body if isinstance(node, ast.Return))
    mapping = {
        "c_int": c.c_int,
        "HDWF": c.c_int,
        "DwfEnumFilter": c.c_int,
        "DwfAcquisitionMode": c.c_int,
        "DwfAnalogInFilter": c.c_int,
        "DwfDigitalInSampleMode": c.c_int,
        "DwfDigitalInClockSource": c.c_int,
        "c_unsigned_int": c.c_uint,
        "DwfTriggerSource": c.c_ubyte,
        "DwfState": c.c_ubyte,
        "c_double": c.c_double,
        "c_const_char_ptr": c.c_char_p,
        "c_void_ptr": c.c_void_p,
        "c_char_array_32": c.POINTER(c.c_char),
        "c_char_array_512": c.POINTER(c.c_char),
    }
    for name in ("c_int", "HDWF", "DwfState", "c_unsigned_int", "c_double"):
        mapping[name + "_ptr"] = c.POINTER(mapping[name])
    mapping["DwfDeviceID_ptr"] = mapping["DwfDeviceVersion_ptr"] = c.POINTER(c.c_int)
    checked = []
    for entry in expression.elts:
        name = ast.literal_eval(entry.elts[0])
        if name not in SIGNATURES:
            continue
        assert entry.elts[1].attr == "c_int", name
        parameters = [mapping[pair.elts[1].attr] for pair in entry.elts[2].elts]
        assert parameters == SIGNATURES[name], (name, parameters, SIGNATURES[name])
        checked.append(name)
    assert set(checked) == set(SIGNATURES)
    return {
        "ok": True,
        "functions_verified": len(checked),
        "reference_sha256": REFERENCE_SHA256,
        "reference_header_version": "3.20.1",
        "target_sdk_version": "3.25.1",
        "vendor_dll_abi_verified": False,
        "hardware_access": False,
        "reference_code_executed": False,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("reference_wheel", type=Path)
    args = parser.parse_args()
    print(json.dumps(check(args.reference_wheel), indent=2))
