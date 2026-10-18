"""Byte preservation/idempotence and native observer state-preservation proof."""
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from loader_result_trace import SITES, injection, patch_bytes
from test_fifo_present_boundary import function


class LoaderResultTrace(unittest.TestCase):
    def fixture(self, eol):
        result = b"/* non-UTF8 \x81\xFF */" + eol
        for owner, sites in SITES.items():
            result += ("void func_%s(ppu_context* ctx) {" % owner).encode() + eol
            for lr, callee in sites:
                result += ("        ctx->lr = 0x%s; func_%s(ctx); DRAIN_TRAMPOLINE(ctx);"
                           % (lr, callee)).encode() + eol
            result += b"        return;" + eol + b"}" + eol
        return result

    def test_exact_insertions_and_idempotence(self):
        for eol in (b"\n", b"\r\n"):
            original = self.fixture(eol)
            patched, count = patch_bytes(original)
            self.assertEqual(count, 14)
            self.assertEqual(patch_bytes(patched), (patched, 0))
            stripped = patched
            for sites in SITES.values():
                for lr, callee in sites:
                    stripped = stripped.replace(injection(lr, callee, eol), b"", 1)
            self.assertEqual(stripped, original)

    def test_wrong_or_duplicate_call_refused(self):
        original = self.fixture(b"\n")
        with self.assertRaises(ValueError):
            patch_bytes(original.replace(b"func_00091584(ctx)", b"func_00091588(ctx)"))
        line = b"        ctx->lr = 0x001EFCA8; func_001E1854(ctx); DRAIN_TRAMPOLINE(ctx);\n"
        with self.assertRaises(ValueError):
            patch_bytes(original.replace(line, line + line))

    def test_unrelated_chunk_unchanged(self):
        original = b"void func_00200000(ppu_context* ctx) {\n/* \x81 */\n}\n"
        self.assertEqual(patch_bytes(original), (original, 0))

    def test_observer_preserves_context_and_is_bounded(self):
        native = (ROOT / "src/tj_crt_overrides.cpp").read_text(encoding="utf-8")
        observer = function(native, "void tj_survival_loader_result_trace(")
        source = r'''
#include <stdint.h>
#include <string.h>
#include <stdarg.h>
#include <stdio.h>
typedef long LONG;
struct ppu_context { uint64_t gpr[32],lr,thread_id; uint32_t cr; };
static LONG InterlockedIncrement(volatile LONG* value) { return ++*value; }
static bool enabled=false;
static unsigned prints=0;
static char* test_getenv(const char*) { return enabled ? (char*)"1" : nullptr; }
static int test_fprintf(FILE*,const char*,...) { ++prints; return 0; }
#define getenv test_getenv
#define fprintf test_fprintf
OBSERVER
int main() {
    ppu_context ctx,before; memset(&ctx,0xA5,sizeof(ctx)); before=ctx;
    tj_survival_loader_result_trace(nullptr,0,0);
    tj_survival_loader_result_trace(&ctx,0x001EFD30,0x00091584);
    if(prints || memcmp(&ctx,&before,sizeof(ctx))) return 1;
    enabled=true;
    for(unsigned i=0;i<160;++i) {
        ctx.gpr[3]=i%3; before=ctx;
        tj_survival_loader_result_trace(&ctx,0x001EFD30,0x00091584);
        if(memcmp(&ctx,&before,sizeof(ctx))) return 2;
    }
    return prints==128 ? 0 : 3;
}
'''.replace("OBSERVER\n", observer + "\n")
        clang = shutil.which("clang++") or r"C:\Program Files\LLVM\bin\clang++.exe"
        with tempfile.TemporaryDirectory(prefix="tj-loader-result-") as temp:
            cpp, exe = Path(temp) / "trace.cpp", Path(temp) / "trace.exe"
            cpp.write_text(source, encoding="utf-8")
            subprocess.run([clang, "-std=c++20", "-O2", str(cpp), "-o", str(exe)], check=True)
            subprocess.run([str(exe)], check=True)


if __name__ == "__main__":
    unittest.main()
