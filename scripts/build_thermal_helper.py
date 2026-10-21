"""Explicit Windows-only helper build with the installed MSVC/Windows SDK."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--visual-studio", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    assert os.name == "nt"
    root = Path(__file__).resolve().parents[1]
    source = root / "native/pt3_capture.cpp"
    output = args.output.absolute()
    output.mkdir()
    setup = args.visual_studio.resolve() / "Common7/Tools/VsDevCmd.bat"
    assert setup.is_file()
    exe = output / "lab-pt3-helper.exe"
    # Every quoted path is validated before use by cmd.exe; no user shell expressions.
    for path in (setup, source, exe, output):
        assert not any(c in str(path) for c in '\r\n"&|<>^%!')
    command = (
        f'call "{setup}" -no_logo -arch=x64 -host_arch=x64 && '
        f"cl.exe /nologo /std:c++17 /EHsc /O2 /MT /W4 /WX /Brepro /DUNICODE /D_UNICODE "
        f'"{source}" /Fe:"{exe}" /Fo:"{output / "pt3.obj"}" '
        "/link /Brepro mfplat.lib mfreadwrite.lib mfuuid.lib mf.lib "
        "ole32.lib ksuser.lib strmiids.lib"
    )
    result = subprocess.run(
        'cmd.exe /d /s /c "' + command + '"', cwd=output, capture_output=True, timeout=120
    )
    (output / "build.stdout").write_bytes(result.stdout)
    (output / "build.stderr").write_bytes(result.stderr)
    assert result.returncode == 0, (
        result.stdout.decode(errors="replace"),
        result.stderr.decode(errors="replace"),
    )
    metadata = {
        "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "exe_sha256": hashlib.sha256(exe.read_bytes()).hexdigest(),
        "size_bytes": exe.stat().st_size,
        "static_crt": True,
        "hardware_access": False,
        "executables_started": ["cmd", "MSVC compiler/linker"],
    }
    (output / "build.json").write_text(json.dumps(metadata, indent=2) + "\n")
    print(json.dumps(metadata))


if __name__ == "__main__":
    main()
