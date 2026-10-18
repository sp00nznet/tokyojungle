"""Compile the actual backend snapshot gate with bounded capture substitutes."""
from pathlib import Path
import argparse
import os
import shutil
import subprocess
import tempfile

from test_fifo_present_boundary import function

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT.parent / "ps3recomp"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", action="store_true")
    args = parser.parse_args()
    backend = (RUNTIME / "libs/video/rsx_d3d12_backend.c").read_text(encoding="utf-8")
    body = function(backend, "static u32 vp_fragment_texture_mask(") + "\n" + function(
        backend, "static void vp_capture_draw_texture_snapshots(")
    # Compile the actual replay loop's gate, stale-owner reset and null-descriptor
    # path. Replace only the expensive materialization body with a call counter.
    start = backend.index("const u32 fragment_mask = vp_fragment_texture_mask(dr);",
                          backend.index("/* Fill this draw's t0-t15 SRV window."))
    end = backend.index("/* Vertex TXL uses its own four descriptors", start)
    prepare = backend[start:end]
    gate = "if ((fragment_mask & (1u << _u)) && dr->tex[_u].set)"
    branch = function(prepare, gate)
    prepare = prepare.replace(branch, gate + " {\n"
        "    ++prepares; prepared |= 1u << _u; dr->tex_rt[_u] = 42; continue;\n} ")
    if args.baseline:
        body = body.replace("!(fragment_mask & (1u << u)) || ", "")
    # Verify real ordering, not just the isolated gate.
    record = function(backend, "static void vp_record_cb(")
    assert record.index("rsx_fp_capture_structural(") < record.index("vp_capture_draw_texture_snapshots(")
    source = r'''
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "rsx_fp_decompiler.h"
#define RSX_MAX_TEXTURES 16
#define RSX_MAX_VERTEX_TEXTURES 4
#define DRAW_SRV_BASE 0
#define DRAW_TEXTURE_COUNT 20
#define DXGI_FORMAT_R8G8B8A8_UNORM 28
#define D3D12_DEFAULT_SHADER_4_COMPONENT_MAPPING 0
typedef struct { int unused; } VPTexSnapshot;
typedef struct {
    u32 off,w,h,depth,fmt,pitch,cube,dimension,mips,border_type,raw,location;
    int set;
} Tex;
typedef struct {
    int fp_program_valid; RSXFPStructuralProgram fp_program;
    Tex tex[16],vtex[4]; VPTexSnapshot* tex_snapshot[16];
    VPTexSnapshot* vtex_snapshot[4];
    u32 tex_snapshot_fault_mask,vtex_snapshot_fault_mask;
    int tex_rt[16],tex_display_parent_requires_fifo[16];
    int tex_display_parent_slot[16],tex_display_parent_record[16];
} D3D12DrawRecord;
static u32 captures, seen, fail_off=~0u;
static u32 prepares,prepared,nulls;
#define srv_write_dimension(slot,res,fmt,map,mips,dim,cube) (nulls |= 1u << (slot))
static int vp_record_texture_is_gpu_owned(const D3D12DrawRecord* d,u32 raw,u32 loc)
{ (void)d; (void)loc; return raw==100; }
static int vp_tex_source_has_border(u32 f,u32 b){(void)f;(void)b;return 0;}
static int vp_capture_tex_snapshot(u32 off,u32 w,u32 h,u32 d,u32 f,u32 p,
    int cube,u32 dim,u32 mips,int border,VPTexSnapshot** out)
{
    static VPTexSnapshot snapshot;
    ++captures; seen |= 1u<<off; *out=&snapshot;
    return off==fail_off ? -1 : 0;
}
BODY
static void prepare_bindings(D3D12DrawRecord* dr) {
    const u32 _d=0;
PREPARE
}
#define CHECK(c,m) do {if(!(c)){fprintf(stderr,"FAIL: %s\n",m);return 1;}}while(0)
int main(int argc,char** argv)
{
    D3D12DrawRecord dr={0};
    dr.fp_program_valid=1; dr.fp_program.texture_read_mask=1u<<9;
    for(u32 i=0;i<16;i++){dr.tex[i].set=1;dr.tex[i].off=i;}
    for(u32 i=0;i<4;i++){dr.vtex[i].set=1;dr.vtex[i].off=i+16;}
    if(argc>1 && atoi(argv[1])) {
        vp_capture_draw_texture_snapshots(&dr); prepare_bindings(&dr);
        CHECK(captures==20 && prepares==16 && prepared==0xffff && !nulls,
              "explicit shader probe lost bindings");
        puts("PASS: shader probe preserves all capture/replay bindings");
        return 0;
    }
    for(u32 i=0;i<16;i++) dr.tex_rt[i]=99;
    prepare_bindings(&dr);
    CHECK(prepares==2 && prepared==((1u<<0)|(1u<<9)) && nulls==(0xffff^prepared),
          "unused fragment bindings still materialized at replay");
    for(u32 i=0;i<16;i++)
        CHECK(dr.tex_rt[i]==((prepared&(1u<<i))?42:-1),"stale GPU owner survived");
    prepares=prepared=nulls=0; dr.fp_program.texture_read_mask=1u<<15;
    prepare_bindings(&dr);
    CHECK(prepares==2 && prepared==((1u<<0)|(1u<<15)),"changed program reused old mask");
    prepares=prepared=nulls=0; dr.fp_program_valid=0;
    prepare_bindings(&dr);
    CHECK(prepares==16 && !nulls,"unknown replay program lost bindings");
    prepares=prepared=nulls=0; dr.fp_program_valid=1;
    dr.fp_program.texture_read_mask=1u<<9; dr.tex[9].set=0;
    prepare_bindings(&dr);
    CHECK(prepares==1 && prepared==1,"disabled input was materialized");
    dr.tex[9].set=1;
    vp_capture_draw_texture_snapshots(&dr);
    CHECK(captures==6 && seen==((1u<<0)|(1u<<9)|(15u<<16)),
          "unused fragment bindings were copied or vertex/fallback inputs lost");
    captures=seen=0; dr.tex[9].raw=100;
    vp_capture_draw_texture_snapshots(&dr);
    CHECK(captures==5,"GPU-owned input became a CPU snapshot");
    captures=seen=0; dr.tex[9].raw=0; dr.fp_program_valid=0;
    vp_capture_draw_texture_snapshots(&dr);
    CHECK(captures==20,"invalid program stopped conserving all bindings");
    captures=seen=0; dr.fp_program_valid=1; fail_off=9;
    vp_capture_draw_texture_snapshots(&dr);
    CHECK(dr.tex_snapshot_fault_mask==(1u<<9),"used-input capture failure was hidden");
    puts("PASS: capture/replay gates, stale GPU owner, program changes, fallback, vertex, invalid and faults");
    return 0;
}
'''.replace("BODY\n", body + "\n").replace("PREPARE\n", prepare + "\n")
    clang = shutil.which("clang") or r"C:\Program Files\LLVM\bin\clang.exe"
    with tempfile.TemporaryDirectory(prefix="tj-tex-use-") as temp:
        path = Path(temp) / "test.c"
        path.write_text(source, encoding="utf-8")
        exe = Path(temp) / "test.exe"
        subprocess.run([clang, "-std=c11", "-D_CRT_SECURE_NO_WARNINGS", "-I",
                        str(RUNTIME / "include"), "-I", str(RUNTIME / "libs/video"),
                        str(path), "-o", str(exe)], check=True)
        probes = ("FP_FORCE", "TJ_FP_PROBE_T0", "TJ_FP_PROBE_T1",
                  "TJ_FP_PROBE_STAGEGRID", "TJ_FP_PROBE_LATEGRID")
        env = {k: v for k, v in os.environ.items() if k not in probes}
        subprocess.run([str(exe)], env=env, check=True)
        for probe in probes:
            subprocess.run([str(exe), "1"], env=dict(env, **{probe: "0"}), check=True)


if __name__ == "__main__":
    main()
