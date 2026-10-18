"""Compile production clear-region functions; no game/window is created."""
from pathlib import Path
import subprocess
import tempfile
from test_fifo_present_boundary import function

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT.parent / "ps3recomp/libs/video/rsx_d3d12_backend.c"


def main():
    backend = BACKEND.read_text()
    source = r'''
#include <stdint.h>
#include <stdio.h>
typedef uint32_t u32;
typedef uint64_t u64;
typedef int32_t LONG;
typedef struct { LONG left,top,right,bottom; } D3D12_RECT;
typedef struct { u32 scissor_x,scissor_y,scissor_w,scissor_h,rt_w,rt_h; } D3D12DrawRecord;
'''
    source += function(backend, "static D3D12_RECT draw_scissor_rect(")
    source += function(backend, "static int draw_clear_rect(")
    source += r'''
static unsigned tests,failures;
static void check(D3D12DrawRecord d,u32 ox,u32 oy,u32 bw,u32 bh,
                  int yes,LONG l,LONG t,LONG r,LONG b) {
    D3D12_RECT out={0};
    int got=draw_clear_rect(&d,ox,oy,bw,bh,&out);
    tests++;
    if(got!=yes || (yes && (out.left!=l || out.top!=t || out.right!=r || out.bottom!=b))) {
        failures++; printf("FAIL region %u result=%d rect=%d,%d,%d,%d\n",
                           tests,got,out.left,out.top,out.right,out.bottom);
    }
}
int main(void) {
    check((D3D12DrawRecord){0,0,8,8,8,8},0,0,8,8,1,0,0,8,8);
    check((D3D12DrawRecord){2,3,4,2,8,8},0,0,8,8,1,2,3,6,5);
    check((D3D12DrawRecord){2,3,4,2,8,8},16,32,64,64,1,18,35,22,37);
    check((D3D12DrawRecord){6,6,99,99,8,8},0,0,8,8,1,6,6,8,8);
    check((D3D12DrawRecord){0,0,99,99,8,8},6,6,8,8,1,6,6,8,8);
    check((D3D12DrawRecord){1,1,99,99,0,0},4,4,8,8,1,5,5,8,8);
    check((D3D12DrawRecord){0,0,99,99,4,4},0,0,8,8,1,0,0,4,4);
    check((D3D12DrawRecord){0,0,0,8,8,8},0,0,8,8,0,0,0,0,0);
    check((D3D12DrawRecord){8,0,8,8,8,8},0,0,8,8,0,0,0,0,0);
    check((D3D12DrawRecord){0,0,8,8,8,8},8,0,8,8,0,0,0,0,0);
    check((D3D12DrawRecord){UINT32_MAX,0,UINT32_MAX,8,8,8},0,0,8,8,0,0,0,0,0);
    printf("clear regions: %u checks, %u failures\n",tests,failures);
    return failures!=0;
}
'''
    with tempfile.TemporaryDirectory(prefix="tj-clear-region-") as temp:
        path = Path(temp)
        c = path / "region.c"
        exe = path / "region.exe"
        c.write_text(source)
        subprocess.run(["C:/Program Files/LLVM/bin/clang.exe", "-std=c17", str(c), "-o", str(exe)], check=True)
        subprocess.run([str(exe)], check=True)
    # Wiring guards: clear coverage cannot claim untouched RGBA/pixels; replay
    # must restore cached bindings after the separate masked-clear root/RTV.
    assert "(dr->clear_flags & RSX_CLEAR_COLOR_MASK) != RSX_CLEAR_COLOR_MASK" in backend
    assert "dr->clear_depth, dr->clear_stencil, 1, &rect" in backend
    assert "!dr->is_clear && draw_color_count != 0 &&" in backend
    assert "off_rt_draw_has_mrt_alias(want, wantm)" in backend
    assert "if (masked_clear_bound)" in backend and "active_root_mode = -1;" in backend
    print("clear replay/coverage wiring: passed")


if __name__ == "__main__":
    main()
