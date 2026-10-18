"""Reject corrupt replay fixtures before VM/GPU setup; no game boot."""
from pathlib import Path
import struct
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
EXE = ROOT / "build-clang/tokyojungle.exe"


def header(blocks=0, commands=1, flip=0, width=1280):
    return (b"TJRSX001" + struct.pack("<III", blocks, commands, flip)
            + bytes(16384 * 4 + 544 * 16)
            + struct.pack("<4I", width, 720, 5120, 0) + bytes(7 * 16)
            + struct.pack("<I", 1) + bytes(240))


def main():
    cases = {
        "short": b"bad",
        "magic": b"BADMAGIC" + header()[8:],
        "count": header(commands=1000001),
        "flip": header(flip=8),
        "display": header(width=8192),
        "block": header(blocks=1) + struct.pack("<II", 0xFFFFFFFF, 16),
        "unaligned": header() + struct.pack("<III", 0xE945, 0, 0),
        "reference": header(blocks=1) + struct.pack("<II", 0x10000000, 0)
                     + struct.pack("<4I", 0xE944, 0, 1, 1),
        "incomplete": header() + struct.pack("<III", 0x1808, 5, 0),
        "trailing": header() + struct.pack("<III", 0xE944, 0, 0) + b"x",
    }
    with tempfile.TemporaryDirectory(prefix="tj-reference-fixture-") as directory:
        for name, payload in cases.items():
            path = Path(directory) / (name + ".tjrsx")
            path.write_bytes(payload)
            result = subprocess.run([str(EXE), "--rsx-reference", str(path)],
                                    capture_output=True, timeout=10)
            output = result.stdout + result.stderr
            if result.returncode != 1 or b"[TJ-REFERENCE] FAILED:" not in output:
                raise RuntimeError(f"{name}: rejection missing: {output!r}")
            if b"[cellGcmSys]" in output or b"[D3D12]" in output:
                raise RuntimeError(f"{name}: reached GPU/GCM setup")
    print(f"PASS {len(cases)}/{len(cases)} fixture rejection cases; no GPU/game boot")


if __name__ == "__main__":
    main()
