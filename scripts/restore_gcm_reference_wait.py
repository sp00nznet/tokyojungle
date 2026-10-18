"""Restore only the original GCM reference fence that an old boot stub removed."""
from pathlib import Path
import argparse
import hashlib
import struct
import sys

ROOT = Path(__file__).resolve().parents[1]
TOOLS = Path(__file__).resolve().parents[2] / "ps3recomp" / "tools"
ELF = Path(__file__).resolve().parents[1] / "input" / "EBOOT.ELF"
START, END = 0x1F8C4C, 0x1F8CCC
ORIGINAL_HASH = "bfb0087c4e776fa9130330322ce845cf4af569c53fa764d6f660753a536d36fa"
STUB = b"void func_001F8C4C(ppu_context* ctx) { /* OVERRIDE: lv2 wait-for-state spin (sys_timer_usleep + poll) */ ctx->gpr[3] = 0; }"


def restored_function():
    data = ELF.read_bytes()
    offset = struct.unpack_from(">Q", data, 32)[0]
    size, count = struct.unpack_from(">HH", data, 54)
    code = None
    for i in range(count):
        kind, _, file_offset, address, _, file_size, _, _ = struct.unpack_from(
            ">IIQQQQQQ", data, offset + i * size)
        if kind == 1 and address <= START and END <= address + file_size:
            code = data[file_offset + START - address:file_offset + END - address]
    if code is None or hashlib.sha256(code).hexdigest() != ORIGINAL_HASH:
        raise RuntimeError("Original GCM reference-wait code identity mismatch")
    sys.path.insert(0, str(TOOLS))
    from ppu_disasm import disassemble_bytes
    from ppu_lifter import PPULifter
    lifter = PPULifter()
    lifter.toc_base = 0x359220
    function = lifter.lift_function(disassemble_bytes(code, START), START, END)
    if function.calls != [0x1F8CCC, 0x1F8BF4, 0x25DD40]:
        raise RuntimeError("Unexpected GCM reference-wait callees")
    return ("void func_001F8C4C(ppu_context* ctx) {\n" +
            "\n".join(function.body_lines) + "\n}").encode()


def repair(path, apply=False):
    path = Path(path)
    original = path.read_bytes()
    signature = b"void func_001F8C4C(ppu_context* ctx) {"
    if signature not in original:
        return 0
    if original.count(signature) != 1:
        raise RuntimeError("Ambiguous reference-wait function")
    restored = restored_function()
    newline = b"\r\n" if b"\r\n" in original else b"\n"
    restored = restored.replace(b"\n", newline)
    if STUB not in original:
        start = original.index(signature)
        end = original.find(newline + b"}", start) + len(newline) + 1
        # A fresh lift already has the fence. Never replace another edited body.
        body = original[start:end]
        required = (b"func_001F8CCC(ctx)", b"func_001F8BF4(ctx)",
                    b"func_0025DD40(ctx)", b"lv2_syscall(ctx)", b"loc_001F8C98:")
        if not all(part in body for part in required):
            raise RuntimeError("Unrecognized reference-wait body; no changes made")
        return 0
    result = original.replace(STUB, restored, 1)
    if apply:
        path.write_bytes(result)
    print(f"{path.name}: reference fence restored={apply} bytes={len(result)-len(original):+d} "
          f"before={hashlib.sha256(original).hexdigest()} after={hashlib.sha256(result).hexdigest()}")
    return 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    changed = sum(repair(p, args.apply) for p in sorted((ROOT / "generated").glob("ppu_recomp_*.cpp")))
    print(f"reference-wait repairs: {changed}")
