"""Compile actual depth ownership, sizing and binding policy; no game launch."""
from pathlib import Path
import subprocess
import tempfile
from test_fifo_present_boundary import function

ROOT = Path(__file__).resolve().parents[1]


def main():
    backend = (ROOT.parent / "ps3recomp/libs/video/rsx_d3d12_backend.c").read_text()
    source = r'''
#include <stdint.h>
#include <stdio.h>
typedef uint32_t u32;
typedef uint64_t u64;
#define MAX_DEPTH_SURFACES 31
typedef struct { u32 fmt,mips,raw,location,pitch,w,h; } Texture;
typedef struct { Texture tex[16]; u32 fp_addr;
    u32 rt_w,rt_h,rt_clip_w,rt_clip_h,rt_clip_x,rt_clip_y,rt_window_x,rt_window_y;
    u32 zeta_format,zeta_pitch,vp_x,vp_y,vp_w,vp_h;
    u32 rt_target,mrt_key,is_vp,is_clear,fp_program_valid;
    u32 scissor_x,scissor_y,scissor_w,scissor_h;
} D3D12DrawRecord;
typedef int32_t LONG;
typedef struct { LONG left,top,right,bottom; } D3D12_RECT;
typedef struct { int valid,format; void *res; u32 raw,local,pitch,w,h;
                 int initialized; u32 first_write_record_plus_one; } DepthSurface;
static struct { DepthSurface depth_surface[MAX_DEPTH_SURFACES]; } s_d3d;
static int tj_indexed_diag_enabled(void) { return 0; }
'''
    source += function(backend, "static int depth_surface_texture_find(")
    source += function(backend, "static void depth_surface_extent(")
    source += function(backend, "static int dr_num_rts(")
    source += function(backend, "static D3D12_RECT draw_scissor_rect(")
    source += r'''
static unsigned tests, failures;
static void check(D3D12DrawRecord d, u32 record, int expected) {
    int got=depth_surface_texture_find(&d,0,record); ++tests;
    if(got!=expected) { ++failures; printf("FAIL case%u got%d expected%d\n",tests,got,expected); }
}
static void extent(D3D12DrawRecord d, u32 ew, u32 eh) {
    u32 w,h; depth_surface_extent(&d,1280,720,&w,&h); ++tests;
    if(w!=ew || h!=eh) { ++failures; printf("FAIL extent%u got%ux%u expected%ux%u\n",tests,w,h,ew,eh); }
}
static void targets(D3D12DrawRecord d, int expected) {
    int got=dr_num_rts(&d); ++tests;
    if(got!=expected) { ++failures; printf("FAIL targets%u got%d expected%d\n",tests,got,expected); }
}
static void scissor(D3D12DrawRecord d, u32 w, u32 h, LONG bottom) {
    D3D12_RECT r=draw_scissor_rect(&d,0,0,w,h); ++tests;
    if(r.bottom!=bottom) { ++failures; printf("FAIL scissor%u got%d expected%d\n",tests,r.bottom,bottom); }
}
int main(void) {
    D3D12DrawRecord base={{{0xA5,1,0x0F110000,1,4096,1024,1024}}}, d;
    DepthSurface ds={1,2,(void*)(uintptr_t)1,0x0F110000,1,4096,1024,1024,0,0};
    s_d3d.depth_surface[3]=ds;
    check(base,5,-1); /* allocated, but never written */
    s_d3d.depth_surface[3].first_write_record_plus_one=6;
    check(base,4,-1); check(base,5,-1); check(base,6,3); /* future, same, prior */
    s_d3d.depth_surface[3].initialized=1;
    check(base,0,3); /* previous frame remains a valid old-content producer */
    d=base; d.tex[0].fmt=0xE5; check(d,6,3); /* unnormalized linear coordinates */
    d=base; d.tex[0].fmt=0x85; check(d,6,-1);
    d=base; d.tex[0].fmt=0xA8; check(d,6,-1);
    d=base; d.tex[0].mips=2; check(d,6,-1);
    d=base; ++d.tex[0].raw; check(d,6,-1);
    d=base; d.tex[0].location=2; check(d,6,-1);
    d=base; d.tex[0].pitch=2048; check(d,6,-1);
    d=base; d.tex[0].w=512; check(d,6,-1);
    d=base; d.tex[0].h=512; check(d,6,-1);
    s_d3d.depth_surface[3].format=1; check(base,6,-1);
    s_d3d.depth_surface[3]=ds; s_d3d.depth_surface[3].valid=0; check(base,6,-1);
    s_d3d.depth_surface[3]=ds; s_d3d.depth_surface[3].initialized=1;
    s_d3d.depth_surface[3].res=0; check(base,6,-1);
    d=(D3D12DrawRecord){0}; d.zeta_format=2; d.zeta_pitch=4096;
    d.rt_clip_w=1024; d.rt_clip_h=1024; d.vp_w=1280; d.vp_h=720;
    extent(d,1024,1024); /* shadow using an unused registered display colour */
    d.rt_clip_w=1280; d.rt_clip_h=720; d.zeta_pitch=5120; extent(d,1280,720);
    d.rt_clip_w=800; d.rt_clip_h=600; d.zeta_pitch=4096; extent(d,1024,600);
    d.rt_clip_w=512; d.rt_clip_h=512; d.zeta_format=1; d.zeta_pitch=1024; extent(d,512,512);
    d.rt_clip_x=16; d.rt_clip_y=8; d.rt_window_x=32; d.rt_window_y=24; extent(d,544,536);
    d=(D3D12DrawRecord){0}; extent(d,1280,720);
    d.vp_w=1920; d.vp_h=1080; extent(d,1920,1080);
    d.rt_clip_w=1024; d.rt_clip_h=720; d.rt_clip_x=UINT32_MAX; extent(d,UINT32_MAX,720);
    d=(D3D12DrawRecord){0}; d.rt_target=1; d.is_vp=1; d.fp_addr=0x7600741;
    d.fp_program_valid=1;
    targets(d,0); /* no colour writes: do not bind the unused colour backing */
    d.mrt_key=15; targets(d,1);
    d.mrt_key=0xFFF0; targets(d,0); /* disabled MRT masks cannot enable colour A */
    d.rt_target=0x13; targets(d,2);
    d.rt_target=0x17; targets(d,3);
    d.rt_target=0x1F; targets(d,4);
    d.mrt_key=0x70000; targets(d,0); /* blend enables alone are not writes */
    d.mrt_key=0x1000; targets(d,4); /* D alone writes; preserve all four slots */
    d.mrt_key=0; d.is_clear=1; targets(d,4);
    d.is_clear=0; d.is_vp=0; targets(d,4);
    d.is_vp=1; d.fp_program_valid=0; targets(d,4);
    d.fp_program_valid=1; d.fp_addr=0; targets(d,4);
    d.scissor_w=1024; d.scissor_h=1024;
    scissor(d,1280,720,720); /* previous colour-relative clipping loses 304 rows */
    scissor(d,1024,1024,1024);
    d.scissor_y=1000; d.scissor_h=100; scissor(d,1024,1024,1024);
    d.scissor_y=1025; scissor(d,1024,1024,1024);
    printf("depth ownership/extent/binding: %u checks, %u failures\n",tests,failures);
    return failures!=0;
}
'''
    with tempfile.TemporaryDirectory(prefix="tj-depth-owner-") as temp:
        path = Path(temp)
        c, exe = path / "owner.c", path / "owner.exe"
        c.write_text(source)
        subprocess.run(["C:/Program Files/LLVM/bin/clang.exe", "-std=c17", str(c), "-o", str(exe)], check=True)
        subprocess.run([str(exe)], check=True)
    # Guard the replay connections as well as testing the production helpers.
    assert "draw_color_count != cur_color_count" in backend
    assert "nrt, nrt ? rh : NULL, FALSE, &cur_dsv" in backend
    assert "draw_viewport_rect(dr, 0, 0, dw, dh)" in backend
    assert "draw_scissor_rect(dr, 0, 0, dw, dh)" in backend
    assert "if (nrt < 0) nrt = 0;" in backend
    assert "texture_state, nrt ? nrt : 1)" in backend


if __name__ == "__main__":
    main()
