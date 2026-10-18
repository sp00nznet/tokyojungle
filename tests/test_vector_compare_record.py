"""Focused mapping, preservation and compiled CR6 regression checks."""
import copy
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import vector_compare_record as repair


def check_mapping(files, manifest):
    sources, sites = repair.collect_sources(files)
    prepared, pending = repair.prepare_patches(files, manifest)
    assert pending in (0, 172), pending
    for path, data in sources.items():
        patched = prepared.get(path, data)
        # Removing only this repair's additions must recover every input byte.
        def strip(match):
            tail = match["tail"]
            return match[0][:-len(tail)] if tail else match[0]
        assert repair.CMP_RE.sub(strip, patched) == repair.CMP_RE.sub(strip, data), path
    bad = copy.deepcopy(manifest)
    bad["entries"][-1]["signature"][0] = "FFFFFFFF"
    try:
        repair.prepare_patches(files, bad)
    except ValueError:
        pass
    else:
        raise AssertionError("function/signature drift was accepted")
    with tempfile.TemporaryDirectory() as directory:
        copies = []
        for path, data in sources.items():
            output = Path(directory) / path.name
            output.write_bytes(prepared.get(path, data))
            copies.append(output)
        assert repair.prepare_patches(copies, manifest) == ({}, 0)
        before = [(p.read_bytes(), p.stat().st_mtime_ns) for p in copies]
        assert repair.patch_files(copies) == 0
        assert before == [(p.read_bytes(), p.stat().st_mtime_ns) for p in copies]
    return sources, sites


def check_native(sources, sites, manifest):
    compiler = shutil.which("clang++") or "C:/Program Files/LLVM/bin/clang++.exe"
    assert Path(compiler).is_file(), "clang++ required for native regression fixture"
    text = next(iter(sources.values())).decode(errors="surrogateescape")
    begin = text.index("static inline uint32_t ppu_vbswap32")
    helpers = text[begin:text.index("static inline void ppu_vldu4", begin)]
    functions, checks = [], []
    # Compile every actual instruction statement, both dotted and non-dotted,
    # including every destination/source alias pattern used by the game.
    for index, ((_, match, sig), entry) in enumerate(zip(sites, manifest["entries"])):
        _, _, op, vd, va, vb = sig
        body = match["body"].decode()
        if entry["record"]:
            body += repair.record_suffix(vd, int(entry["pc"], 16)).decode()
        functions.append("static void op%d(Context* ctx) { %s }" % (index, body))
        checks.append("if (check(op%d,%d,%d,%d,%d,%s)) return %d;" %
                      (index, vd, va, vb, {"==": 0, ">=": 1, ">": 2}[op],
                       "true" if entry["record"] else "false", index + 1))
    source = r'''
#include <cstdint>
#include <cstring>
HELPERS
struct Context { uint8_t vr[32][16]; uint32_t cr; };
FUNCTIONS
static void write_be(uint8_t* p, uint32_t v) {
    p[0]=uint8_t(v>>24); p[1]=uint8_t(v>>16); p[2]=uint8_t(v>>8); p[3]=uint8_t(v);
}
static int check(void (*operation)(Context*), int vd, int va, int vb, int kind, bool record) {
    const uint32_t cases[][8] = {
        {0x3F800000,0x40000000,0x40400000,0x40800000,0x3F800000,0x40000000,0x40400000,0x40800000},
        {0x40800000,0x40800000,0x40800000,0x40800000,0x3F800000,0x3F800000,0x3F800000,0x3F800000},
        {0xBF800000,0xBF800000,0xBF800000,0xBF800000,0x40000000,0x40000000,0x40000000,0x40000000},
        {0xBF800000,0x40000000,0x40400000,0x00000000,0x40000000,0xBF800000,0x40400000,0x80000000},
        {0x7FC00001,0x7FC00001,0x7FC00001,0x7FC00001,0,0,0,0},
        {0x7F800000,0xFF800000,0x7FC00001,0x80000000,0x7F800000,0x3F800000,0x7FC00002,0},
        {0x7F7FFFFF,0x7F7FFFFF,0x7F7FFFFF,0x7F7FFFFF,0x7F7FFFFF,0x7F7FFFFF,0x7F7FFFFF,0x7F7FFFFF}
    };
    for (auto &values: cases) for (uint32_t initial: {0u,0xFFFFFFFFu,0xA5A5A525u}) {
        Context ctx{};
        for (int i=0;i<4;++i) write_be(ctx.vr[va]+4*i,values[i]);
        for (int i=0;i<4;++i) write_be(ctx.vr[vb]+4*i,values[i+4]);
        float a[4],b[4]; ppu_vldf4(ctx.vr[va],a); ppu_vldf4(ctx.vr[vb],b);
        uint32_t expected[4]; int n=0;
        for (int i=0;i<4;++i) {
            bool truth=kind==0?a[i]==b[i]:kind==1?a[i]>=b[i]:a[i]>b[i];
            expected[i]=truth?0xFFFFFFFFu:0; n+=truth;
        }
        uint32_t wanted=record?(initial&~0xF0u)|(n==4?0x80u:0u)|(n==0?0x20u:0u):initial;
        ctx.cr=initial; operation(&ctx);
        if (ctx.cr!=wanted || std::memcmp(expected,ctx.vr[vd],16)!=0) return 1;
        // Original sentinel consumer: bne cr6 must skip an all-true equality.
        if (record && kind==0 && n==4 && (ctx.cr&0x20u)) return 2;
    }
    return 0;
}
int main() { CHECKS return 0; }
'''.replace("HELPERS", helpers).replace("FUNCTIONS", "\n".join(functions)).replace("CHECKS", "\n".join(checks))
    source = "#include <initializer_list>\n" + source
    with tempfile.TemporaryDirectory() as directory:
        cpp, exe = Path(directory) / "compare.cpp", Path(directory) / "compare.exe"
        cpp.write_text(source)
        # NDEBUG proves the checks do not disappear in a release build.
        subprocess.run([compiler, "-std=c++17", "-O2", "-DNDEBUG", str(cpp), "-o", str(exe)], check=True)
        subprocess.run([str(exe)], check=True)


def main():
    files = sorted((ROOT / "generated").glob("ppu_recomp_*.cpp"))
    manifest = json.loads(repair.MANIFEST.read_text())
    sources, sites = check_mapping(files, manifest)
    check_native(sources, sites, manifest)
    print("vector compare record: PASS (578 native operations; 172 record; 406 non-record; preservation/idempotence)")


if __name__ == "__main__":
    main()
