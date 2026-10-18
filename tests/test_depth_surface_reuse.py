"""Exercise actual depth allocation policy with counted D3D resource calls."""
from pathlib import Path
import subprocess
import tempfile
from test_fifo_present_boundary import function

ROOT = Path(__file__).resolve().parents[1]


def main():
    backend = (ROOT.parent / "ps3recomp/libs/video/rsx_d3d12_backend.c").read_text()
    source = r'''
#include <initguid.h>
#include <windows.h>
#include <d3d12.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
typedef uint8_t u8;
typedef uint32_t u32;
typedef uint64_t u64;
#define MAX_DEPTH_SURFACES 31
typedef struct {
    u32 rt_w,rt_h,rt_clip_w,rt_clip_h,rt_clip_x,rt_clip_y,rt_window_x,rt_window_y;
    u32 zeta_raw,zeta_local,zeta_format,zeta_pitch,vp_x,vp_y,vp_w,vp_h;
} D3D12DrawRecord;
typedef struct {
    int valid,format; ID3D12Resource* res; u32 raw,local,pitch,w,h;
    int initialized,sample_dirty,sample;
} DepthSurface;
static struct {
    u32 width,height; ID3D12Device* device;
    DepthSurface depth_surface[MAX_DEPTH_SURFACES];
} s_d3d;
/* Ownership-OFF allocation policy; owned recording is tested separately. */
#define d3d12_owned_dsv(r,d,h) s_d3d.device->lpVtbl->CreateDepthStencilView(s_d3d.device,r,d,h)
typedef struct { u64 depth_allocations,depth_extent_preserves; } PerfSample;
static PerfSample* s_tj_gcm2d_backend_perf_sample;
static int tj_gcm2d_backend_perf_enabled(void) { return 0; }
static unsigned creates,releases,views,live_resources,fail_next,checks,failures;
static D3D12_RESOURCE_DESC last_desc;
static D3D12_DEPTH_STENCIL_VIEW_DESC last_view;
static ID3D12Resource resource;
static ULONG STDMETHODCALLTYPE release_resource(ID3D12Resource* self) {
    (void)self; ++releases; --live_resources; return 0;
}
static HRESULT STDMETHODCALLTYPE create_resource(ID3D12Device* self,
    const D3D12_HEAP_PROPERTIES* hp,D3D12_HEAP_FLAGS flags,
    const D3D12_RESOURCE_DESC* desc,D3D12_RESOURCE_STATES state,
    const D3D12_CLEAR_VALUE* clear,REFIID iid,void** out) {
    (void)self;(void)hp;(void)flags;(void)state;(void)clear;(void)iid;
    ++creates; last_desc=*desc;
    if(fail_next) { fail_next=0; *out=NULL; return E_OUTOFMEMORY; }
    ++live_resources; *out=&resource; return S_OK;
}
static void STDMETHODCALLTYPE create_view(ID3D12Device* self,
    ID3D12Resource* res,const D3D12_DEPTH_STENCIL_VIEW_DESC* desc,
    D3D12_CPU_DESCRIPTOR_HANDLE handle) {
    (void)self;(void)res;(void)handle; ++views; last_view=*desc;
}
static void rsx_d3d12_depth_sample_release(int* sample) { *sample=0; }
static D3D12_CPU_DESCRIPTOR_HANDLE depth_dsv_handle(u8 descriptor) {
    D3D12_CPU_DESCRIPTOR_HANDLE h={descriptor}; return h;
}
'''
    source += function(backend, "static void depth_surface_extent(")
    source += function(backend, "static u8 depth_surface_get(")
    source += r'''
static void check(int ok,const char* reason) {
    ++checks; if(!ok) { ++failures; if(failures<10) printf("FAIL %s\n",reason); }
}
int main(void) {
    static ID3D12ResourceVtbl rv={.Release=release_resource};
    static ID3D12DeviceVtbl dv={.CreateCommittedResource=create_resource,
                              .CreateDepthStencilView=create_view};
    static ID3D12Device device;
    resource.lpVtbl=&rv; device.lpVtbl=&dv;
    s_d3d.width=1280;s_d3d.height=720;s_d3d.device=&device;
    PerfSample perf={0};s_tj_gcm2d_backend_perf_sample=&perf;
    D3D12DrawRecord a={0},b;
    a.zeta_raw=0x10000;a.zeta_local=1;a.zeta_format=2;a.zeta_pitch=4096;
    a.rt_clip_w=1024;a.rt_clip_h=1024;
    b=a;b.rt_clip_w=1280;b.rt_clip_h=720;
    u8 slot=depth_surface_get(&a);
    check(slot==1 && creates==1 && views==1,"initial allocation");
    check(depth_surface_get(&b)==slot,"grow retains descriptor identity");
    DepthSurface* ds=&s_d3d.depth_surface[slot-1];
    check(ds->w==1280 && ds->h==1024,"grow preserves the other dimension");
    check(last_desc.Width==1280 && last_desc.Height==1024,"D3D allocation covers both views");
    ds->initialized=1;ds->sample_dirty=0;ds->sample=42;
    for(unsigned i=0;i<1000;i++) {
        check(depth_surface_get((i&1)?&b:&a)==slot,"alternating view retains slot");
        check(ds->initialized && !ds->sample_dirty && ds->sample==42,
              "contained views preserve depth content and sample ownership");
    }
    check(creates==2 && releases==1 && views==2,"alternating views need only one grow");
    unsigned before=creates;
    D3D12DrawRecord bad=a;bad.rt_clip_x=UINT32_MAX;
    check(!depth_surface_get(&bad) && creates==before,"overflow rejected before allocation");
    bad=a;bad.rt_clip_h=8193;
    check(!depth_surface_get(&bad) && creates==before,"oversized extent rejected");
    bad=a;bad.zeta_pitch=0;
    check(!depth_surface_get(&bad) && !depth_surface_get(NULL),"missing source rejected");
    D3D12DrawRecord separate=a;
    ++separate.zeta_raw;check(depth_surface_get(&separate)!=slot,"different raw is separate");
    separate=a;separate.zeta_local=0;
    check(depth_surface_get(&separate)!=slot,"different location is separate");
    separate=a;separate.zeta_pitch=5120;
    check(depth_surface_get(&separate)!=slot,"different pitch is separate");
    separate=a;separate.zeta_format=1;
    check(depth_surface_get(&separate)!=slot && last_view.Format==DXGI_FORMAT_D16_UNORM,
          "different depth format is separate");
    b.rt_clip_h=2048;
    check(depth_surface_get(&b)==slot && ds->w==1280 && ds->h==2048,
          "height growth preserves width");
    check(!ds->initialized && ds->sample_dirty && !ds->sample,
          "real growth resets previous sample and initialization");
    b.rt_clip_w=2048;fail_next=1;
    check(!depth_surface_get(&b) && !ds->valid && !ds->res,
          "failed allocation cannot expose released storage");
    check(depth_surface_get(&b)==slot && ds->w==2048 && ds->h==2048,
          "failed allocation can retry normally");
    for(unsigned i=0;i<MAX_DEPTH_SURFACES;i++) if(s_d3d.depth_surface[i].res)
        s_d3d.depth_surface[i].res->lpVtbl->Release(s_d3d.depth_surface[i].res);
    check(live_resources==0,"every allocated lifetime released");
    check(perf.depth_allocations==creates && perf.depth_extent_preserves==1,
          "diagnostic allocation and extent counters describe actual work");
    printf("depth allocation checks=%u failures=%u creates=%u releases=%u\n",
           checks,failures,creates,releases);
    return failures?1:0;
}
'''
    with tempfile.TemporaryDirectory(prefix="tj-depth-reuse-") as temp:
        c, exe = Path(temp) / "depth.c", Path(temp) / "depth.exe"
        c.write_text(source)
        subprocess.run([r"C:\Program Files\LLVM\bin\clang.exe", "-std=c11", "-O2",
                        "-D_CRT_SECURE_NO_WARNINGS", str(c), "-o", str(exe)], check=True)
        subprocess.run([str(exe)], check=True)


if __name__ == "__main__":
    main()
