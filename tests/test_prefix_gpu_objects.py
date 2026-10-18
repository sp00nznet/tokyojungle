"""Exercise actual COM pinning/recording wrappers with real D3D12 ABI types."""
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from test_fifo_present_boundary import function

ROOT = Path(__file__).resolve().parents[1]


def main():
    backend = (ROOT.parent / "ps3recomp/libs/video/rsx_d3d12_backend.c").read_text(encoding="utf-8")
    signatures = [
        "static int d3d12_prefix_owner_object_capture(",
        "static int d3d12_prefix_owner_object_reserved(",
        "static int d3d12_prefix_owner_object_mutable(",
        "static void d3d12_owned_barriers(",
        "static void d3d12_owned_copy_texture(",
        "static void d3d12_owned_rtv(",
        "static void d3d12_owned_dsv(",
        "static void d3d12_owned_pso(",
        "static void d3d12_owned_root(",
        "static int d3d12_prefix_owner_targets_capture(",
    ]
    bodies = [function(backend, signature) for signature in signatures]
    scratch = backend[backend.index("#define GPU_SCRATCH_SLOTS"):backend.index("/* One-frame only readback plans.")]
    mirror_end = backend.index("} GpuMirror;") + len("} GpuMirror;")
    mirror_type = backend[backend.rfind("typedef struct {", 0, mirror_end):mirror_end]
    # A direct recording call is allowed only inside its pinning wrapper.
    rest = backend
    for body in bodies:
        rest = rest.replace(body, "")
    for name in ("ResourceBarrier", "CopyTextureRegion", "SetPipelineState", "SetGraphicsRootSignature"):
        assert not re.search(r"s_d3d\.cmd_list->lpVtbl->" + name + r"\(", rest), name
    for name in ("CreateRenderTargetView", "CreateDepthStencilView"):
        assert not re.search(r"s_d3d\.device->lpVtbl->" + name + r"\(", rest), name
    srv = function(backend, "static void srv_write_dimension(")
    assert srv.index("d3d12_prefix_owner_object_capture(res)") < srv.index("CreateShaderResourceView(")
    render = function(backend, "static void render_frame(")
    assert render.index("d3d12_prefix_owner_object_capture(s_d3d.depth_buffer)") < render.index("OMSetRenderTargets(")
    assert render.index("d3d12_prefix_owner_object_capture(ds->sample.heap)") < render.index("rsx_d3d12_depth_sample_update(")
    assert "d3d12_prefix_owner_targets_capture(want, want_display," in render
    for signature, resource in [
        ("static int vp_upload_tex_slot(", "t->up"),
        ("static int off_rt_get(", "r->up"),
        ("static int gpu_mirror_get(", "m->up"),
    ]:
        body = function(backend, signature)
        assert body.index("d3d12_prefix_owner_object_mutable(" + resource + ")") < body.index(resource + "->lpVtbl->Map("), signature
    for signature in ("static void gpu_scratch_retain(", "static void gpu_mirror_retire("):
        body = function(backend, signature)
        assert "d3d12_prefix_owner_object_mutable" in body
    take = function(backend, "static int gpu_scratch_take(")
    assert "!d3d12_prefix_owner_object_reserved(a->res)" in take
    assert "!d3d12_prefix_owner_object_reserved(a->up)" in take
    fixture = r'''
#define WIN32_LEAN_AND_MEAN
#define CINTERFACE
#include <windows.h>
#include <d3d12.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
typedef uint32_t u32;typedef uint64_t u64;typedef uint8_t u8;
SCRATCH
MIRROR_TYPE
#define PREFIX_OBJECT_CAP 8
#define MAX_OFF_RTS 16
#define MAX_DEPTH_SURFACES 31
#define MAX_DISPLAY_SURFACES 8
#define FRAME_COUNT 2
typedef struct { IUnknown* objects[PREFIX_OBJECT_CAP];u32 object_count;int phase,completion_checked; } D3D12PrefixOwner;
typedef struct {ID3D12Resource* res;} Target;
static struct {D3D12PrefixOwner prefix_owners[2];u32 prefix_owner_active;int gcm2d_backend_halted;ID3D12Device* device;ID3D12GraphicsCommandList* cmd_list;
    Target off_rt[MAX_OFF_RTS],display_surface[MAX_DISPLAY_SURFACES],depth_surface[MAX_DEPTH_SURFACES],off_rt_scratch[4];
    ID3D12Resource* render_targets[FRAME_COUNT];ID3D12Resource* depth_buffer;u32 frame_index;} s_d3d;
static unsigned checks,failures,calls[6];static int enabled=1;
#define CHECK(c,m) do{++checks;if(!(c)){++failures;fprintf(stderr,"FAIL: %s\n",m);}}while(0)
static void gcm2d_backend_halt(const char* reason){(void)reason;s_d3d.gcm2d_backend_halted=1;}
static int d3d12_prefix_ownership_enabled(void){return enabled;}
static int mutation_guard_block[2];
static unsigned mutation_guard_calls[2];
static int d3d12_prefix_prepare_before_mutation(const void* p){(void)p;++mutation_guard_calls[0];return mutation_guard_block[0]?-1:0;}
static int d3d12_prefix_queue_ahead_before_mutation(const void* p){(void)p;++mutation_guard_calls[1];return mutation_guard_block[1]?-1:0;}
typedef struct {IUnknown iface;ULONG refs;} Object;
static ULONG STDMETHODCALLTYPE addref(IUnknown* p){return ++((Object*)p)->refs;}
static ULONG STDMETHODCALLTYPE release(IUnknown* p){return --((Object*)p)->refs;}
static IUnknownVtbl ov={NULL,addref,release};
static void STDMETHODCALLTYPE barriers(ID3D12GraphicsCommandList* p,UINT n,const D3D12_RESOURCE_BARRIER* b){(void)p;(void)n;(void)b;++calls[0];}
static void STDMETHODCALLTYPE copy(ID3D12GraphicsCommandList* p,const D3D12_TEXTURE_COPY_LOCATION* d,UINT x,UINT y,UINT z,const D3D12_TEXTURE_COPY_LOCATION* s,const D3D12_BOX* b){(void)p;(void)d;(void)x;(void)y;(void)z;(void)s;(void)b;++calls[1];}
static void STDMETHODCALLTYPE rtv(ID3D12Device* p,ID3D12Resource* r,const D3D12_RENDER_TARGET_VIEW_DESC* d,D3D12_CPU_DESCRIPTOR_HANDLE h){(void)p;(void)r;(void)d;(void)h;++calls[2];}
static void STDMETHODCALLTYPE dsv(ID3D12Device* p,ID3D12Resource* r,const D3D12_DEPTH_STENCIL_VIEW_DESC* d,D3D12_CPU_DESCRIPTOR_HANDLE h){(void)p;(void)r;(void)d;(void)h;++calls[3];}
static void STDMETHODCALLTYPE pso(ID3D12GraphicsCommandList* p,ID3D12PipelineState* s){(void)p;(void)s;++calls[4];}
static void STDMETHODCALLTYPE root(ID3D12GraphicsCommandList* p,ID3D12RootSignature* s){(void)p;(void)s;++calls[5];}
PRODUCTION
SCRATCH_FUNCTIONS
static void drop_pins(u32 slot){D3D12PrefixOwner* o=&s_d3d.prefix_owners[slot];for(u32 i=0;i<o->object_count;++i)o->objects[i]->lpVtbl->Release(o->objects[i]);memset(o,0,sizeof(*o));}
int main(void){
    ID3D12DeviceVtbl dv={0};dv.CreateRenderTargetView=rtv;dv.CreateDepthStencilView=dsv;ID3D12Device dev={&dv};
    ID3D12GraphicsCommandListVtbl lv={0};lv.ResourceBarrier=barriers;lv.CopyTextureRegion=copy;lv.SetPipelineState=pso;lv.SetGraphicsRootSignature=root;ID3D12GraphicsCommandList list={&lv};
    s_d3d.device=&dev;s_d3d.cmd_list=&list;Object obj[9];for(unsigned i=0;i<9;++i){obj[i].iface.lpVtbl=&ov;obj[i].refs=1;}
    D3D12_RESOURCE_BARRIER b[4]={0};b[0].Type=D3D12_RESOURCE_BARRIER_TYPE_TRANSITION;b[0].Transition.pResource=(ID3D12Resource*)&obj[0];
    b[1].Type=D3D12_RESOURCE_BARRIER_TYPE_ALIASING;b[1].Aliasing.pResourceBefore=(ID3D12Resource*)&obj[0];b[1].Aliasing.pResourceAfter=(ID3D12Resource*)&obj[1];
    b[2].Type=D3D12_RESOURCE_BARRIER_TYPE_UAV;b[2].UAV.pResource=NULL;b[3].Type=D3D12_RESOURCE_BARRIER_TYPE_UAV;b[3].UAV.pResource=(ID3D12Resource*)&obj[2];
    D3D12_TEXTURE_COPY_LOCATION dst={0},src={0};dst.pResource=(ID3D12Resource*)&obj[1];src.pResource=(ID3D12Resource*)&obj[3];D3D12_CPU_DESCRIPTOR_HANDLE h={0};
    d3d12_owned_barriers(4,b);d3d12_owned_copy_texture(&dst,1,2,3,&src,NULL);d3d12_owned_rtv(src.pResource,NULL,h);d3d12_owned_dsv(src.pResource,NULL,h);d3d12_owned_pso((ID3D12PipelineState*)&obj[4]);d3d12_owned_root((ID3D12RootSignature*)&obj[5]);
    for(unsigned i=0;i<6;++i)CHECK(calls[i]==1,"default recording changed");for(unsigned i=0;i<9;++i)CHECK(obj[i].refs==1,"default recording pinned objects");
    s_d3d.prefix_owner_active=1;s_d3d.prefix_owners[0].phase=1;
    d3d12_owned_barriers(4,b);CHECK(s_d3d.prefix_owners[0].object_count==3&&calls[0]==2,"barrier resources not retained");
    d3d12_owned_copy_texture(&dst,1,2,3,&src,NULL);CHECK(s_d3d.prefix_owners[0].object_count==4&&calls[1]==2,"copy source/destination not retained");
    d3d12_owned_rtv(src.pResource,NULL,h);d3d12_owned_dsv(src.pResource,NULL,h);CHECK(obj[3].refs==2&&s_d3d.prefix_owners[0].object_count==4,"view duplicate pin");
    d3d12_owned_pso((ID3D12PipelineState*)&obj[4]);d3d12_owned_root((ID3D12RootSignature*)&obj[5]);CHECK(obj[4].refs==2&&obj[5].refs==2,"shader objects not retained");
    CHECK(d3d12_prefix_owner_object_capture(NULL)==0,"null alias rejected");
    for(int phase=1;phase<=3;++phase){s_d3d.prefix_owners[0].phase=phase;s_d3d.prefix_owners[0].completion_checked=0;s_d3d.gcm2d_backend_halted=0;CHECK(d3d12_prefix_owner_object_mutable(&obj[3])<0&&s_d3d.gcm2d_backend_halted,"reserved upload overwrite accepted");}
    s_d3d.prefix_owners[0].phase=2;s_d3d.prefix_owners[0].completion_checked=1;s_d3d.gcm2d_backend_halted=0;CHECK(d3d12_prefix_owner_object_mutable(&obj[3])==0,"checked object could not recycle");
    mutation_guard_block[0]=1;mutation_guard_calls[0]=mutation_guard_calls[1]=0;
    CHECK(d3d12_prefix_owner_object_mutable(&obj[8])<0&&mutation_guard_calls[0]==1&&!mutation_guard_calls[1],"prepare guard did not stop mutation before queue-ahead guard");
    mutation_guard_block[0]=0;mutation_guard_block[1]=1;
    CHECK(d3d12_prefix_owner_object_mutable(&obj[8])<0&&mutation_guard_calls[0]==2&&mutation_guard_calls[1]==1,"queue-ahead guard did not stop mutation");
    mutation_guard_block[1]=0;
    s_d3d.prefix_owners[0].phase=3;CHECK(d3d12_prefix_owner_object_reserved(&obj[3]),"poisoned object was reusable");
    s_d3d.prefix_owners[0].phase=1;s_d3d.gcm2d_backend_halted=0;
    d3d12_owned_pso((ID3D12PipelineState*)&obj[6]);d3d12_owned_pso((ID3D12PipelineState*)&obj[7]);CHECK(s_d3d.prefix_owners[0].object_count==8,"capacity setup");
    d3d12_owned_pso((ID3D12PipelineState*)&obj[7]);CHECK(obj[7].refs==2&&calls[4]==5,"full duplicate rejected");
    d3d12_owned_root((ID3D12RootSignature*)&obj[8]);CHECK(s_d3d.gcm2d_backend_halted&&calls[5]==2&&obj[8].refs==1,"overflow recorded or retained extra object");
    s_d3d.gcm2d_backend_halted=0;s_d3d.prefix_owner_active=2;s_d3d.prefix_owners[1].phase=1;CHECK(d3d12_prefix_owner_object_capture(&obj[0])==0&&obj[0].refs==3,"second owner did not pin independently");
    drop_pins(0);CHECK(obj[0].refs==2&&d3d12_prefix_owner_object_reserved(&obj[0]),"first release destroyed second pin");drop_pins(1);CHECK(obj[0].refs==1,"last pin leaked");
    s_d3d.prefix_owner_active=3;s_d3d.gcm2d_backend_halted=0;CHECK(d3d12_prefix_owner_object_capture(&obj[0])<0&&obj[0].refs==1,"invalid owner accepted");
    s_d3d.prefix_owner_active=1;s_d3d.prefix_owners[0].phase=2;s_d3d.gcm2d_backend_halted=0;CHECK(d3d12_prefix_owner_object_capture(&obj[0])<0&&obj[0].refs==1,"submitted owner captured a new object");
    enabled=0;CHECK(!d3d12_prefix_owner_object_reserved(&obj[0]),"disabled reservation changed default");
    enabled=1;s_d3d.prefix_owner_active=1;s_d3d.gcm2d_backend_halted=0;s_d3d.prefix_owners[0].phase=1;
    CHECK(d3d12_prefix_owner_object_capture(&obj[0])==0,"scratch reservation setup");
    GpuScratchAllocation allocation={0},out={0};allocation.res=(ID3D12Resource*)&obj[0];allocation.up=(ID3D12Resource*)&obj[1];allocation.kind=GPU_SCRATCH_MIRROR;allocation.w=8;allocation.h=8;allocation.bytes=64;
    gpu_scratch_retain(&allocation);CHECK(s_d3d.gcm2d_backend_halted&&allocation.res==(ID3D12Resource*)&obj[0]&&!s_gpu_scratch_bytes,"reserved allocation entered idle pool");
    GpuMirror mirror={0};mirror.res=allocation.res;mirror.up=allocation.up;mirror.w=8;mirror.h=8;mirror.raw=123;mirror.valid=1;mirror.allocation_bytes=64;
    s_d3d.gcm2d_backend_halted=0;gpu_mirror_retire(&mirror);CHECK(s_d3d.gcm2d_backend_halted&&mirror.res==allocation.res&&mirror.raw==123&&mirror.valid,"reserved mirror identity retired");
    s_gpu_scratch[0]=allocation;s_gpu_scratch_bytes=64;
    for(int phase=1;phase<=3;++phase){s_d3d.prefix_owners[0].phase=phase;s_d3d.prefix_owners[0].completion_checked=0;CHECK(gpu_scratch_take(GPU_SCRATCH_MIRROR,8,8,&out)==0&&s_gpu_scratch_bytes==64&&s_gpu_scratch[0].res==allocation.res,"reserved allocation was reused");}
    s_d3d.prefix_owners[0].phase=2;s_d3d.prefix_owners[0].completion_checked=1;s_d3d.gcm2d_backend_halted=0;
    CHECK(gpu_scratch_take(GPU_SCRATCH_MIRROR,8,8,&out)==1&&out.res==allocation.res&&!s_gpu_scratch_bytes,"checked allocation did not become reusable");
    gpu_mirror_retire(&mirror);CHECK(!mirror.res&&!mirror.valid&&s_gpu_scratch_bytes==64,"checked mirror did not retire");
    drop_pins(0);gpu_scratch_dispose(&s_gpu_scratch[0]);CHECK(obj[0].refs==0&&obj[1].refs==0,"completed scratch resources leaked");
    for(unsigned i=0;i<9;++i)obj[i].refs=1;
    s_d3d.prefix_owner_active=1;s_d3d.prefix_owners[0].phase=1;s_d3d.gcm2d_backend_halted=0;
    s_d3d.off_rt[2].res=(ID3D12Resource*)&obj[0];s_d3d.depth_surface[0].res=(ID3D12Resource*)&obj[1];s_d3d.display_surface[0].res=(ID3D12Resource*)&obj[2];
    s_d3d.off_rt_scratch[0].res=(ID3D12Resource*)&obj[3];s_d3d.off_rt_scratch[1].res=(ID3D12Resource*)&obj[4];s_d3d.off_rt_scratch[2].res=(ID3D12Resource*)&obj[5];
    s_d3d.off_rt[0].res=(ID3D12Resource*)&obj[4];s_d3d.off_rt[1].res=(ID3D12Resource*)&obj[5];s_d3d.render_targets[1]=(ID3D12Resource*)&obj[6];s_d3d.frame_index=1;s_d3d.depth_buffer=(ID3D12Resource*)&obj[7];
    int mrt[3]={0,1,-1},none[3]={-1,-1,-1};
    CHECK(d3d12_prefix_owner_targets_capture(2,-1,0,mrt,1)==0&&obj[0].refs==2&&obj[1].refs==2&&obj[4].refs==2&&obj[5].refs==2,"existing target/depth/MRT without barriers not retained");
    CHECK(d3d12_prefix_owner_targets_capture(2,0,0,none,1)==0&&obj[2].refs==2&&obj[0].refs==2,"display target selection wrong");
    CHECK(d3d12_prefix_owner_targets_capture(2,-1,1,mrt,1)==0&&obj[3].refs==2&&obj[4].refs==2&&obj[5].refs==2,"aliased MRT scratch targets wrong");
    CHECK(d3d12_prefix_owner_targets_capture(-1,-1,0,none,0)==0&&obj[6].refs==2&&obj[7].refs==2,"fallback target/depth not retained");
    drop_pins(0);s_d3d.prefix_owners[0].phase=1;
    CHECK(d3d12_prefix_owner_targets_capture(MAX_OFF_RTS,-1,0,none,0)<0,"invalid primary accepted");s_d3d.gcm2d_backend_halted=0;
    CHECK(d3d12_prefix_owner_targets_capture(-1,MAX_DISPLAY_SURFACES,0,none,0)<0,"invalid display accepted");s_d3d.gcm2d_backend_halted=0;
    CHECK(d3d12_prefix_owner_targets_capture(-1,-1,0,none,MAX_DEPTH_SURFACES+1)<0,"invalid depth accepted");s_d3d.gcm2d_backend_halted=0;
    s_d3d.frame_index=FRAME_COUNT;CHECK(d3d12_prefix_owner_targets_capture(-1,-1,0,none,0)<0,"invalid frame accepted");s_d3d.frame_index=1;s_d3d.gcm2d_backend_halted=0;
    mrt[0]=MAX_OFF_RTS;CHECK(d3d12_prefix_owner_targets_capture(-1,-1,0,mrt,0)<0,"invalid MRT accepted");drop_pins(0);s_d3d.prefix_owners[0].phase=1;s_d3d.gcm2d_backend_halted=0;
    s_d3d.off_rt[2].res=NULL;CHECK(d3d12_prefix_owner_targets_capture(2,-1,0,none,0)<0,"missing target accepted");drop_pins(0);
    printf("Prefix GPU objects: %u checks, %u failures\n",checks,failures);return failures?1:0;
}
'''.replace("PRODUCTION", "\n".join(bodies)).replace("SCRATCH_FUNCTIONS", "\n".join(
    function(backend, signature) for signature in (
        "static void gpu_scratch_dispose(", "static int gpu_scratch_take(",
        "static void gpu_scratch_retain(", "static void gpu_mirror_retire("))
    ).replace("SCRATCH\n", scratch).replace("MIRROR_TYPE", mirror_type)
    compiler = shutil.which("clang") or r"C:\Program Files\LLVM\bin\clang.exe"
    with tempfile.TemporaryDirectory(prefix="tj-prefix-objects-") as directory:
        source, exe = Path(directory) / "objects.c", Path(directory) / "objects.exe"
        source.write_text(fixture, encoding="utf-8")
        subprocess.run([compiler, "-std=c11", str(source), "-o", str(exe)], check=True)
        subprocess.run([str(exe)], check=True, timeout=10)


if __name__ == "__main__":
    main()
