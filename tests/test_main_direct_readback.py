"""Compile production terminal-export admission, planning, copy, and commit.

Fake D3D resources carry real pixels. This proves host ordering and rectangle
preservation, not hardware performance or full gameplay.
"""
from pathlib import Path
import os
import shutil
import subprocess
import tempfile

from test_fifo_present_boundary import function
from test_main_mapping_lifetime import struct

ROOT = Path(__file__).resolve().parents[1]
VIDEO = ROOT.parent / "ps3recomp/libs/video"


def main():
    backend = (VIDEO / "rsx_d3d12_backend.c").read_text(encoding="utf-8")
    # Definitions with forward declarations must be selected explicitly.
    names = (
        "static int gpu_main_canonical_identity(",
        "static int gpu_blit_record_mapping_valid(",
        "static int gpu_mirror_canonical_overlap(",
        "static int exact_writeback_direct_identity(",
        "static ID3D12Resource* gpu_blit_endpoint_resource(const GpuBlitEndpoint* e)\n{",
        "static D3D12_RESOURCE_STATES gpu_blit_endpoint_state(const GpuBlitEndpoint* e)\n{",
        "static void gpu_blit_endpoint_transition(const GpuBlitEndpoint* e, D3D12_RESOURCE_STATES to)\n{",
        "static int gpu_blit_direct_source_valid(const RsxD3D12ExactBlit* b,\n                                       const GpuBlitEndpoint* src)\n{",
        "static int tj_main_direct_readback_enabled(",
        "static int tj_main_mapped_commit_enabled(",
        "static int tj_main_direct_reject(",
        "static int gpu_blit_direct_readback_admit(",
        "int rsx_d3d12_backend_complete_fifo_prefix_at(",
        "static void exact_writeback_transfer_baseline(",
        "static int exact_writeback_preflight(",
        "static int exact_writeback_queue(",
        "static void exact_writeback_failure_set(",
        "static int exact_writeback_map_convert_all(",
        "static int guest_world_succeeded(",
        "static int exact_writeback_commit_all(",
    )
    functions = "\n".join(function(backend, name) for name in names)
    plan = struct(backend, "ExactBlitWritebackPlan")
    assert "trace_source_flags" in plan
    render = function(backend, "static void render_frame(")
    frame_writeback = render[render.index("/* Must finish before any diagnostics or Present."):]
    exact_record = function(backend, "int rsx_d3d12_backend_record_exact_blit(")
    assert "scaled_blit_src_authoritative" not in exact_record
    assert exact_record.index("memset(dr, 0, sizeof(*dr))") < exact_record.index(
        "dr->is_scaled_blit = 1;")
    local_main = function(backend, "int rsx_d3d12_backend_complete_local_main_prefix(void)")
    assert local_main.index("d3d12_prefix_overlap_drain(RSX_COMPLETION_LOCAL_MAIN_COPY)") < \
        local_main.index("const D3D12DrawRecord* dr = &s_d3d.draws[s_d3d.draw_count - 1]") < \
        local_main.index("rsx_d3d12_backend_complete_fifo_prefix_at(RSX_COMPLETION_LOCAL_MAIN_COPY)")
    prefix_owner = function(backend, "static void render_fifo_prefix_owned(void)")
    prepare_decline = prefix_owner.index("const int prepare_overlap = d3d12_prefix_prepare_admit();")
    entry_drain = prefix_owner.index(
        "if (!prepare_overlap && d3d12_prefix_overlap_drain(RSX_COMPLETION_OTHER) != 0) return;",
        prepare_decline,
    )
    owner_begin = prefix_owner.index("d3d12_prefix_owner_begin(slot)", entry_drain)
    render_entry = prefix_owner.index("render_frame(0, 0, 0, 1);", owner_begin)
    prepare = function(backend, "static int d3d12_prefix_prepare_admit(void)")
    assert "!dr->is_vp || dr->is_scaled_blit" in prepare
    assert prepare_decline < entry_drain < owner_begin < render_entry
    endpoint_capture = render.index("gpu_blit_endpoint_get(&dr->scaled_blit, 1, &src)")
    authority_refresh = render.index(
        "dr->scaled_blit_src_authoritative =\n                    gpu_blit_preflight_authoritative_source(_d, dr, &src)",
        endpoint_capture,
    )
    admission = render.index("gpu_blit_direct_readback_admit(_d, dr, &src, fifo_prefix)",
                             authority_refresh)
    assert endpoint_capture < authority_refresh < admission
    direct_source = function(backend,
        "static int gpu_blit_direct_source_valid(const RsxD3D12ExactBlit* b,\n                                       const GpuBlitEndpoint* src)\n{")
    assert "gpu_blit_endpoint_resource(src)" in direct_source
    assert "resource->lpVtbl->GetDesc(resource, &desc)" in direct_source
    direct_admit = function(backend, "static int gpu_blit_direct_readback_admit(")
    assert direct_admit.index("!dr->scaled_blit_src_authoritative") < direct_admit.index(
        "!gpu_blit_direct_source_valid(&dr->scaled_blit, src)")
    assert render.index("gpu_blit_direct_readback_admit") < render.index(
        "endpoint_fault = gpu_blit_endpoint_get(&dr->scaled_blit, 0, &dst)")
    execute = function(backend, "static int gpu_blit_execute(")
    assert execute.index('"display-source-unproduced"') < execute.index(
        "if (dr->scaled_blit_direct_readback) return 0;") < execute.index(
        "ID3D12Resource* dst_res")
    assert render.index("gpu_blit_execute(dr, d)") < render.index("exact_writeback_queue(d)")
    assert render.index("int fence_ok =") < render.index("exact_writeback_map_convert_all")
    assert "else exact_writeback_abandon_after_submit();" in render
    mapped_getter = function(backend, "static int tj_main_mapped_commit_enabled(")
    assert 'getenv("TJ_MAIN_MAPPED_COMMIT")' in mapped_getter
    assert 'strcmp(value, "1") == 0' in mapped_getter
    allocator = function(backend, "static int exact_writeback_readback_alloc(")
    assert allocator.index("p->mapped_commit = tj_main_mapped_commit_enabled()") < \
        allocator.index("if (!p->mapped_commit)")
    map_all = function(backend, "static int exact_writeback_map_convert_all(")
    assert map_all.index("->Map(") < map_all.index("p->mapped_rgba = (const u8*)mapped")
    assert "if (p->mapped_commit)" in map_all and "p->readback_mapped" in map_all and "->Unmap(" in map_all
    commit_all = function(backend, "static int exact_writeback_commit_all(")
    assert commit_all.index('"COMMIT_STORAGE"') < commit_all.index("guest_world_exclusive_begin")
    assert commit_all.index("guest_world_exclusive_begin") < commit_all.index("rsx_exact_blit_rgba_to_guest_argb")
    assert commit_all.index("rsx_exact_blit_rgba_to_guest_argb") < commit_all.index("guest_world_exclusive_end")
    assert "->Map(" not in commit_all and "->Unmap(" not in commit_all
    release = function(backend, "static void exact_writeback_release(")
    assert release.index("->Unmap(") < release.index("gpu_scratch_retain")
    assert frame_writeback.index("wait_for_gpu_checked() == 0") < \
        frame_writeback.index("exact_writeback_map_convert_all") < \
        frame_writeback.index("exact_writeback_commit_all") < \
        frame_writeback.index("if (fence_ok) exact_writeback_release()")
    assert render.index("exact_writeback_map_convert_all") < render.index("exact_writeback_commit_all")
    assert render.index("exact_writeback_commit_all") < render.index("if (fence_ok) exact_writeback_release()")
    assert frame_writeback.index("if (!wb_ok)") < frame_writeback.index("Present(")
    fixture = r'''
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <d3d12.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "rsx_d3d12_backend.h"
#include "rsx_scaled_blit.h"
#define MAX_DRAWS 4
#define MAX_GPU_MIRRORS 4
#define MAX_DISPLAY_SURFACES 4
#define MAX_OFF_RTS 4
enum { GPU_BLIT_DISPLAY, GPU_BLIT_OFFRT, GPU_BLIT_MIRROR };
typedef struct { int kind,slot; u32 x,y; } GpuBlitEndpoint;
MIRROR
PLAN
FAILURE
typedef struct {
    int is_scaled_blit,is_vp,scaled_blit_prepared,scaled_blit_direct_readback;
    int scaled_blit_src_authoritative,scaled_blit_executed;
    int scaled_blit_src_kind,scaled_blit_src_slot,scaled_blit_dst_kind,scaled_blit_dst_slot;
    u32 scaled_blit_src_x,scaled_blit_src_y,scaled_blit_dst_x,scaled_blit_dst_y;
    u8 rt_local,rt_mrt_local[3];
    RsxD3D12ExactBlit scaled_blit;
} D3D12DrawRecord;
static struct {
    D3D12DrawRecord draws[MAX_DRAWS]; u32 draw_count;
    GpuMirror gpu_mirror[MAX_GPU_MIRRORS];
    struct { ID3D12Resource* res; D3D12_RESOURCE_STATES st; } display_surface[4],off_rt[4];
    int gcm2d_backend_halted;
} s_d3d;
static ExactBlitWritebackPlan s_exact_writeback[MAX_DRAWS];
static u32 s_exact_writeback_count;
static RsxD3D12CompletionReason s_tj_completion_reason=RSX_COMPLETION_LOCAL_MAIN_COPY;
static RsxD3D12CompletionReason observed_reason;
static int completion_result;
static int tj_gcm2d_backend_perf_enabled(void) { return 0; }
static int tj_prefix_cost_enabled(void) { return 0; }
static int tj_prefix_wait_trace_enabled(void) { return 0; }
static int tj_main_mapped_commit_enabled(void);
int rsx_d3d12_backend_complete_fifo_prefix(void) {
    observed_reason=s_tj_completion_reason; return completion_result;
}
static u8* vm_base;
static u32 ppu_vm_size=4096;
static u64 generation=2;
static int resolve_ok=1,fail_alloc,fail_map,fail_map_on,fail_map_success_null,fail_map_error_data;
static int fail_copy,world,poison,compose,rejects;
static unsigned unmap_without_view;
static unsigned checks,failures,copies,pins,bumps,transitions,maps,unmaps,active_maps;
#define CHECK(c) do { ++checks; if(!(c)) { ++failures; fprintf(stderr,"FAIL %d: %s\n",__LINE__,#c); } } while(0)
u64 cellGcmMainMappingGeneration(void) { return generation; }
int cellGcmResolveLocatedChecked(u8 local,u32 raw,u32* ea) {
    *ea=raw+(local?0x800u:0x100u); return resolve_ok;
}
static int rsx_survival_trace_is_armed(void) { return 0; }
static int tj_compose_target_enabled(void) { return compose; }
static int legacy_record_supported(const D3D12DrawRecord* dr) { (void)dr; return 0; }
static int dr_rt_attachment_enabled(const D3D12DrawRecord* dr,int t) { (void)dr; return t==0; }
static int guest_world_is_poisoned(void) { return poison; }
static int guest_world_exclusive_begin(void) {
    CHECK(!world);
    for(u32 i=0;i<s_exact_writeback_count;i++)
        if(s_exact_writeback[i].mapped_commit)
            CHECK(s_exact_writeback[i].mapped_rgba && s_exact_writeback[i].readback_mapped && active_maps);
    world=1; return 1;
}
static int guest_world_exclusive_end(void) {
    CHECK(world);
    for(u32 i=0;i<s_exact_writeback_count;i++)
        if(s_exact_writeback[i].mapped_commit)
            CHECK(s_exact_writeback[i].mapped_rgba && s_exact_writeback[i].readback_mapped && active_maps);
    world=0; return 1;
}
static void spu_reservation_bump_locked(u32 line) { CHECK(world && !(line&127)); ++bumps; }
typedef struct { ID3D12Resource iface; D3D12_RESOURCE_DESC desc; u8 data[4096]; } FakeResource;
static D3D12_RESOURCE_DESC* STDMETHODCALLTYPE fake_desc(ID3D12Resource* r,D3D12_RESOURCE_DESC* out) {
    *out=((FakeResource*)r)->desc; return out;
}
static HRESULT STDMETHODCALLTYPE fake_map(ID3D12Resource* r,UINT sub,const D3D12_RANGE* range,void** out) {
    (void)sub;(void)range; CHECK(!world); ++maps; *out=NULL;
    if(fail_map_success_null) return S_OK;
    if(fail_map || (fail_map_on && maps==fail_map_on)) {
        if(fail_map_error_data) *out=((FakeResource*)r)->data;
        return E_FAIL;
    }
    *out=((FakeResource*)r)->data; ++active_maps; return S_OK;
}
static void STDMETHODCALLTYPE fake_unmap(ID3D12Resource* r,UINT sub,const D3D12_RANGE* range) {
    (void)r;(void)sub;(void)range; CHECK(!world); ++unmaps;
    if(active_maps) --active_maps; else ++unmap_without_view;
}
static ID3D12ResourceVtbl vtable={.GetDesc=fake_desc,.Map=fake_map,.Unmap=fake_unmap};
static FakeResource source,readback,readback2;
static void display_surface_transition(int slot,D3D12_RESOURCE_STATES to) {
    ++transitions; s_d3d.display_surface[slot].st=to;
}
static void off_rt_transition(int slot,D3D12_RESOURCE_STATES to) {
    ++transitions; s_d3d.off_rt[slot].st=to;
}
static void gpu_mirror_transition(int slot,D3D12_RESOURCE_STATES to) {
    ++transitions; s_d3d.gpu_mirror[slot].st=to;
}
static void d3d12_owned_copy_texture(const D3D12_TEXTURE_COPY_LOCATION* d,UINT x,UINT y,UINT z,
                                    const D3D12_TEXTURE_COPY_LOCATION* s,const D3D12_BOX* box) {
    CHECK(!world && x==0 && y==0 && z==0 && box);
    CHECK(d->Type==D3D12_TEXTURE_COPY_TYPE_PLACED_FOOTPRINT &&
          s->Type==D3D12_TEXTURE_COPY_TYPE_SUBRESOURCE_INDEX &&
          d->PlacedFootprint.Footprint.RowPitch==256);
    CHECK(s_d3d.off_rt[0].st==D3D12_RESOURCE_STATE_COPY_SOURCE ||
          s_d3d.display_surface[0].st==D3D12_RESOURCE_STATE_COPY_SOURCE);
    // Model the owned copy wrapper rejecting resource capture before recording.
    if(fail_copy) { s_d3d.gcm2d_backend_halted=1; return; }
    FakeResource* src=(FakeResource*)s->pResource; FakeResource* dst=(FakeResource*)d->pResource;
    u32 w=box->right-box->left,h=box->bottom-box->top;
    for(u32 row=0;row<h;++row)
        memcpy(dst->data+row*d->PlacedFootprint.Footprint.RowPitch,
               src->data+((box->top+row)*src->desc.Width+box->left)*4,w*4);
    ++copies; pins+=2;
}
static void exact_writeback_release(void) {
    for(u32 i=0;i<s_exact_writeback_count;++i) {
        if(s_exact_writeback[i].readback_mapped && s_exact_writeback[i].readback) {
            D3D12_RANGE wr={0,0};
            s_exact_writeback[i].readback->lpVtbl->Unmap(s_exact_writeback[i].readback,0,&wr);
            s_exact_writeback[i].readback_mapped=0;
        }
        s_exact_writeback[i].mapped_rgba=NULL;
        free(s_exact_writeback[i].packed_argb); free(s_exact_writeback[i].baseline);
    }
    memset(s_exact_writeback,0,sizeof s_exact_writeback); s_exact_writeback_count=0;
}
static int exact_writeback_readback_alloc(ExactBlitWritebackPlan* p,const char** code,HRESULT* hr) {
    (void)code;(void)hr; if(fail_alloc) return -1;
    p->mapped_commit=tj_main_mapped_commit_enabled();
    p->packed_bytes=(u64)p->w*p->h*4;
    if(!p->mapped_commit) p->packed_argb=malloc((size_t)p->packed_bytes);
    p->readback_pitch=(p->w*4+255)&~255u; p->readback=&readback.iface;
    memset(readback.data,0xCC,sizeof readback.data);
    memset(readback2.data,0xCC,sizeof readback2.data);
    return p->mapped_commit || p->packed_argb ? 0:-1;
}
static void exact_writeback_preflight_reject(const char* code,u32 rec,const RsxD3D12ExactBlit* b,
    int slot,u64 x,u64 y,u64 nx,u64 ny,HRESULT hr) {
    (void)code;(void)rec;(void)b;(void)slot;(void)x;(void)y;(void)nx;(void)ny;(void)hr; ++rejects;
}
FUNCTIONS
static void setup(void) {
    exact_writeback_release();
    for(u32 d=0;d<MAX_DRAWS;++d) {
        free(s_d3d.draws[d].scaled_blit.dst_snapshot);
        free(s_d3d.draws[d].scaled_blit.dst_post_snapshot);
    }
    memset(&s_d3d,0,sizeof s_d3d); memset(vm_base,0x55,ppu_vm_size);
    memset(&source,0,sizeof source); source.iface.lpVtbl=&vtable;
    readback.iface.lpVtbl=&vtable; readback2.iface.lpVtbl=&vtable;
    source.desc.Dimension=D3D12_RESOURCE_DIMENSION_TEXTURE2D;
    source.desc.Width=9; source.desc.Height=8; source.desc.DepthOrArraySize=1;
    source.desc.MipLevels=1; source.desc.SampleDesc.Count=1;
    source.desc.Format=DXGI_FORMAT_R8G8B8A8_UNORM;
    for(u32 i=0;i<9*8*4;++i) source.data[i]=(u8)(i*7+3);
    s_d3d.off_rt[0].res=&source.iface; s_d3d.off_rt[0].st=D3D12_RESOURCE_STATE_RENDER_TARGET;
    s_d3d.display_surface[0].res=&source.iface;
    s_d3d.display_surface[0].st=D3D12_RESOURCE_STATE_PIXEL_SHADER_RESOURCE;
    s_d3d.draw_count=1; generation=2; resolve_ok=1; fail_alloc=fail_map=fail_copy=world=poison=compose=0;
    copies=pins=bumps=transitions=maps=unmaps=active_maps=rejects=unmap_without_view=0;
    fail_map_on=fail_map_success_null=fail_map_error_data=0;
    s_tj_completion_reason=RSX_COMPLETION_LOCAL_MAIN_COPY;
    D3D12DrawRecord* dr=&s_d3d.draws[0];
    dr->is_scaled_blit=dr->is_vp=dr->scaled_blit_prepared=dr->scaled_blit_direct_readback=1;
    dr->scaled_blit_src_authoritative=1; dr->scaled_blit_src_kind=GPU_BLIT_OFFRT;
    dr->scaled_blit_src_x=2; dr->scaled_blit_src_y=1;
    dr->scaled_blit_dst_kind=GPU_BLIT_MIRROR; dr->scaled_blit_dst_slot=-1;
    RsxD3D12ExactBlit* b=&dr->scaled_blit;
    b->src_local=1; b->src_raw=0x10; b->src_ea=0x810;
    b->dst_ea=0x100; b->mapping_generation=2; b->dst_pitch=32;
    b->src_pitch=36; b->src_w=9;b->src_h=8;b->dst_w=6;b->dst_h=8;
    b->src_x=1;b->src_y=1;b->dst_x=1;b->dst_y=2;b->copy_w=3;b->copy_h=2;
    b->dst_snapshot_bytes=256;b->dst_snapshot=malloc(256);
    b->dst_post_snapshot_bytes=24;b->dst_post_snapshot=malloc(24);
    memset(b->dst_snapshot,0x55,256); memset(b->dst_post_snapshot,0x55,24);
}
static int admit(void) {
    D3D12DrawRecord* dr=&s_d3d.draws[0];
    GpuBlitEndpoint src={dr->scaled_blit_src_kind,dr->scaled_blit_src_slot,
                        dr->scaled_blit_src_x,dr->scaled_blit_src_y};
    return gpu_blit_direct_readback_admit(0,dr,&src,1);
}
int main(void) {
    vm_base=malloc(ppu_vm_size); CHECK(vm_base!=NULL); setup();
    const char* env=getenv("TJ_MAIN_DIRECT_READBACK"); int enabled=env && !strcmp(env,"1");
    const char* mapped_env=getenv("TJ_MAIN_MAPPED_COMMIT");
    int mapped_enabled=mapped_env && !strcmp(mapped_env,"1");
    CHECK(tj_main_direct_readback_enabled()==enabled); CHECK(admit()==enabled);
    CHECK(tj_main_mapped_commit_enabled()==mapped_enabled);
    s_tj_completion_reason=RSX_COMPLETION_INPUT;
    CHECK(rsx_d3d12_backend_complete_fifo_prefix_at(RSX_COMPLETION_LOCAL_MAIN_COPY)==0);
    CHECK(observed_reason==(enabled?RSX_COMPLETION_LOCAL_MAIN_COPY:RSX_COMPLETION_INPUT));
    CHECK(s_tj_completion_reason==RSX_COMPLETION_INPUT);
    completion_result=-1;
    CHECK(rsx_d3d12_backend_complete_fifo_prefix_at((RsxD3D12CompletionReason)999)==-1);
    CHECK(observed_reason==(enabled?RSX_COMPLETION_OTHER:RSX_COMPLETION_INPUT));
    CHECK(s_tj_completion_reason==RSX_COMPLETION_INPUT);
    completion_result=0;s_tj_completion_reason=RSX_COMPLETION_LOCAL_MAIN_COPY;
    D3D12DrawRecord* dr=&s_d3d.draws[0]; RsxD3D12ExactBlit* b=&dr->scaled_blit;
    b->src_cpu_owned=1;CHECK(!admit());b->src_cpu_owned=0;
    b->src_local=0;CHECK(!admit());b->src_local=1;
    dr->scaled_blit_src_authoritative=0;CHECK(!admit());dr->scaled_blit_src_authoritative=1;
    s_d3d.draw_count=2;CHECK(!admit());s_d3d.draw_count=1;
    s_tj_completion_reason=RSX_COMPLETION_TEXTURE_RELEASE;CHECK(!admit());s_tj_completion_reason=RSX_COMPLETION_LOCAL_MAIN_COPY;
    source.desc.Format=DXGI_FORMAT_B8G8R8A8_UNORM;CHECK(!admit());source.desc.Format=DXGI_FORMAT_R8G8B8A8_UNORM;
    source.desc.SampleDesc.Count=2;CHECK(!admit());source.desc.SampleDesc.Count=1;
    dr->scaled_blit_src_x=UINT32_MAX;CHECK(!admit());dr->scaled_blit_src_x=2;
    dr->scaled_blit_src_slot=-1;CHECK(!admit());dr->scaled_blit_src_slot=0;
    dr->scaled_blit_src_kind=GPU_BLIT_MIRROR;CHECK(!admit());dr->scaled_blit_src_kind=GPU_BLIT_OFFRT;
    compose=1;CHECK(!admit());compose=0;
    generation=4;CHECK(!admit());generation=2;
    resolve_ok=0;CHECK(!admit());resolve_ok=1;
    s_d3d.gpu_mirror[0]=(GpuMirror){.valid=1,.canonical_ea=0x100,.canonical_span=256,.mapping_generation=2};
    CHECK(!admit());memset(s_d3d.gpu_mirror,0,sizeof s_d3d.gpu_mirror);
    --b->dst_snapshot_bytes;CHECK(!admit());++b->dst_snapshot_bytes;
    b->dst_pitch=31;CHECK(!admit());b->dst_pitch=32;
    // Endpoint slots are captured by index; revalidate their live resource
    // after the callback's owner drain, before accepting direct readback.
    setup();
    GpuBlitEndpoint captured={dr->scaled_blit_src_kind,dr->scaled_blit_src_slot,
                              dr->scaled_blit_src_x,dr->scaled_blit_src_y};
    CHECK(gpu_blit_endpoint_resource(&captured)==&source.iface);
    s_d3d.off_rt[0].res=NULL;
    CHECK(!gpu_blit_direct_source_valid(&dr->scaled_blit,&captured));
    if(enabled) CHECK(!gpu_blit_direct_readback_admit(0,dr,&captured,1));
    CHECK(!s_exact_writeback_count && !copies && !maps && !transitions);
    setup();dr=&s_d3d.draws[0];b=&dr->scaled_blit;
    CHECK(gpu_blit_record_mapping_valid(b)); CHECK(exact_writeback_preflight()==0 && s_exact_writeback_count==1);
    ExactBlitWritebackPlan* p=&s_exact_writeback[0];
    CHECK(p->trace_source_flags==0);
    CHECK(p->mapped_commit==mapped_enabled && ((p->packed_argb==NULL)==mapped_enabled));
    CHECK(p->direct_readback && p->mirror_slot==UINT32_MAX && p->guest_ea==0x144 && p->canonical_span==256);
    CHECK(p->baseline && !b->dst_post_snapshot && b->dst_post_snapshot_bytes==0);
    CHECK(!copies && !maps && !p->queued);
    u8 before[4096];memcpy(before,vm_base,sizeof before);
    CHECK(exact_writeback_queue(0)==0 && p->queued && dr->scaled_blit_executed);
    CHECK(copies==1 && pins==2 && transitions==2 && maps==0 &&
          s_d3d.off_rt[0].st==D3D12_RESOURCE_STATE_RENDER_TARGET && !memcmp(before,vm_base,sizeof before));
    ExactWritebackFailure failure={0};
    CHECK(exact_writeback_map_convert_all(&failure)==0 && !failure.code);
    CHECK(maps==1 && active_maps==(unsigned)mapped_enabled && unmaps==(unsigned)!mapped_enabled);
    CHECK((p->mapped_rgba!=NULL)==mapped_enabled);
    CHECK(exact_writeback_commit_all(&failure)==0 && !failure.code && !world && bumps);
    CHECK(active_maps==(unsigned)mapped_enabled && ((p->mapped_rgba!=NULL)==mapped_enabled) &&
          (p->readback_mapped==mapped_enabled));
    for(u32 i=0;i<4096;++i) {
        int row=(i>=0x144 && i<0x150)?0:(i>=0x164 && i<0x170)?1:-1;
        if(row<0) CHECK(vm_base[i]==before[i]);
        else { u32 c=i-(row?0x164:0x144);u32 rgba=(u32[]){3,0,1,2}[c%4];
            CHECK(vm_base[i]==source.data[((2+row)*9+3+c/4)*4+rgba]); }
    }
    exact_writeback_release();
    CHECK(!active_maps && unmaps==1 && !s_exact_writeback_count);
    setup(); fail_alloc=1; u8* baseline=b->dst_post_snapshot;
    CHECK(exact_writeback_preflight()<0 && b->dst_post_snapshot==baseline && !copies && !maps);
    setup(); CHECK(exact_writeback_preflight()==0); CHECK(exact_writeback_queue(0)==0);
    s_exact_writeback[0].readback_mapped=1; failure=(ExactWritebackFailure){0};
    CHECK(exact_writeback_map_convert_all(&failure)<0 && !strcmp(failure.code,"MAP_STORAGE") &&
          !maps && !unmaps);
    s_exact_writeback[0].readback_mapped=0;
    setup(); CHECK(exact_writeback_preflight()==0); CHECK(exact_writeback_queue(0)==0);
    fail_map=1; failure=(ExactWritebackFailure){0};memcpy(before,vm_base,sizeof before);
    CHECK(exact_writeback_map_convert_all(&failure)<0 && !strcmp(failure.code,"MAP_RESULT") &&
          !memcmp(before,vm_base,sizeof before));
    setup(); CHECK(exact_writeback_preflight()==0); CHECK(exact_writeback_queue(0)==0);
    fail_map_success_null=1; failure=(ExactWritebackFailure){0};
    CHECK(exact_writeback_map_convert_all(&failure)<0 && !strcmp(failure.code,"MAP_RESULT") &&
          maps==1 && !unmaps && !active_maps && !unmap_without_view &&
          s_exact_writeback[0].readback_mapped);
    exact_writeback_release();
    CHECK(unmaps==1 && !active_maps && unmap_without_view==1 &&
          !s_exact_writeback_count);
    setup(); CHECK(exact_writeback_preflight()==0); CHECK(exact_writeback_queue(0)==0);
    fail_map=1; fail_map_error_data=1; failure=(ExactWritebackFailure){0};
    CHECK(exact_writeback_map_convert_all(&failure)<0 && !strcmp(failure.code,"MAP_RESULT"));
    CHECK(maps==1 && !unmaps && !active_maps && !unmap_without_view);
    CHECK(!s_exact_writeback[0].readback_mapped);
    setup(); CHECK(exact_writeback_preflight()==0); CHECK(exact_writeback_queue(0)==0);
    p=&s_exact_writeback[0]; p->mapped_commit=1; p->readback_pitch=16384; p->h=8192;
    s_exact_writeback[1]=*p; s_exact_writeback[1].record=1;
    s_exact_writeback[1].baseline=NULL; s_exact_writeback[1].packed_argb=NULL;
    s_exact_writeback[1].mapped_rgba=NULL; s_exact_writeback[1].readback=&readback2.iface;
    s_exact_writeback[1].readback_pitch=16384; s_exact_writeback[1].h=8193;
    s_exact_writeback_count=2; failure=(ExactWritebackFailure){0};
    CHECK(exact_writeback_map_convert_all(&failure)<0 && !strcmp(failure.code,"MAP_LIMIT") &&
          !maps && !unmaps && !active_maps);
    setup(); CHECK(exact_writeback_preflight()==0); CHECK(exact_writeback_queue(0)==0);
    p=&s_exact_writeback[0];
    s_exact_writeback[1]=*p;
    s_exact_writeback[1].record=1;
    s_exact_writeback[1].baseline=malloc((size_t)p->packed_bytes);
    CHECK(s_exact_writeback[1].baseline!=NULL);
    memcpy(s_exact_writeback[1].baseline,p->baseline,(size_t)p->packed_bytes);
    s_exact_writeback[1].packed_argb=mapped_enabled?NULL:malloc((size_t)p->packed_bytes);
    CHECK(mapped_enabled || s_exact_writeback[1].packed_argb!=NULL);
    s_exact_writeback[1].readback=&readback2.iface;
    s_exact_writeback[1].mapped_rgba=NULL;
    s_exact_writeback_count=2; fail_map_on=2; failure=(ExactWritebackFailure){0};
    memcpy(before,vm_base,sizeof before);
    CHECK(exact_writeback_map_convert_all(&failure)<0 && !strcmp(failure.code,"MAP_RESULT") &&
          maps==2 && !memcmp(before,vm_base,sizeof before));
    CHECK(active_maps==(unsigned)mapped_enabled && unmaps==(unsigned)!mapped_enabled);
    exact_writeback_release();
    CHECK(!active_maps && unmaps==1);
    setup(); CHECK(exact_writeback_preflight()==0);CHECK(exact_writeback_queue(0)==0);
    failure=(ExactWritebackFailure){0};CHECK(exact_writeback_map_convert_all(&failure)==0);
    generation=4;memcpy(before,vm_base,sizeof before);
    CHECK(exact_writeback_commit_all(&failure)<0 && !strcmp(failure.code,"GEN") &&
          !memcmp(before,vm_base,sizeof before) && !bumps);
    setup(); CHECK(exact_writeback_preflight()==0);CHECK(exact_writeback_queue(0)==0);
    failure=(ExactWritebackFailure){0};CHECK(exact_writeback_map_convert_all(&failure)==0);
    vm_base[0x144]^=1;memcpy(before,vm_base,sizeof before);
    CHECK(exact_writeback_commit_all(&failure)<0 && !strcmp(failure.code,"BASELINE") &&
          !memcmp(before,vm_base,sizeof before) && !bumps);
    // Replay must reject a source lost or invalidated after successful planning.
    setup(); CHECK(exact_writeback_preflight()==0);memcpy(before,vm_base,sizeof before);
    s_d3d.off_rt[0].res=NULL;
    CHECK(exact_writeback_queue(0)<0);
    CHECK(!s_exact_writeback[0].queued && !dr->scaled_blit_executed &&
          !copies && !maps && !transitions && !memcmp(before,vm_base,sizeof before));
    setup(); CHECK(exact_writeback_preflight()==0);memcpy(before,vm_base,sizeof before);
    source.desc.Width=4;
    CHECK(exact_writeback_queue(0)<0);
    CHECK(!s_exact_writeback[0].queued && !dr->scaled_blit_executed &&
          !copies && !maps && !transitions && !memcmp(before,vm_base,sizeof before));
    setup(); CHECK(exact_writeback_preflight()==0);memcpy(before,vm_base,sizeof before);
    fail_copy=1;
    CHECK(exact_writeback_queue(0)<0);
    CHECK(s_d3d.gcm2d_backend_halted && !s_exact_writeback[0].queued &&
          !dr->scaled_blit_executed && !copies && !maps && !pins &&
          s_d3d.off_rt[0].st==D3D12_RESOURCE_STATE_RENDER_TARGET &&
          !memcmp(before,vm_base,sizeof before));
    setup();s_d3d.draws[1]=s_d3d.draws[0];s_d3d.draws[1].scaled_blit.dst_snapshot=NULL;
    s_d3d.draws[1].scaled_blit.dst_post_snapshot=NULL;s_d3d.draw_count=2;
    CHECK(exact_writeback_preflight()<0 && !copies && !maps);
    setup();dr->scaled_blit_src_kind=GPU_BLIT_DISPLAY;CHECK(admit()==enabled);
    CHECK(exact_writeback_preflight()==0 && exact_writeback_queue(0)==0 &&
          s_d3d.display_surface[0].st==D3D12_RESOURCE_STATE_PIXEL_SHADER_RESOURCE);
    setup();s_d3d.draws[1]=s_d3d.draws[0];s_d3d.draws[1].scaled_blit.dst_snapshot=NULL;
    s_d3d.draws[1].scaled_blit.dst_post_snapshot=malloc(24);s_d3d.draws[0].is_scaled_blit=0;
    s_d3d.draws[1].scaled_blit.dst_snapshot=malloc(256);
    s_d3d.draw_count=2;GpuBlitEndpoint ep={GPU_BLIT_OFFRT,0,2,1};
    CHECK(!gpu_blit_direct_readback_admit(1,&s_d3d.draws[1],&ep,1));
    s_d3d.draws[0].rt_local=1;
    CHECK(gpu_blit_direct_readback_admit(1,&s_d3d.draws[1],&ep,1)==enabled);
    CHECK(!gpu_blit_direct_readback_admit(1,&s_d3d.draws[1],&ep,0));
    setup();
    // Exercise both observed title export shapes without allocating game data.
    ppu_vm_size=0;
    source.desc.Width=1280;source.desc.Height=720;
    dr->scaled_blit_src_x=dr->scaled_blit_src_y=0;
    b->src_x=b->src_y=b->dst_x=b->dst_y=0;
    b->src_w=b->dst_w=1280;b->src_h=b->dst_h=b->copy_h=720;
    b->src_pitch=b->dst_pitch=5120;b->dst_snapshot_bytes=5120u*720;
    b->copy_w=1024;b->dst_post_snapshot_bytes=1024u*720*4;
    CHECK(admit()==enabled);
    b->copy_w=256;b->dst_post_snapshot_bytes=256u*720*4;
    CHECK(admit()==enabled);
    CHECK(admit()==enabled);ppu_vm_size=4096;
    // The larger shape must exceed this fixture's 4 KiB VM and fall back.
    CHECK(!admit());
    setup();exact_writeback_release();free(b->dst_snapshot);free(b->dst_post_snapshot);free(vm_base);
    printf("MAIN direct readback: %u checks, %u failures, flag=%d\n",checks,failures,enabled);
    return failures?1:0;
}
'''
    for key, value in {"MIRROR": struct(backend, "GpuMirror"),
                       "PLAN": plan,
                       "FAILURE": struct(backend, "ExactWritebackFailure"),
                       "FUNCTIONS": functions}.items():
        fixture = fixture.replace(key + "\n", value + "\n")
    compiler = shutil.which("clang") or r"C:\Program Files\LLVM\bin\clang.exe"
    with tempfile.TemporaryDirectory(prefix="tj-direct-readback-") as temp:
        source, exe = Path(temp) / "direct.c", Path(temp) / "direct.exe"
        source.write_text(fixture, encoding="utf-8")
        subprocess.run([compiler, "-std=c11", "-O2", "-D_CRT_SECURE_NO_WARNINGS",
                        "-I", str(VIDEO), "-I", str(ROOT.parent / "ps3recomp/include"),
                        str(source), "-o", str(exe)], check=True)
        for flag in (None, "0", "1", "true", "01"):
            env = os.environ.copy()
            env.pop("TJ_MAIN_DIRECT_READBACK", None)
            env.pop("TJ_MAIN_MAPPED_COMMIT", None)
            if flag is not None:
                env["TJ_MAIN_DIRECT_READBACK"] = flag
            subprocess.run([str(exe)], env=env, check=True)
        env = os.environ.copy()
        env["TJ_MAIN_DIRECT_READBACK"] = "1"
        env["TJ_MAIN_MAPPED_COMMIT"] = "1"
        subprocess.run([str(exe)], env=env, check=True)


if __name__ == "__main__":
    main()
