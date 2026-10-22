"""Installed I2C/SPI CLI checks against explicitly synthetic saved captures."""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path


def verify(run, cli, root):
    tests = Path(__file__).resolve().parents[1] / "tests"
    producer = (
        f"import sys,pathlib;sys.path.insert(0,{str(tests)!r});"
        "from logic_fixtures import save;"
        "from bus_fixtures import bus_capture,i2c_signal,spi_signal;"
        f"p=pathlib.Path({str(root)!r});"
        "s=i2c_signal([{'address':72,'data':[2]},{'address':72,'read':True,'data':[18,52],'acks':[True,True,False]}]);"
        "save(p,bus_capture(s),'i2c-capture.json');"
        "save(p,bus_capture({ch:bits[:50] for ch,bits in s.items()}),'i2c-partial.json');"
        "save(p,bus_capture(s,ok=False),'i2c-failed.json');"
        "[(lambda s,n:save(p,bus_capture(s),n))(spi_signal([150,0],[53,255],mode=m),"
        "'spi-mode'+str(m)+'.json') for m in range(4)];"
        "s=spi_signal();"
        "save(p,bus_capture({ch:bits[:70] for ch,bits in s.items()}),'spi-partial.json')"
    )
    run("bus-fixtures", [*cli[:2], "-c", producer])
    hashes = {}
    for name in (
        "i2c-capture",
        "i2c-partial",
        "i2c-failed",
        "spi-partial",
        *["spi-mode" + str(i) for i in range(4)],
    ):
        path = root / (name + ".json")
        hashes[name] = hashlib.sha256(path.read_bytes()).hexdigest()

    def args(name, kind):
        return [
            *cli,
            "logic",
            kind,
            str(root / (name + ".json")),
            "--sha256",
            hashes[name],
            "--json",
        ]

    selected = ["--scl", "0", "--sda", "1"]
    good = [*args("i2c-capture", "i2c"), *selected, "--output", str(root / "i2c-analysis.json")]
    result = json.loads(run("bus-i2c", good))
    assert result["ok"]
    analysis = result["data"]["analysis"]["analysis"]
    assert len(analysis["transfers"]) == 2 and analysis["nack_count"] == 1
    assert analysis["transfers"][1]["start_condition"] == "repeated_start"
    assert [b["value"] for b in analysis["transfers"][1]["bytes"]] == [145, 18, 52]
    assert json.loads(run("bus-overwrite", good, 2))["error"]["code"] == "OUTPUT_EXISTS"
    for name, status in (("i2c-partial", "invalid"), ("i2c-failed", "source_failed")):
        bad = json.loads(run("bus-" + name, [*args(name, "i2c"), *selected], 3))
        assert bad["data"]["analysis"]["status"] == status
    wrong = args("i2c-capture", "i2c")
    wrong[wrong.index("--sha256") + 1] = "0" * 64
    assert (
        json.loads(run("bus-wrong-hash", [*wrong, *selected], 2))["error"]["code"]
        == "HASH_MISMATCH"
    )
    for mode in range(4):
        result = json.loads(
            run(
                "bus-spi-mode" + str(mode),
                [
                    *args("spi-mode" + str(mode), "spi"),
                    "--sck",
                    "0",
                    "--cs",
                    "1",
                    "--mosi",
                    "2",
                    "--miso",
                    "3",
                    "--mode",
                    str(mode),
                ],
            )
        )
        assert result["ok"]
        derived = result["data"]["analysis"]
        a = derived["analysis"]
        assert base64.b64decode(a["bytes_base64"]["mosi"][0]) == b"\x96\x00"
        assert base64.b64decode(a["bytes_base64"]["miso"][0]) == b"\x35\xff"
        assert derived["evidence_origin"] == "test_double"
        assert all(
            derived[k] is False
            for k in (
                "hardware_access",
                "executables_started",
                "physical_device_verified",
                "physical_timing_verified",
                "protocol_timing_compliance_verified",
            )
        )
    bad = json.loads(
        run(
            "bus-spi-partial",
            [*args("spi-partial", "spi"), "--sck", "0", "--cs", "1", "--mosi", "2", "--mode", "0"],
            3,
        )
    )
    assert not bad["ok"] and bad["data"]["analysis"]["analysis"]["bytes_base64"] is None
    for name, sha in hashes.items():
        assert hashlib.sha256((root / (name + ".json")).read_bytes()).hexdigest() == sha
    return {"ok": True, "hardware_access": False, "synthetic_only": True, "spi_modes": 4}
