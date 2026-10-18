"""Insert one read-only depth-command provenance hook without re-encoding PPU code."""
from pathlib import Path
import argparse

SIGNATURE = b"void func_001F9828(ppu_context* ctx) {"
SEAM = b"        vm_write32(ctx->gpr[9] + 0x0, ctx->gpr[0]);\nloc_001F9878:"
HOOK = (
    b"        /* tj-depth-mask-emit-trace */\n"
    b"        extern void tj_depth_mask_emit_trace(ppu_context*, uint32_t, uint32_t, uint32_t);\n"
    b"        tj_depth_mask_emit_trace(ctx, (uint32_t)ctx->gpr[9], (uint32_t)ctx->gpr[30],\n"
    b"                                 (uint32_t)vm_read64(ctx->gpr[1] + 0x90));\n"
)


def patched_bytes(data):
    if SIGNATURE not in data:
        return data
    if data.count(SIGNATURE) != 1:
        raise RuntimeError("Ambiguous depth-mask function")
    newline = b"\r\n" if b"\r\n" in data else b"\n"
    start = data.index(SIGNATURE)
    end = data.find(newline + b"}", start)
    if end < 0:
        raise RuntimeError("Unterminated depth-mask function")
    body = data[start:end]
    seam = SEAM.replace(b"\n", newline)
    hook = HOOK.replace(b"\n", newline)
    replacement = seam.replace(b"loc_001F9878:", hook + b"loc_001F9878:")
    if replacement in body:
        return data
    if b"tj-depth-mask-emit-trace" in body or body.count(seam) != 1:
        raise RuntimeError("Unrecognized depth-mask trace seam; no changes")
    return data[:start] + body.replace(seam, replacement, 1) + data[end:]


def repair(path, apply=False):
    path = Path(path)
    before = path.read_bytes()
    after = patched_bytes(before)
    if apply and after != before:
        path.write_bytes(after)
    return int(after != before)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    files = sorted((Path(__file__).resolve().parents[1] / "generated").glob("ppu_recomp_*.cpp"))
    print("depth-emit trace patches:", sum(repair(path, args.apply) for path in files))
