"""Compile actual VMX word sites and shared-lifter output against guest-byte oracles."""
import argparse
import bisect
from collections import Counter
import hashlib
import importlib.util
import json
from pathlib import Path
import random
import re
import shutil
import struct
import subprocess
import sys
import tempfile
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import vector_word_be as patch

LIFTER = Path(__file__).resolve().parents[2] / "ps3recomp" / "tools" / "ppu_lifter.py"
SOURCE = ROOT / "generated/ppu_recomp_000.cpp"
ELF = Path(__file__).resolve().parents[1] / "input" / "EBOOT.ELF"
FIXED = re.compile(
    rb"\{ uint32_t a\[4\],b\[4\],d\[4\]; "
    rb"ppu_vldu4\(&ctx->vr\[(?P<a>\d+)\],a\); "
    rb"ppu_vldu4\(&ctx->vr\[(?P<b>\d+)\],b\); (?P<body>[^\r\n]+?)"
    rb"ppu_vstu4\(&ctx->vr\[(?P<d>\d+)\],d\);(?P<tail>[^\r\n]*?) "
    rb"\} /\* ppu-vmx-word-be \*/"
)


def operation(body):
    if b"r=" in body:
        return "signed_gt" if b"(int32_t)" in body else "unsigned_gt"
    return "add" if b"a[i]+b[i]" in body else "sub"


def oracle(a, b, op):
    def signed(x):
        return x if x < 0x80000000 else x - 0x100000000
    if op == "add":
        return [(x + y) & 0xFFFFFFFF for x, y in zip(a, b)]
    if op == "sub":
        return [(x - y) & 0xFFFFFFFF for x, y in zip(a, b)]
    if op == "signed_gt":
        return [0xFFFFFFFF if signed(x) > signed(y) else 0 for x, y in zip(a, b)]
    return [0xFFFFFFFF if x > y else 0 for x, y in zip(a, b)]


def byte_list(words):
    return ",".join(str(x) for x in struct.pack(">4I", *words))


def check_original_sites(data, matches):
    functions = list(re.finditer(rb"(?m)^void func_([0-9A-F]{8})\(ppu_context\* ctx\) \{", data))
    positions = [f.start() for f in functions]
    starts = [int(f[1], 16) for f in functions]
    elf = ELF.read_bytes()
    assert hashlib.sha256(elf).hexdigest() == "6d253f6429485b43b816fd6884611377626ee95db5dd9200ea58395c93a0812d"
    phoff = struct.unpack_from(">Q", elf, 32)[0]
    entsize, count = struct.unpack_from(">HH", elf, 54)
    segments = [struct.unpack_from(">IIQQQQQQ", elf, phoff + i * entsize) for i in range(count)]
    found = {}
    for function in json.loads((ROOT / "generated/EBOOT.functions.json").read_text()):
        lo, hi = int(function["start"], 16), int(function["end"], 16)
        # This fixture covers ppu_recomp_000.cpp, not later generated shards.
        if not starts[0] <= lo <= starts[-1]:
            continue
        for kind, flags, off, addr, _, size, _, _ in segments:
            if kind != 1 or not flags & 1 or not addr <= lo < hi <= addr + size:
                continue
            for pc in range(lo, hi, 4):
                word = struct.unpack_from(">I", elf, off + pc - addr)[0]
                if word >> 26 != 4:
                    continue
                op = {128: "add", 1152: "sub"}.get(word & 2047)
                if op is None:
                    op = {646: "unsigned_gt", 902: "signed_gt"}.get(word & 1023)
                if op:
                    found[pc] = (op, (word >> 21) & 31, (word >> 16) & 31, (word >> 11) & 31,
                                 bool(word & 1024) if "gt" in op else False)
            break
    original_counts = Counter(starts[bisect.bisect_right(starts, pc) - 1] for pc in found)
    actual_counts = Counter(starts[bisect.bisect_right(positions, m.start()) - 1] for m in matches)
    assert original_counts == actual_counts, {
        f"{fn:08X}": (original_counts[fn], actual_counts[fn])
        for fn in original_counts.keys() | actual_counts.keys() if original_counts[fn] != actual_counts[fn]}
    assert len(found) == len(matches) == 66, (len(found), len(matches))
    for (pc, original), m in zip(sorted(found.items()), matches):
        actual = (operation(m[0]), *(int(m[k]) for k in ("d", "a", "b")), b"c6=" in m[0])
        assert actual == original, (hex(pc), actual, original)
        index = bisect.bisect_right(positions, m.start()) - 1
        assert starts[index] <= pc < starts[index + 1]
    print("original ELF opcode, operands, CR6 and function ownership: 66/66 PASS")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", action="store_true")
    parser.add_argument("--static-only", action="store_true")
    args = parser.parse_args()
    data = SOURCE.read_bytes()
    after, stats = patch.patched_bytes(data)
    if args.baseline:
        assert dict(stats) == {"add": 48, "compare": 18}, stats
        matches = sorted([*patch.ARITH.finditer(data), *patch.CMP.finditer(data)], key=lambda m: m.start())
        # Every non-target byte, including invalid UTF-8, survives the repair.
        cursor = 0
        rebuilt = bytearray()
        for m in matches:
            rebuilt.extend(data[cursor:m.start()])
            rebuilt.extend(patch.emit(m, "ty" in m.groupdict()))
            cursor = m.end()
        rebuilt.extend(data[cursor:])
        assert bytes(rebuilt) == after
        assert patch.patched_bytes(after) == (after, {})
    else:
        assert not stats and after == data
        matches = list(FIXED.finditer(data))
    assert len(matches) == 66, len(matches)
    check_original_sites(data, matches)
    if args.static_only:
        return
    # Exercise byte preservation and LF/CRLF idempotence independently of disk.
    sample = (b"static inline void ppu_vldu4(\xff\n" +
              b"{ uint32_t* d=(uint32_t*)&ctx->vr[1]; uint32_t* a=(uint32_t*)&ctx->vr[2]; "
              b"uint32_t* b=(uint32_t*)&ctx->vr[3]; for(int i=0;i<4;i++) d[i]=a[i]+b[i]; }\n\xfe")
    for content in (sample, sample.replace(b"\n", b"\r\n")):
        transformed, counts = patch.patched_bytes(content)
        assert counts == {"add": 1} and transformed.startswith(content.split(b"\n")[0])
        assert transformed.endswith(b"\xfe")
        assert patch.patched_bytes(transformed) == (transformed, {})

    spec = importlib.util.spec_from_file_location("ppu_lifter_word_test", LIFTER)
    lifter_module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = lifter_module
    spec.loader.exec_module(lifter_module)
    lifter = lifter_module.PPULifter()
    entries = []
    for m in matches:
        body = m[0]
        entries.append((body, *(int(m[k]) for k in ("d", "a", "b"))))
        if not args.baseline:
            op = operation(body)
            mn = {"add": "vadduwm", "sub": "vsubuwm", "signed_gt": "vcmpgtsw", "unsigned_gt": "vcmpgtuw"}[op]
            if b"c6=" in body:
                mn += "."
            insn = SimpleNamespace(addr=0, mnemonic=mn, operands=f"v{int(m['d'])}, v{int(m['a'])}, v{int(m['b'])}")
            assert lifter._translate_op(insn, lifter_module.LiftedFunction()).encode() == body
    for mn in ("vadduwm", "vsubuwm", "vcmpgtuw", "vcmpgtuw.", "vcmpgtsw", "vcmpgtsw."):
        for d, a, b in ((0, 1, 2), (1, 1, 2), (2, 1, 2), (1, 1, 1)):
            insn = SimpleNamespace(addr=0, mnemonic=mn, operands=f"v{d}, v{a}, v{b}")
            entries.append((lifter._translate_op(insn, lifter_module.LiftedFunction()).encode(), d, a, b))
    helper_start = data.index(b"static inline uint32_t ppu_vbswap32")
    helper_end = data.index(b"\n/* The guest timebase", helper_start)
    helpers = data[helper_start:helper_end].decode("ascii")
    cpp = ["#include <cstdint>\n#include <cstring>\n#include <cstdio>\n", helpers,
           "struct Context { uint8_t vr[32][16]; uint32_t cr; };\n"]
    for i, (body, _, _, _) in enumerate(entries):
        cpp.append(f"static void site{i}(Context* ctx) {{ {body.decode('ascii')} }}\n")
    cpp.append("int main() { unsigned checks=0, failures=0; Context ctx, expected;\n")
    pairs = [
        ([0xFF, 0xFFFF, 0xFFFFFF, 0xFFFFFFFF], [1, 1, 1, 1]),
        ([0, 0x100, 0x80000000, 0x7FFFFFFF], [1, 0xFF, 0x7FFFFFFF, 0x80000000]),
        ([0x100] * 4, [0xFF] * 4), ([0xFF] * 4, [0x100] * 4),
        ([0, 0x80000000, 0xFFFFFFFF, 0x100], [0, 0x80000000, 0xFFFFFFFF, 0x100]),
        ([0xFF000000, 0x00FF0000, 0x0000FF00, 0x000000FF], [0x01000000, 0x00010000, 0x00000100, 1]),
    ]
    rng = random.Random(0xBE4097)
    pairs += [([rng.getrandbits(32) for _ in range(4)], [rng.getrandbits(32) for _ in range(4)]) for _ in range(10)]
    for i, (body, d, a, b) in enumerate(entries):
        op = operation(body)
        for av, bv in pairs:
            if a == b:
                bv = av
            result = oracle(av, bv, op)
            cr = 0xA5B6C7D8
            if b"c6=" in body:
                cr = (cr & ~0xF0) | (0x80 if all(result) else 0) | (0x20 if not any(result) else 0)
            cpp.append(f"{{ uint8_t a[16]={{{byte_list(av)}}}, b[16]={{{byte_list(bv)}}}, want[16]={{{byte_list(result)}}}; "
                       f"memset(&ctx,0xCD,sizeof(ctx)); ctx.cr=0xA5B6C7D8u; memcpy(ctx.vr[{a}],a,16); memcpy(ctx.vr[{b}],b,16); "
                       f"expected=ctx; memcpy(expected.vr[{d}],want,16); expected.cr=0x{cr:08X}u; "
                       f"site{i}(&ctx); ++checks; failures+=memcmp(&ctx,&expected,sizeof(ctx))!=0; }}\n")
    cpp.append('printf("VMX word native: %u checks, %u failures\\n", checks, failures); return failures ? 1 : 0; }\n')
    compiler = shutil.which("clang++") or "C:/Program Files/LLVM/bin/clang++.exe"
    with tempfile.TemporaryDirectory(prefix="tj-vmx-word-") as tmp:
        source, exe = Path(tmp) / "word.cpp", Path(tmp) / "word.exe"
        source.write_text("".join(cpp), encoding="ascii")
        subprocess.run([compiler, "-O2", "-DNDEBUG", "-std=c++17", str(source), "-o", str(exe)], check=True)
        result = subprocess.run([str(exe)], check=False)
        if args.baseline:
            assert result.returncode == 1, "unfixed native sites unexpectedly passed"
        else:
            assert result.returncode == 0
    print("byte preservation, idempotence: PASS")
    if not args.baseline:
        print("all 66 generated sites match shared-lifter output: PASS")


if __name__ == "__main__":
    main()
