"""Compile production copy recording/endpoint routing; no GPU or user save."""
from pathlib import Path
import shutil
import subprocess
import tempfile
from test_fifo_present_boundary import function

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT.parent / "ps3recomp"


def main():
    backend = (RUNTIME / "libs/video/rsx_d3d12_backend.c").read_text(encoding="utf-8")
    body = "\n".join(function(backend, signature) for signature in (
        "static int tj_main_overwrite_enabled(",
        "static int gpu_blit_endpoint_get(",
        "static int exact_writeback_authoritative_source(",
        "int rsx_d3d12_backend_record_exact_blit("))
    # Production is C, which permits the reject jump past this declaration.
    # Hoist only the declaration in this C++ fixture, retaining its assignment
    # and every ownership/rejection statement at the original position.
    body = body.replace("int rsx_d3d12_backend_record_exact_blit(const RsxD3D12ExactBlit* blit)\n{",
                        "int rsx_d3d12_backend_record_exact_blit(const RsxD3D12ExactBlit* blit)\n{\n    D3D12DrawRecord* dr;")
    body = body.replace("D3D12DrawRecord* dr = &s_d3d.draws[s_d3d.draw_count++];",
                        "dr = &s_d3d.draws[s_d3d.draw_count++];")
    source = r'''
#include <stdint.h>
#include <stddef.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "rsx_d3d12_backend.h"
#define MAX_GPU_MIRRORS 4
#define MAX_DISPLAY_SURFACES 8
#define MAX_DRAWS 4
enum {GPU_BLIT_DISPLAY=1,GPU_BLIT_OFFRT=2,GPU_BLIT_MIRROR=3};
typedef struct {int kind,slot;u32 x,y;} GpuBlitEndpoint;
typedef struct {int unused;} CellGcmDisplayInfo;
typedef struct {int is_vp,is_scaled_blit,rt_is_display,rt_target;u32 rt_display_raw,rt_raw[4];RsxD3D12ExactBlit scaled_blit;} D3D12DrawRecord;
static struct {
 int initialized,gcm2d_backend_halted;u32 draw_count;
 const char* gcm2d_endpoint_reason;
 struct {int immutable_source,authoritative;} gpu_mirror[MAX_GPU_MIRRORS];
 struct {int valid;} display_surface[MAX_DISPLAY_SURFACES];
 D3D12DrawRecord draws[MAX_DRAWS];
} s_d3d;
static int faulted,mirror_calls,local_calls,mirror_fail,find_result=-1,rt_result=-1,overlap;
static u64 generation=2;
static u8 seen_local;static u32 seen_raw,seen_overwrite;static const u8* seen_snapshot;static u64 seen_bytes;
static int rsx_survival_trace_is_armed(){return 0;}
int cellGcmGetDisplaySurfaceContaining(u32,u32,u32,u32,CellGcmDisplayInfo*,u32*,u32*){++local_calls;return 0;}
int cellGcmGetDisplaySurfaceInfo(u32,u32,u32,u32,CellGcmDisplayInfo*){++local_calls;return 0;}
static int display_surface_get(const CellGcmDisplayInfo*){return -1;}
static int off_rt_find_containing(u8,u32,u32,u32,u32,u32*,u32*){return rt_result;}
static int gpu_main_canonical_identity(u32 raw,u32 pitch,u32,u32 h,u32* ea,u64* span,u64* gen){*ea=raw+0x4C00000;*span=u64(pitch)*h;*gen=generation;return 0;}
static int gpu_mirror_find_canonical_containing(u32,u32,u32,u32,u64,u32,u32*,u32*){return find_result;}
static int gpu_mirror_canonical_overlap(u32,u64,u64,u32){return overlap;}
static int gpu_mirror_get(u8 local,u32 raw,u32 pitch,u32,u32 h,const u8* data,u64 bytes,int immutable,u32 overwrite){
 ++mirror_calls;seen_local=local;seen_raw=raw;seen_snapshot=data;seen_bytes=bytes;seen_overwrite=overwrite;
 if(mirror_fail || !data || bytes<u64(pitch)*h)return -1;
 s_d3d.gpu_mirror[0].immutable_source=immutable;return 0;
}
static void rsx_d3d12_blit_snapshot_release(u8* snapshot,u64 bytes){(void)bytes;free(snapshot);}
void rsx_d3d12_backend_note_exact_blit_fault(){++faulted;}
FUNCTIONS
static unsigned checks,failed;
#define CHECK(c) do{++checks;if(!(c)){++failed;printf("FAIL line %d\n",__LINE__);}}while(0)
static void release(){for(u32 i=0;i<s_d3d.draw_count;++i){auto& b=s_d3d.draws[i].scaled_blit;free(b.src_snapshot);free(b.dst_snapshot);free(b.dst_post_snapshot);}s_d3d.draw_count=0;}
static RsxD3D12ExactBlit mapcopy(){
 RsxD3D12ExactBlit b={};b.src_raw=0x9923280;b.dst_raw=0x3B40000;
 b.src_ea=0xC9923280;b.dst_ea=0x8740000;b.mapping_generation=2;
 b.src_pitch=1440;b.dst_pitch=1472;b.src_w=b.dst_w=b.copy_w=360;b.src_h=b.dst_h=b.copy_h=192;
 b.src_local=b.src_cpu_owned=1;b.src_snapshot_bytes=1440*192;b.dst_snapshot_bytes=1472*192;
 b.dst_post_snapshot_bytes=360*192*4;b.src_snapshot=(u8*)malloc(b.src_snapshot_bytes);
 b.dst_snapshot=(u8*)malloc(b.dst_snapshot_bytes);b.dst_post_snapshot=(u8*)malloc(b.dst_post_snapshot_bytes);return b;
}
int main(){
 const char* flag=getenv("TJ_MAIN_OVERWRITE");
 CHECK(tj_main_overwrite_enabled()==(flag && !strcmp(flag,"1")));
 static_assert(sizeof(RsxD3D12ExactBlit)==104 && offsetof(RsxD3D12ExactBlit,src_snapshot)==56,"capture ABI changed");
 s_d3d.initialized=1;auto b=mapcopy();GpuBlitEndpoint e={};
 CHECK(rsx_d3d12_backend_record_exact_blit(&b)==1 && !faulted);
 CHECK(s_d3d.draws[0].scaled_blit.src_snapshot==b.src_snapshot);
 CHECK(gpu_blit_endpoint_get(&b,1,&e)==0 && e.kind==GPU_BLIT_MIRROR);
 CHECK(mirror_calls==1 && !local_calls && seen_local==1 && seen_raw==b.src_raw);
 CHECK(seen_snapshot==b.src_snapshot && seen_bytes==b.src_snapshot_bytes);
 s_d3d.gpu_mirror[0].authoritative=1;
 CHECK(!exact_writeback_authoritative_source(&e)); // Immutable CPU source never owns GPU writeback.
 mirror_fail=1;CHECK(gpu_blit_endpoint_get(&b,1,&e)<0);mirror_fail=0;
 CHECK(gpu_blit_endpoint_get(&b,0,&e)==0 && !seen_local && seen_raw==b.dst_raw);
 CHECK(seen_snapshot==b.dst_snapshot && !s_d3d.gpu_mirror[0].immutable_source);
 CHECK(seen_overwrite==0); // Immutable CPU sources retain the whole initial destination.
 b.src_cpu_owned=0;
 CHECK(gpu_blit_endpoint_get(&b,0,&e)==0);
 CHECK(seen_overwrite==(tj_main_overwrite_enabled()?360u:0u)); // Eight padding columns retained.
 b.dst_x=1;CHECK(gpu_blit_endpoint_get(&b,0,&e)==0 && seen_overwrite==0);b.dst_x=0;
 b.dst_y=1;CHECK(gpu_blit_endpoint_get(&b,0,&e)<0 && seen_overwrite==0);b.dst_y=0;
 --b.copy_h;CHECK(gpu_blit_endpoint_get(&b,0,&e)==0 && seen_overwrite==0);++b.copy_h;
 b.dst_pitch=1440;b.dst_snapshot_bytes=1440*192;
 CHECK(gpu_blit_endpoint_get(&b,0,&e)==0 && seen_overwrite==(tj_main_overwrite_enabled()?360u:0u));
 b.dst_pitch=1472;b.dst_snapshot_bytes=1472*192;
 find_result=1;auto existing_calls=mirror_calls;
 CHECK(gpu_blit_endpoint_get(&b,0,&e)==0 && e.slot==1 && mirror_calls==existing_calls);find_result=-1;
 generation=4;CHECK(gpu_blit_endpoint_get(&b,0,&e)<0);generation=2;
 release();
 b=mapcopy();b.src_cpu_owned=0;free(b.src_snapshot);b.src_snapshot=nullptr;b.src_snapshot_bytes=0;
 CHECK(rsx_d3d12_backend_record_exact_blit(&b)==1);
 auto prior=mirror_calls;CHECK(gpu_blit_endpoint_get(&b,1,&e)<0 && mirror_calls==prior);
 rt_result=2;CHECK(gpu_blit_endpoint_get(&b,1,&e)==0 && e.kind==GPU_BLIT_OFFRT && e.slot==2);
 CHECK(exact_writeback_authoritative_source(&e));rt_result=-1;release();
 b=mapcopy();b.src_cpu_owned=0;CHECK(!rsx_d3d12_backend_record_exact_blit(&b) && faulted==1);
 b=mapcopy();free(b.src_snapshot);b.src_snapshot=nullptr;CHECK(!rsx_d3d12_backend_record_exact_blit(&b) && faulted==2);
 b=mapcopy();b.src_local=0;CHECK(!rsx_d3d12_backend_record_exact_blit(&b) && faulted==3);
 b=mapcopy();b.src_cpu_owned=b.src_local=0;b.src_raw=0x1000;b.src_ea=0x4C01000;
 CHECK(rsx_d3d12_backend_record_exact_blit(&b)==1);
 CHECK(gpu_blit_endpoint_get(&b,1,&e)==0 && !seen_local);
 CHECK(seen_overwrite==0); // MAIN sources remain immutable complete snapshots.
 CHECK(gpu_blit_endpoint_get(&b,0,&e)==0 && seen_overwrite==0); // MAIN producer is conservative.
 overlap=1;CHECK(gpu_blit_endpoint_get(&b,1,&e)<0);overlap=0;
 find_result=1;s_d3d.gpu_mirror[1].authoritative=0;CHECK(gpu_blit_endpoint_get(&b,1,&e)<0);
 s_d3d.gpu_mirror[1].authoritative=1;CHECK(gpu_blit_endpoint_get(&b,1,&e)==0 && e.slot==1);
 release();printf("LOCAL copy routing: %u/%u passed\n",checks-failed,checks);return failed?1:0;
}
'''.replace("FUNCTIONS", body)
    compiler = shutil.which("clang++") or shutil.which("g++") or r"C:\Program Files\LLVM\bin\clang++.exe"
    with tempfile.TemporaryDirectory(prefix="tj-local-routing-") as temp:
        cpp, exe = Path(temp)/"routing.cpp", Path(temp)/"routing.exe"
        cpp.write_text(source, encoding="utf-8")
        subprocess.run([compiler, "-std=c++17", "-O2", "-I", str(RUNTIME/"include"),
                        "-I", str(RUNTIME/"libs/video"), str(cpp), "-o", str(exe)], check=True)
        import os
        for value in (None, "0", "1", "true", "01"):
            env = os.environ.copy()
            env.pop("TJ_MAIN_OVERWRITE", None)
            if value is not None:
                env["TJ_MAIN_OVERWRITE"] = value
            subprocess.run([str(exe)], check=True, env=env)


if __name__ == "__main__":
    main()
