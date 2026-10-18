"""Ensure diagnostic write watches do not enable guest reservation invalidation."""
from pathlib import Path
import shutil
import subprocess
import tempfile

from test_fifo_present_boundary import function

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT.parent / "ps3recomp"


def main():
    loader = (RUNTIME / "runtime/ppu/ppu_loader.cpp").read_text(encoding="utf-8")
    init = function(loader, "static BOOL CALLBACK ppu_resv_init_store_active(")
    hook = function(loader, 'extern "C" void ppu_resv_break_store(')
    source = r'''
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#define CALLBACK
#define TRUE 1
typedef int BOOL;
typedef void* PINIT_ONCE;
typedef void* PVOID;
struct ppu_context { uint64_t reserve_addr; };
#define PPU_RESV_MAX 4
#define PPU_RESV_INVALID 0x100000000ull
static ppu_context* g_resv_ctxs[PPU_RESV_MAX];
static long g_resv_ctx_n;
static int g_resv_store_active;
static bool env_resv,env_watch;
static unsigned traces,checks,failures;
static const char* test_getenv(const char* key) {
    return ((!strcmp(key,"PPU_RESV_STORE") && env_resv) ||
            (!strcmp(key,"YDKJ_WWATCH") && env_watch)) ? "1" : nullptr;
}
#define getenv test_getenv
static void ppu_resv_iwwatch(uint64_t,uint32_t) { if(env_watch) ++traces; }
INIT
HOOK
static void check(bool ok,const char* label) {
    ++checks; if(!ok) { ++failures; printf("FAIL %s resv=%d watch=%d\n",label,env_resv,env_watch); }
}
static void probe(uint64_t ea,uint32_t width,bool expected_overlap) {
    ppu_context watched={0x40},other={0x80},invalid={PPU_RESV_INVALID};
    g_resv_ctxs[0]=&watched; g_resv_ctxs[1]=&other;
    g_resv_ctxs[2]=nullptr; g_resv_ctxs[3]=&invalid; g_resv_ctx_n=4;
    traces=0;
    if(g_resv_store_active>0) ppu_resv_break_store(ea,width);
    check(watched.reserve_addr==(env_resv && expected_overlap?PPU_RESV_INVALID:0x40),
          "reservation policy and byte overlap");
    check(other.reserve_addr==0x80 && invalid.reserve_addr==PPU_RESV_INVALID,
          "other reservations preserved");
    check(traces==(env_watch && width && ea+width>=ea?1u:0u),"watch remains independent");
}
int main() {
    for(unsigned mode=0;mode<4;++mode) {
        env_resv=(mode&1)!=0; env_watch=(mode&2)!=0;
        ppu_resv_init_store_active(nullptr,nullptr,nullptr);
        check((g_resv_store_active>0)==(env_resv||env_watch),"fast-path gating");
        probe(0x40,1,true); probe(0x43,1,true); probe(0x44,1,false);
        probe(0x3F,2,true); probe(0x3D,4,true); probe(0x39,8,true);
        probe(0x38,8,false); probe(0x40,0,false); probe(UINT64_MAX,4,false);
    }
    printf("Write-watch reservation modes: %u checks, %u failures\n",checks,failures);
    return failures?1:0;
}
'''.replace("INIT\n", init + "\n").replace("HOOK\n", hook + "\n")
    clang = shutil.which("clang++") or r"C:\Program Files\LLVM\bin\clang++.exe"
    with tempfile.TemporaryDirectory(prefix="tj-watch-reservation-") as temp:
        cpp, exe = Path(temp) / "modes.cpp", Path(temp) / "modes.exe"
        cpp.write_text(source, encoding="utf-8")
        subprocess.run([clang, "-std=c++20", "-O2", str(cpp), "-o", str(exe)], check=True)
        result = subprocess.run([str(exe)], check=False)
    memory = (RUNTIME / "runtime/ppu/ppu_memory.h").read_text(encoding="utf-8")
    for width in (1, 2, 4, 8):
        assert "if (g_resv_store_active > 0) ppu_resv_break_store(addr, %d);" % width in memory
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
