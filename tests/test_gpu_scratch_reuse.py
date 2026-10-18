"""Exercise production scratch reuse, uploads, barriers, and failure ownership."""
from pathlib import Path
import os
import shutil
import subprocess
import tempfile

from test_fifo_present_boundary import function

ROOT = Path(__file__).resolve().parents[1]


def typedef(source, name):
    end = source.index("} " + name + ";") + len(name) + 3
    return source[source.rfind("typedef struct {", 0, end):end]


def main():
    backend = (ROOT.parent / "ps3recomp/libs/video/rsx_d3d12_backend.c").read_text(encoding="utf-8")
    # Keep the proven fence and abandonment boundaries, not just cache mechanics.
    assert "if (fence_ok && wb_ok) gpu_mirror_release_prefix_exports();" in backend
    assert "else exact_writeback_abandon_after_submit();" in backend
    abandon = function(backend, "static void exact_writeback_abandon_after_submit(")
    assert "gpu_scratch_retain" not in abandon
    preflight = function(backend, "static int exact_writeback_preflight(")
    transfer = function(backend, "static void exact_writeback_transfer_baseline(")
    cleanup_start = backend.index("static void gpu_blit_record_snapshots_release_range(", 500000)
    cleanup = function(backend[cleanup_start:], "static void gpu_blit_record_snapshots_release_range(")
    readback_alloc = function(backend, "static int exact_writeback_readback_alloc(")
    mapped_getter = function(backend, "static int tj_main_mapped_commit_enabled(")
    assert 'getenv("TJ_MAIN_MAPPED_COMMIT")' in mapped_getter
    assert 'strcmp(value, "1") == 0' in mapped_getter
    assert readback_alloc.index("p->mapped_commit = tj_main_mapped_commit_enabled()") < \
        readback_alloc.index("if (!p->mapped_commit)")
    assert "p->baseline = (u8*)malloc" not in readback_alloc
    assert "p->baseline = b->dst_post_snapshot;" in transfer
    assert "b->dst_post_snapshot = NULL;" in transfer
    assert "b->dst_post_snapshot_bytes = 0;" in transfer
    assert preflight.index("exact_writeback_readback_alloc") < preflight.index(
        "exact_writeback_transfer_baseline(p, b);")
    assert "rsx_d3d12_blit_snapshot_release(dr->scaled_blit.dst_post_snapshot," in cleanup
    render = function(backend, "static void render_frame(")
    assert render.index("if (!s_d3d.prefix_prepare_owner && wait_for_gpu_checked() != 0)") < \
        render.index("if (!s_d3d.prefix_prepare_owner) s_d3d.fifo_prefix_outstanding = 0;") < \
        render.index("gpu_mirror_reset_batch();")
    scratch = backend[backend.index("#define GPU_SCRATCH_SLOTS"):backend.index("/* One-frame only readback plans.")]
    fixture = r'''
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <d3d12.h>
#include <stdint.h>
#include "rsx_argb_upload.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
typedef uint8_t u8; typedef uint32_t u32; typedef uint64_t u64;
typedef struct { u8* dst_post_snapshot; u64 dst_post_snapshot_bytes; } RsxD3D12ExactBlit;
#define MAX_GPU_MIRRORS 16
#define MAX_DRAWS 4
static const GUID test_resource_iid={0};
#define IID_ID3D12Resource test_resource_iid
MIRROR_TYPE
PLAN_TYPE
SCRATCH
static struct {
    ID3D12Device* device; ID3D12GraphicsCommandList* cmd_list;
    GpuMirror gpu_mirror[MAX_GPU_MIRRORS];
    const char* gcm2d_endpoint_reason; u32 gcm2d_mirror_peak;
} s_d3d;
/* The extracted mirror function keeps upload attribution inert here; the
 * focused prefix-cost fixture compiles and exercises the production helpers. */
enum { TJ_MIRROR_UPLOAD_OWNER_GUARD, TJ_MIRROR_UPLOAD_MAP,
       TJ_MIRROR_UPLOAD_CONVERT, TJ_MIRROR_UPLOAD_UNMAP,
       TJ_MIRROR_UPLOAD_TRANSITION_COPY };
typedef struct { int inactive; } TJMirrorUploadCostSample;
static void tj_prefix_mirror_upload_cost_begin(TJMirrorUploadCostSample* sample) { (void)sample; }
static void tj_prefix_mirror_upload_cost_phase(TJMirrorUploadCostSample* sample, unsigned phase) { (void)sample;(void)phase; }
static void tj_prefix_mirror_upload_cost_skip(TJMirrorUploadCostSample* sample, unsigned phase) { (void)sample;(void)phase; }
static void tj_prefix_mirror_upload_cost_abort(TJMirrorUploadCostSample* sample, int map_failure) { (void)sample;(void)map_failure; }
static void tj_prefix_mirror_upload_cost_finish(TJMirrorUploadCostSample* sample, u32 physical_w, u32 overwrite_w, u32 height) { (void)sample;(void)physical_w;(void)overwrite_w;(void)height; }
/* This fixture checks the original ownership-OFF allocation/upload path.
 * test_prefix_gpu_objects exercises the actual ownership wrappers and guards. */
static int d3d12_prefix_owner_object_reserved(const void* p) { (void)p; return 0; }
static int d3d12_prefix_owner_object_mutable(const void* p) { (void)p; return 0; }
#define d3d12_owned_barriers(n,b) s_d3d.cmd_list->lpVtbl->ResourceBarrier(s_d3d.cmd_list,n,b)
#define d3d12_owned_copy_texture(d,x,y,z,s,b) s_d3d.cmd_list->lpVtbl->CopyTextureRegion(s_d3d.cmd_list,d,x,y,z,s,b)
static ExactBlitWritebackPlan s_exact_writeback[MAX_DRAWS];
static u32 s_exact_writeback_count;
static unsigned tracked_frees;
static void tracked_free(void* p) { if (p) ++tracked_frees; free(p); }
static void rsx_d3d12_blit_snapshot_release(u8* p,u64 bytes) { (void)bytes; tracked_free(p); }
static struct {
    u64 mirror_create_ticks,mirror_upload_ticks,readback_host_alloc_ticks,readback_d3d_alloc_ticks;
} *s_tj_gcm2d_backend_perf_sample;
static LONGLONG tj_gcm2d_backend_perf_subphase_begin(void) { return 0; }
static void tj_gcm2d_backend_perf_subphase_end(u64* ticks,LONGLONG start) { (void)ticks;(void)start; }
static u64 generation=2;
static int gpu_main_canonical_identity(u32 raw,u32 pitch,u32 w,u32 h,u32* ea,u64* span,u64* gen) {
    (void)w; *ea=raw+0x1000; *span=(u64)pitch*h; *gen=generation; return 0;
}
static int gpu_mirror_canonical_overlap(u32 ea,u64 span,u64 gen,u32 before) {
    (void)ea;(void)span;(void)gen;(void)before; return 0;
}
typedef struct {
    ID3D12Resource iface; D3D12_RESOURCE_DESC desc;
    D3D12_RESOURCE_STATES state; u8* data; u64 size;
} FakeResource;
static unsigned creates,releases,barriers,copies,checks,queries,maps,unmaps;
static int fail_map,fail_create,fail_query;
#define CHECK(c) do { ++checks; if(!(c)) { fprintf(stderr,"FAIL line %d: %s\n",__LINE__,#c); exit(1); } } while(0)
static ULONG STDMETHODCALLTYPE fake_release(ID3D12Resource* resource) {
    FakeResource* r=(FakeResource*)resource; ++releases; free(r->data); free(r); return 0;
}
static HRESULT STDMETHODCALLTYPE fake_map(ID3D12Resource* resource,UINT sub,const D3D12_RANGE* range,void** data) {
    (void)sub;(void)range; ++maps; *data=NULL; if(fail_map) return E_FAIL;
    *data=((FakeResource*)resource)->data; return S_OK;
}
static void STDMETHODCALLTYPE fake_unmap(ID3D12Resource* resource,UINT sub,const D3D12_RANGE* range) {
    (void)resource;(void)sub;(void)range; ++unmaps;
}
static ID3D12ResourceVtbl resource_vtable={.Release=fake_release,.Map=fake_map,.Unmap=fake_unmap};
static u64 descriptor_bytes(const D3D12_RESOURCE_DESC* desc) {
    return desc->Dimension==D3D12_RESOURCE_DIMENSION_BUFFER ? desc->Width : desc->Width*desc->Height*4;
}
static HRESULT STDMETHODCALLTYPE fake_create(ID3D12Device* device,const D3D12_HEAP_PROPERTIES* heap,
    D3D12_HEAP_FLAGS flags,const D3D12_RESOURCE_DESC* desc,D3D12_RESOURCE_STATES state,
    const D3D12_CLEAR_VALUE* clear,REFIID iid,void** output) {
    (void)device;(void)heap;(void)flags;(void)clear;(void)iid;
    *output=NULL; if(fail_create) return E_OUTOFMEMORY;
    FakeResource* r=calloc(1,sizeof(*r)); CHECK(r!=NULL);
    r->iface.lpVtbl=&resource_vtable; r->desc=*desc; r->state=state;
    r->size=descriptor_bytes(desc); r->data=malloc((size_t)r->size); CHECK(r->data!=NULL);
    memset(r->data,0xcc,(size_t)r->size); *output=r; ++creates; return S_OK;
}
static D3D12_RESOURCE_ALLOCATION_INFO* STDMETHODCALLTYPE fake_allocation(ID3D12Device* device,
    D3D12_RESOURCE_ALLOCATION_INFO* out,UINT mask,UINT count,const D3D12_RESOURCE_DESC* desc) {
    (void)device;(void)mask; CHECK(count==1); ++queries;
    out->SizeInBytes=fail_query ? UINT64_MAX : ((descriptor_bytes(desc)+65535)&~65535ULL);
    out->Alignment=65536; return out;
}
static void STDMETHODCALLTYPE fake_barrier(ID3D12GraphicsCommandList* list,UINT count,const D3D12_RESOURCE_BARRIER* barriers_in) {
    (void)list;
    for(UINT i=0;i<count;++i) {
        const D3D12_RESOURCE_BARRIER* b=&barriers_in[i];
        FakeResource* r=(FakeResource*)b->Transition.pResource;
        CHECK(b->Type==D3D12_RESOURCE_BARRIER_TYPE_TRANSITION && r->state==b->Transition.StateBefore);
        r->state=b->Transition.StateAfter; ++barriers;
    }
}
static void STDMETHODCALLTYPE fake_copy(ID3D12GraphicsCommandList* list,const D3D12_TEXTURE_COPY_LOCATION* dst,
    UINT x,UINT y,UINT z,const D3D12_TEXTURE_COPY_LOCATION* src,const D3D12_BOX* box) {
    (void)list; CHECK(y==0 && z==0);
    FakeResource* d=(FakeResource*)dst->pResource; FakeResource* s=(FakeResource*)src->pResource;
    CHECK(d->state==D3D12_RESOURCE_STATE_COPY_DEST && s->state==D3D12_RESOURCE_STATE_GENERIC_READ);
    u32 left=box?box->left:0, right=box?box->right:(u32)d->desc.Width;
    CHECK(x==left && right<=d->desc.Width && (!box || (!box->top && box->bottom==d->desc.Height && box->back==1)));
    for(u32 row=0;row<d->desc.Height;++row)
        memcpy(d->data+(row*d->desc.Width+x)*4,
            s->data+row*src->PlacedFootprint.Footprint.RowPitch+left*4,(size_t)(right-left)*4);
    ++copies;
}
static ID3D12DeviceVtbl device_vtable={.CreateCommittedResource=fake_create,.GetResourceAllocationInfo=fake_allocation};
static ID3D12GraphicsCommandListVtbl list_vtable={.ResourceBarrier=fake_barrier,.CopyTextureRegion=fake_copy};
static ID3D12Device device={&device_vtable};
static ID3D12GraphicsCommandList list={&list_vtable};
FUNCTIONS
static void verify_pixels(int slot,const u8* source,u32 pixels) {
    FakeResource* r=(FakeResource*)s_d3d.gpu_mirror[slot].res;
    for(u32 i=0;i<pixels;++i) {
        CHECK(r->data[i*4]==source[i*4+1] && r->data[i*4+1]==source[i*4+2] &&
              r->data[i*4+2]==source[i*4+3] && r->data[i*4+3]==source[i*4]);
    }
}
static void verify_partial_upload(int slot,const u8* source,u32 logical_w,
                                  u32 physical_w,u32 h,u32 overwritten) {
    FakeResource* r=(FakeResource*)s_d3d.gpu_mirror[slot].res;
    FakeResource* upload=(FakeResource*)s_d3d.gpu_mirror[slot].up;
    int left_upload_untouched=1,left_copy_absent=1,right_tail_exact=1,padding_exact=1;
    for(u32 y=0;y<h;++y) {
        u64 row=(u64)y*physical_w*4;
        for(u32 x=0;x<overwritten;++x) {
            for(u32 c=0;c<4;++c) {
                if(upload->data[row+x*4+c]!=0xcc) left_upload_untouched=0;
                if(r->data[row+x*4+c]!=0xcc) left_copy_absent=0;
            }
        }
        for(u32 x=overwritten;x<physical_w;++x) {
            u64 offset=row+x*4;
            for(u32 c=0;c<4;++c) {
                u8 expected=source[offset+((c+1u)&3u)];
                if(upload->data[offset+c]!=expected || r->data[offset+c]!=expected)
                    right_tail_exact=0;
                if(x>=logical_w &&
                   (upload->data[offset+c]!=expected || r->data[offset+c]!=expected))
                    padding_exact=0;
            }
        }
    }
    CHECK(upload->size==(u64)physical_w*4*h);
    CHECK(left_upload_untouched);
    CHECK(left_copy_absent);
    CHECK(right_tail_exact); /* Includes every logical-right and physical-pitch byte. */
    CHECK(padding_exact); /* Explicitly checks x=[logical_w, physical_w). */
}
static void readback_plan(u32 w,u32 h) {
    memset(s_exact_writeback,0,sizeof(s_exact_writeback)); s_exact_writeback_count=1;
    s_exact_writeback[0].w=w; s_exact_writeback[0].h=h; s_exact_writeback[0].dxgi=DXGI_FORMAT_R8G8B8A8_UNORM;
}
static GpuScratchAllocation small_allocation(u32 width,u64 charge) {
    D3D12_RESOURCE_DESC desc={0}; desc.Dimension=D3D12_RESOURCE_DIMENSION_BUFFER; desc.Width=width; desc.Height=1;
    GpuScratchAllocation a={0}; a.kind=GPU_SCRATCH_READBACK; a.w=width; a.h=1; a.bytes=charge;
    a.st=D3D12_RESOURCE_STATE_COPY_DEST;
    CHECK(SUCCEEDED(fake_create(&device,NULL,0,&desc,a.st,NULL,&test_resource_iid,(void**)&a.res)));
    return a;
}
int main(void) {
    const char* mapped_env=getenv("TJ_MAIN_MAPPED_COMMIT");
    int mapped_expected=mapped_env && !strcmp(mapped_env,"1");
    CHECK(tj_main_mapped_commit_enabled()==mapped_expected);
    s_d3d.device=&device; s_d3d.cmd_list=&list;
    u8 snapshot[64]; for(u32 i=0;i<sizeof(snapshot);++i) snapshot[i]=(u8)i;
    int slot=gpu_mirror_get(0,0x100,16,4,2,snapshot,sizeof(snapshot),0,0);
    CHECK(slot==0 && creates==2 && copies==1 && barriers==1 && queries==2);
    verify_pixels(slot,snapshot,8);
    ID3D12Resource* original=s_d3d.gpu_mirror[slot].res;
    gpu_mirror_release_prefix_exports();
    GpuMirror zero; memset(&zero,0,sizeof(zero));
    CHECK(!memcmp(&zero,&s_d3d.gpu_mirror[slot],sizeof(zero)) && releases==0 && s_gpu_scratch_bytes==131072);
    generation=4; for(u32 i=0;i<sizeof(snapshot);++i) snapshot[i]+=23;
    slot=gpu_mirror_get(0,0x200,16,4,2,snapshot,sizeof(snapshot),0,0);
    CHECK(s_d3d.gpu_mirror[slot].res==original && creates==2 && copies==2 && barriers==3 && queries==2);
    CHECK(s_d3d.gpu_mirror[slot].canonical_ea==0x1200 && s_d3d.gpu_mirror[slot].mapping_generation==4);
    verify_pixels(slot,snapshot,8);
    int local=gpu_mirror_get(1,0x300,16,4,2,snapshot,sizeof(snapshot),0,0);
    gpu_mirror_release_prefix_exports();
    CHECK(s_d3d.gpu_mirror[local].valid && s_d3d.gpu_mirror[local].raw==0x300);
    gpu_mirror_reset_batch(); CHECK(!s_d3d.gpu_mirror[local].valid);
    slot=gpu_mirror_get(1,0x300,16,4,2,snapshot,sizeof(snapshot),1,0);
    CHECK(s_d3d.gpu_mirror[slot].immutable_source==1); gpu_mirror_release_prefix_exports();
    CHECK(!s_d3d.gpu_mirror[slot].valid);
    unsigned before=creates;
    slot=gpu_mirror_get(0,0x400,32,8,2,snapshot,sizeof(snapshot),0,0);
    CHECK(creates==before+2); verify_pixels(slot,snapshot,16); gpu_mirror_reset_batch();
    unsigned old_copies=copies, old_barriers=barriers, old_releases=releases;
    fail_map=1; CHECK(gpu_mirror_get(0,0x500,32,8,2,snapshot,sizeof(snapshot),0,0)<0); fail_map=0;
    CHECK(releases==old_releases+2 && copies==old_copies && barriers==old_barriers);
    gpu_mirror_reset_batch(); gpu_scratch_release_all(); CHECK(s_gpu_scratch_bytes==0);
    before=creates;
    for(unsigned pass=0;pass<200;++pass) {
        for(u32 i=0;i<32;++i) snapshot[i]=(u8)(i+pass);
        generation+=2; slot=gpu_mirror_get(0,0x800+pass*16,16,4,2,snapshot,32,0,0);
        CHECK(slot==0 && !s_d3d.gpu_mirror[slot].authoritative && !s_d3d.gpu_mirror[slot].first_write_record_plus_one);
        verify_pixels(slot,snapshot,8); gpu_mirror_release_prefix_exports();
    }
    CHECK(creates==before+2); gpu_scratch_release_all();
    // Fresh and reused allocations: only left columns may stay uninitialized
    // before the FIFO writer. Every right/padding column retains its snapshot.
    // Tight upload rows must leave the omitted columns and row padding intact.
    before=creates;
    for(unsigned pass=0;pass<200;++pass) {
        u32 overwritten=1+pass%4;
        for(u32 i=0;i<32;++i) snapshot[i]=(u8)(i+pass);
        unsigned old_maps=maps;old_copies=copies;
        fail_map=overwritten==4;
        slot=gpu_mirror_get(0,0x2000+pass*16,16,overwritten,2,snapshot,32,0,overwritten);
        fail_map=0;
        CHECK(slot==0 && !s_d3d.gpu_mirror[slot].authoritative && !s_d3d.gpu_mirror[slot].first_write_record_plus_one);
        CHECK(maps==old_maps+(overwritten<4) && copies==old_copies+(overwritten<4));
        FakeResource* r=(FakeResource*)s_d3d.gpu_mirror[slot].res;
        FakeResource* upload=(FakeResource*)s_d3d.gpu_mirror[slot].up;
        for(u32 y=0;y<2;++y) {
            for(u32 x=overwritten;x<4;++x) {
                u32 i=(y*4+x)*4;
                CHECK(r->data[i]==snapshot[i+1] && r->data[i+1]==snapshot[i+2] &&
                      r->data[i+2]==snapshot[i+3] && r->data[i+3]==snapshot[i]);
            }
            for(u32 i=0;i<overwritten*4;++i) CHECK(upload->data[y*256+i]==0xcc);
            for(u32 i=16;i<256;++i) CHECK(upload->data[y*256+i]==0xcc);
            // Emulate the later FIFO writer; the resulting whole physical row
            // must agree with an ordinary fully initialized mirror.
            for(u32 x=0;x<overwritten;++x) {
                u32 i=(y*4+x)*4;
                r->data[i]=snapshot[i+1];r->data[i+1]=snapshot[i+2];
                r->data[i+2]=snapshot[i+3];r->data[i+3]=snapshot[i];
            }
        }
        verify_pixels(slot,snapshot,8);
        // Reset the upload poison so the next reused allocation checks exact
        // byte writes independently of the previous pass's narrower rectangle.
        memset(upload->data,0xcc,(size_t)upload->size);
        gpu_mirror_release_prefix_exports();
    }
    CHECK(creates==before+2);
    CHECK(gpu_mirror_get(0,0x6000,16,4,2,snapshot,32,0,5)<0);
    CHECK(gpu_mirror_get(1,0x6000,16,4,2,snapshot,32,0,4)<0);
    CHECK(gpu_mirror_get(0,0x6000,16,4,2,snapshot,32,1,4)<0);
    CHECK(gpu_mirror_get(0,0x6000,16,4,2,NULL,32,0,4)<0);
    CHECK(gpu_mirror_get(0,0x6000,16,4,2,snapshot,31,0,4)<0);
    old_releases=releases;old_copies=copies;old_barriers=barriers;
    fail_map=1;CHECK(gpu_mirror_get(0,0x6000,16,4,2,snapshot,32,0,3)<0);fail_map=0;
    CHECK(releases==old_releases+2 && copies==old_copies && barriers==old_barriers);
    gpu_mirror_reset_batch();gpu_scratch_release_all();
    CHECK(s_gpu_scratch_bytes==0);
    const u32 wide_logical_w=1024,wide_physical_w=1280,wide_h=720;
    const u32 wide_pitch=wide_physical_w*4;
    const u64 wide_bytes=(u64)wide_pitch*wide_h;
    u8* wide_snapshot=malloc((size_t)wide_bytes);
    CHECK(wide_snapshot!=NULL); if(!wide_snapshot) return 1;
    for(u64 i=0;i<wide_bytes;++i) wide_snapshot[i]=(u8)(i*37u+i/4u);
    const u32 overwrite_cases[2]={1024,256};
    const unsigned wide_creates=creates,wide_maps=maps,wide_unmaps=unmaps;
    const unsigned wide_copies=copies,wide_releases=releases;
    ID3D12Resource* wide_res=NULL; ID3D12Resource* wide_upload=NULL;
    u64 retained_wide_bytes=0;
    for(unsigned which=0;which<2;++which) {
        u32 overwritten=overwrite_cases[which];
        unsigned maps_before=maps,unmaps_before=unmaps,copies_before=copies;
        unsigned releases_before=releases,creates_before=creates;
        int wide_slot=gpu_mirror_get(0,0x70000+which*0x10000,wide_pitch,
            wide_logical_w,wide_h,wide_snapshot,wide_bytes,0,overwritten);
        CHECK(wide_slot==0); if(wide_slot<0) { free(wide_snapshot); return 1; }
        GpuMirror* mirror=&s_d3d.gpu_mirror[wide_slot];
        CHECK(mirror->canonical_ea==0x71000+which*0x10000 &&
              mirror->mapping_generation==generation &&
              !mirror->authoritative && !mirror->first_write_record_plus_one);
        CHECK(maps==maps_before+1 && unmaps==unmaps_before+1 && copies==copies_before+1);
        CHECK(releases==releases_before);
        if(!which) {
            wide_res=mirror->res; wide_upload=mirror->up;
            CHECK(creates==creates_before+2);
        } else {
            CHECK(mirror->res==wide_res && mirror->up==wide_upload);
            CHECK(creates==creates_before);
        }
        verify_partial_upload(wide_slot,wide_snapshot,wide_logical_w,
                              wide_physical_w,wide_h,overwritten);
        gpu_mirror_release_prefix_exports();
        GpuMirror zero; memset(&zero,0,sizeof(zero));
        CHECK(!memcmp(&zero,&s_d3d.gpu_mirror[wide_slot],sizeof(zero)));
        CHECK(releases==wide_releases && s_gpu_scratch_bytes>0);
        if(!which) retained_wide_bytes=s_gpu_scratch_bytes;
        else CHECK(s_gpu_scratch_bytes==retained_wide_bytes);
    }
    CHECK(creates==wide_creates+2 && maps==wide_maps+2 &&
          unmaps==wide_unmaps+2 && copies==wide_copies+2);
    unsigned fail_maps_before=maps,fail_unmaps_before=unmaps;
    unsigned fail_copies_before=copies,fail_barriers_before=barriers;
    unsigned fail_releases_before=releases;
    fail_map=1;
    CHECK(gpu_mirror_get(0,0x90000,wide_pitch,wide_logical_w,wide_h,
                         wide_snapshot,wide_bytes,0,1024)<0);
    fail_map=0;
    CHECK(maps==fail_maps_before+1 && unmaps==fail_unmaps_before);
    CHECK(copies==fail_copies_before && barriers==fail_barriers_before);
    CHECK(releases==fail_releases_before+2 && s_gpu_scratch_bytes==0);
    gpu_mirror_reset_batch(); gpu_scratch_release_all();
    CHECK(s_gpu_scratch_bytes==0 && creates==releases);
    free(wide_snapshot);
    const char* code=NULL; HRESULT hr=S_OK;
    readback_plan(3,2); CHECK(exact_writeback_readback_alloc(&s_exact_writeback[0],&code,&hr)==0);
    CHECK(s_exact_writeback[0].mapped_commit==mapped_expected &&
          ((s_exact_writeback[0].packed_argb==NULL)==mapped_expected));
    RsxD3D12ExactBlit record={malloc(s_exact_writeback[0].packed_bytes),s_exact_writeback[0].packed_bytes};
    exact_writeback_transfer_baseline(&s_exact_writeback[0],&record);
    CHECK(s_exact_writeback[0].baseline!=NULL && !record.dst_post_snapshot && !record.dst_post_snapshot_bytes);
    original=s_exact_writeback[0].readback; before=creates; old_releases=releases;
    unsigned freed_before=tracked_frees;
    unsigned unmaps_before=unmaps;
    if(mapped_expected) {
        void* view=NULL; D3D12_RANGE range={0,(SIZE_T)s_exact_writeback[0].readback_pitch*s_exact_writeback[0].h};
        CHECK(SUCCEEDED(s_exact_writeback[0].readback->lpVtbl->Map(s_exact_writeback[0].readback,0,&range,&view)) && view);
        s_exact_writeback[0].mapped_rgba=(const u8*)view;
        s_exact_writeback[0].readback_mapped=1;
    }
    exact_writeback_release();
    CHECK(releases==old_releases && !s_exact_writeback_count &&
          tracked_frees==freed_before+(mapped_expected?1:2));
    CHECK(unmaps==unmaps_before+(unsigned)mapped_expected);
    readback_plan(5,2); CHECK(exact_writeback_readback_alloc(&s_exact_writeback[0],&code,&hr)==0);
    CHECK(s_exact_writeback[0].readback==original && creates==before && s_exact_writeback[0].packed_bytes==40);
    record=(RsxD3D12ExactBlit){malloc(s_exact_writeback[0].packed_bytes),s_exact_writeback[0].packed_bytes};
    exact_writeback_transfer_baseline(&s_exact_writeback[0],&record);
    CHECK(s_exact_writeback[0].baseline!=NULL && !record.dst_post_snapshot && !record.dst_post_snapshot_bytes);
    freed_before=tracked_frees;
    exact_writeback_abandon_after_submit();
    CHECK(!s_exact_writeback_count && releases==old_releases && s_gpu_scratch_bytes==0 &&
          tracked_frees==freed_before+(mapped_expected?1:2));
    fake_release(original); /* The fixture reclaims the deliberately abandoned reference. */
    readback_plan(5,2); fail_create=1;
    CHECK(exact_writeback_readback_alloc(&s_exact_writeback[0],&code,&hr)<0); fail_create=0;
    exact_writeback_release(); CHECK(s_gpu_scratch_bytes==0);
    readback_plan(5,2); fail_query=1;
    CHECK(exact_writeback_readback_alloc(&s_exact_writeback[0],&code,&hr)==0); fail_query=0;
    old_releases=releases; exact_writeback_release(); CHECK(releases==old_releases+1 && s_gpu_scratch_bytes==0);
    for(unsigned i=0;i<3;++i) { GpuScratchAllocation a=small_allocation(i+1,64ULL<<20); gpu_scratch_retain(&a); }
    CHECK(s_gpu_scratch_bytes==GPU_SCRATCH_MAX_BYTES && s_gpu_scratch_evictions==1);
    GpuScratchAllocation rejected=small_allocation(100,GPU_SCRATCH_MAX_BYTES+1);
    old_releases=releases; gpu_scratch_retain(&rejected);
    CHECK(!rejected.res && releases==old_releases+1 && s_gpu_scratch_bytes==GPU_SCRATCH_MAX_BYTES);
    gpu_scratch_release_all(); u64 evictions=s_gpu_scratch_evictions;
    for(unsigned i=0;i<GPU_SCRATCH_SLOTS+1;++i) { GpuScratchAllocation a=small_allocation(i+1,65536); gpu_scratch_retain(&a); }
    CHECK(s_gpu_scratch_evictions==evictions+1 && s_gpu_scratch_bytes==GPU_SCRATCH_SLOTS*65536ULL);
    gpu_scratch_release_all(); CHECK(s_gpu_scratch_bytes==0 && creates==releases);
    printf("PASS: %u production scratch/upload/lifetime checks, plus 4 fence/ownership guards\n",checks);
    return 0;
}
'''
    fixture = fixture.replace("MIRROR_TYPE\n", typedef(backend, "GpuMirror") + "\n")
    fixture = fixture.replace("PLAN_TYPE\n", typedef(backend, "ExactBlitWritebackPlan") + "\n")
    fixture = fixture.replace("SCRATCH\n", scratch + "\n")
    signatures = [
        "static void gpu_scratch_dispose(", "static int gpu_scratch_take(",
        "static void gpu_scratch_retain(", "static void gpu_scratch_release_all(",
        "static u64 gpu_scratch_allocation_bytes(", "static void gpu_mirror_retire(",
        "static void gpu_mirror_reset_batch(", "static void gpu_mirror_release_prefix_exports(",
        "static int gpu_mirror_find(", "static void gpu_mirror_transition(",
        "static int gpu_mirror_get(", "static int tj_main_mapped_commit_enabled(",
        "static int exact_writeback_readback_alloc(",
        "static void exact_writeback_transfer_baseline(",
        "static void exact_writeback_release(", "static void exact_writeback_abandon_after_submit(",
    ]
    bodies = []
    for signature in signatures:
        body = function(backend, signature)
        if signature in ("static void exact_writeback_release(",
                         "static void exact_writeback_abandon_after_submit("):
            body = body.replace("free(", "tracked_free(")
        bodies.append(body)
    fixture = fixture.replace("FUNCTIONS\n", "\n".join(bodies) + "\n")
    compiler = shutil.which("clang") or r"C:\Program Files\LLVM\bin\clang.exe"
    with tempfile.TemporaryDirectory(prefix="tj-gpu-scratch-") as temp:
        source, exe = Path(temp) / "test.c", Path(temp) / "test.exe"
        source.write_text(fixture, encoding="utf-8")
        subprocess.run([compiler, "-std=c11", "-O2", "-D_CRT_SECURE_NO_WARNINGS", "-I",
                        str(ROOT.parent / "ps3recomp/libs/video"),
                        str(source), "-o", str(exe)], check=True)
        for value in (None, "0", "true", "01", "1"):
            env = os.environ.copy()
            env.pop("TJ_MAIN_MAPPED_COMMIT", None)
            if value is not None:
                env["TJ_MAIN_MAPPED_COMMIT"] = value
            subprocess.run([str(exe)], env=env, check=True)


if __name__ == "__main__":
    main()
