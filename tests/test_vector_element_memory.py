"""Exercise original VMX element sites, generator output, and guest-byte oracles."""
import argparse
import bisect
from collections import Counter
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys
import tempfile
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import vector_element_memory as patch

LIFTER = Path(__file__).resolve().parents[2] / "ps3recomp" / "tools" / "ppu_lifter.py"
ELF = Path(__file__).resolve().parents[1] / "input" / "EBOOT.ELF"
SOURCE = ROOT / "generated/ppu_recomp_000.cpp"
OPS = {7: "lvebx", 39: "lvehx", 71: "lvewx", 135: "stvebx", 167: "stvehx", 199: "stvewx"}


def operands(match):
    regs = [int(x) for x in re.findall(rb"gpr\[(\d+)\]", match["ea"])]
    ra, rb = regs if len(regs) == 2 else (0, regs[0])
    v = int(re.search(rb"vr\[(\d+)\]", match[0])[1])
    return v, ra, rb


def original_sites(data, matches, baseline):
    functions = list(re.finditer(rb"(?m)^void func_([0-9A-F]{8})\(ppu_context\* ctx\) \{", data))
    positions = [m.start() for m in functions]
    starts = [int(m[1], 16) for m in functions]
    elf = ELF.read_bytes()
    assert hashlib.sha256(elf).hexdigest() == "6d253f6429485b43b816fd6884611377626ee95db5dd9200ea58395c93a0812d"
    phoff = struct.unpack_from(">Q", elf, 32)[0]
    entsize, count = struct.unpack_from(">HH", elf, 54)
    segments = [struct.unpack_from(">IIQQQQQQ", elf, phoff + i * entsize) for i in range(count)]
    found = {}
    for function in json.loads((ROOT / "generated/EBOOT.functions.json").read_text()):
        lo, hi = int(function["start"], 16), int(function["end"], 16)
        if not starts[0] <= lo <= starts[-1]:
            continue
        for kind, flags, off, addr, _, size, _, _ in segments:
            if kind != 1 or not flags & 1 or not addr <= lo < hi <= addr + size:
                continue
            for pc in range(lo, hi, 4):
                word = struct.unpack_from(">I", elf, off + pc - addr)[0]
                mn = OPS.get((word >> 1) & 1023) if word >> 26 == 31 else None
                if mn:
                    found[pc] = (mn, (word >> 21) & 31, (word >> 16) & 31, (word >> 11) & 31)
            break
    original_counts = Counter(starts[bisect.bisect_right(starts, pc) - 1] for pc in found)
    actual_counts = Counter(starts[bisect.bisect_right(positions, m.start()) - 1] for m, _ in matches)
    assert original_counts == actual_counts, {
        f"{fn:08X}": (original_counts[fn], actual_counts[fn])
        for fn in original_counts.keys() | actual_counts.keys() if original_counts[fn] != actual_counts[fn]}
    assert len(found) == len(matches) == 844, (len(found), len(matches))
    for (pc, expected), (match, store) in zip(sorted(found.items()), matches):
        mn = patch.operation(match, store) if baseline else match["mn"].decode("ascii")
        actual = (mn, *operands(match))
        assert actual == expected, (hex(pc), actual, expected)
    print("Original ELF opcode, register operands and function ownership: 844/844 PASS")
    print("Instruction counts:", dict(Counter(site[0] for site in found.values())))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", action="store_true")
    args = parser.parse_args()
    data = SOURCE.read_bytes()
    after, counts = patch.patched_bytes(data)
    if args.baseline:
        matches = sorted([(m, False) for m in patch.LOAD.finditer(data)] +
                         [(m, True) for m in patch.STORE.finditer(data)], key=lambda x: x[0].start())
        assert sum(counts.values()) == 844, counts
        cursor, rebuilt = 0, bytearray()
        for match, store in matches:
            rebuilt.extend(data[cursor:match.start()])
            rebuilt.extend(patch.emit(match, store))
            cursor = match.end()
        rebuilt.extend(data[cursor:])
        assert bytes(rebuilt) == after
    else:
        assert not counts and after == data
        matches = [(m, m["mn"].startswith(b"st")) for m in patch.FIXED.finditer(data)]
    assert patch.patched_bytes(after) == (after, {})
    original_sites(data, matches, args.baseline)
    sample = b"\xff\n{ uint64_t ea = ctx->gpr[9]; memcpy(vm_base + (uint32_t)ea, &ctx->vr[10], 4); }\n\xfe"
    for content in (sample, sample.replace(b"\n", b"\r\n")):
        result, changed = patch.patched_bytes(content)
        assert changed == {"stvewx": 1} and result[:2] == content[:2] and result[-2:] == content[-2:]
        assert patch.patched_bytes(result) == (result, {})

    spec = importlib.util.spec_from_file_location("ppu_lifter_element_test", LIFTER)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    lifter = module.PPULifter()
    entries = []
    for match, store in matches:
        mn = patch.operation(match, store) if args.baseline else match["mn"].decode("ascii")
        v, ra, rb = operands(match)
        entries.append((match[0], mn, v, ra, rb))
        insn = SimpleNamespace(addr=0, mnemonic=mn, operands=f"v{v}, r{ra}, r{rb}")
        emitted = lifter._translate_op(insn, module.LiftedFunction()).encode()
        assert emitted == match[0], (mn, v, ra, rb, emitted, match[0])
    for mn in OPS.values():
        for v, ra, rb in ((0, 0, 9), (31, 4, 9), (13, 4, 4), (1, 0, 0), (2, 9, 0)):
            insn = SimpleNamespace(addr=0, mnemonic=mn, operands=f"v{v}, r{ra}, r{rb}")
            entries.append((lifter._translate_op(insn, module.LiftedFunction()).encode(), mn, v, ra, rb))
    cpp = ["#include <cstdint>\n#include <cstring>\n#include <cstdio>\n"
           "struct Context { uint64_t gpr[32]; uint8_t vr[32][16]; uint32_t cr, xer; };\n"
           "static uint8_t memory[1024], expected_memory[1024]; static uint8_t* vm_base=memory;\n"]
    for i, (body, _, _, _, _) in enumerate(entries):
        cpp.append(f"static void site{i}(Context* ctx) {{ {body.decode('ascii')} }}\n")
    cpp.append("int main() { unsigned checks=0, failures=0; Context ctx, expected;\n")
    for i, (_, mn, v, ra, rb) in enumerate(entries):
        size = {"b": 1, "h": 2, "w": 4}[mn[-2]]
        cpp.append(f"{{ for(unsigned low=0; low<16; ++low) {{ memset(&ctx,0xA5,sizeof(ctx)); "
                   "for(unsigned r=0;r<32;++r) { ctx.gpr[r]=0x100000040ULL; "
                   "for(unsigned b=0;b<16;++b) ctx.vr[r][b]=(uint8_t)(19*r+11*b+5); } "
                   f"ctx.gpr[{rb}]=0x100000080ULL+low; expected=ctx; "
                   "for(unsigned k=0;k<1024;++k) memory[k]=(uint8_t)(53*k+17); "
                   "memcpy(expected_memory,memory,sizeof(memory)); "
                   f"uint32_t address=(uint32_t)({'ctx.gpr['+str(ra)+']+' if ra else ''}ctx.gpr[{rb}]); ")
        if mn.startswith("st"):
            cpp.append(f"address &= ~{size-1}u; memcpy(expected_memory+address,ctx.vr[{v}]+(address%16),{size}); ")
        else:
            cpp.append(f"memcpy(expected.vr[{v}],memory+(address & ~15u),16); ")
        cpp.append(f"site{i}(&ctx); ++checks; failures += memcmp(&ctx,&expected,sizeof(ctx))!=0 || "
                   "memcmp(memory,expected_memory,sizeof(memory))!=0; } }\n")
    cpp.append('printf("VMX element native: %u checks, %u failures\\n",checks,failures); return failures?1:0; }\n')
    compiler = shutil.which("clang++") or "C:/Program Files/LLVM/bin/clang++.exe"
    with tempfile.TemporaryDirectory(prefix="tj-vmx-element-") as tmp:
        source, exe = Path(tmp)/"elements.cpp", Path(tmp)/"elements.exe"
        source.write_text("".join(cpp), encoding="ascii")
        subprocess.run([compiler,"-O1","-DNDEBUG","-std=c++17",str(source),"-o",str(exe)],check=True)
        result = subprocess.run([str(exe)],check=False)
        assert result.returncode == (1 if args.baseline else 0)
    print("All 844 sites match shared generator; non-target bytes and idempotence: PASS")


if __name__ == "__main__":
    main()
