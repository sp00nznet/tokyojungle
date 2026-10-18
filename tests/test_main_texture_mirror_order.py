"""Exercise production MAIN mirror lookups with prior/same/future FIFO writers.

Host-only ownership proof, not D3D12 or gameplay proof. Old signatures remain
callable so the test records behavioral failures before the ordering repair.
"""
from pathlib import Path
import shutil
import subprocess
import tempfile

from test_fifo_present_boundary import function

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT.parent / "ps3recomp"


def main():
    backend = (RUNTIME / "libs/video/rsx_d3d12_backend.c").read_text(encoding="utf-8")
    find = function(backend, "static int gpu_mirror_find_canonical_containing(")
    overlap = function(backend, "static int gpu_mirror_canonical_overlap(")
    ordered = "u32 before_record" in find and "u32 before_record" in overlap
    args = ", record" if ordered else ""
    source = r'''
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include "rsx_scaled_blit.h"
typedef uint32_t u32;
typedef uint64_t u64;
#define MAX_GPU_MIRRORS 4
typedef struct {
    int valid, immutable_source, local;
    u32 pitch, w, h, canonical_ea, first_write_record_plus_one;
    u64 canonical_span, mapping_generation;
} GpuMirror;
static struct { GpuMirror gpu_mirror[MAX_GPU_MIRRORS]; } s_d3d;
static u64 current_generation=2;
static u64 cellGcmMainMappingGeneration(void) { return current_generation; }
FIND
OVERLAP
static unsigned checks, failures;
#define CHECK(c,m) do { ++checks; if (!(c)) { ++failures; printf("FAIL: %s\n",m); } } while(0)
static void probe(u32 record,u32 ea,u32 pitch,u32 w,u32 h,u64 gen,
                  int expected_find,int expected_overlap,const char* label) {
    u32 x=0,y=0;
    int found=gpu_mirror_find_canonical_containing(ea,pitch,w,h,gen ORDER_ARG,&x,&y);
    int aliases=gpu_mirror_canonical_overlap(ea,(u64)pitch*(h-1)+(u64)w*4,gen ORDER_ARG);
    ++checks;
    if(found!=expected_find) { ++failures; printf("FAIL %s: find=%d expected=%d\n",label,found,expected_find); }
    ++checks;
    if(aliases!=expected_overlap) { ++failures; printf("FAIL %s: overlap=%d expected=%d\n",label,aliases,expected_overlap); }
}
int main(void) {
    GpuMirror base={1,0,0,64,16,16,0x1000,11,1024,2};
    s_d3d.gpu_mirror[0]=base;
    probe(9,0x1000,64,16,16,2,-1,0,"future exact backing");
    probe(10,0x1000,64,16,16,2,-1,0,"same-record write is not prior");
    probe(11,0x1000,64,16,16,2,0,1,"prior exact backing");
    probe(9,0x1044,64,8,8,2,-1,0,"future interior view");
    probe(11,0x1044,64,8,8,2,0,1,"prior interior remains visible");
    probe(9,0x0f00,64,16,8,2,-1,0,"future partial overlap");
    probe(11,0x0f00,64,16,8,2,-1,1,"prior partial overlap still rejected");
    probe(9,0x1000,128,16,8,2,-1,0,"future different pitch");
    probe(11,0x1000,128,16,8,2,-1,1,"prior different pitch still rejected");
    probe(UINT32_MAX,0x1000,64,16,16,2,0,1,"preflight sees every backing");
    s_d3d.gpu_mirror[0].first_write_record_plus_one=0;
    probe(11,0x1000,64,16,16,2,-1,0,"allocated but unwritten");
    probe(UINT32_MAX,0x1000,64,16,16,2,0,1,"preflight reserves unwritten backing");
    s_d3d.gpu_mirror[0]=base; s_d3d.gpu_mirror[0].mapping_generation=4;
    probe(9,0x1000,64,16,16,2,-1,0,"future backing cannot invalidate earlier snapshot");
    probe(11,0x1000,64,16,16,2,-2,-1,"prior backing mapping drift remains fatal");
    s_d3d.gpu_mirror[0]=base; current_generation=4;
    probe(9,0x1000,64,16,16,2,-2,-1,"live mapping drift remains fatal");
    current_generation=3;
    probe(9,0x1000,64,16,16,3,-2,-1,"in-progress mapping remains fatal");
    current_generation=2; s_d3d.gpu_mirror[1]=base;
    s_d3d.gpu_mirror[1].first_write_record_plus_one=5;
    probe(6,0x1000,64,16,16,2,1,1,"future slot cannot hide prior owner");
    memset(s_d3d.gpu_mirror,0,sizeof(s_d3d.gpu_mirror));
    s_d3d.gpu_mirror[0]=base; s_d3d.gpu_mirror[0].immutable_source=1;
    probe(20,0x1000,64,16,16,2,-1,0,"immutable snapshot excluded");
    s_d3d.gpu_mirror[0]=base; s_d3d.gpu_mirror[0].local=1;
    probe(20,0x1000,64,16,16,2,-1,0,"LOCAL is not MAIN");
    s_d3d.gpu_mirror[0]=base; s_d3d.gpu_mirror[0].valid=0;
    probe(20,0x1000,64,16,16,2,-1,0,"invalid slot excluded");
    s_d3d.gpu_mirror[0]=base;
    probe(20,0x2000,64,16,16,2,-1,0,"disjoint snapshot");
    u32 x=0,y=0;
    CHECK(rsx_scaled_blit_alias_origin(0x1000,0x1044,64,16,16,8,8,&x,&y) && x==1 && y==1,
          "production alias origin changed");
    printf("MAIN texture ordering: %u checks, %u failures\n",checks,failures);
    return failures ? 1 : 0;
}
'''.replace("FIND\n", find + "\n").replace("OVERLAP\n", overlap + "\n").replace(" ORDER_ARG", args)
    clang = shutil.which("clang") or r"C:\Program Files\LLVM\bin\clang.exe"
    with tempfile.TemporaryDirectory(prefix="tj-main-texture-order-") as temp:
        path = Path(temp)
        c, exe = path / "order.c", path / "order.exe"
        c.write_text(source, encoding="utf-8")
        subprocess.run([clang, "-std=c11", "-D_CRT_SECURE_NO_WARNINGS", "-I",
                        str(RUNTIME / "include"), "-I", str(RUNTIME / "libs/video"),
                        str(c), "-o", str(exe)], check=True)
        result = subprocess.run([str(exe)], check=False)
    if ordered:
        # Guard actual replay/preflight wiring as well as isolated helpers.
        texture = backend[backend.index("u32 tex_ea = 0, tx = 0, ty = 0;"):]
        texture = texture[:texture.index("VPTexFormatInfo tex_info;")]
        assert "tex_generation, _d, &tx, &ty" in texture
        assert "tex_ea, tex_span, tex_generation, _d" in texture
        assert "mirror >= 0 && (tx || ty ||" in texture
        assert "mapping_generation, UINT32_MAX" in backend
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
