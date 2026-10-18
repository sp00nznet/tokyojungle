"""Production ownership gate: captured map copy, prior producers and evicted owners."""
from pathlib import Path
import shutil
import subprocess
import tempfile

from test_fifo_present_boundary import function

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT.parent / "ps3recomp"

def main():
    backend = (RUNTIME / "libs/video/rsx_d3d12_backend.c").read_text(encoding="utf-8")
    functions = "\n".join(function(backend, signature) for signature in (
        "static void local_gpu_claim_span(", "static void local_gpu_claim_surface(",
        "static int local_gpu_span_is_cpu(", "static void local_gpu_claim_pending(",
        "int rsx_d3d12_backend_local_source_is_cpu("))
    source = r'''
#include <stdint.h>
#include <stdio.h>
#include <string.h>
typedef uint8_t u8;typedef uint16_t u16;typedef uint32_t u32;typedef uint64_t u64;
typedef int DXGI_FORMAT;
#define MAX_OFF_RTS 16
#define MAX_DEPTH_SURFACES 31
#define MAX_DISPLAY_SURFACES 8
#define MAX_DRAWS 32
#define TJ_LOCAL_VRAM_BYTES (256u<<20)
#define TJ_LOCAL_PAGE_SHIFT 12u
static u8 s_local_gpu_pages[TJ_LOCAL_VRAM_BYTES>>TJ_LOCAL_PAGE_SHIFT];
static int s_local_gpu_ownership_unknown;
struct OffRT {void* res;u8 local;u32 off,raw_pitch,w,h,dxgi;};
struct DepthSurface {int valid;u8 local;u32 raw,pitch,w,h,format;};
struct DisplaySurface {void* res;u32 raw,pitch,w,h;};
struct RsxD3D12ExactBlit {u8 dst_local;u32 dst_raw,dst_pitch,dst_x,dst_y,copy_w,copy_h,dst_w,dst_h;};
struct D3D12DrawRecord {
    int is_scaled_blit,is_vp,legacy,enabled;
    RsxD3D12ExactBlit scaled_blit;
    u32 rt_off,rt_mrt[3],rt_raw[4],rt_pitch[4],rt_fmt,rt_w,rt_h,rt_window_x,rt_window_y;
    u8 rt_local,rt_mrt_local[3],rt_raw_local[4],zeta_local;
    u32 zeta_raw,zeta_pitch,zeta_format,depth_w,depth_h;
};
static struct {
    int initialized,gcm2d_backend_halted;u32 width,height,draw_count;
    OffRT off_rt[MAX_OFF_RTS];DepthSurface depth_surface[MAX_DEPTH_SURFACES];
    DisplaySurface display_surface[MAX_DISPLAY_SURFACES];D3D12DrawRecord draws[MAX_DRAWS];
} s_d3d;
static u32 dxgi_bytes_per_pixel(DXGI_FORMAT format){return format==2?2:4;}
static DXGI_FORMAT rsx_surface_dxgi(u32 format){return format;}
static int legacy_record_supported(const D3D12DrawRecord* d){return d->legacy;}
static int dr_rt_attachment_enabled(const D3D12DrawRecord* d,int t){return (d->enabled>>t)&1;}
static void depth_surface_extent(const D3D12DrawRecord* d,u32,u32,u32* w,u32* h){*w=d->depth_w;*h=d->depth_h;}
static u32 tile_raw,tile_base,tile_x,tile_y;
static u32 off_rt_tile_canonical(u32 raw,u32,u32,u64,u32* x,u32* y){
    *x=*y=0;if(tile_raw && raw==tile_raw){*x=tile_x;*y=tile_y;return tile_base;}return raw;
}
FUNCTIONS
static unsigned checks,failures;
#define CHECK(c) do{++checks;if(!(c)){++failures;printf("FAIL line %d\n",__LINE__);}}while(0)
static void reset(){memset(&s_d3d,0,sizeof(s_d3d));memset(s_local_gpu_pages,0,sizeof(s_local_gpu_pages));s_local_gpu_ownership_unknown=0;s_d3d.initialized=1;s_d3d.width=1280;s_d3d.height=720;tile_raw=0;}
int main(){
    reset();
    // Exact captured failure; existing GPU backings are disjoint from the map image.
    s_d3d.off_rt[0]={(void*)1,0,0,0x1400,1280,720,28};
    s_d3d.off_rt[1]={(void*)1,1,0x8200000,0xA00,640,360,28};
    s_d3d.off_rt[2]={(void*)1,1,0xE300000,0x2800,2560,1064,28};
    s_d3d.off_rt[3]={(void*)1,1,0x7400000,0x800,512,512,28};
    s_d3d.off_rt[4]={(void*)1,1,0x83E0000,0xA00,640,360,28};
    CHECK(rsx_d3d12_backend_local_source_is_cpu(0x9923280,0x5A0,360,192));
    CHECK(!rsx_d3d12_backend_local_source_is_cpu(0x8200000,0xA00,640,360));
    memset(s_d3d.off_rt,0,sizeof(s_d3d.off_rt)); // Eviction must not resurrect CPU authority.
    CHECK(!rsx_d3d12_backend_local_source_is_cpu(0x8201000,64,16,1));
    CHECK(rsx_d3d12_backend_local_source_is_cpu(0x9923280,0x5A0,360,192));
    reset();s_d3d.draw_count=1;auto& d=s_d3d.draws[0];
    d.is_vp=1;d.enabled=1;d.rt_local=d.rt_raw_local[0]=1;
    d.rt_off=d.rt_raw[0]=0x10000;d.rt_pitch[0]=64;d.rt_w=d.rt_h=16;
    CHECK(!rsx_d3d12_backend_local_source_is_cpu(0x10000,64,16,16));
    CHECK(rsx_d3d12_backend_local_source_is_cpu(0x20000,64,16,16));
    s_d3d.draw_count=0;CHECK(!rsx_d3d12_backend_local_source_is_cpu(0x10000,64,16,16));
    reset();s_d3d.draw_count=1;d=s_d3d.draws[0];d.is_scaled_blit=1;
    d.scaled_blit={1,0x30000,64,0,16,16,16,16,32};
    CHECK(!rsx_d3d12_backend_local_source_is_cpu(0x30000,64,16,1));
    CHECK(rsx_d3d12_backend_local_source_is_cpu(0x40000,64,16,1));
    reset();s_d3d.draw_count=1;d=s_d3d.draws[0];d.is_vp=1;d.enabled=1;
    d.rt_off=d.rt_raw[0]=0x50000;d.rt_w=d.rt_h=16;d.rt_pitch[0]=64;
    CHECK(rsx_d3d12_backend_local_source_is_cpu(0x50000,64,16,16)); // MAIN is distinct.
    reset();s_d3d.draw_count=1;d=s_d3d.draws[0];d.is_vp=1;d.zeta_local=1;
    d.zeta_raw=0x60000;d.zeta_pitch=64;d.zeta_format=1;d.depth_w=32;d.depth_h=16;
    CHECK(!rsx_d3d12_backend_local_source_is_cpu(0x60000,64,16,1));
    reset();s_d3d.display_surface[0]={(void*)1,0x70000,64,16,16};
    CHECK(!rsx_d3d12_backend_local_source_is_cpu(0x70000,64,16,1));
    reset();s_d3d.draw_count=1;d=s_d3d.draws[0];d.is_vp=1;d.enabled=1;
    d.rt_local=d.rt_raw_local[0]=1;d.rt_off=d.rt_raw[0]=0x81100;d.rt_pitch[0]=256;
    d.rt_w=d.rt_h=16;tile_raw=0x81100;tile_base=0x80000;tile_x=0;tile_y=17;
    CHECK(!rsx_d3d12_backend_local_source_is_cpu(0x81000,64,16,1));
    reset();
    CHECK(!rsx_d3d12_backend_local_source_is_cpu(0x1000,63,15,1));
    CHECK(!rsx_d3d12_backend_local_source_is_cpu(0x1000,64,17,1));
    CHECK(!rsx_d3d12_backend_local_source_is_cpu(0x1000,0,1,1));
    CHECK(!local_gpu_span_is_cpu(0xFFFFFF0,32));
    CHECK(!local_gpu_span_is_cpu(0,0));
    for(u32 i=0;i<128;++i){
        reset();u32 raw=0x100000+i*4096;local_gpu_claim_span(raw+4095,2);
        CHECK(!local_gpu_span_is_cpu(raw,1));CHECK(!local_gpu_span_is_cpu(raw+4096,1));
        CHECK(local_gpu_span_is_cpu(raw+8192,1));
    }
    reset();local_gpu_claim_surface(0x1000,UINT32_MAX,1,2,4);
    CHECK(s_local_gpu_ownership_unknown && !local_gpu_span_is_cpu(0x9923280,276480));
    reset();s_d3d.gcm2d_backend_halted=1;CHECK(!rsx_d3d12_backend_local_source_is_cpu(0x1000,64,16,1));
    printf("LOCAL copy ownership: %u/%u checks passed\n",checks-failures,checks);return failures?1:0;
}
'''.replace("FUNCTIONS", functions)
    compiler = shutil.which("clang++") or shutil.which("g++") or r"C:\Program Files\LLVM\bin\clang++.exe"
    with tempfile.TemporaryDirectory(prefix="tj-local-copy-") as temp:
        cpp, exe = Path(temp)/"ownership.cpp", Path(temp)/"ownership.exe"
        cpp.write_text(source,encoding="utf-8")
        subprocess.run([compiler,"-std=c++17","-O2",str(cpp),"-o",str(exe)],check=True)
        subprocess.run([str(exe)],check=True)

if __name__ == "__main__":
    main()
