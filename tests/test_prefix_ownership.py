"""Extract real prefix ownership/entry code; fake COM endpoints, no game/GPU."""
from pathlib import Path
import os
import re
import shutil
import subprocess
import tempfile
from test_fifo_present_boundary import function

ROOT = Path(__file__).resolve().parents[1]


def main():
    backend = (ROOT.parent / "ps3recomp/libs/video/rsx_d3d12_backend.c").read_text(encoding="utf-8")
    header = (ROOT.parent / "ps3recomp/libs/video/rsx_d3d12_backend.h").read_text(encoding="utf-8")
    reason_start = header.index("typedef enum RsxD3D12CompletionReason")
    reasons = header[reason_start:header.index("} RsxD3D12CompletionReason;", reason_start) + len("} RsxD3D12CompletionReason;")]
    cost_start = backend.index("enum { TJ_COST_SETUP")
    cost_enum = backend[cost_start:backend.index("};", cost_start) + 2]
    wait_start = backend.index("enum {\n    TJ_PREFIX_WAIT_SYNC,")
    wait_enum_end = backend.index("typedef struct {\n    u64 calls, wait_ticks;", wait_start)
    wait_trace_enums = backend[wait_start:wait_enum_end]
    end = backend.index("} D3D12PrefixOwner;") + len("} D3D12PrefixOwner;")
    start = backend.rfind("typedef struct {", 0, end)
    owner_type = backend[start:end]
    signatures = (
        "static int d3d12_prefix_ownership_enabled(void)\n{",
        "static int d3d12_prefix_owner_object_capture(",
        "static int d3d12_prefix_owner_object_reserved(",
        "static int d3d12_prefix_owner_object_mutable(",
        "static int d3d12_prefix_owner_texture_reserved(",
        "static int d3d12_prefix_owner_texture_mutable(",
        "static int d3d12_prefix_owner_texture_capture(",
        "static int d3d12_prefix_owner_descriptors_init(",
        "static void d3d12_prefix_owner_release_uploads(",
        "static int d3d12_prefix_owner_context_init(",
        "static int d3d12_prefix_owner_begin(",
        "static int d3d12_prefix_owner_before_upload(",
        "static int d3d12_prefix_owner_submit(",
        "static int d3d12_prefix_owner_signal(",
        "static int d3d12_prefix_owner_checked_completion(",
        "static int d3d12_prefix_owner_wait(",
        "static int d3d12_prefix_owner_complete(",
        "static int d3d12_prefix_owner_end(",
        "static int d3d12_prefix_owner_drain_ready(",
        "static int d3d12_prefix_overlap_drain_with_reasons(",
        "static int d3d12_prefix_overlap_drain(",
        "static int d3d12_prefix_owner_destroy(",
        "static void render_fifo_prefix_owned(",
        "int rsx_d3d12_backend_complete_fifo_prefix(",
    )
    production = "\n".join(function(backend, s) for s in signatures)
    upload_names = re.findall(r"^static u32 (upload_\w+)\(", backend, re.M)
    assert len(upload_names) == 15
    for name in upload_names:
        body = function(backend, "static u32 " + name + "(")
        basic = int(name in ("upload_vertices_from_rsx", "upload_quads_from_rsx"))
        vertex_only = int(not basic)
        assert body[body.index("{") + 1:].lstrip().startswith(
            f"if (d3d12_prefix_owner_before_upload({basic}, {vertex_only}, NULL) != 0) return 0;")
    cb = function(backend, "static void vp_record_cb(")
    marker = cb.index("const int retained_marker =")
    ordinary_guard = cb.index("d3d12_prefix_owner_before_upload(0, 0, NULL)")
    capture = cb.index("rsx_fp_capture_structural(vm_base + fp_off,")
    retained_guard = cb.index("d3d12_prefix_owner_before_upload(0, 0, dr)")
    snapshots = cb.index("vp_capture_draw_texture_snapshots(dr)")
    assert "s_d3d.prefix_owners[latest - 1].phase == 2" in cb[marker:ordinary_guard]
    assert "s_d3d.prefix_owners[latest - 1].sequence ==" in cb[marker:ordinary_guard]
    assert "s_d3d.prefix_vertex_prepare_sequence;" in cb[marker:ordinary_guard]
    assert cb.count("rsx_fp_capture_structural(") == 1
    cb_write = cb.index("memset(ts, 0, FP_CB_STRIDE)")
    assert marker < ordinary_guard < capture < retained_guard < snapshots < cb_write
    render = function(backend, "static void render_frame(")
    assert "render_allocator->lpVtbl->Reset(render_allocator)" in render
    assert "s_d3d.prefix_owners[s_d3d.prefix_owner_active - 1].allocator" in render
    prefix = render[render.index("if (fifo_prefix) {", render.index("s_dbg_last_draws")):]
    assert prefix.index("d3d12_prefix_owner_submit()") < prefix.index("->ExecuteCommandLists(")
    assert prefix.index("d3d12_prefix_owner_complete()") < prefix.index(
        "exact_writeback_map_convert_all(")
    assert prefix.index("if (!wb_ok)") < prefix.index(".cpu_retirement_ready = 1;")
    assert prefix.index("gpu_mirror_release_prefix_exports()") < prefix.index(".cpu_retirement_ready = 1;")
    assert prefix.index("tj_compose_capture_write()") < prefix.index(".cpu_retirement_ready = 1;")
    shutdown = function(backend, "void rsx_d3d12_backend_shutdown(")
    assert shutdown.index("prefix_owners[i].phase >= 2") < shutdown.index("gpu_scratch_release_all();")
    upload = function(backend, "static int vp_upload_tex_slot(")
    assert upload.count("d3d12_prefix_owner_texture_capture(") == 3
    assert upload.index("d3d12_prefix_owner_texture_mutable(") < upload.index("GetCopyableFootprints(")
    assert "cached->used || d3d12_prefix_owner_texture_reserved((u32)i)" in upload
    trim = function(backend, "static void vp_texture_cache_trim_sources(")
    assert "!d3d12_prefix_owner_texture_reserved((u32)i)" in trim
    # Every VP draw emits its complete private descriptor window; no old atlas
    # or fixed cache descriptor is sampled by a supported legacy draw.
    assert "srv_write" in render and "DRAW_SRV_BASE + _d * DRAW_TEXTURE_COUNT" in render
    assert "#if 0\n    /* Upload the bound font atlas" in render
    slot_end = backend.index("} VPTexSlot;") + len("} VPTexSlot;")
    slot_start = backend.rfind("typedef struct {", 0, slot_end)
    slot_type = backend[slot_start:slot_end]
    sampler_get = function(backend, "static ID3D12DescriptorHeap* sampler_heap_get(")
    fixture = r'''
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <initializer_list>
#include <vector>
#include <utility>
using u32=uint32_t; using u64=uint64_t; using LONGLONG=int64_t; using HRESULT=int32_t;
using u16=uint16_t; using u8=uint8_t;
COMPLETION_REASONS
COST_ENUM
WAIT_TRACE_ENUMS
static RsxD3D12CompletionReason s_tj_completion_reason;
static u32 s_exact_writeback_count;
static LONGLONG tj_prefix_cost_begin(){return 0;}
static LONGLONG tj_prefix_cost_mark(LONGLONG,unsigned,unsigned){return 0;}
static void tj_prefix_gpu_collect(u32){}
static void tj_prefix_gpu_wait_start(u32){}
static void tj_prefix_gpu_release(u32){}
#define MAX_VERTICES 512
#define MAX_DRAWS 32
#define DRAW_TEXTURE_COUNT 20
#define PREFIX_OBJECT_CAP (MAX_DRAWS * 2u + 512u)
#define VERTEX_STRIDE 36
#define VP_VERT_STRIDE 256
#define VP_CB_STRIDE 8448
#define FP_CB_STRIDE 256
#define FRAME_COUNT 2
#define VP_TEX_SLOTS 4
#define SRV_HEAP_SIZE 16
#define SAMPLER_HEAP_SIZE 8
#define SAMPLER_HEAP_PAGE_COUNT 3
#define D3D12_DESCRIPTOR_HEAP_TYPE_CBV_SRV_UAV 0
#define D3D12_DESCRIPTOR_HEAP_TYPE_SAMPLER 1
#define D3D12_DESCRIPTOR_HEAP_FLAG_SHADER_VISIBLE 1
#define DXGI_FORMAT_R8_UNORM 1
#define D3D12_SRV_DIMENSION_TEXTURE2D 1
#define D3D12_DEFAULT_SHADER_4_COMPONENT_MAPPING 5768
#define FAILED(hr) ((hr)<0)
#define D3D12_COMMAND_LIST_TYPE_DIRECT 0
static int IID_ID3D12CommandAllocator,IID_ID3D12GraphicsCommandList,IID_ID3D12GraphicsCommandList8;
static int IID_ID3D12DescriptorHeap;
struct D3D12_DESCRIPTOR_HEAP_DESC { int Type,NumDescriptors,Flags; };
struct D3D12_SHADER_RESOURCE_VIEW_DESC { int Format,ViewDimension,Shader4ComponentMapping; struct{int MipLevels;}Texture2D; };
struct D3D12_CPU_DESCRIPTOR_HANDLE { u64 ptr; };
struct ID3D12Resource; struct ID3D12Fence; struct ID3D12CommandAllocator;
using IUnknown=ID3D12Resource;
struct ID3D12GraphicsCommandList; struct ID3D12GraphicsCommandList8; struct Device; struct ID3D12DescriptorHeap;
struct ResourceVT { void(*AddRef)(ID3D12Resource*); void(*Release)(ID3D12Resource*); };
struct FenceVT { void(*AddRef)(ID3D12Fence*); void(*Release)(ID3D12Fence*); u64(*GetCompletedValue)(ID3D12Fence*); };
struct AllocatorVT { void(*Release)(ID3D12CommandAllocator*); };
struct List8VT { void(*Release)(ID3D12GraphicsCommandList8*); };
struct ListVT { HRESULT(*QueryInterface)(ID3D12GraphicsCommandList*,void*,void**);
    HRESULT(*Close)(ID3D12GraphicsCommandList*); void(*Release)(ID3D12GraphicsCommandList*); };
struct DeviceVT { HRESULT(*CreateCommandAllocator)(Device*,int,void*,void**);
    HRESULT(*CreateCommandList)(Device*,int,int,ID3D12CommandAllocator*,void*,void*,void**);
    HRESULT(*CreateDescriptorHeap)(Device*,D3D12_DESCRIPTOR_HEAP_DESC*,void*,void**);
    void(*CreateShaderResourceView)(Device*,ID3D12Resource*,D3D12_SHADER_RESOURCE_VIEW_DESC*,D3D12_CPU_DESCRIPTOR_HANDLE); };
struct HeapVT { void(*GetCPUDescriptorHandleForHeapStart)(ID3D12DescriptorHeap*,D3D12_CPU_DESCRIPTOR_HANDLE*); void(*Release)(ID3D12DescriptorHeap*); };
struct ID3D12DescriptorHeap { HeapVT* lpVtbl; int refs,id,type,size; };
struct ID3D12Resource { ResourceVT* lpVtbl; int refs; };
struct ID3D12Fence { FenceVT* lpVtbl; int refs; u64 completed; };
struct ID3D12CommandAllocator { AllocatorVT* lpVtbl; int refs; };
struct ID3D12GraphicsCommandList8 { List8VT* lpVtbl; int refs; };
struct ID3D12GraphicsCommandList { ListVT* lpVtbl; int refs; ID3D12GraphicsCommandList8* extension; };
struct Device { DeviceVT* lpVtbl; };
struct VPTexSnapshot { u32 refs; };
typedef struct D3D12DrawRecord D3D12DrawRecord;
typedef struct {u32 offset,value;} D3D12TextureLabel;
SLOT_TYPE
OWNER_TYPE
static struct {
    Device* device; ID3D12Fence* fence;
    ID3D12Resource *vertex_buffer,*vp_vb,*vp_cb,*vp_fpcb;
    ID3D12GraphicsCommandList* cmd_list; ID3D12GraphicsCommandList8* cmd_list8;
    D3D12PrefixOwner prefix_owners[2]; u32 prefix_owner_active,frame_index,draw_count;
    int vp_parity,initialized,gcm2d_backend_halted,fifo_prefix_outstanding;
    u64 fence_values[2],fifo_prefix_serial;
    u64 prefix_submit_sequence,prefix_retired_sequence,prefix_vertex_prepare_sequence;
    u32 prefix_deferred_owner, prefix_prepare_owner; int prefix_overlap_request;
    u64 prefix_queue_ahead_record_retires;
    u64 prefix_prepare_admitted, prefix_prepare_pending, prefix_prepare_mid_drains, prefix_prepare_late_drains;
    D3D12TextureLabel texture_labels[8];
    u32 texture_label_count,texture_label_flush_kind; int texture_label_armed;
    ID3D12DescriptorHeap* srv_heap; ID3D12DescriptorHeap* sampler_heaps[SAMPLER_HEAP_PAGE_COUNT];
    u32 srv_inc,sampler_inc; VPTexSlot vp_tex[VP_TEX_SLOTS];
} s_d3d;
static unsigned checks,failures,resource_adds,resource_releases,fence_adds,fence_releases;
static unsigned alloc_created,list_created,list_closes,alloc_releases,list_releases,list8_releases;
static unsigned render_calls,submitted,published,haltes;
static unsigned heap_created,heap_releases,null_writes,snapshot_releases;
static int add_sampler_page;
static int create_fail,render_mode,wait_fail,survival=1; static u64 completed_value;
static int signal_fail; static u64 waited_ticket; static unsigned signals,waits;
static std::vector<std::pair<u32,u32>> label_writes;
#define CHECK(c,m) do{++checks;if(!(c)){++failures;fprintf(stderr,"FAIL %s at %d\n",m,__LINE__);}}while(0)
static void resource_add(ID3D12Resource* r){++resource_adds;++r->refs;}
static void resource_release(ID3D12Resource* r){++resource_releases;--r->refs;}
static void fence_add(ID3D12Fence* r){++fence_adds;++r->refs;}
static void fence_release(ID3D12Fence* r){++fence_releases;--r->refs;}
static u64 fence_get(ID3D12Fence* r){return r->completed;}
static void allocator_release(ID3D12CommandAllocator* r){++alloc_releases;--r->refs;}
static void list_release(ID3D12GraphicsCommandList* r){++list_releases;--r->refs;}
static void list8_release(ID3D12GraphicsCommandList8* r){++list8_releases;--r->refs;}
static HRESULT list_query(ID3D12GraphicsCommandList* r,void*,void** out){*out=r->extension;if(*out)++r->extension->refs;return *out?0:-1;}
static HRESULT list_close(ID3D12GraphicsCommandList*){++list_closes;return create_fail==3?-1:0;}
static ResourceVT resource_vt={resource_add,resource_release};
static FenceVT fence_vt={fence_add,fence_release,fence_get};
static AllocatorVT allocator_vt={allocator_release}; static List8VT list8_vt={list8_release};
static ListVT list_vt={list_query,list_close,list_release};
static ID3D12CommandAllocator allocators[2]; static ID3D12GraphicsCommandList lists[2];
static ID3D12GraphicsCommandList8 extensions[2],ordinary8;
static ID3D12GraphicsCommandList ordinary; static ID3D12Resource resources[4];
static ID3D12Fence fence,other_fence;
static ID3D12DescriptorHeap heaps[12],ordinary_srv,ordinary_sampler;
static ID3D12Resource texture_resources[4]; static VPTexSnapshot snapshots[2];
static void heap_start(ID3D12DescriptorHeap* h,D3D12_CPU_DESCRIPTOR_HANDLE* out){out->ptr=(u64)h->id*1000;}
static void heap_release(ID3D12DescriptorHeap* h){++heap_releases;--h->refs;}
static HeapVT heap_vt={heap_start,heap_release};
static HRESULT create_heap(Device*,D3D12_DESCRIPTOR_HEAP_DESC* d,void*,void** out){
    if(create_fail==4||(create_fail==5&&d->Type==D3D12_DESCRIPTOR_HEAP_TYPE_SAMPLER))return -1;
    auto& h=heaps[heap_created++];h={&heap_vt,1,(int)heap_created,d->Type,d->NumDescriptors};*out=&h;return 0;
}
static void create_srv(Device*,ID3D12Resource* res,D3D12_SHADER_RESOURCE_VIEW_DESC* d,D3D12_CPU_DESCRIPTOR_HANDLE h){
    CHECK(!res&&d->Texture2D.MipLevels==1&&h.ptr%1000<SRV_HEAP_SIZE*4,"invalid private null descriptor");++null_writes;
}
static HRESULT create_allocator(Device*,int,void*,void** out){
    if(create_fail==1)return -1;*out=&allocators[alloc_created++];return 0;
}
static HRESULT create_list(Device*,int,int,ID3D12CommandAllocator*,void*,void*,void** out){
    if(create_fail==2)return -1;*out=&lists[list_created++];return 0;
}
static DeviceVT device_vt={create_allocator,create_list,create_heap,create_srv}; static Device device={&device_vt};
static void gcm2d_backend_halt(const char*){++haltes;s_d3d.gcm2d_backend_halted=1;}
static int d3d12_command_result_checked(HRESULT hr,const char* why){if(hr<0){gcm2d_backend_halt(why);return -1;}return 0;}
static int rsx_survival_trace_is_armed(){return survival;}
static int tj_present_work_trace_enabled(){return 0;}
static int tj_prefix_wait_trace_enabled(){return 0;}
static void tj_prefix_wait_trace_owner(u32,RsxD3D12CompletionReason,u32,u64,int,int){}
static void tj_present_work_trace_arm(int){}
static LONGLONG tj_present_work_trace_begin(){return 0;}
static void tj_present_work_trace_cpu_finish(LONGLONG,int){}
static struct { u64 owner_wait_calls, owner_wait_ticks, cpu_clock_invalid; } s_tj_present_work_trace;
static LONGLONG tj_present_work_trace_sync_begin(){return 0;}
static void tj_present_work_trace_sync_finish(LONGLONG,u64*,u64*){}
static int wait_for_gpu_checked(){
    ++s_d3d.fence_values[s_d3d.frame_index];fence.completed=completed_value;return wait_fail?-1:0;
}
/* Exact queue/wait endpoints; the production fence fixture separately exercises
 * Signal HRESULTs, event wakes, timeout and device removal. */
static int signal_for_gpu_checked(u64* ticket){
    ++signals;*ticket=0;u64 want=++s_d3d.fence_values[s_d3d.frame_index];
    if(signal_fail)return -1;*ticket=want;return 0;
}
static int wait_for_fence_value_checked(u64 ticket,u32){
    ++waits;waited_ticket=ticket;fence.completed=completed_value;return wait_fail?-1:0;
}
static int tj_gcm2d_backend_perf_enabled(){return 0;}
static LONGLONG tj_gcm2d_backend_perf_now(){return 0;}
static int tj_gpu_completion_perf_result(LONGLONG,int result){return result;}
static void d3d12_publish_texture_label_batch(const D3D12TextureLabel* labels,u32 count,u32){
    for(u32 i=0;i<count;++i)label_writes.push_back({labels[i].offset,labels[i].value});
}
static void d3d12_publish_texture_labels(){++published;for(auto& o:s_d3d.prefix_owners)CHECK(!o.phase,"publication before owned retirement");}
static void render_frame(u32,u32,int,int);
static void vp_texture_snapshot_release(VPTexSnapshot** p){if(*p){--(*p)->refs;++snapshot_releases;*p=nullptr;}}
static int d3d12_prefix_overlap_drain(RsxD3D12CompletionReason);
/* Replay preparation is intentionally disabled in this ownership fixture. */
static int d3d12_prefix_prepare_admit(){return 0;}
static int d3d12_prefix_prepare_retire(){return 0;}
static int d3d12_prefix_prepare_before_wait(){return 0;}
static int d3d12_prefix_prepare_before_mutation(const void*){return 0;}
static int d3d12_prefix_queue_ahead_enabled(){return 0;}
static int d3d12_prefix_queue_ahead_prepare(RsxD3D12CompletionReason){return 0;}
static int d3d12_prefix_queue_ahead_record_bank_ready(int){return 0;}
static int d3d12_prefix_vertex_prepare_overlap_enabled(){return 0;}
static int d3d12_prefix_current_gpu_alias(int,const D3D12DrawRecord*){return 0;}
static int d3d12_prefix_queue_ahead_retire_old(u32,RsxD3D12CompletionReason){return -1;}
static int d3d12_prefix_queue_ahead_before_mutation(const void*){return 0;}
PRODUCTION
SAMPLER_GET
static void render_frame(u32,u32,int,int prefix){
    ++render_calls;CHECK(prefix==1,"owned context invoked Present");
    if(!s_d3d.prefix_owner_active){s_d3d.draw_count=0;return;}
    auto& o=s_d3d.prefix_owners[s_d3d.prefix_owner_active-1];
    CHECK(s_d3d.cmd_list==o.list&&s_d3d.cmd_list8==o.list8,"wrong context aliases");
    CHECK(o.phase==1&&o.uploads[1]==s_d3d.vp_vb&&resources[1].refs==2,"upload not retained during preparation");
    CHECK(s_d3d.srv_heap==o.srv_heap&&s_d3d.sampler_heaps[0]==o.sampler_heaps[0]&&o.srv_heap!=&ordinary_srv,"private descriptor aliases missing");
    if(render_mode==3)create_fail=5;
    if(add_sampler_page&&!sampler_heap_get(1))return;
    if(render_mode==1){gcm2d_backend_halt("pre-submit");return;}
    if(d3d12_prefix_owner_submit()!=0)return;
    ++submitted;
    if(render_mode==4){
        if(d3d12_prefix_owner_signal()!=0)return;
        o.cpu_retirement_ready=1;s_d3d.prefix_deferred_owner=s_d3d.prefix_owner_active;
        s_d3d.draw_count=0;s_d3d.vp_parity^=1;s_d3d.fifo_prefix_outstanding=1;
        return;
    }
    if(d3d12_prefix_owner_complete()!=0)return;
    if(render_mode==2){gcm2d_backend_halt("post-fence MAIN commit");return;}
    s_d3d.draw_count=0;s_d3d.vp_parity^=1;++s_d3d.fifo_prefix_serial;
    o.cpu_retirement_ready=1;
}
static void reset(){
    memset(&s_d3d,0,sizeof(s_d3d));
    resource_adds=resource_releases=fence_adds=fence_releases=0;
    alloc_created=list_created=list_closes=alloc_releases=list_releases=list8_releases=0;
    render_calls=submitted=published=haltes=0;create_fail=render_mode=wait_fail=0;completed_value=5;
    signal_fail=0;waited_ticket=0;signals=waits=0;label_writes.clear();
    heap_created=heap_releases=null_writes=snapshot_releases=0;add_sampler_page=0;
    for(int i=0;i<2;++i){allocators[i]={&allocator_vt,1};extensions[i]={&list8_vt,0};lists[i]={&list_vt,1,&extensions[i]};}
    ordinary8={&list8_vt,1};ordinary={&list_vt,1,&ordinary8};
    for(auto& r:resources)r={&resource_vt,1};
    for(auto& r:texture_resources)r={&resource_vt,1};for(auto& snap:snapshots)snap={1};
    ordinary_srv={&heap_vt,1,100,0,SRV_HEAP_SIZE};ordinary_sampler={&heap_vt,1,101,1,SAMPLER_HEAP_SIZE};
    fence={&fence_vt,1,0};other_fence={&fence_vt,1,0};
    s_d3d.device=&device;s_d3d.fence=&fence;s_d3d.initialized=1;s_d3d.draw_count=1;
    s_d3d.vertex_buffer=&resources[0];s_d3d.vp_vb=&resources[1];s_d3d.vp_cb=&resources[2];s_d3d.vp_fpcb=&resources[3];
    s_d3d.cmd_list=&ordinary;s_d3d.cmd_list8=&ordinary8;s_d3d.fence_values[0]=4;
    s_d3d.srv_heap=&ordinary_srv;s_d3d.sampler_heaps[0]=&ordinary_sampler;s_d3d.srv_inc=s_d3d.sampler_inc=4;
}
static void aliases_restored(){CHECK(!s_d3d.prefix_owner_active&&s_d3d.cmd_list==&ordinary&&s_d3d.cmd_list8==&ordinary8&&s_d3d.srv_heap==&ordinary_srv&&s_d3d.sampler_heaps[0]==&ordinary_sampler&&!s_d3d.sampler_heaps[1],"ordinary context/descriptors not restored");}
static void activate(int slot){s_d3d.prefix_owner_active=slot+1;s_d3d.srv_heap=s_d3d.prefix_owners[slot].srv_heap;memcpy(s_d3d.sampler_heaps,s_d3d.prefix_owners[slot].sampler_heaps,sizeof(s_d3d.sampler_heaps));}
static void seed_texture(){auto& t=s_d3d.vp_tex[0];t.res=&texture_resources[0];t.up=&texture_resources[1];t.source_snapshot=&snapshots[0];t.source_hash=73;t.w=64;t.h=32;t.mips=1;t.dimension=2;t.fmt=0x85;}
int main(int argc,char** argv){
    const int enabled=argc>1&&atoi(argv[1]);reset();CHECK(d3d12_prefix_ownership_enabled()==enabled,"strict opt-in");
    if(!enabled){s_d3d.vp_parity=99;s_d3d.prefix_owners[0].phase=3;CHECK(d3d12_prefix_owner_before_upload(1,0,NULL)==0,"disabled guard changes baseline");reset();CHECK(rsx_d3d12_backend_complete_fifo_prefix()==0&&render_calls==1&&!alloc_created,"disabled path creates owned context");}
    else {
        CHECK(rsx_d3d12_backend_complete_fifo_prefix()==0&&submitted==1&&published==1,"owned prefix did not finish");aliases_restored();
        CHECK(resource_adds==4&&resource_releases==4&&fence_adds==1&&fence_releases==1,"checked prefix leaked/released early");
        s_d3d.draw_count=1;completed_value=6;CHECK(rsx_d3d12_backend_complete_fifo_prefix()==0&&submitted==2&&published==2,"second bank did not finish");aliases_restored();
        CHECK(alloc_created==2&&list_created==2&&s_d3d.prefix_owners[0].list!=s_d3d.prefix_owners[1].list&&s_d3d.prefix_owners[0].allocator!=s_d3d.prefix_owners[1].allocator,"contexts not independent");
        CHECK(heap_created==4&&null_writes==SRV_HEAP_SIZE*2&&s_d3d.prefix_owners[0].srv_heap!=s_d3d.prefix_owners[1].srv_heap&&s_d3d.prefix_owners[0].sampler_heaps[0]!=s_d3d.prefix_owners[1].sampler_heaps[0],"private descriptor heaps not independent");
        s_d3d.draw_count=1;completed_value=7;CHECK(rsx_d3d12_backend_complete_fifo_prefix()==0&&alloc_created==2&&list_created==2,"retired context not reused");
        CHECK(d3d12_prefix_owner_destroy(0)==0&&d3d12_prefix_owner_destroy(1)==0&&alloc_releases==2&&list_releases==2&&list8_releases==2,"context shutdown leak");
        CHECK(heap_releases==4&&ordinary_srv.refs==1&&ordinary_sampler.refs==1,"descriptor shutdown released ordinary or leaked owned heaps");
        reset();add_sampler_page=1;CHECK(rsx_d3d12_backend_complete_fifo_prefix()==0&&s_d3d.prefix_owners[0].sampler_heaps[1]&&heap_created==3,"lazy sampler page not retained");aliases_restored();CHECK(d3d12_prefix_owner_destroy(0)==0&&heap_releases==3,"lazy page leak");
        reset();add_sampler_page=1;render_mode=3;CHECK(rsx_d3d12_backend_complete_fifo_prefix()<0&&!submitted&&!published&&resource_releases==4,"failed lazy sampler page submitted/published");aliases_restored();CHECK(d3d12_prefix_owner_destroy(0)==0&&heap_releases==2,"failed lazy page cleanup leak");
        for(int kind=0;kind<2;++kind){reset();if(kind)s_d3d.sampler_inc=0;else s_d3d.srv_inc=0;CHECK(rsx_d3d12_backend_complete_fifo_prefix()<0&&!submitted&&!published&&!heap_created&&!null_writes,"missing descriptor stride wrote/submitted descriptors");aliases_restored();CHECK(d3d12_prefix_owner_destroy(0)==0&&alloc_releases==1&&list_releases==1,"missing stride context leak");}
        for(int slot=0;slot<2;++slot){reset();s_d3d.vp_parity=slot;CHECK(d3d12_prefix_owner_begin(slot)==0,"bank reserve failed");auto& o=s_d3d.prefix_owners[slot];
            CHECK(o.upload_first[0]==0&&o.upload_first[1]==(u64)slot*MAX_VERTICES*VP_VERT_STRIDE&&o.upload_first[2]==(u64)slot*MAX_DRAWS*VP_CB_STRIDE&&o.upload_first[3]==(u64)slot*MAX_DRAWS*FP_CB_STRIDE,"wrong reserved byte ranges");
            CHECK(o.upload_bytes[0]==MAX_VERTICES*VERTEX_STRIDE&&o.upload_bytes[1]==MAX_VERTICES*VP_VERT_STRIDE,"wrong reservation widths");
            CHECK(d3d12_prefix_owner_end(slot)==0&&resource_releases==4,"unsubmitted cancellation leaks");}
        for(int why=0;why<5;++why){reset();if(why==0)wait_fail=1;if(why==1)completed_value=4;if(why==2)completed_value=UINT64_MAX;if(why==3)render_mode=2;if(why==4)create_fail=3;
            CHECK(rsx_d3d12_backend_complete_fifo_prefix()<0&&!published,"failed owner published label");aliases_restored();
            if(why<4){CHECK(s_d3d.prefix_owners[0].phase==3&&!resource_releases&&resources[1].refs==2,"failed submitted resources released");CHECK(d3d12_prefix_owner_destroy(0)<0&&!list_releases,"shutdown released uncertain owner");}
            else CHECK(!submitted&&!resource_adds,"failed context submitted/reserved uploads");}
        for(int why:{1,2,4,5}){reset();create_fail=why;CHECK(rsx_d3d12_backend_complete_fifo_prefix()<0&&!submitted&&!published&&!resource_adds,"factory failure did not fail closed");aliases_restored();CHECK(d3d12_prefix_owner_destroy(0)==0&&heap_releases==heap_created,"unsubmitted factory resources leak");}
        reset();render_mode=1;CHECK(rsx_d3d12_backend_complete_fifo_prefix()<0&&!submitted&&!published&&resource_releases==4,"unsubmitted failure retained GPU ownership");aliases_restored();
        reset();s_d3d.fence_values[0]=UINT64_MAX;CHECK(rsx_d3d12_backend_complete_fifo_prefix()<0&&!submitted&&!published&&resource_releases==4,"fence wrap submitted work");
        for(int why=0;why<4;++why){reset();CHECK(d3d12_prefix_owner_begin(0)==0,"ticket setup");activate(0);CHECK(d3d12_prefix_owner_submit()==0&&d3d12_prefix_owner_signal()==0,"ticket submit/signal");fence.completed=5;
            if(why==0)s_d3d.fence=&other_fence;if(why==1)s_d3d.prefix_owners[0].signal_queued=0;if(why==2)s_d3d.prefix_owners[0].frame_index=2;if(why==3)s_d3d.prefix_owners[0].fence_value=UINT64_MAX;
            CHECK(d3d12_prefix_owner_checked_completion(0,0)<0&&d3d12_prefix_owner_end(0)<0&&!resource_releases,"wrong fence/ticket accepted");}
        reset();s_d3d.prefix_owners[0].phase=2;s_d3d.vp_parity=1;
        CHECK(d3d12_prefix_owner_before_upload(0,0,NULL)==0,"disjoint VP bank rejected");CHECK(d3d12_prefix_owner_before_upload(1,0,NULL)<0,"shared basic VB overwrite allowed");
        reset();s_d3d.prefix_owners[1].phase=2;s_d3d.vp_parity=1;CHECK(d3d12_prefix_owner_before_upload(0,0,NULL)<0,"live VP bank overwrite allowed");
        reset();s_d3d.prefix_owners[0].phase=3;s_d3d.vp_parity=1;CHECK(d3d12_prefix_owner_before_upload(0,0,NULL)<0,"failed owner reuse allowed");
        reset();s_d3d.prefix_owners[1].phase=1;CHECK(d3d12_prefix_owner_before_upload(0,0,NULL)<0,"foreign unsubmitted reservation overwritten");
        reset();s_d3d.vp_parity=2;CHECK(d3d12_prefix_owner_before_upload(0,0,NULL)<0,"invalid parity accepted");
        reset();CHECK(d3d12_prefix_owner_begin(2)<0,"invalid slot accepted");reset();s_d3d.prefix_owner_active=3;CHECK(d3d12_prefix_owner_submit()<0,"invalid active slot accepted");
        reset();seed_texture();CHECK(d3d12_prefix_owner_begin(0)==0,"texture setup");activate(0);CHECK(d3d12_prefix_owner_texture_capture(0)==0&&snapshots[0].refs==2&&texture_resources[0].refs==2&&texture_resources[1].refs==2,"texture not retained");
        CHECK(d3d12_prefix_owner_texture_capture(0)==0&&snapshots[0].refs==2&&s_d3d.prefix_owners[0].texture_count==1,"duplicate texture pin added references");s_d3d.vp_tex[0].used=1;CHECK(d3d12_prefix_owner_texture_capture(0)==0,"used marker changes texture identity");
        CHECK(d3d12_prefix_owner_texture_reserved(0)&&!d3d12_prefix_owner_texture_reserved(1),"reservation range wrong");CHECK(d3d12_prefix_owner_end(0)==0&&snapshots[0].refs==1&&texture_resources[0].refs==1&&texture_resources[1].refs==1&&!d3d12_prefix_owner_texture_reserved(0),"cancelled texture still held");
        for(int field=0;field<16;++field){reset();seed_texture();CHECK(d3d12_prefix_owner_begin(0)==0,"version setup");activate(0);CHECK(d3d12_prefix_owner_texture_capture(0)==0,"version capture");auto& t=s_d3d.vp_tex[0];switch(field){case 0:t.res=&texture_resources[2];break;case 1:t.up=&texture_resources[3];break;case 2:t.source_snapshot=&snapshots[1];break;case 3:t.source_hash++;break;case 4:t.off++;break;case 5:t.w++;break;case 6:t.h++;break;case 7:t.depth++;break;case 8:t.fmt++;break;case 9:t.pitch++;break;case 10:t.mips++;break;case 11:t.cube++;break;case 12:t.dimension++;break;case 13:t.dxgi++;break;case 14:t.has_border++;break;case 15:s_d3d.prefix_owners[0].phase=2;break;}
            CHECK(d3d12_prefix_owner_texture_capture(0)<0&&snapshots[0].refs==2&&texture_resources[0].refs==2,"changed texture version accepted/released");}
        for(int phase=1;phase<=3;++phase){reset();seed_texture();CHECK(d3d12_prefix_owner_begin(0)==0,"held setup");activate(0);CHECK(d3d12_prefix_owner_texture_capture(0)==0,"held capture");s_d3d.prefix_owners[0].phase=phase;s_d3d.prefix_owner_active=0;CHECK(d3d12_prefix_owner_texture_mutable(0)<0&&d3d12_prefix_owner_texture_reserved(0),"held version overwritten");if(phase>=2)CHECK(d3d12_prefix_owner_destroy(0)<0&&texture_resources[0].refs==2&&!heap_releases,"uncertain texture/descriptor released");}
        reset();seed_texture();CHECK(d3d12_prefix_owner_begin(0)==0,"retirement setup");activate(0);CHECK(d3d12_prefix_owner_texture_capture(0)==0&&d3d12_prefix_owner_submit()==0&&d3d12_prefix_owner_signal()==0,"texture submit");fence.completed=5;s_d3d.prefix_owners[0].cpu_retirement_ready=1;CHECK(d3d12_prefix_owner_checked_completion(0,0)==0&&d3d12_prefix_owner_end(0)==0&&snapshots[0].refs==1&&texture_resources[0].refs==1,"checked texture did not retire");
        reset();CHECK(d3d12_prefix_owner_begin(0)==0,"wrong heap setup");s_d3d.prefix_owner_active=1;CHECK(d3d12_prefix_owner_submit()<0&&!submitted,"ordinary descriptors submitted as private");
        reset();seed_texture();CHECK(d3d12_prefix_owner_begin(0)==0,"shared version setup");activate(0);CHECK(d3d12_prefix_owner_texture_capture(0)==0,"first shared pin");s_d3d.prefix_owner_active=0;CHECK(d3d12_prefix_owner_begin(1)==0,"second shared setup");activate(1);CHECK(d3d12_prefix_owner_texture_capture(0)==0&&snapshots[0].refs==3&&texture_resources[0].refs==3,"identical version not retained independently");CHECK(d3d12_prefix_owner_end(0)==0&&snapshots[0].refs==2&&d3d12_prefix_owner_texture_reserved(0),"first retirement freed shared version");CHECK(d3d12_prefix_owner_end(1)==0&&snapshots[0].refs==1&&!d3d12_prefix_owner_texture_reserved(0),"last shared reservation leak");
    }
    if(enabled){
        reset();render_mode=4;s_d3d.texture_labels[0]={4,99};s_d3d.texture_label_count=1;
        render_fifo_prefix_owned();aliases_restored();
        CHECK(signals==1&&!waits&&s_d3d.prefix_deferred_owner==1&&!resource_releases&&
            s_d3d.prefix_owners[0].phase==2&&label_writes.empty(),"deferred wrapper waited or freed owner");
        CHECK(d3d12_prefix_owner_before_upload(0,0,NULL)==0&&!waits&&resources[1].refs==2,
            "next disjoint bank cannot prepare while GPU is pending");
        CHECK(d3d12_prefix_owner_before_upload(1,0,NULL)==0&&waits==1&&resources[1].refs==1&&
            !s_d3d.prefix_deferred_owner&&!s_d3d.fifo_prefix_outstanding&&
            s_d3d.fifo_prefix_serial==1&&label_writes.size()==1,"shared upload did not drain first");
        reset();render_mode=4;render_fifo_prefix_owned();s_d3d.vp_parity=0;
        CHECK(d3d12_prefix_owner_before_upload(0,0,NULL)==0&&waits==1&&!s_d3d.prefix_deferred_owner,
            "same upload bank reused before completion");
        reset();render_mode=4;render_fifo_prefix_owned();wait_fail=1;
        CHECK(d3d12_prefix_owner_before_upload(1,0,NULL)<0&&!resource_releases&&
            s_d3d.prefix_deferred_owner==1&&s_d3d.prefix_owners[0].phase==3,
            "failed reuse drain released GPU storage");
        reset();render_mode=4;render_fifo_prefix_owned();s_d3d.texture_label_armed=2;
        CHECK(d3d12_prefix_overlap_drain(RSX_COMPLETION_DRAIN)<0&&!waits&&!resource_releases,
            "drain retired before current label was attached");
        // Two submitted owners, later frame counters, repeated label addresses.
        // Drain must use each exact ticket, independent of the active alias.
        reset();s_d3d.texture_labels[0]={4,11};s_d3d.texture_labels[1]={4,12};
        s_d3d.texture_label_count=2;s_d3d.texture_label_flush_kind=2;
        CHECK(d3d12_prefix_owner_begin(0)==0,"first ordered reserve");activate(0);
        CHECK(d3d12_prefix_owner_submit()==0&&d3d12_prefix_owner_signal()==0,"first ordered submit");
        CHECK(!s_d3d.texture_label_count&&s_d3d.prefix_owners[0].label_count==2&&
            s_d3d.prefix_owners[0].label_flush_kind==2&&label_writes.empty(),"labels not transferred privately");
        s_d3d.prefix_owner_active=0;s_d3d.texture_labels[0]={4,13};s_d3d.texture_label_count=1;
        CHECK(d3d12_prefix_owner_begin(1)==0,"second ordered reserve");activate(1);
        CHECK(d3d12_prefix_owner_submit()==0&&d3d12_prefix_owner_signal()==0,"second ordered submit");
        CHECK(s_d3d.prefix_owners[0].fence_value==5&&s_d3d.prefix_owners[1].fence_value==6&&
            signals==2&&!waits&&label_writes.empty(),"signal waited or overwrote a ticket");
        s_d3d.prefix_owner_active=0;s_d3d.frame_index=1;s_d3d.fence_values[0]=99;s_d3d.fence_values[1]=100;
        completed_value=5;
        CHECK(d3d12_prefix_owner_wait(0,TJ_PREFIX_WAIT_SYNC,RSX_COMPLETION_OTHER)==0&&waited_ticket==5&&signals==2&&label_writes.empty(),"old owner depends on new counter or publishes before CPU work");
        s_d3d.prefix_owners[0].cpu_retirement_ready=s_d3d.prefix_owners[1].cpu_retirement_ready=1;
        completed_value=6;
        CHECK(d3d12_prefix_owner_drain_ready(RSX_COMPLETION_OTHER)==0&&waited_ticket==6&&signals==2&&
            s_d3d.prefix_retired_sequence==2&&!s_d3d.prefix_owners[0].phase&&!s_d3d.prefix_owners[1].phase,
            "ordered drain failed across frame/counter change");
        CHECK((label_writes==std::vector<std::pair<u32,u32>>({{4,11},{4,12},{4,13}})),"labels lost, combined or reordered");
        CHECK(d3d12_prefix_owner_drain_ready(RSX_COMPLETION_OTHER)==0&&label_writes.size()==3,"second drain duplicated labels");
        for(int failure=0;failure<2;++failure){
            reset();
            for(u32 slot=0;slot<2;++slot){
                s_d3d.prefix_owner_active=0;s_d3d.texture_labels[0]={4,slot+20};s_d3d.texture_label_count=1;
                CHECK(d3d12_prefix_owner_begin(slot)==0,"ordered failure reserve");activate(slot);
                CHECK(d3d12_prefix_owner_submit()==0&&d3d12_prefix_owner_signal()==0,"ordered failure submit");
                s_d3d.prefix_owners[slot].cpu_retirement_ready=1;
            }
            s_d3d.prefix_owner_active=0;
            if(failure==0){
                completed_value=6;CHECK(d3d12_prefix_owner_wait(1,TJ_PREFIX_WAIT_SYNC,RSX_COMPLETION_OTHER)==0,"later fence can finish first");
                CHECK(d3d12_prefix_owner_end(1)<0&&label_writes.empty()&&!resource_releases&&
                    s_d3d.prefix_retired_sequence==0,"later completion published ahead of older owner");
            }else{
                completed_value=5;
                CHECK(d3d12_prefix_owner_drain_ready(RSX_COMPLETION_OTHER)<0&&label_writes.size()==1&&label_writes[0].second==20&&
                    s_d3d.prefix_retired_sequence==1&&!s_d3d.prefix_owners[0].phase&&
                    s_d3d.prefix_owners[1].phase==3&&resource_releases==4,
                    "second-owner failure lost first completion or published later label");
            }
        }
        for(int failure=0;failure<7;++failure){
            reset();s_d3d.texture_labels[0]={4,77};s_d3d.texture_label_count=1;
            CHECK(d3d12_prefix_owner_begin(0)==0,"held label reserve");activate(0);
            CHECK(d3d12_prefix_owner_submit()==0,"held label submit");
            if(failure==0)signal_fail=1;
            int signaled=d3d12_prefix_owner_signal();s_d3d.prefix_owner_active=0;
            auto& owner=s_d3d.prefix_owners[0];owner.cpu_retirement_ready=failure!=1;
            if(failure==2)completed_value=4;
            if(failure==3)completed_value=UINT64_MAX;
            if(failure==4)wait_fail=1;
            if(failure==5)owner.sequence=2;
            if(failure==6)owner.label_count=9;
            CHECK(d3d12_prefix_owner_drain_ready(RSX_COMPLETION_OTHER)<0&&label_writes.empty()&&!resource_releases&&
                owner.label_count&&s_d3d.prefix_retired_sequence==0,"failed drain released/published uncertain work");
            CHECK(failure!=0||signaled<0,"failed signal accepted");
        }
        reset();s_d3d.texture_labels[0]={4,42};s_d3d.texture_label_count=1;render_mode=2;
        CHECK(rsx_d3d12_backend_complete_fifo_prefix()<0&&label_writes.empty()&&
            s_d3d.prefix_owners[0].label_count==1&&!resource_releases,"MAIN failure published held label");
        reset();s_d3d.texture_labels[0]={4,42};s_d3d.texture_label_count=1;render_mode=1;
        CHECK(rsx_d3d12_backend_complete_fifo_prefix()<0&&label_writes.empty()&&
            s_d3d.texture_label_count==1&&resource_releases==4,"unsubmitted failure lost pending label");
        reset();CHECK(d3d12_prefix_owner_begin(0)==0,"no-signal reserve");activate(0);
        CHECK(d3d12_prefix_owner_submit()==0,"no-signal submit");s_d3d.prefix_owner_active=0;
        CHECK(d3d12_prefix_owner_wait(0,TJ_PREFIX_WAIT_SYNC,RSX_COMPLETION_OTHER)<0&&!signals&&!waits&&!resource_releases,"unsignaled submission treated as complete");
        reset();CHECK(d3d12_prefix_owner_begin(0)==0,"duplicate-signal reserve");activate(0);
        CHECK(d3d12_prefix_owner_submit()==0&&d3d12_prefix_owner_signal()==0,"duplicate-signal submit");
        CHECK(d3d12_prefix_owner_signal()<0&&signals==1&&!resource_releases,"duplicate queue signal replaced owner ticket");
        reset();CHECK(d3d12_prefix_owner_begin(0)==0,"object cancellation setup");activate(0);
        CHECK(d3d12_prefix_owner_object_capture(&resources[0])==0&&resources[0].refs==3,"actual object not retained");
        CHECK(d3d12_prefix_owner_object_capture(&resources[0])==0&&resources[0].refs==3&&s_d3d.prefix_owners[0].object_count==1,"duplicate object retained twice");
        CHECK(d3d12_prefix_owner_end(0)==0&&resources[0].refs==1&&!s_d3d.prefix_owners[0].object_count,"cancelled object did not release");
        reset();CHECK(d3d12_prefix_owner_begin(0)==0,"object failure setup");activate(0);
        CHECK(d3d12_prefix_owner_object_capture(&resources[0])==0&&d3d12_prefix_owner_submit()==0,"object submission setup");
        CHECK(d3d12_prefix_owner_checked_completion(0,-1)<0,"object failure not held");s_d3d.prefix_owner_active=0;
        CHECK(d3d12_prefix_owner_destroy(0)<0&&resources[0].refs==3&&d3d12_prefix_owner_object_reserved(&resources[0]),"uncertain object released/reused");
        reset();CHECK(d3d12_prefix_owner_begin(0)==0,"object completion setup");activate(0);
        CHECK(d3d12_prefix_owner_object_capture(&resources[0])==0&&d3d12_prefix_owner_submit()==0,"object checked submission");
        CHECK(d3d12_prefix_owner_signal()==0,"object signal");fence.completed=5;s_d3d.prefix_owners[0].cpu_retirement_ready=1;
        CHECK(d3d12_prefix_owner_checked_completion(0,0)==0&&!d3d12_prefix_owner_object_reserved(&resources[0]),"checked object still prevents recycling");
        CHECK(d3d12_prefix_owner_end(0)==0&&resources[0].refs==1&&!s_d3d.prefix_owners[0].object_count,"checked object did not release");
    }
    printf("Prefix ownership enabled=%d: %u checks, %u failures\n",enabled,checks,failures);return failures?1:0;
}
'''.replace("SLOT_TYPE", slot_type).replace("OWNER_TYPE", owner_type).replace("PRODUCTION", production).replace("SAMPLER_GET", sampler_get).replace("COMPLETION_REASONS", reasons).replace("COST_ENUM", cost_enum).replace("WAIT_TRACE_ENUMS", wait_trace_enums)
    compiler = shutil.which("clang++") or r"C:\Program Files\LLVM\bin\clang++.exe"
    with tempfile.TemporaryDirectory(prefix="tj-prefix-owner-") as directory:
        cpp, exe = Path(directory) / "owner.cpp", Path(directory) / "owner.exe"
        cpp.write_text(fixture, encoding="utf-8")
        subprocess.run([compiler, "-std=c++17", str(cpp), "-o", str(exe)], check=True)
        for value in (None, "0", "", "1x", "1"):
            env = dict(os.environ)
            env.pop("TJ_PREFIX_OWNERSHIP", None)
            if value is not None:
                env["TJ_PREFIX_OWNERSHIP"] = value
            subprocess.run([str(exe), str(int(value == "1"))], env=env, check=True, timeout=10)


if __name__ == "__main__":
    main()
