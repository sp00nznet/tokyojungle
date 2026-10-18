"""Repair VMX element memory emissions without relifting or decoding source text.

Vector registers retain guest byte order. STVEBX/STVEHX/STVEWX select their
element using the aligned effective address, not always vector byte zero.
LVEBX/LVEHX/LVEWX use the PS3 PPU aligned-block behavior used by RPCS3:
https://github.com/RPCS3/rpcs3/blob/master/rpcs3/Emu/Cell/PPUInterpreter.cpp
"""
import argparse
from collections import Counter
from pathlib import Path
import re

EA = rb"(?P<ea>ctx->gpr\[\d+\]|\(ctx->gpr\[\d+\] \+ ctx->gpr\[\d+\]\))"
LOAD = re.compile(
    rb"\{ uint64_t ea = " + EA + rb"; memset\(&ctx->vr\[(?P<v>\d+)\], 0, 16\); "
    rb"memcpy\(&ctx->vr\[(?P=v)\], vm_base \+ \(uint32_t\)ea, (?P<size>[124])\); \}"
)
STORE = re.compile(
    rb"\{ uint64_t ea = " + EA + rb"; memcpy\(vm_base \+ \(uint32_t\)ea, "
    rb"&ctx->vr\[(?P<v>\d+)\], (?P<size>[124])\); \}"
)
FIXED = re.compile(
    rb"\{ uint64_t ea = " + EA + rb"; [^\r\n]+? \} /\* tj-ppu-(?P<mn>stve[bhw]x|lve[bhw]x) \*/"
)


def operation(match, store=False):
    return ("stve" if store else "lve") + {1: "b", 2: "h", 4: "w"}[int(match["size"])] + "x"


def emit(match, store=False):
    ea, v, size = match["ea"].decode("ascii"), int(match["v"]), int(match["size"])
    if store:
        body = (f"{{ uint64_t ea = {ea}; ea &= ~{size - 1}ULL; "
                f"memcpy(vm_base + (uint32_t)ea, (const uint8_t*)&ctx->vr[{v}] + (ea & 0xFULL), {size}); }}")
    else:
        body = (f"{{ uint64_t ea = {ea}; memcpy(&ctx->vr[{v}], "
                "vm_base + (uint32_t)(ea & ~0xFULL), 16); }")
    return (body + f" /* tj-ppu-{operation(match, store)} */").encode("ascii")


def patched_bytes(data):
    counts = Counter()
    def replace(match, store=False):
        counts[operation(match, store)] += 1
        return emit(match, store)
    after = LOAD.sub(replace, data)
    after = STORE.sub(lambda match: replace(match, True), after)
    return after, counts


def repair(path, apply=False):
    path = Path(path)
    before = path.read_bytes()
    after, counts = patched_bytes(before)
    if apply and after != before:
        path.write_bytes(after)
    return counts


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    counts = Counter()
    for path in sorted((Path(__file__).resolve().parents[1] / "generated").glob("ppu_recomp_*.cpp")):
        counts.update(repair(path, args.apply))
    print("VMX element memory patches:", dict(counts))
