"""Exercise extracted prefix replay-prepare admission and retirement helpers."""
from pathlib import Path
import os
import re
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
VIDEO = ROOT.parent / "ps3recomp" / "libs" / "video"


def definition(source, signature):
    start = source.find(signature)
    while start >= 0:
        brace = source.find("{", start)
        semi = source.find(";", start)
        if brace >= 0 and (semi < 0 or brace < semi):
            depth, end = 1, brace + 1
            while depth:
                depth += (source[end] == "{") - (source[end] == "}")
                end += 1
            return source[start:end]
        start = source.find(signature, start + len(signature))
    raise AssertionError(f"production definition missing: {signature}")


def test_prefix_replay_prepare():
    backend = (VIDEO / "rsx_d3d12_backend.c").read_text(encoding="utf-8")
    wait_start = backend.index("enum {\n    TJ_PREFIX_WAIT_SYNC,")
    wait_enum_end = backend.index("typedef struct {\n    u64 calls, wait_ticks;", wait_start)
    wait_trace_enums = backend[wait_start:wait_enum_end]
    wait_types_end = backend.index("/* Exact opt-in.", wait_enum_end)
    wait_trace_types = backend[wait_enum_end:wait_types_end]
    prepare_key = re.search(
        r"typedef struct\s*\{\s*const void\* texture;.*?\}\s*D3D12PrepareTextureKey;",
        backend, re.S,
    )
    assert prepare_key
    prepare_reasons = re.search(
        r"enum\s*\{\s*TJ_PREPARE_ACCEPTED,.*?\};", backend, re.S,
    )
    assert prepare_reasons
    functions = "\n".join(definition(backend, signature) for signature in (
        "static int d3d12_prefix_ownership_enabled(void)",
        "static int d3d12_prefix_overlap_enabled(void)",
        "static int tj_prefix_cost_enabled(void)",
        "static int d3d12_prefix_prepare_enabled(void)",
        "static int d3d12_prefix_queue_ahead_enabled(void)",
        "static int d3d12_prefix_vertex_prepare_overlap_enabled(void)",
        "static int d3d12_prefix_queue_ahead_prepare(RsxD3D12CompletionReason reason)",
        "static int d3d12_prefix_queue_ahead_retire_old(u32 slot,",
        "static int d3d12_prefix_queue_ahead_before_mutation(const void* pointer)",
        "static int d3d12_prefix_texture_gpu_alias(u32 raw,",
        "static u32 vp_fragment_texture_mask(",
        "static int d3d12_prefix_current_gpu_alias(int include_fragment,",
        "static int d3d12_prefix_queue_ahead_record_bank_ready(int vertex_only)",
        "static int d3d12_prefix_prepare_add_texture(",
        "static int d3d12_prefix_owner_texture_reserved(",
        "static int d3d12_prefix_owner_object_reserved(",
        "static int d3d12_prefix_prepare_decision(u32 reason, int result)",
        "static int d3d12_prefix_prepare_admit(void)",
        "static int d3d12_prefix_queued_pair_enabled(void)",
        "static int d3d12_prefix_queued_pair_preflight(",
        "static int d3d12_prefix_queued_pair_retire(void)",
        "static int d3d12_prefix_owner_checked_completion(",
        "static int d3d12_prefix_owner_wait(",
        "static int d3d12_prefix_owner_submit(void)",
        "static int d3d12_prefix_owner_signal(void)",
        "static int d3d12_prefix_owner_end(",
        "static int d3d12_prefix_owner_drain_ready(RsxD3D12CompletionReason reason)",
        "static int d3d12_prefix_overlap_drain_with_reasons(",
        "static int d3d12_prefix_overlap_drain(",
        "static int d3d12_prefix_owner_before_upload(",
        "static int d3d12_prefix_prepare_retire(void)",
        "static int d3d12_prefix_prepare_before_wait(void)",
        "static int d3d12_prefix_prepare_before_mutation(",
        "static int d3d12_prefix_owner_object_mutable(",
        "static int d3d12_prefix_overlap_eligible(void)",
    ))

    # Keep admission's mask proof tied to the exact binding helper used by replay.
    binding_mask = definition(backend, "static u32 vp_fragment_texture_mask(")
    admission = definition(backend, "static int d3d12_prefix_prepare_admit(void)")
    assert "const u32 mask = vp_fragment_texture_mask(dr);" in admission
    assert "dr->fp_program.texture_read_mask | 1u" in binding_mask
    render = definition(backend, "static void render_frame(u32 selected_buffer_id,")
    late = render[render.index("if (s_d3d.prefix_prepare_owner)"):]
    select_pair = late.index("if (overlap_ready && !texture_release_nowait_ready &&")
    preflight = late.index("d3d12_prefix_queued_pair_preflight()", select_pair)
    assert "if (queued_pair < 0) return;" in late[preflight:late.index("if (!queued_pair)")]
    default_retire = late.index("d3d12_prefix_prepare_retire() != 0", preflight)
    submit = late.index("d3d12_prefix_owner_submit()", default_retire)
    execute = late.index("ExecuteCommandLists", submit)
    assert select_pair < preflight < default_retire < submit < execute
    assert "if (!queued_pair)" in late[preflight:default_retire]
    assert "return;" in late[default_retire:submit]
    signal = late.index("d3d12_prefix_owner_signal()", execute)
    cpu_ready = late.index("owner->cpu_retirement_ready = 1", signal)
    retire_pair = late.index("d3d12_prefix_queued_pair_retire()", cpu_ready)
    assert execute < signal < cpu_ready < retire_pair
    assert "d3d12_prefix_queued_pair_retire() != 0) return;" in late[retire_pair:]

    prefix_wrapper = definition(backend, "static void render_fifo_prefix_owned(void)")
    ahead_prepare = prefix_wrapper.index("d3d12_prefix_queue_ahead_prepare(s_tj_completion_reason)")
    prepare_admit = prefix_wrapper.index("d3d12_prefix_prepare_admit()", ahead_prepare)
    owner_begin = prefix_wrapper.index("d3d12_prefix_owner_begin(slot)", prepare_admit)
    assert ahead_prepare < prepare_admit < owner_begin
    overlap_submit = definition(backend, "static int d3d12_prefix_overlap_submit(")
    assert overlap_submit.index("d3d12_prefix_queue_ahead_prepare(reason)") < overlap_submit.index(
        "d3d12_prefix_prepare_admit()"
    ) < overlap_submit.index("render_fifo_prefix_owned()")

    wrapper = definition(backend, "static void render_fifo_prefix_owned(void)")
    body_return = wrapper.index("render_frame(0, 0, 0, 1);")
    cleanup_retire = wrapper.index("(void)d3d12_prefix_prepare_retire();", body_return)
    restore_active = wrapper.index("s_d3d.prefix_owner_active = 0;", cleanup_retire)
    tail = wrapper[restore_active:]
    assert cleanup_retire < restore_active
    assert "s_d3d.prefix_deferred_owner == slot + 1" in tail
    assert "if (owner->phase == 1 || s_d3d.gcm2d_backend_halted)" in tail
    assert "(void)d3d12_prefix_owner_end(slot);" in tail

    label = definition(backend, "int rsx_d3d12_backend_prepare_texture_label(void)")
    early_overlap = "if (d3d12_prefix_prepare_admit() && d3d12_prefix_overlap_eligible())"
    assert early_overlap in label
    assert label.index(early_overlap) < label.index(
        "return d3d12_prefix_overlap_submit(1, RSX_COMPLETION_TEXTURE_RELEASE);"
    ) < label.index("d3d12_prefix_overlap_drain(RSX_COMPLETION_TEXTURE_RELEASE)")
    assert label.index("d3d12_prefix_overlap_drain(RSX_COMPLETION_TEXTURE_RELEASE)") < label.index(
        "if (!d3d12_texture_label_batch_enabled() || !d3d12_texture_prefix_can_batch())"
    )

    before_upload = definition(backend, "static int d3d12_prefix_owner_before_upload(")
    bank_gate = before_upload.index("(basic ||")
    marker_fallback = before_upload.index("if (s_d3d.prefix_prepare_owner)", bank_gate)
    assert bank_gate < marker_fallback < marker_fallback + 350
    assert "d3d12_prefix_prepare_before_wait() != 0" in before_upload[marker_fallback:]
    ahead_upload = before_upload.index("if (!basic && d3d12_prefix_queue_ahead_enabled() &&")
    ready_call = before_upload.index("if (d3d12_prefix_queue_ahead_record_bank_ready(vertex_only))", ahead_upload)
    partial_retire = before_upload.index("d3d12_prefix_queue_ahead_retire_old(slot,", ready_call)
    latest_guard = before_upload.index("s_d3d.prefix_deferred_owner == latest", partial_retire)
    record_counter = before_upload.index("++s_d3d.prefix_queue_ahead_record_retires", partial_retire)
    full_fallback = before_upload.index("else if (d3d12_prefix_overlap_drain(", record_counter)
    assert ahead_upload < ready_call < partial_retire < latest_guard < record_counter < full_fallback
    assert "if (!basic" in before_upload[ahead_upload:ready_call]
    assert "RSX_COMPLETION_RESOURCE_RELEASE" in before_upload[full_fallback:]
    wrapper = definition(backend, "static void render_fifo_prefix_owned(void)")
    wrapper_admit = wrapper.index("const int prepare_overlap = d3d12_prefix_prepare_admit();")
    rejected = wrapper.index("if (!prepare_overlap && d3d12_prefix_overlap_drain(", wrapper_admit)
    begin = wrapper.index("d3d12_prefix_owner_begin(slot)")
    assert wrapper_admit < rejected < begin
    vp_record = definition(backend, "static void vp_record_cb(u32 slot,")
    marker = vp_record.index("const int retained_marker =")
    ordinary_guard = vp_record.index(
        "if (!retained_marker && d3d12_prefix_owner_before_upload(0, 0, NULL) != 0) return;"
    )
    fp_capture = vp_record.index("rsx_fp_capture_structural(vm_base + fp_off,")
    retained_guard = vp_record.index(
        "if (retained_marker && d3d12_prefix_owner_before_upload(0, 0, dr) != 0) return;"
    )
    snapshots = vp_record.index("vp_capture_draw_texture_snapshots(dr)")
    cb_write = vp_record.index("memset(ts, 0, FP_CB_STRIDE)")
    marker_predicate = vp_record[marker:ordinary_guard]
    marker_predicate_flat = " ".join(marker_predicate.split())
    assert "d3d12_prefix_vertex_prepare_overlap_enabled()" in marker_predicate_flat
    assert "s_d3d.prefix_vertex_prepare_sequence && latest && latest <= 2" in marker_predicate_flat
    assert "s_d3d.prefix_owners[latest - 1].phase == 2" in marker_predicate
    assert "!s_d3d.prefix_owners[latest - 1].label_count" in marker_predicate
    assert "!s_d3d.texture_label_count && !s_d3d.texture_label_armed" in marker_predicate
    assert "s_d3d.prefix_owners[latest - 1].sequence ==" in marker_predicate
    assert "s_d3d.prefix_vertex_prepare_sequence;" in vp_record[marker:ordinary_guard]
    assert vp_record.count("rsx_fp_capture_structural(") == 1
    assert marker < ordinary_guard < fp_capture < retained_guard < snapshots < cb_write

    record_ready = definition(backend, "static int d3d12_prefix_queue_ahead_record_bank_ready(int vertex_only)")
    assert "s_d3d.draw_count" in record_ready and "s_d3d.vp_vb_offset" in record_ready
    assert "s_d3d.vb_offset" in record_ready and "s_d3d.prefix_deferred_owner" in record_ready
    assert "const int include_fragment = !(vertex_only &&" in record_ready
    assert "d3d12_prefix_current_gpu_alias(include_fragment, NULL)" in record_ready
    assert "if (!d3d12_prefix_queue_ahead_enabled()" in record_ready
    assert "return 1;" in record_ready
    assert "tj_main_direct_readback_enabled()" not in admission
    assert "tj_main_direct_readback_enabled()" not in record_ready
    nowait_ready = definition(backend, "static int d3d12_texture_release_nowait_ready(")
    assert "tj_main_direct_readback_enabled()" in nowait_ready

    # Direct mode only enables a plan when a qualifying endpoint is present.
    # A real plan still makes overlap ineligible and must finish synchronously.
    preflight = render.index("if (exact_writeback_preflight() != 0)")
    overlap_start = render.index("int overlap_ready =", preflight)
    overlap_end = render.index("int texture_release_nowait_ready", overlap_start)
    overlap_gate = render[overlap_start:overlap_end]
    assert "!s_exact_writeback_count" in overlap_gate
    assert "s_d3d.gpu_mirror[i].immutable_source" in overlap_gate
    execute = render.index("ExecuteCommandLists", overlap_end)
    overlap_submit = render.index("if (overlap_ready) {", execute)
    sync_start = render.index("ExactWritebackFailure failure = {0};", overlap_submit)
    checked_fence = render.index("int fence_ok = d3d12_prefix_owner_complete() == 0;", sync_start)
    fence_failure = render.index("if (!fence_ok) {", checked_fence)
    fence_else = render.index("} else {", fence_failure)
    map_convert = render.index("int map_ok = exact_writeback_map_convert_all(&failure) == 0;", fence_else)
    commit = render.index("int commit_ok = exact_writeback_commit_all(&failure) == 0;", map_convert)
    failed_fence_path = render[fence_failure:fence_else]
    assert preflight < overlap_start < execute < overlap_submit < sync_start < checked_fence
    assert checked_fence < fence_failure < fence_else < map_convert < commit
    assert "return;" in render[overlap_submit:sync_start]
    assert "exact_writeback_map_convert_all" not in failed_fence_path
    assert "exact_writeback_commit_all" not in failed_fence_path
    assert render.index("if (fence_ok) exact_writeback_release();", commit) < render.index(
        "else exact_writeback_abandon_after_submit();", commit
    )

    vertex_gate = definition(backend, "static int d3d12_prefix_vertex_prepare_overlap_enabled(void)")
    assert 'strcmp(value, "1") == 0' in vertex_gate
    assert "d3d12_prefix_queue_ahead_enabled()" in vertex_gate
    aliases = definition(backend, "static int d3d12_prefix_current_gpu_alias(int include_fragment,")
    retained_start = aliases.index("if (retained)")
    retained_end = aliases.index(
        "\n    for (u32 u = 0; include_fragment", retained_start
    )
    retained_aliases = aliases[retained_start:retained_end]
    assert "vp_fragment_texture_mask(retained)" in retained_aliases
    assert "retained->tex[u]" in retained_aliases
    assert "retained->vtex[u]" in retained_aliases
    assert "fp_addr" not in retained_aliases
    assert "s_d3d.cur_texs" not in retained_aliases and "s_d3d.cur_vtexs" not in retained_aliases
    assert "include_fragment && u < RSX_MAX_TEXTURES" in aliases
    assert "u < RSX_MAX_VERTEX_TEXTURES" in aliases
    marker_validation = before_upload.index("if (s_d3d.prefix_vertex_prepare_sequence)")
    marker_id = before_upload.index("latest > 2", marker_validation)
    marker_sequence = before_upload.index("s_d3d.prefix_owners[latest - 1].sequence", marker_validation)
    marker_clear_stale = before_upload.index("s_d3d.prefix_vertex_prepare_sequence = 0", marker_validation)
    late_alias_drain = before_upload.index("else if (!vertex_only)", marker_validation)
    alias_probe = before_upload.index("d3d12_prefix_current_gpu_alias(1, retained)", late_alias_drain)
    late_drain = before_upload.index(
        "d3d12_prefix_overlap_drain(RSX_COMPLETION_RESOURCE_RELEASE)", alias_probe
    )
    marker_clear_success = before_upload.index("s_d3d.prefix_vertex_prepare_sequence = 0", late_drain)
    assert marker_validation < marker_id < marker_sequence < marker_clear_stale < late_alias_drain
    assert alias_probe < late_drain < marker_clear_success < ahead_upload
    retired_a = before_upload.index("d3d12_prefix_queue_ahead_retire_old(slot, s_tj_completion_reason)")
    marker_set = before_upload.index("s_d3d.prefix_vertex_prepare_sequence =", retired_a)
    assert ahead_upload < retired_a < marker_set < full_fallback
    assert "vertex_only && d3d12_prefix_vertex_prepare_overlap_enabled()" in before_upload[retired_a:marker_set]

    alias_scan = definition(backend, "static int d3d12_prefix_texture_gpu_alias(u32 raw,")
    assert "off_rt_find(raw, local) >= 0" in alias_scan
    assert "raw < (u64)r->off + (u64)r->raw_pitch * r->h" in alias_scan
    assert "ds->raw == raw" in alias_scan
    assert "cellGcmGetDisplaySurfaceInfo(raw, pitch, w, h, &info)" in alias_scan
    assert admission.index("d3d12_prefix_texture_gpu_alias(dr->tex[u].raw") < admission.index(
        "if (!dr->tex_snapshot[u])"
    )
    assert admission.index("d3d12_prefix_texture_gpu_alias(dr->vtex[u].raw") < admission.index(
        "if (!dr->vtex_snapshot[u])"
    )
    assert "if (d3d12_prefix_queue_ahead_enabled() &&" in admission[
        admission.index("d3d12_prefix_texture_gpu_alias(dr->tex[u].raw") - 100:
        admission.index("d3d12_prefix_texture_gpu_alias(dr->tex[u].raw")
    ]
    assert "if (d3d12_prefix_queue_ahead_enabled() &&" in admission[
        admission.index("d3d12_prefix_texture_gpu_alias(dr->vtex[u].raw") - 100:
        admission.index("d3d12_prefix_texture_gpu_alias(dr->vtex[u].raw")
    ]
    mutable = definition(backend, "static int d3d12_prefix_owner_object_mutable(")
    assert mutable.index("d3d12_prefix_prepare_before_mutation(pointer)") < mutable.index(
        "d3d12_prefix_queue_ahead_before_mutation(pointer)"
    ) < mutable.index("d3d12_prefix_owner_object_reserved(pointer)")
    ahead_retire = definition(backend, "static int d3d12_prefix_queue_ahead_retire_old(u32 slot,")
    soft_gate = ahead_retire.index("if (s_exact_writeback_count ||")
    assert "return d3d12_prefix_overlap_drain_with_reasons(" in ahead_retire[soft_gate:]
    assert "reason, RSX_COMPLETION_RESOURCE_RELEASE" in ahead_retire[soft_gate:]

    source = r'''
#include <stdint.h>
#include <stddef.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
using u8=uint8_t; using u32=uint32_t; using u64=uint64_t;
using LONGLONG=int64_t;
#define MAX_DRAWS 8
#define MAX_GPU_MIRRORS 4
#define FRAME_COUNT 2
#define VP_TEX_SLOTS 4
#define RSX_MAX_TEXTURES 16
#define RSX_MAX_VERTEX_TEXTURES 4
#define MAX_OFF_RTS 16
#define MAX_DEPTH_SURFACES 31
#define PREFIX_OBJECT_CAP 16
#define SAMPLER_HEAP_PAGE_COUNT 3
enum RsxD3D12CompletionReason { RSX_COMPLETION_OTHER=0, RSX_COMPLETION_RESOURCE_RELEASE=1, RSX_COMPLETION_REASON_COUNT=2 };
PREFIX_WAIT_ENUMS
PREFIX_WAIT_TYPES
struct D3D12TextureLabel { u32 offset,value; };
struct ID3D12Fence;
struct FenceVtable { u64(*GetCompletedValue)(ID3D12Fence*); };
struct ID3D12Fence { FenceVtable* lpVtbl; u64 completed; };
struct VPTexSnapshot { int identity; };
struct Tex { int set; u32 raw,location,w,h,depth,fmt,pitch,dimension,mips; int cube; u8 border_type; };
struct VPTexSlot { void* res; };
struct OffRT { void* res; u32 off,w,h; u8 local; u32 logical_w,logical_h,raw_pitch; };
struct DepthSurface { void* res; u32 raw,pitch,w,h; u8 local,format; int valid; };
struct CellGcmDisplayInfo { u32 offset,pitch,width,height; };
struct GpuMirror { int valid,local,immutable_source; };
struct D3D12DrawRecord {
    int is_vp,is_clear,is_scaled_blit,fp_program_valid;
    u32 fp_addr;
    struct { u32 texture_read_mask; } fp_program;
    Tex tex[RSX_MAX_TEXTURES],vtex[RSX_MAX_VERTEX_TEXTURES];
    VPTexSnapshot* tex_snapshot[RSX_MAX_TEXTURES];
    VPTexSnapshot* vtex_snapshot[RSX_MAX_VERTEX_TEXTURES];
    u32 tex_snapshot_fault_mask,vtex_snapshot_fault_mask;
};
struct D3D12PrefixOwner {
    int phase,signal_queued,cpu_retirement_ready,completion_checked;
    ID3D12Fence* completion_fence; u64 fence_value,sequence; u32 frame_index;
    void* srv_heap; void* sampler_heaps[3];
    VPTexSlot textures[VP_TEX_SLOTS];
    u32 texture_count; u64 upload_first[4],upload_bytes[4];
    D3D12TextureLabel labels[8]; u32 label_count,label_flush_kind;
    void* objects[PREFIX_OBJECT_CAP]; u32 object_count;
};
KEY_TYPE
static struct {
    int gcm2d_backend_halted,frame_index,vp_parity,prefix_owner_active;
    int fifo_prefix_epoch_open,fifo_prefix_outstanding,texture_label_armed;
    int texture_release_nowait_request,dump_frames_left,texture_label_flush_kind;
    u32 prefix_deferred_owner,prefix_prepare_owner,draw_count,texture_label_count;
    u32 vp_vb_offset,vb_offset;
    u64 prefix_submit_sequence,prefix_retired_sequence,prefix_vertex_prepare_sequence;
    u64 fence_values[FRAME_COUNT];
    u64 fifo_prefix_serial,prefix_queued_pairs;
    u64 prefix_queue_ahead_deferred,prefix_queue_ahead_bank_drains,prefix_queue_ahead_record_retires;
    void* srv_heap; void* sampler_heaps[3]; D3D12TextureLabel texture_labels[8];
    ID3D12Fence* fence; D3D12PrefixOwner prefix_owners[2];
    GpuMirror gpu_mirror[MAX_GPU_MIRRORS]; D3D12DrawRecord draws[MAX_DRAWS];
    Tex cur_texs[RSX_MAX_TEXTURES],cur_vtexs[RSX_MAX_VERTEX_TEXTURES];
    OffRT off_rt[MAX_OFF_RTS]; DepthSurface depth_surface[MAX_DEPTH_SURFACES];
    u64 prefix_prepare_mid_drains;
} s_d3d;
static int s_exact_writeback_count;
static int survival_on=1,gpu_owned,drain_fail,drains,halts;
static int prefix_wait_trace_enabled;
static int indexed_records,compose_enabled,compose_capture_enabled,gcm_perf,main_readback;
static u32 tile_alias_raw=UINT32_MAX,display_alias_raw=UINT32_MAX;
static VPTexSnapshot snapshot_a{1},snapshot_b{2};
static int wait_calls,signal_fail,wait_fail,release_a,release_b,published_batches,published_count;
static u64 waited_tickets[8]; static D3D12TextureLabel published_labels[16];
struct LARGE_INTEGER { LONGLONG QuadPart; };
static LARGE_INTEGER s_tj_prefix_cost_frequency;
static int QueryPerformanceFrequency(LARGE_INTEGER* p){p->QuadPart=1000000;return 1;}
static u64 GetTickCount64(){return 1000;}
static int rsx_survival_trace_is_armed(){return survival_on;}
static int tj_indexed_diag_has_records(){return indexed_records;}
static int tj_compose_target_enabled(){return compose_enabled;}
static int tj_compose_capture_enabled(){return compose_capture_enabled;}
static int tj_gcm2d_backend_perf_enabled(){return gcm_perf;}
static int tj_main_direct_readback_enabled(){return main_readback;}
static int vp_record_texture_is_gpu_owned(const D3D12DrawRecord*,u32,u32){return gpu_owned;}
static int off_rt_find(u32 raw,u8 local){
    if(local&&raw==tile_alias_raw)return 0;
    for(u32 i=0;i<MAX_OFF_RTS;++i)
        if(s_d3d.off_rt[i].res&&s_d3d.off_rt[i].off==raw&&s_d3d.off_rt[i].local==local)return (int)i;
    return -1;
}
int cellGcmGetDisplaySurfaceInfo(u32 raw,u32,u32,u32,CellGcmDisplayInfo*){
    return raw==display_alias_raw;
}
static void gcm2d_backend_halt(const char*){++halts;s_d3d.gcm2d_backend_halted=1;}
static u64 fence_completed(ID3D12Fence* f){return f->completed;}
static FenceVtable fence_vtable={fence_completed};
static ID3D12Fence fence_a{&fence_vtable,0},fence_b{&fence_vtable,0};
static int wait_for_fence_value_checked(u64 ticket,u32){
    if(wait_calls<8)waited_tickets[wait_calls]=ticket;++wait_calls;
    if(wait_fail)return -1;s_d3d.fence->completed=ticket;return 0;
}
static int signal_for_gpu_checked(u64* ticket){
    if(signal_fail)return -1;*ticket=++s_d3d.fence_values[s_d3d.frame_index];return 0;
}
static void tj_prefix_gpu_collect(u32){}
static void tj_prefix_gpu_wait_start(u32){}
static int tj_present_work_trace_enabled(){return 0;}
static LONGLONG tj_present_work_trace_begin(){return 0;}
static void tj_present_work_trace_cpu_finish(LONGLONG,int){}
/* This ownership fixture keeps telemetry off; clock accounting is tested separately. */
static struct { u64 owner_wait_ticks,owner_wait_calls,cpu_clock_invalid; } s_tj_present_work_trace;
static LONGLONG tj_present_work_trace_sync_begin(){return 0;}
static void tj_present_work_trace_sync_finish(LONGLONG,u64*,u64*){}
static LONGLONG tj_prefix_cost_begin(){return 0;}
static void tj_prefix_cost_mark(LONGLONG,int,RsxD3D12CompletionReason){}
static const int TJ_COST_DEFERRED=1;
static void d3d12_prefix_owner_release_uploads(D3D12PrefixOwner* o){
    if(o==&s_d3d.prefix_owners[0])++release_a;else if(o==&s_d3d.prefix_owners[1])++release_b;
    o->texture_count=o->object_count=0;
    memset(o->textures,0,sizeof(o->textures));memset(o->objects,0,sizeof(o->objects));
}
static void d3d12_publish_texture_label_batch(const D3D12TextureLabel* labels,u32 count,u32){
    ++published_batches;
    for(u32 i=0;i<count&&published_count<16;++i)published_labels[published_count++]=labels[i];
}
static int d3d12_prefix_owner_drain_ready(RsxD3D12CompletionReason);
static int d3d12_prefix_overlap_drain_with_reasons(RsxD3D12CompletionReason,RsxD3D12CompletionReason);
static int d3d12_prefix_overlap_drain(RsxD3D12CompletionReason);
static int d3d12_prefix_prepare_enabled(void);
static int d3d12_prefix_queued_pair_enabled(void);
static int d3d12_prefix_queue_ahead_enabled(void);
static int d3d12_prefix_queue_ahead_retire_old(u32,RsxD3D12CompletionReason);
static int d3d12_prefix_queue_ahead_before_mutation(const void*);
static int d3d12_prefix_overlap_eligible(void);
static int d3d12_prefix_owner_texture_reserved(u32);
static int d3d12_prefix_owner_object_reserved(const void*);
static int d3d12_prefix_prepare_retire(void);
static int d3d12_prefix_owner_wait(u32,u32,RsxD3D12CompletionReason);
static int d3d12_prefix_owner_end(u32);
static int d3d12_prefix_prepare_before_wait(void);
static int d3d12_prefix_prepare_before_mutation(const void*);
PREPARE_REASON_ENUM
static RsxD3D12CompletionReason s_tj_completion_reason=RSX_COMPLETION_OTHER;
static int tj_prefix_wait_trace_enabled(){return prefix_wait_trace_enabled;}
static void tj_prefix_wait_trace_owner(u32,RsxD3D12CompletionReason,u32,u64,int,int){}
FUNCTIONS
static int checks,failures;
static void check(int ok,const char* label){if(!ok){++failures;fprintf(stderr,"FAIL %s\n",label);}++checks;}
static void reset_valid(){
    memset(&s_d3d,0,sizeof(s_d3d));s_exact_writeback_count=0;
    memset(&s_tj_prefix_wait_trace,0,sizeof(s_tj_prefix_wait_trace));
    survival_on=1;gpu_owned=drain_fail=drains=halts=0;
    indexed_records=compose_enabled=compose_capture_enabled=gcm_perf=main_readback=0;
    tile_alias_raw=display_alias_raw=UINT32_MAX;
    wait_calls=signal_fail=wait_fail=release_a=release_b=published_batches=published_count=0;
    memset(waited_tickets,0,sizeof(waited_tickets));memset(published_labels,0,sizeof(published_labels));
    fence_a.completed=fence_b.completed=0;
    s_d3d.prefix_deferred_owner=1;s_d3d.prefix_submit_sequence=9;s_d3d.prefix_retired_sequence=8;
    s_d3d.fifo_prefix_epoch_open=s_d3d.fifo_prefix_outstanding=1;
    s_d3d.frame_index=0;s_d3d.vp_parity=1;s_d3d.draw_count=1;s_d3d.fence=&fence_a;
    s_d3d.fence_values[0]=17;
    auto& old=s_d3d.prefix_owners[0];old.phase=2;old.signal_queued=old.cpu_retirement_ready=1;
    old.completion_fence=&fence_a;old.fence_value=17;old.frame_index=0;old.sequence=9;
    auto& d=s_d3d.draws[0];d.is_vp=d.fp_program_valid=1;d.fp_program.texture_read_mask=1;
    d.tex[0]={1,100,1,8,8,1,0,32,2,1,0,0};d.tex_snapshot[0]=&snapshot_a;
}
static void deny(int (*change)(void),const char* label){reset_valid();main_readback=1;change();check(!d3d12_prefix_prepare_admit(),label);}
static int wrong_parity(){s_d3d.vp_parity=0;return 0;}
static int next_slot_busy(){s_d3d.prefix_owners[1].phase=1;return 0;}
static int wrong_frame(){s_d3d.prefix_owners[0].frame_index=1;return 0;}
static int wrong_counter(){s_d3d.fence_values[0]=18;return 0;}
static int wrong_fence(){s_d3d.prefix_owners[0].completion_fence=&fence_b;return 0;}
static int wrong_sequence(){s_d3d.prefix_owners[0].sequence=8;return 0;}
static int wrong_retired(){s_d3d.prefix_retired_sequence=7;return 0;}
static int no_signal(){s_d3d.prefix_owners[0].signal_queued=0;return 0;}
static int no_cpu_ready(){s_d3d.prefix_owners[0].cpu_retirement_ready=0;return 0;}
static int bad_fence_value(){s_d3d.prefix_owners[0].fence_value=UINT64_MAX;return 0;}
static int epoch_closed(){s_d3d.fifo_prefix_epoch_open=0;return 0;}
static int no_outstanding(){s_d3d.fifo_prefix_outstanding=0;return 0;}
static int has_writeback(){s_exact_writeback_count=1;return 0;}
static int has_label(){s_d3d.texture_label_count=1;return 0;}
static int has_armed_label(){s_d3d.texture_label_armed=1;return 0;}
static int has_scaled_blit(){s_d3d.draws[0].is_scaled_blit=1;return 0;}
static int has_gpu_input(){gpu_owned=1;return 0;}
static int immutable_mirror(){s_d3d.gpu_mirror[0]={1,1,1};return 0;}
static int nonlocal_mirror(){s_d3d.gpu_mirror[0]={1,0,0};return 0;}
static int unknown_fragment_program(){s_d3d.draws[0].fp_program_valid=0;return 0;}
static void configure_n_draws(u32 n,int distinct){
    reset_valid();s_d3d.draw_count=n;
    for(u32 i=0;i<n;++i){s_d3d.draws[i]=s_d3d.draws[0];
        if(distinct)s_d3d.draws[i].tex[0].raw+=i;}
}
static void configure_pair_preflight(){
    reset_valid();s_d3d.prefix_submit_sequence=10;s_d3d.prefix_retired_sequence=9;
    s_d3d.prefix_prepare_owner=1;s_d3d.prefix_owner_active=2;s_d3d.prefix_deferred_owner=1;
    auto& old=s_d3d.prefix_owners[0];old.sequence=10;old.fence_value=17;
    old.label_count=1;old.labels[0]={10,1010};old.texture_count=1;old.object_count=1;
    old.textures[0].res=(void*)0x101;old.objects[0]=(void*)0x102;
    auto& next=s_d3d.prefix_owners[1];next.phase=1;next.completion_fence=&fence_a;
    next.srv_heap=(void*)0x201;next.sampler_heaps[0]=(void*)0x202;
    s_d3d.srv_heap=next.srv_heap;s_d3d.sampler_heaps[0]=next.sampler_heaps[0];
}
static void submit_pair_candidate(){
    check(d3d12_prefix_queued_pair_preflight()==1,"valid pair passes preflight");
    auto& next=s_d3d.prefix_owners[1];
    next.texture_count=1;next.object_count=1;
    next.textures[0].res=(void*)0x301;next.objects[0]=(void*)0x302;
    check(d3d12_prefix_owner_submit()==0,"new owner submits after preflight");
    check(s_d3d.prefix_owners[1].sequence==11&&s_d3d.prefix_submit_sequence==11&&
        s_d3d.prefix_owners[1].phase==2,"new sequence is ordered after old");
    check(d3d12_prefix_owner_signal()==0,"new exact fence signal succeeds");
    next.cpu_retirement_ready=1;
    /* A sentinel newer label proves old-only retirement leaves next untouched. */
    s_d3d.prefix_owners[1].label_count=1;s_d3d.prefix_owners[1].labels[0]={20,2020};
}
static void test_queued_pair(){
    configure_pair_preflight();
    check(d3d12_prefix_queued_pair_preflight()==1&&wait_calls==0&&
        s_d3d.prefix_owners[0].phase==2&&s_d3d.prefix_owners[1].phase==1,
        "preflight validates without retiring either owner");
    configure_pair_preflight();s_d3d.texture_label_count=1;
    check(d3d12_prefix_queued_pair_preflight()==0&&!halts,
        "label drift declines optimization instead of halting");
    check(d3d12_prefix_prepare_retire()==0&&s_d3d.prefix_owners[0].phase==0&&
        s_d3d.prefix_owners[1].phase==1&&!s_d3d.fifo_prefix_outstanding,
        "decline uses original old-before-new phase-1 retirement path");
    configure_pair_preflight();s_d3d.fifo_prefix_epoch_open=0;
    check(d3d12_prefix_queued_pair_preflight()==0&&!halts,
        "epoch drift declines queued pair");
    configure_pair_preflight();s_d3d.fifo_prefix_outstanding=0;
    check(d3d12_prefix_queued_pair_preflight()==0&&!halts,
        "outstanding drift declines queued pair");
    configure_pair_preflight();s_d3d.fence_values[0]=18;
    check(d3d12_prefix_queued_pair_preflight()==0&&!halts,
        "old ticket no longer latest declines queued pair");
    configure_pair_preflight();s_exact_writeback_count=1;
    check(d3d12_prefix_queued_pair_preflight()==0&&!halts,
        "queued MAIN plan declines queued pair");
    configure_pair_preflight();s_d3d.prefix_owners[0].sequence=9;
    check(d3d12_prefix_queued_pair_preflight()<0&&halts,
        "invalid owner sequence is structural and halts");

    configure_pair_preflight();
    check(d3d12_prefix_owner_end(0)<0&&published_batches==0&&release_a==0&&
        s_d3d.prefix_owners[0].label_count==1,
        "old labels and refs cannot release before checked completion");

    configure_pair_preflight();submit_pair_candidate();
    check(s_d3d.prefix_owners[1].fence_value==18&&s_d3d.prefix_owners[0].fence_value==17&&
        s_d3d.fence_values[0]==18,"new ticket is greater and is the latest frame ticket");
    check(d3d12_prefix_queued_pair_retire()==0,"queued pair retires exact old owner");
    check(wait_calls==1&&waited_tickets[0]==17,"pair waits only the older exact ticket");
    check(s_d3d.prefix_owners[0].phase==0&&release_a==1&&release_b==0&&
        published_batches==1&&published_count==1&&published_labels[0].value==1010,
        "checked old owner alone releases its refs and labels");
    check(s_d3d.prefix_owners[1].phase==2&&!s_d3d.prefix_owners[1].completion_checked&&
        s_d3d.prefix_owners[1].texture_count==1&&s_d3d.prefix_owners[1].object_count==1&&
        s_d3d.prefix_owners[1].label_count==1&&s_d3d.prefix_owners[1].labels[0].value==2020,
        "new owner refs and pending label remain held");
    check(s_d3d.prefix_retired_sequence==10&&s_d3d.prefix_submit_sequence==11&&
        s_d3d.prefix_deferred_owner==2&&!s_d3d.prefix_prepare_owner&&
        s_d3d.fifo_prefix_outstanding&&s_d3d.fifo_prefix_serial==0&&s_d3d.prefix_queued_pairs==1,
        "outstanding epoch advances to new owner without serial completion");
    check(d3d12_prefix_overlap_drain(RSX_COMPLETION_RESOURCE_RELEASE)==0&&
        wait_calls==2&&waited_tickets[1]==18,"later ordered drain waits exact new ticket");
    check(s_d3d.prefix_owners[1].phase==0&&release_b==1&&published_batches==2&&
        published_count==2&&published_labels[1].value==2020&&
        !s_d3d.prefix_deferred_owner&&!s_d3d.fifo_prefix_outstanding&&s_d3d.fifo_prefix_serial==1,
        "later drain releases new owner and closes the epoch");

    configure_pair_preflight();submit_pair_candidate();signal_fail=0;wait_fail=1;
    check(d3d12_prefix_queued_pair_retire()<0&&halts&&wait_calls==1&&waited_tickets[0]==17,
        "post-new-signal old-wait failure halts");
    check(s_d3d.prefix_owners[0].phase==3&&s_d3d.prefix_owners[1].phase==2&&
        s_d3d.prefix_owners[0].object_count==1&&s_d3d.prefix_owners[1].object_count==1&&
        s_d3d.prefix_owners[0].label_count==1&&s_d3d.prefix_owners[1].label_count==1&&
        release_a==0&&release_b==0&&published_batches==0&&
        s_d3d.prefix_deferred_owner==1&&s_d3d.prefix_prepare_owner==1&&
        s_d3d.fifo_prefix_outstanding,
        "failed old wait retains both submitted owners, refs, and labels");

    configure_pair_preflight();check(d3d12_prefix_queued_pair_preflight()==1,
        "signal-failure setup passes preflight");
    s_d3d.prefix_owners[1].texture_count=1;s_d3d.prefix_owners[1].object_count=1;
    s_d3d.prefix_owners[1].textures[0].res=(void*)0x401;
    s_d3d.prefix_owners[1].objects[0]=(void*)0x402;
    check(d3d12_prefix_owner_submit()==0,"signal-failure candidate submits");
    s_d3d.prefix_owners[1].label_count=1;s_d3d.prefix_owners[1].labels[0]={30,3030};
    signal_fail=1;
    check(d3d12_prefix_owner_signal()<0&&halts&&s_d3d.prefix_owners[0].phase==2&&
        s_d3d.prefix_owners[1].phase==3&&s_d3d.prefix_owners[0].object_count==1&&
        s_d3d.prefix_owners[1].object_count==1&&s_d3d.prefix_owners[1].texture_count==1&&
        s_d3d.prefix_owners[1].label_count==1&&release_a==0&&release_b==0&&published_batches==0,
        "new signal failure halts without releasing either owner");

    configure_pair_preflight();
    check(d3d12_prefix_owner_before_upload(1,0,NULL)==0&&wait_calls==1&&
        waited_tickets[0]==17&&s_d3d.prefix_owners[0].phase==0&&
        s_d3d.prefix_owners[1].phase==1&&!s_d3d.prefix_prepare_owner,
        "basic upload conflict retires prepared old owner before touching banks");
}
static void configure_submitted_pair(){
    configure_pair_preflight();
    auto& old=s_d3d.prefix_owners[0];auto& next=s_d3d.prefix_owners[1];
    old.sequence=10;old.fence_value=17;old.label_count=1;old.labels[0]={10,1010};
    old.texture_count=1;old.object_count=1;old.textures[0].res=(void*)0x101;
    old.objects[0]=(void*)0x102;
    next.phase=2;next.signal_queued=next.cpu_retirement_ready=1;
    next.completion_fence=&fence_a;next.frame_index=0;next.sequence=11;next.fence_value=18;
    next.texture_count=1;next.object_count=1;next.textures[0].res=(void*)0x201;
    next.objects[0]=(void*)0x202;next.label_count=1;next.labels[0]={20,2020};
    s_d3d.prefix_submit_sequence=11;s_d3d.prefix_retired_sequence=9;
    s_d3d.prefix_deferred_owner=2;s_d3d.prefix_prepare_owner=0;
    s_d3d.prefix_owner_active=0;s_d3d.vp_parity=0;s_d3d.fence_values[0]=18;
    s_d3d.fifo_prefix_epoch_open=s_d3d.fifo_prefix_outstanding=1;
}
static void configure_record_bank_ready(){
    configure_submitted_pair();
    s_d3d.draw_count=0;
    s_d3d.vp_vb_offset=s_d3d.vb_offset=0;
}
static void configure_single_deferred_owner(){
    reset_valid();
    s_d3d.draw_count=0;s_d3d.vp_parity=0;
    auto& owner=s_d3d.prefix_owners[0];
    owner.texture_count=1;owner.textures[0].res=(void*)0x101;
    owner.object_count=1;owner.objects[0]=(void*)0x102;
    owner.label_count=1;owner.labels[0]={10,1010};
    s_d3d.prefix_deferred_owner=1;
}
static void set_current_fragment_alias(){
    s_d3d.cur_texs[0]={1,0x1010,1,8,4,1,0,64,2,1,0,0};
    s_d3d.off_rt[0]={(void*)0x909,0x1000,8,4,1,8,4,64};
}
static void set_current_vertex_alias(){
    s_d3d.cur_vtexs[0]={1,0x3450,1,8,8,1,0,32,2,1,0,0};
    s_d3d.depth_surface[0]={(void*)0x90A,0x3450,32,8,8,1,0,1};
}
static void test_texture_gpu_alias(){
    reset_valid();
    check(!d3d12_prefix_texture_gpu_alias(0x5000,1,8,8,32),
        "ordinary local texture is not an alias");
    check(d3d12_prefix_texture_gpu_alias(0x5000,0,8,8,32),
        "unknown texture location is conservatively rejected");

    reset_valid();s_d3d.off_rt[0]={(void*)0x901,0x1000,8,4,1,8,4,64};
    check(d3d12_prefix_texture_gpu_alias(0x1000,1,8,4,64),
        "exact OffRT identity is rejected");
    check(d3d12_prefix_texture_gpu_alias(0x1010,1,8,4,64),
        "packed texture address inside OffRT backing span is rejected");
    check(!d3d12_prefix_texture_gpu_alias(0x1100,1,8,4,64),
        "address exactly at OffRT backing end is not aliased");
    check(!d3d12_prefix_texture_gpu_alias(0x1110,1,8,4,64),
        "address outside OffRT backing span is not aliased");
    check(!d3d12_prefix_texture_gpu_alias(0x1010,2,8,4,64),
        "OffRT in another memory locality is not aliased");

    reset_valid();tile_alias_raw=0x2340;
    check(d3d12_prefix_texture_gpu_alias(0x2340,1,8,8,32),
        "tile metadata exact alias is rejected");
    check(!d3d12_prefix_texture_gpu_alias(0x2340,2,8,8,32),
        "tile metadata from another locality is ignored");

    reset_valid();s_d3d.depth_surface[0]={(void*)0x902,0x3450,32,8,8,1,0,1};
    check(d3d12_prefix_texture_gpu_alias(0x3450,1,8,8,32),
        "valid depth surface exact raw identity is rejected");
    check(!d3d12_prefix_texture_gpu_alias(0x3450,2,8,8,32),
        "depth surface from another locality is ignored");

    reset_valid();display_alias_raw=0x4560;
    check(d3d12_prefix_texture_gpu_alias(0x4560,1,8,8,32),
        "display/history metadata alias is rejected");
    check(!d3d12_prefix_texture_gpu_alias(0x4560,2,8,8,32),
        "display/history metadata lookup is local-memory only");
}
static void test_record_bank_readiness(){
    configure_record_bank_ready();
    check(d3d12_prefix_queue_ahead_record_bank_ready(0),
        "two exact submitted owners permit the empty-record first upload");
#define DENY_BANK(change,label) do { configure_record_bank_ready(); main_readback=1; change; \
    check(!d3d12_prefix_queue_ahead_record_bank_ready(0),label); } while(0)
    DENY_BANK(survival_on=0,"unarmed Survival disables partial retirement");
    DENY_BANK(s_d3d.gcm2d_backend_halted=1,"halted renderer disables partial retirement");
    DENY_BANK(s_d3d.prefix_owner_active=1,"active owner disables partial retirement");
    DENY_BANK(s_d3d.prefix_prepare_owner=1,"prepared owner disables partial retirement");
    DENY_BANK(s_d3d.draw_count=1,"existing records disable partial retirement");
    DENY_BANK(s_d3d.vp_vb_offset=1,"VP upload offset disables partial retirement");
    DENY_BANK(s_d3d.vb_offset=1,"general upload offset disables partial retirement");
    DENY_BANK(s_d3d.texture_label_count=1,"pending labels disable partial retirement");
    DENY_BANK(s_d3d.texture_label_armed=1,"armed labels disable partial retirement");
    DENY_BANK(s_exact_writeback_count=1,"pending MAIN plan disables partial retirement");
    DENY_BANK(s_d3d.fifo_prefix_epoch_open=0,"closed epoch disables partial retirement");
    DENY_BANK(s_d3d.fifo_prefix_outstanding=0,"missing outstanding marker disables partial retirement");
    DENY_BANK(s_d3d.vp_parity=2,"invalid parity disables partial retirement");
    DENY_BANK(s_d3d.frame_index=FRAME_COUNT,"invalid frame disables partial retirement");
    DENY_BANK(s_d3d.prefix_deferred_owner=0,"missing latest owner disables partial retirement");
    DENY_BANK(s_d3d.prefix_deferred_owner=3,"out-of-range latest owner disables partial retirement");
    DENY_BANK(s_d3d.texture_release_nowait_request=1,"nowait release disables partial retirement");
    DENY_BANK(s_d3d.dump_frames_left=1,"dump observer disables partial retirement");
    DENY_BANK(indexed_records=1,"indexed observer disables partial retirement");
    DENY_BANK(compose_enabled=1,"compose target disables partial retirement");
    DENY_BANK(compose_capture_enabled=1,"compose capture disables partial retirement");
    DENY_BANK(gcm_perf=1,"GCM performance observer disables partial retirement");
    configure_record_bank_ready();main_readback=1;
    check(d3d12_prefix_queue_ahead_record_bank_ready(0),
        "direct-readback mode permits an empty-record bank with no current MAIN plan");
    DENY_BANK(s_exact_writeback_count=1,"current exact MAIN plan disables partial retirement in direct mode");
    DENY_BANK(s_d3d.texture_label_count=1,"pending labels disable partial retirement in direct mode");
    DENY_BANK(s_d3d.texture_label_armed=1,"armed labels disable partial retirement in direct mode");
    DENY_BANK(s_d3d.prefix_deferred_owner=1,"same parity as latest owner disables partial retirement");
    DENY_BANK(s_d3d.prefix_owners[0].phase=1,"old owner phase must be submitted");
    DENY_BANK(s_d3d.prefix_owners[1].phase=1,"new owner phase must be submitted");
    DENY_BANK(s_d3d.prefix_owners[0].signal_queued=0,"old exact signal is required");
    DENY_BANK(s_d3d.prefix_owners[1].signal_queued=0,"new exact signal is required");
    DENY_BANK(s_d3d.prefix_owners[0].cpu_retirement_ready=0,"old CPU retirement must be ready");
    DENY_BANK(s_d3d.prefix_owners[1].cpu_retirement_ready=0,"new CPU retirement must be ready");
    DENY_BANK(s_d3d.prefix_owners[0].fence_value=0,"old exact ticket must be nonzero");
    DENY_BANK(s_d3d.prefix_owners[0].fence_value=UINT64_MAX,"old removal ticket is invalid");
    DENY_BANK(s_d3d.prefix_owners[1].fence_value=0,"new exact ticket must be nonzero");
    DENY_BANK(s_d3d.prefix_owners[1].fence_value=UINT64_MAX,"new removal ticket is invalid");
    DENY_BANK(s_d3d.prefix_owners[1].fence_value=17,"new ticket must follow old ticket");
    DENY_BANK(s_d3d.prefix_owners[0].completion_fence=nullptr,"old owner must have exact fence");
    DENY_BANK(s_d3d.prefix_owners[0].completion_fence=&fence_b,"old owner must use active fence");
    DENY_BANK(s_d3d.prefix_owners[1].completion_fence=&fence_b,"new owner must use active fence");
    DENY_BANK(s_d3d.prefix_owners[0].frame_index=1,"old owner frame must match active frame");
    DENY_BANK(s_d3d.prefix_owners[1].frame_index=1,"new owner frame must match active frame");
    DENY_BANK(s_d3d.fence_values[0]=19,"new ticket must be latest frame ticket");
    DENY_BANK(s_d3d.prefix_owners[0].sequence=9,"old owner sequence must follow retired sequence");
    DENY_BANK(s_d3d.prefix_owners[0].sequence=UINT64_MAX,"old owner sequence overflow is invalid");
    DENY_BANK(s_d3d.prefix_owners[1].sequence=12,"new sequence must immediately follow old");
    DENY_BANK(s_d3d.prefix_submit_sequence=12,"new sequence must equal submit sequence");
    DENY_BANK(s_d3d.gpu_mirror[0]=(GpuMirror{1,0,0}),"nonlocal GPU mirror disables partial retirement");
    DENY_BANK(s_d3d.gpu_mirror[0]=(GpuMirror{1,1,1}),"immutable GPU mirror disables partial retirement");
    DENY_BANK(set_current_fragment_alias(),
        "current fragment OffRT alias disables partial retirement");
    DENY_BANK(set_current_vertex_alias(),
        "current vertex depth alias disables partial retirement");
#undef DENY_BANK
}
static void test_record_bank_upload_path(){
    configure_record_bank_ready();
    main_readback=1;
    check(d3d12_prefix_queue_ahead_record_bank_ready(0),"record-retire success preflight");
    check(d3d12_prefix_owner_before_upload(0,1,NULL)==0&&wait_calls==1&&waited_tickets[0]==17&&
        s_d3d.prefix_owners[0].phase==0&&s_d3d.prefix_owners[1].phase==2&&
        s_d3d.prefix_deferred_owner==2&&s_d3d.fifo_prefix_outstanding&&
        s_d3d.fifo_prefix_serial==0&&s_d3d.prefix_retired_sequence==10&&
        s_d3d.prefix_submit_sequence==11&&s_d3d.prefix_queue_ahead_record_retires==1&&
        release_a==1&&release_b==0&&published_count==1&&published_labels[0].value==1010,
        "first empty-record upload retires A only and records the transition");
    check(s_d3d.prefix_owners[1].object_count==1&&s_d3d.prefix_owners[1].texture_count==1&&
        s_d3d.prefix_owners[1].label_count==1&&s_d3d.prefix_owners[1].labels[0].value==2020,
        "partial retirement preserves B refs and labels");

    configure_record_bank_ready();
    check(d3d12_prefix_owner_before_upload(1,0,NULL)==0&&wait_calls==2&&
        waited_tickets[0]==17&&waited_tickets[1]==18&&release_a==1&&release_b==1&&
        !s_d3d.prefix_deferred_owner&&!s_d3d.fifo_prefix_outstanding&&
        s_d3d.fifo_prefix_serial==1&&s_d3d.prefix_queue_ahead_record_retires==0,
        "basic uploads retain the original full ordered drain");

    configure_record_bank_ready();s_d3d.draw_count=1;
    check(!d3d12_prefix_queue_ahead_record_bank_ready(1),"nonempty record misses partial-retire preflight");
    check(d3d12_prefix_owner_before_upload(0,1,NULL)==0&&wait_calls==2&&
        waited_tickets[0]==17&&waited_tickets[1]==18&&release_a==1&&release_b==1&&
        !s_d3d.prefix_deferred_owner&&s_d3d.fifo_prefix_serial==1&&
        s_d3d.prefix_queue_ahead_record_retires==0,
        "nonempty record falls back to the original full ordered drain");

    configure_record_bank_ready();s_d3d.cur_texs[0]={1,0x1000,1,8,4,1,0,64,2,1,0,0};
    s_d3d.off_rt[0]={(void*)0x903,0x1000,8,4,1,8,4,64};
    check(!d3d12_prefix_queue_ahead_record_bank_ready(0),
        "ordinary nonvertex callback keeps the fragment alias in the old bank-ready gate");
    check(d3d12_prefix_owner_before_upload(0,0,NULL)==0&&wait_calls==2&&release_a==1&&release_b==1&&
        s_d3d.prefix_queue_ahead_record_retires==0&&
        !s_d3d.prefix_vertex_prepare_sequence,
        "initial two-owner nonvertex callback keeps the full ordered drain without a marker");

    configure_record_bank_ready();main_readback=1;wait_fail=1;
    check(d3d12_prefix_owner_before_upload(0,1,NULL)<0&&halts==1&&wait_calls==1&&
        s_d3d.prefix_owners[0].phase==3&&s_d3d.prefix_owners[1].phase==2&&
        s_d3d.prefix_owners[0].object_count==1&&s_d3d.prefix_owners[1].object_count==1&&
        s_d3d.prefix_owners[0].label_count==1&&s_d3d.prefix_owners[1].label_count==1&&
        s_d3d.prefix_deferred_owner==2&&s_d3d.fifo_prefix_outstanding&&
        s_d3d.fifo_prefix_serial==0&&release_a==0&&release_b==0&&published_batches==0&&
        s_d3d.prefix_queue_ahead_record_retires==0,
        "failed exact-A completion halts without releasing either owner");
}
static void test_vertex_prepare_overlap(){
    const int enabled=d3d12_prefix_vertex_prepare_overlap_enabled();
    configure_record_bank_ready();set_current_fragment_alias();
    if(!enabled){
        check(!d3d12_prefix_queue_ahead_record_bank_ready(1),
            "default-off gate keeps packed fragment alias in the bank-ready rejection");
        check(d3d12_prefix_owner_before_upload(0,1,NULL)==0&&wait_calls==2&&
            waited_tickets[0]==17&&waited_tickets[1]==18&&release_a==1&&release_b==1&&
            !s_d3d.prefix_deferred_owner&&!s_d3d.fifo_prefix_outstanding&&
            s_d3d.fifo_prefix_serial==1&&s_d3d.prefix_queue_ahead_record_retires==0&&
            !s_d3d.prefix_vertex_prepare_sequence,
            "default-off vertex upload preserves the full ordered drain");
        return;
    }

    check(d3d12_prefix_queue_ahead_record_bank_ready(1),
        "vertex-only preflight skips the packed current fragment alias");
    check(d3d12_prefix_owner_before_upload(0,1,NULL)==0&&wait_calls==1&&
        waited_tickets[0]==17&&s_d3d.prefix_owners[0].phase==0&&
        s_d3d.prefix_owners[1].phase==2&&s_d3d.prefix_deferred_owner==2&&
        s_d3d.prefix_retired_sequence==10&&s_d3d.prefix_submit_sequence==11&&
        s_d3d.fifo_prefix_outstanding&&s_d3d.fifo_prefix_serial==0&&
        release_a==1&&release_b==0&&published_count==1&&
        published_labels[0].value==1010&&s_d3d.prefix_queue_ahead_record_retires==1&&
        s_d3d.prefix_vertex_prepare_sequence==s_d3d.prefix_owners[1].sequence,
        "vertex-only upload retires exact A while preserving B and the open epoch");
    check(s_d3d.prefix_owners[1].object_count==1&&
        s_d3d.prefix_owners[1].objects[0]==(void*)0x202&&
        s_d3d.prefix_owners[1].texture_count==1&&
        s_d3d.prefix_owners[1].textures[0].res==(void*)0x201&&
        s_d3d.prefix_owners[1].label_count==1&&
        s_d3d.prefix_owners[1].labels[0].value==2020,
        "partial retirement retains B object, texture, and label references");

    s_d3d.prefix_vertex_prepare_sequence=0;
    check(d3d12_prefix_owner_before_upload(0,0,NULL)==0&&wait_calls==1&&
        s_d3d.prefix_owners[1].phase==2&&s_d3d.prefix_deferred_owner==2&&
        !s_d3d.prefix_vertex_prepare_sequence&&s_d3d.fifo_prefix_outstanding&&
        s_d3d.fifo_prefix_serial==0,
        "marker zero preserves the prior pending-B path on a fragment alias");

    s_d3d.prefix_vertex_prepare_sequence=s_d3d.prefix_owners[1].sequence;
    check(d3d12_prefix_owner_before_upload(0,0,NULL)==0&&wait_calls==2&&
        waited_tickets[1]==18&&s_d3d.prefix_owners[1].phase==0&&release_b==1&&
        published_count==2&&published_labels[1].value==2020&&
        !s_d3d.prefix_deferred_owner&&!s_d3d.fifo_prefix_outstanding&&
        s_d3d.fifo_prefix_serial==1&&!s_d3d.prefix_vertex_prepare_sequence,
        "matching marker drains exact B on packed fragment alias before snapshot capture");

    configure_record_bank_ready();
    check(d3d12_prefix_owner_before_upload(0,1,NULL)==0&&wait_calls==1&&
        s_d3d.prefix_vertex_prepare_sequence==s_d3d.prefix_owners[1].sequence,
        "no-alias setup records the matching B sequence after A retires");
    check(d3d12_prefix_owner_before_upload(0,0,NULL)==0&&wait_calls==1&&
        s_d3d.prefix_owners[1].phase==2&&s_d3d.prefix_deferred_owner==2&&
        !s_d3d.prefix_vertex_prepare_sequence&&s_d3d.fifo_prefix_outstanding&&
        s_d3d.fifo_prefix_serial==0,
        "successful no-alias callback clears the marker without draining B");

    configure_record_bank_ready();
        check(d3d12_prefix_owner_before_upload(0,1,NULL)==0&&wait_calls==1&&
        s_d3d.prefix_vertex_prepare_sequence==s_d3d.prefix_owners[1].sequence,
        "stale-marker setup retires A and records B's sequence");
    set_current_fragment_alias();
    s_d3d.prefix_vertex_prepare_sequence=s_d3d.prefix_owners[1].sequence-1;
        check(d3d12_prefix_owner_before_upload(0,0,NULL)==0&&wait_calls==1&&
        s_d3d.prefix_owners[1].phase==2&&s_d3d.prefix_deferred_owner==2&&
        !s_d3d.prefix_vertex_prepare_sequence&&s_d3d.fifo_prefix_outstanding&&
        s_d3d.fifo_prefix_serial==0,
        "stale marker for the same owner slot clears without draining its newer sequence");

    for(u32 invalid_latest=0;invalid_latest<=3;invalid_latest+=3){
        configure_record_bank_ready();
        check(d3d12_prefix_owner_before_upload(0,1,NULL)==0&&wait_calls==1&&
            s_d3d.prefix_vertex_prepare_sequence==s_d3d.prefix_owners[1].sequence,
            "invalid-latest setup records the live B sequence");
        set_current_fragment_alias();s_d3d.prefix_deferred_owner=invalid_latest;
        check(d3d12_prefix_owner_before_upload(0,0,NULL)==0&&wait_calls==1&&
            s_d3d.prefix_deferred_owner==invalid_latest&&
            s_d3d.prefix_owners[1].phase==2&&s_d3d.prefix_vertex_prepare_sequence==0,
            "absent/out-of-range latest owner clears the marker without indexing or waiting");
    }

    configure_record_bank_ready();
    check(d3d12_prefix_owner_before_upload(0,1,NULL)==0&&wait_calls==1&&
        s_d3d.prefix_vertex_prepare_sequence==s_d3d.prefix_owners[1].sequence,
        "vertex alias setup leaves B pending with its sequence marker");
    set_current_vertex_alias();
    check(d3d12_prefix_owner_before_upload(0,0,NULL)==0&&wait_calls==2&&
        waited_tickets[1]==18&&s_d3d.prefix_owners[1].phase==0&&release_b==1&&
        published_count==2&&published_labels[1].value==2020&&
        !s_d3d.prefix_deferred_owner&&!s_d3d.fifo_prefix_outstanding&&
        s_d3d.fifo_prefix_serial==1&&!s_d3d.prefix_vertex_prepare_sequence,
        "matching marker drains exact B on current vertex-texture alias before snapshot capture");

    configure_record_bank_ready();set_current_vertex_alias();
    check(!d3d12_prefix_queue_ahead_record_bank_ready(1),
        "vertex-only gate still rejects a current vertex-texture depth alias");
    check(d3d12_prefix_owner_before_upload(0,1,NULL)==0&&wait_calls==2&&
        waited_tickets[0]==17&&waited_tickets[1]==18&&release_a==1&&release_b==1&&
        s_d3d.prefix_queue_ahead_record_retires==0,
        "current vertex-texture alias retains the full ordered drain");

    configure_record_bank_ready();set_current_fragment_alias();
    check(d3d12_prefix_owner_before_upload(0,1,NULL)==0&&wait_calls==1&&
        waited_tickets[0]==17&&s_d3d.prefix_owners[0].phase==0&&
        s_d3d.prefix_owners[1].phase==2&&release_a==1&&release_b==0&&
        s_d3d.prefix_vertex_prepare_sequence==s_d3d.prefix_owners[1].sequence,
        "late-failure setup retires only A");
    wait_fail=1;
    check(d3d12_prefix_owner_before_upload(0,0,NULL)<0&&halts==1&&wait_calls==2&&
        waited_tickets[1]==18&&s_d3d.prefix_owners[0].phase==0&&
        s_d3d.prefix_owners[1].phase==3&&
        s_d3d.prefix_owners[1].object_count==1&&
        s_d3d.prefix_owners[1].objects[0]==(void*)0x202&&
        s_d3d.prefix_owners[1].texture_count==1&&
        s_d3d.prefix_owners[1].textures[0].res==(void*)0x201&&
        s_d3d.prefix_owners[1].label_count==1&&
        s_d3d.prefix_owners[1].labels[0].value==2020&&
        s_d3d.prefix_deferred_owner==2&&s_d3d.fifo_prefix_outstanding&&
        s_d3d.fifo_prefix_serial==0&&release_a==1&&release_b==0&&
        published_count==1&&published_labels[0].value==1010&&
        s_d3d.prefix_vertex_prepare_sequence==s_d3d.prefix_owners[1].sequence,
        "failed late B wait retains B refs and label and closes before snapshot capture");
}
static void set_record_fragment(D3D12DrawRecord& record,u32 unit,u32 raw){
    record.tex[unit]={1,raw,1,8,4,1,0,64,2,1,0,0};
}
static void set_record_vertex(D3D12DrawRecord& record,u32 unit,u32 raw){
    record.vtex[unit]={1,raw,1,8,8,1,0,32,2,1,0,0};
}
static void start_retained_marker(D3D12DrawRecord& record,u32 mask,int valid=1){
    configure_record_bank_ready();
    s_d3d.prefix_owners[1].label_count=0;
    check(d3d12_prefix_owner_before_upload(0,1,NULL)==0&&wait_calls==1&&
        s_d3d.prefix_owners[0].phase==0&&s_d3d.prefix_owners[1].phase==2&&
        s_d3d.prefix_vertex_prepare_sequence==s_d3d.prefix_owners[1].sequence,
        "label-free retained setup retires A and records B sequence");
    record=D3D12DrawRecord{};record.fp_addr=0x1000;
    record.fp_program_valid=valid;record.fp_program.texture_read_mask=mask;
}
static void check_retained_b_drain(D3D12DrawRecord& record,const char* label){
    check(d3d12_prefix_owner_before_upload(0,0,&record)==0&&wait_calls==2&&
        waited_tickets[1]==18&&s_d3d.prefix_owners[1].phase==0&&release_b==1&&
        !s_d3d.prefix_deferred_owner&&!s_d3d.fifo_prefix_outstanding&&
        s_d3d.fifo_prefix_serial==1&&!s_d3d.prefix_vertex_prepare_sequence&&
        !s_d3d.prefix_owners[1].object_count&&!s_d3d.prefix_owners[1].texture_count&&
        !s_d3d.prefix_owners[1].label_count&&published_count==1&&
        published_labels[0].value==1010,label);
}
static void test_retained_record_mask(int expect_probe){
    D3D12DrawRecord mask_record{};mask_record.fp_program_valid=1;
    mask_record.fp_program.texture_read_mask=1u<<3;
    check(vp_fragment_texture_mask(&mask_record)==
        (expect_probe?0xFFFFu:((1u<<3)|1u)),
        "known FP uses captured read mask unless shader probe requests full guard");
    mask_record.fp_program.texture_read_mask=0;
    check(vp_fragment_texture_mask(&mask_record)==(expect_probe?0xFFFFu:1u),
        "t0 remains in the retained mask as the fallback PSO input");
    mask_record.fp_program_valid=0;
    check(vp_fragment_texture_mask(&mask_record)==0xFFFFu,
        "unknown FP program keeps the full fragment guard");

    if(expect_probe){
        reset_valid();D3D12DrawRecord probe{};probe.fp_program_valid=1;
        probe.fp_program.texture_read_mask=1u<<1;
        set_record_fragment(probe,15,0x1010);
        s_d3d.off_rt[0]={(void*)0x90E,0x1000,8,4,1,8,4,64};
        check(vp_fragment_texture_mask(&probe)==0xFFFFu&&
            d3d12_prefix_current_gpu_alias(1,&probe),
            "FP probe program restores full alias checks through the highest fragment slot");
    }

    if(!d3d12_prefix_vertex_prepare_overlap_enabled())return;

    D3D12DrawRecord retained{};
    start_retained_marker(retained,1u<<1);
    set_record_fragment(retained,1,0x5000);
    set_record_fragment(retained,2,0x1010);
    s_d3d.off_rt[0]={(void*)0x909,0x1000,8,4,1,8,4,64};
    s_d3d.cur_texs[1]={1,0x1010,1,8,4,1,0,64,2,1,0,0};
    s_d3d.cur_texs[2]={1,0x5000,1,8,4,1,0,64,2,1,0,0};
    retained.fp_addr=0x2000; /* Live guest shader address changed after capture. */
    check(d3d12_prefix_current_gpu_alias(1,NULL)&&
        !d3d12_prefix_current_gpu_alias(1,&retained),
        "retained mask ignores changed live shader bindings and an unused packed alias");
    check(d3d12_prefix_owner_before_upload(0,0,&retained)==0&&wait_calls==1&&
        s_d3d.prefix_owners[1].phase==2&&
        s_d3d.prefix_owners[1].object_count==1&&s_d3d.prefix_owners[1].texture_count==1&&
        !s_d3d.prefix_owners[1].label_count&&s_d3d.prefix_vertex_prepare_sequence==0&&
        release_b==0&&published_count==1&&published_labels[0].value==1010,
        "unused retained GPU binding avoids B wait and preserves B object/texture refs");

    start_retained_marker(retained,1u<<1);
    set_record_fragment(retained,1,0x1010);
    s_d3d.off_rt[0]={(void*)0x90B,0x1000,8,4,1,8,4,64};
    check(d3d12_prefix_current_gpu_alias(1,&retained),
        "used retained packed OffRT fragment binding remains a GPU alias");
    check_retained_b_drain(retained,"used packed alias drains B and releases its retained refs");

    start_retained_marker(retained,1u<<1);
    set_record_fragment(retained,1,0x3450);
    s_d3d.depth_surface[0]={(void*)0x90C,0x3450,32,8,8,1,0,1};
    check_retained_b_drain(retained,"used retained depth fragment binding drains B");

    start_retained_marker(retained,0,0);
    set_record_fragment(retained,15,0x1010);
    s_d3d.off_rt[0]={(void*)0x90D,0x1000,8,4,1,8,4,64};
    check(vp_fragment_texture_mask(&retained)==0xFFFFu&&
        d3d12_prefix_current_gpu_alias(1,&retained),
        "unknown retained program guards every fragment binding");
    check_retained_b_drain(retained,"unknown retained program drains B for a high-slot GPU alias");

    if(expect_probe){
        start_retained_marker(retained,1u<<1);
        set_record_fragment(retained,15,0x1010);
        s_d3d.off_rt[0]={(void*)0x90E,0x1000,8,4,1,8,4,64};
        check(vp_fragment_texture_mask(&retained)==0xFFFFu&&
            d3d12_prefix_current_gpu_alias(1,&retained),
            "FP probe program restores the full retained fragment guard");
        check_retained_b_drain(retained,"probe guard drains B for a high-slot GPU alias");
    }

    start_retained_marker(retained,0);
    set_record_fragment(retained,0,0x1010);
    s_d3d.off_rt[0]={(void*)0x90F,0x1000,8,4,1,8,4,64};
    check((vp_fragment_texture_mask(&retained)&1u)&&
        d3d12_prefix_current_gpu_alias(1,&retained),
        "retained t0 fallback binding remains guarded");
    check_retained_b_drain(retained,"retained t0 fallback alias drains B");

    start_retained_marker(retained,0);
    set_record_vertex(retained,0,0x3450);
    s_d3d.depth_surface[0]={(void*)0x910,0x3450,32,8,8,1,0,1};
    check(d3d12_prefix_current_gpu_alias(1,&retained),
        "retained vertex texture guard stays full regardless of FP mask");
    check_retained_b_drain(retained,"retained vertex texture depth alias drains B");

    start_retained_marker(retained,1u<<1);
    set_record_fragment(retained,1,0x1010);
    s_d3d.off_rt[0]={(void*)0x911,0x1000,8,4,1,8,4,64};
    wait_fail=1;
    check(d3d12_prefix_owner_before_upload(0,0,&retained)<0&&wait_calls==2&&
        waited_tickets[1]==18&&s_d3d.prefix_owners[1].phase==3&&
        s_d3d.prefix_vertex_prepare_sequence==s_d3d.prefix_owners[1].sequence&&
        s_d3d.prefix_owners[1].object_count==1&&s_d3d.prefix_owners[1].texture_count==1&&
        !s_d3d.prefix_owners[1].label_count&&release_b==0&&published_count==1&&
        published_labels[0].value==1010,
        "failed retained B wait preserves the marker and B refs");
}
static void test_vertex_prepare_queue_ahead_off(){
    check(!d3d12_prefix_queue_ahead_enabled()&&
        !d3d12_prefix_vertex_prepare_overlap_enabled(),
        "vertex-prepare overlap requires QueueAhead");
    configure_single_deferred_owner();set_current_fragment_alias();
    check(!d3d12_prefix_queue_ahead_record_bank_ready(1),
        "QueueAhead off disables vertex-only record-bank retirement");
    check(d3d12_prefix_owner_before_upload(0,1,NULL)==0&&wait_calls==1&&
        waited_tickets[0]==17&&s_d3d.prefix_owners[0].phase==0&&release_a==1&&
        !s_d3d.prefix_deferred_owner&&!s_d3d.fifo_prefix_outstanding&&
        s_d3d.fifo_prefix_serial==1&&!s_d3d.prefix_vertex_prepare_sequence,
        "QueueAhead off keeps the exact full wait before bank reuse");
}
static void test_prepare_alias_rejections(){
    reset_valid();main_readback=1;s_d3d.off_rt[0]={(void*)0x905,100,8,8,1,8,8,32};
    check(!d3d12_prefix_prepare_admit(),
        "direct-readback prepare rejects an exact packed OffRT fragment alias");
    reset_valid();main_readback=1;s_d3d.off_rt[0]={(void*)0x906,0x2000,8,4,1,8,4,64};
    s_d3d.draws[0].tex[0].raw=0x2010;
    check(!d3d12_prefix_prepare_admit(),
        "direct-readback prepare rejects a fragment inside packed OffRT backing span");
    reset_valid();main_readback=1;s_d3d.depth_surface[0]={(void*)0x907,0x3450,32,8,8,1,0,1};
    s_d3d.draws[0].tex[0].raw=0x3450;
    check(!d3d12_prefix_prepare_admit(),
        "direct-readback prepare rejects a depth surface fragment alias");
    reset_valid();main_readback=1;display_alias_raw=0x4560;s_d3d.draws[0].tex[0].raw=0x4560;
    check(!d3d12_prefix_prepare_admit(),
        "direct-readback prepare rejects a display/history fragment alias");
    reset_valid();main_readback=1;s_d3d.draws[0].vtex[0]={1,0x5670,1,8,8,1,0,32,2,1,0,0};
    s_d3d.draws[0].vtex_snapshot[0]=&snapshot_b;
    s_d3d.depth_surface[0]={(void*)0x908,0x5670,32,8,8,1,0,1};
    check(!d3d12_prefix_prepare_admit(),
        "direct-readback prepare rejects a vertex depth surface alias");
}
static void test_queue_ahead(){
    test_texture_gpu_alias();
    test_record_bank_readiness();
    test_record_bank_upload_path();
    test_vertex_prepare_overlap();
    test_prepare_alias_rejections();
    prefix_wait_trace_enabled=1;
    reset_valid();s_d3d.prefix_deferred_owner=0;
    check(d3d12_prefix_queue_ahead_prepare(RSX_COMPLETION_OTHER)==0&&
        d3d12_prefix_queue_ahead_prepare(RSX_COMPLETION_OTHER)==0&&
        s_tj_prefix_wait_trace.prepare.calls==2&&
        s_tj_prefix_wait_trace.prepare.outcomes[TJ_PREFIX_WAIT_PREPARE_NO_DEFERRED]==2&&
        s_tj_prefix_wait_trace.prepare.outcomes[TJ_PREFIX_WAIT_PREPARE_BUSY]==0,
        "prepare trace counts each call at its first no-deferred rejection");
    configure_record_bank_ready();s_d3d.draw_count=1;
    s_d3d.texture_label_count=1;s_exact_writeback_count=1;
    check(!d3d12_prefix_queue_ahead_record_bank_ready(0)&&
        !d3d12_prefix_queue_ahead_record_bank_ready(0)&&
        s_tj_prefix_wait_trace.record_bank.calls==2&&
        s_tj_prefix_wait_trace.record_bank.outcomes[TJ_PREFIX_WAIT_RECORD_UPLOAD_STATE]==2&&
        s_tj_prefix_wait_trace.record_bank.outcomes[TJ_PREFIX_WAIT_RECORD_LABELS]==0&&
        s_tj_prefix_wait_trace.record_bank.outcomes[TJ_PREFIX_WAIT_RECORD_WRITEBACK]==0,
        "record-bank trace counts only the first upload-state rejection per call");
    prefix_wait_trace_enabled=0;
    configure_pair_preflight();submit_pair_candidate();
    check(d3d12_prefix_queue_ahead_enabled(),"exact queue-ahead flag enables over queued-pair mode");
    const int deferred=d3d12_prefix_queued_pair_retire();
    /* The enclosing owned-render wrapper restores active context after the
     * helper returns; emulate that call-site boundary before later observers. */
    s_d3d.prefix_owner_active=0;
    check(deferred==0&&wait_calls==0&&
        s_d3d.prefix_owners[0].phase==2&&s_d3d.prefix_owners[1].phase==2&&
        s_d3d.prefix_deferred_owner==2&&!s_d3d.prefix_prepare_owner&&
        s_d3d.fifo_prefix_outstanding&&s_d3d.fifo_prefix_serial==0&&
        s_d3d.prefix_queued_pairs==1&&s_d3d.prefix_queue_ahead_deferred==1&&
        release_a==0&&release_b==0&&published_batches==0,
        "queued-pair mode defers both retirements and retains epoch state");
    const int reclaimed=d3d12_prefix_queue_ahead_retire_old(0,RSX_COMPLETION_OTHER);
    check(reclaimed==0&&wait_calls==1&&waited_tickets[0]==17&&
        s_d3d.prefix_owners[0].phase==0&&published_batches==1&&
        published_labels[0].value==1010&&release_a==1&&release_b==0,
        "bank reclaim waits and releases exact A only");
    check(s_d3d.prefix_owners[1].phase==2&&s_d3d.prefix_owners[1].object_count==1&&
        s_d3d.prefix_owners[1].texture_count==1&&s_d3d.prefix_owners[1].label_count==1,
        "bank reclaim retains newest B references and labels");
    check(s_d3d.prefix_deferred_owner==2&&s_d3d.fifo_prefix_outstanding&&
        s_d3d.fifo_prefix_serial==0&&s_d3d.prefix_queue_ahead_bank_drains==1,
        "bank reclaim preserves latest owner and outstanding epoch markers");

    configure_submitted_pair();
    check(d3d12_prefix_queue_ahead_prepare(RSX_COMPLETION_OTHER)==0&&wait_calls==1&&waited_tickets[0]==17&&
        s_d3d.prefix_owners[0].phase==0&&s_d3d.prefix_owners[1].phase==2&&
        s_d3d.prefix_deferred_owner==2&&s_d3d.fifo_prefix_serial==0,
        "pre-begin queue-ahead prepare reclaims the old parity bank");
    check(d3d12_prefix_prepare_admit()&&s_d3d.prefix_owner_active==0,
        "reclaimed slot permits admission before the next owner begins");

    configure_submitted_pair();
    check(d3d12_prefix_owner_object_mutable((void*)0x102)==0&&wait_calls==1&&
        waited_tickets[0]==17&&s_d3d.prefix_owners[0].phase==0&&
        s_d3d.prefix_owners[1].phase==2&&s_d3d.prefix_deferred_owner==2&&
        s_d3d.fifo_prefix_outstanding&&s_d3d.fifo_prefix_serial==0&&release_a==1&&release_b==0,
        "old-only mutation retires A and preserves latest B");

    configure_submitted_pair();
    check(d3d12_prefix_owner_object_mutable((void*)0x202)==0&&wait_calls==2&&
        waited_tickets[0]==17&&waited_tickets[1]==18&&
        s_d3d.prefix_owners[0].phase==0&&s_d3d.prefix_owners[1].phase==0&&
        published_batches==2&&published_labels[0].value==1010&&published_labels[1].value==2020&&
        !s_d3d.prefix_deferred_owner&&!s_d3d.fifo_prefix_outstanding&&
        s_d3d.fifo_prefix_serial==1&&release_a==1&&release_b==1,
        "newest-object mutation fully drains owners in sequence");

    configure_submitted_pair();s_d3d.prefix_owners[1].objects[1]=(void*)0x303;
    s_d3d.prefix_owners[1].object_count=2;s_d3d.prefix_owners[0].objects[1]=(void*)0x303;
    s_d3d.prefix_owners[0].object_count=2;
    check(d3d12_prefix_owner_object_mutable((void*)0x303)==0&&wait_calls==2&&
        waited_tickets[0]==17&&waited_tickets[1]==18&&
        !s_d3d.prefix_deferred_owner&&!s_d3d.fifo_prefix_outstanding&&
        s_d3d.fifo_prefix_serial==1&&release_a==1&&release_b==1,
        "shared-object mutation takes full ordered drain");

    configure_submitted_pair();wait_fail=1;
    check(d3d12_prefix_queue_ahead_retire_old(0,RSX_COMPLETION_OTHER)<0&&halts==1&&wait_calls==1&&
        s_d3d.prefix_owners[0].phase==3&&s_d3d.prefix_owners[1].phase==2&&
        s_d3d.prefix_owners[0].object_count==1&&s_d3d.prefix_owners[1].object_count==1&&
        s_d3d.prefix_owners[0].label_count==1&&s_d3d.prefix_owners[1].label_count==1&&
        s_d3d.prefix_deferred_owner==2&&s_d3d.fifo_prefix_outstanding&&
        s_d3d.fifo_prefix_serial==0&&release_a==0&&release_b==0&&published_batches==0,
        "failed old-ticket wait halts with both owners and labels retained");

    configure_submitted_pair();s_d3d.fence_values[0]=19;
    check(d3d12_prefix_queue_ahead_retire_old(0,RSX_COMPLETION_OTHER)==0&&wait_calls==1&&
        waited_tickets[0]==17&&s_d3d.prefix_owners[0].phase==0&&
        s_d3d.prefix_owners[1].phase==2&&s_d3d.prefix_owners[1].fence_value==18&&
        s_d3d.prefix_deferred_owner==2&&s_d3d.fifo_prefix_serial==0,
        "advanced global ticket still retires only exact old owner");

    configure_submitted_pair();s_d3d.fence_values[0]=17;s_exact_writeback_count=1;
    check(d3d12_prefix_queue_ahead_retire_old(0,RSX_COMPLETION_OTHER)==0&&wait_calls==2&&
        waited_tickets[0]==17&&waited_tickets[1]==18&&
        s_d3d.prefix_owners[0].phase==0&&s_d3d.prefix_owners[1].phase==0&&
        s_exact_writeback_count==1&&!s_d3d.prefix_deferred_owner&&
        !s_d3d.fifo_prefix_outstanding&&s_d3d.fifo_prefix_serial==1&&
        release_a==1&&release_b==1&&published_count==2,
        "stale global ticket falls back to full drain and preserves pending writeback plan");

    configure_submitted_pair();s_d3d.texture_label_armed=1;
    check(d3d12_prefix_queue_ahead_retire_old(0,RSX_COMPLETION_OTHER)==0&&wait_calls==2&&
        waited_tickets[0]==17&&waited_tickets[1]==18&&s_d3d.texture_label_armed==1&&
        !s_d3d.prefix_deferred_owner&&!s_d3d.fifo_prefix_outstanding&&
        s_d3d.fifo_prefix_serial==1&&release_a==1&&release_b==1,
        "armed-label soft gate drains both owners without dropping the armed callback");

    configure_submitted_pair();s_d3d.fifo_prefix_outstanding=0;
    check(d3d12_prefix_queue_ahead_retire_old(0,RSX_COMPLETION_OTHER)==0&&wait_calls==2&&
        waited_tickets[0]==17&&waited_tickets[1]==18&&
        !s_d3d.prefix_deferred_owner&&!s_d3d.fifo_prefix_outstanding&&
        s_d3d.fifo_prefix_serial==1&&release_a==1&&release_b==1,
        "missing outstanding marker uses original full drain");

    configure_submitted_pair();s_d3d.frame_index=1;
    check(d3d12_prefix_queue_ahead_retire_old(0,RSX_COMPLETION_OTHER)==0&&wait_calls==2&&
        waited_tickets[0]==17&&waited_tickets[1]==18&&
        !s_d3d.prefix_deferred_owner&&s_d3d.fifo_prefix_serial==1&&
        release_a==1&&release_b==1,
        "frame drift falls back to the checked full drain");

    configure_submitted_pair();s_d3d.fence=&fence_b;
    check(d3d12_prefix_queue_ahead_retire_old(0,RSX_COMPLETION_OTHER)<0&&halts==1&&wait_calls==0&&
        s_d3d.prefix_owners[0].phase==3&&s_d3d.prefix_owners[1].phase==2&&
        s_d3d.prefix_owners[0].object_count==1&&s_d3d.prefix_owners[1].object_count==1&&
        s_d3d.prefix_deferred_owner==2&&s_d3d.fifo_prefix_outstanding&&
        release_a==0&&release_b==0&&published_batches==0,
        "current-fence drift takes checked full-drain path and retains invalid-ticket owners");

    configure_submitted_pair();s_d3d.prefix_owners[1].completion_fence=&fence_b;
    check(d3d12_prefix_queue_ahead_retire_old(0,RSX_COMPLETION_OTHER)<0&&halts==1&&wait_calls==0&&
        s_d3d.prefix_owners[0].phase==2&&s_d3d.prefix_owners[1].phase==2&&
        release_a==0&&release_b==0,
        "different owner fences are structural corruption and halt without release");
}
int main(int argc,char** argv){
    const int expect=argc>1?atoi(argv[1]):0;
    const int expect_pair=argc>2?atoi(argv[2]):0;
    const int expect_ahead=argc>3?atoi(argv[3]):0;
    const int expect_overlap=argc>4?atoi(argv[4]):0;
    const int expect_ownership=argc>5?atoi(argv[5]):0;
    const int expect_cost=argc>6?atoi(argv[6]):0;
    const int expect_probe=argc>7?atoi(argv[7]):0;
    check(tj_prefix_cost_enabled()==expect_cost,"strict production prefix-cost gate");
    check(d3d12_prefix_ownership_enabled()==expect_ownership,"production ownership gate");
    check(d3d12_prefix_overlap_enabled()==expect_overlap,"production overlap gate");
    const int enabled=d3d12_prefix_prepare_enabled();
    const int pair_enabled=d3d12_prefix_queued_pair_enabled();
    const int ahead_enabled=d3d12_prefix_queue_ahead_enabled();
    check(enabled==expect,"strict prepare opt-in / overlap dependency");
    check(pair_enabled==expect_pair,"strict queued-pair opt-in / prepare dependency");
    check(ahead_enabled==expect_ahead,"strict queue-ahead opt-in / queued-pair dependency");
    reset_valid();
    check(d3d12_prefix_overlap_eligible()==expect_overlap,
        "prefix-cost gate does not change production overlap eligibility");
    test_retained_record_mask(expect_probe);
    if(expect_probe){
        printf("FP probe retained mask: %d checks, %d failures\n",checks,failures);
        return failures?1:0;
    }
    if(!enabled){reset_valid();check(!d3d12_prefix_prepare_admit(),"default-off admission");
        printf("Prefix replay prepare: %d checks, %d failures\n",checks,failures);return failures?1:0;}
    if(!ahead_enabled)test_vertex_prepare_queue_ahead_off();
    if(!pair_enabled){
        configure_pair_preflight();
        check(!d3d12_prefix_queued_pair_enabled()&&d3d12_prefix_prepare_retire()==0&&
            wait_calls==1&&waited_tickets[0]==17&&s_d3d.prefix_owners[0].phase==0&&
            s_d3d.prefix_owners[1].phase==1,
            "queued-pair default-off keeps original prepared-owner drain");
    }
    if(pair_enabled&&!ahead_enabled){
        configure_record_bank_ready();
        check(!d3d12_prefix_queue_ahead_record_bank_ready(0),
            "record-bank partial retirement stays disabled with QueueAhead off");
        reset_valid();
        s_d3d.off_rt[0]={(void*)0x904,100,8,8,1,8,8,32};
        check(d3d12_prefix_prepare_admit(),
            "queue-ahead-off admission preserves prior OffRT snapshot behavior");
    }
    reset_valid();main_readback=1;
    check(d3d12_prefix_prepare_admit(),
        "direct-readback mode permits ordinary exact-owner VP admission without a current plan");
    deny(wrong_parity,"next upload bank must be opposite owner");
    deny(next_slot_busy,"next upload owner must be free");
    deny(wrong_frame,"owner frame must match current frame");
    deny(wrong_counter,"owner ticket must equal exact frame fence counter");
    deny(wrong_fence,"owner ticket must use active fence");
    deny(wrong_sequence,"owner sequence must be current submit sequence");
    deny(wrong_retired,"owner sequence must be exact next after retired");
    deny(no_signal,"prior owner must have queued signal");
    deny(no_cpu_ready,"prior owner CPU retirement must be ready");
    deny(bad_fence_value,"removal sentinel ticket must reject");
    deny(epoch_closed,"closed main epoch must reject");
    deny(no_outstanding,"missing outstanding prefix must reject");
    deny(has_writeback,"pending MAIN writeback must reject");
    deny(has_label,"pending labels must reject");
    deny(has_armed_label,"armed label must reject");
    deny(has_scaled_blit,"scaled blit must reject in direct-readback mode");
    deny(has_gpu_input,"GPU-owned input must reject");
    deny(immutable_mirror,"immutable mirror must reject");
    deny(nonlocal_mirror,"nonlocal mirror must reject");
    deny(unknown_fragment_program,"unknown fragment program must reject");
    reset_valid();s_d3d.draws[0].is_clear=1;s_d3d.draws[0].fp_program_valid=0;
    check(d3d12_prefix_prepare_admit(),"owned clear has no fragment binding requirement");
    reset_valid();s_d3d.draws[0].tex[1].set=1;s_d3d.draws[0].tex_snapshot[1]=nullptr;
    check(d3d12_prefix_prepare_admit(),"unused fragment descriptor follows shared replay mask");

    D3D12PrepareTextureKey keys[4]{};u32 count=0;Tex same={1,77,1,8,8,1,0,32,2,1,0,0};
    check(d3d12_prefix_prepare_add_texture(keys,&count,4,&same,sizeof(same),&snapshot_a,0)==0&&count==1,
        "first resource key recorded");
    Tex copy=same;
    check(d3d12_prefix_prepare_add_texture(keys,&count,4,&copy,sizeof(copy),&snapshot_a,0)==0&&count==1,
        "identical snapshot key deduplicates");
    check(d3d12_prefix_prepare_add_texture(keys,&count,4,&copy,sizeof(copy),&snapshot_b,0)==0&&count==2,
        "different immutable snapshot remains distinct");
    check(d3d12_prefix_prepare_add_texture(keys,&count,4,&copy,sizeof(copy),&snapshot_a,1)==0&&count==3,
        "vertex binding kind remains distinct");

    configure_n_draws(5,0);check(d3d12_prefix_prepare_admit(),"five identical inputs need one upload slot");
    configure_n_draws(5,1);check(!d3d12_prefix_prepare_admit(),"five distinct inputs exceed four free upload slots");
    configure_n_draws(3,1);s_d3d.prefix_owners[0].textures[0].res=(void*)3;
    check(d3d12_prefix_prepare_admit(),"distinct inputs fit after exact owner-reserved slot subtraction");
    configure_n_draws(4,1);s_d3d.prefix_owners[0].textures[0].res=(void*)3;
    check(!d3d12_prefix_prepare_admit(),"owner-reserved slot cannot be counted as free");

    int old_obj=1,new_obj=2;
    reset_valid();s_d3d.prefix_prepare_owner=1;s_d3d.prefix_owner_active=2;
    s_d3d.prefix_owners[1].phase=1;s_d3d.prefix_owners[0].objects[0]=&old_obj;
    s_d3d.prefix_owners[0].object_count=1;
    check(d3d12_prefix_owner_object_mutable(&old_obj)==0&&wait_calls==1&&release_a==1&&
        !s_d3d.prefix_prepare_owner&&s_d3d.prefix_retired_sequence==9,
        "old owner mutation drains then rechecks released object");
    reset_valid();s_d3d.prefix_prepare_owner=1;s_d3d.prefix_owner_active=2;
    s_d3d.prefix_owners[1].phase=1;s_d3d.prefix_owners[1].objects[0]=&new_obj;
    s_d3d.prefix_owners[1].object_count=1;s_d3d.prefix_owners[0].objects[0]=&old_obj;
    s_d3d.prefix_owners[0].object_count=1;
    check(d3d12_prefix_owner_object_mutable(&new_obj)<0&&wait_calls==1&&release_a==1&&halts==1,
        "new owner object remains reserved and halts after old-owner drain");
    reset_valid();s_d3d.prefix_prepare_owner=1;s_d3d.prefix_owner_active=2;
    s_d3d.prefix_owners[1].phase=1;wait_fail=1;
    check(d3d12_prefix_prepare_before_wait()<0&&wait_calls==1&&
        s_d3d.prefix_prepare_owner==1&&s_d3d.prefix_deferred_owner==1&&
        s_d3d.prefix_owners[0].phase==3,
        "failed mid-wait drain retains exact old owner and prepare reservation");
    reset_valid();s_d3d.prefix_prepare_owner=1;s_d3d.prefix_owner_active=2;
    s_d3d.prefix_owners[1].phase=1;s_exact_writeback_count=1;
    check(d3d12_prefix_prepare_retire()<0&&wait_calls==0&&halts==1&&
        s_d3d.prefix_prepare_owner==1,"writeback invariant rejects retirement without dropping owner");
    if(pair_enabled&&!ahead_enabled)test_queued_pair();
    if(ahead_enabled)test_queue_ahead();
    printf("Prefix replay prepare: %d checks, %d failures\n",checks,failures);
    return failures?1:0;
}
'''.replace("FUNCTIONS", functions).replace("KEY_TYPE", prepare_key.group()) \
   .replace("PREPARE_REASON_ENUM", prepare_reasons.group()) \
   .replace("PREFIX_WAIT_ENUMS", wait_trace_enums).replace("PREFIX_WAIT_TYPES", wait_trace_types)

    compiler = shutil.which("clang++") or r"C:\Program Files\LLVM\bin\clang++.exe"
    with tempfile.TemporaryDirectory(prefix="tj-prefix-prepare-") as directory:
        cpp, exe = Path(directory) / "prepare.cpp", Path(directory) / "prepare.exe"
        cpp.write_text(source, encoding="utf-8")
        subprocess.run([compiler, "-std=c++17", "-D_CRT_SECURE_NO_WARNINGS",
                        "-Wno-unused-function", str(cpp), "-o", str(exe)], check=True)
        clean_env = dict(os.environ)
        for key in ("TJ_PREFIX_REPLAY_PREPARE", "TJ_PREFIX_QUEUED_PAIR",
                    "TJ_PREFIX_QUEUE_AHEAD", "TJ_PREFIX_OVERLAP", "TJ_PREFIX_OWNERSHIP",
                    "TJ_PREFIX_VERTEX_PREPARE_OVERLAP",
                    "TJ_PREFIX_COST", "TJ_GCM2D_PERF", "TJ_PREFIX_GPU_COST",
                    "TJ_MAIN_DIRECT_READBACK", "TJ_TEXTURE_RELEASE_NOWAIT",
                    "TJ_PREFIX_COST_NOWAIT"):
            clean_env.pop(key, None)
        for key in ("FP_FORCE", "TJ_FP_PROBE_T0", "TJ_FP_PROBE_T1",
                    "TJ_FP_PROBE_STAGEGRID", "TJ_FP_PROBE_LATEGRID"):
            clean_env.pop(key, None)
        def run(args, env):
            result = subprocess.run([str(exe), *args], env=env,
                                    check=False, capture_output=True, text=True)
            assert result.returncode == 0, result.stdout + result.stderr
            return result.stdout.strip(), result.stderr.strip()

        off_out, off_err = run(["0", "0", "0", "0", "0", "0"], dict(clean_env))
        assert "0 failures" in off_out and not off_err
        for value in ("", "0", "1x"):
            env = dict(clean_env, TJ_PREFIX_QUEUE_AHEAD=value,
                       TJ_PREFIX_QUEUED_PAIR="1", TJ_PREFIX_REPLAY_PREPARE="1",
                       TJ_PREFIX_OVERLAP="1", TJ_PREFIX_OWNERSHIP="1")
            out, _ = run(["1", "1", "0", "1", "1", "0"], env)
            assert "0 failures" in out
        # Each nested opt-in must preserve its dependency gate.
        run(["1", "0", "0", "1", "1", "0"], dict(clean_env,
            TJ_PREFIX_QUEUE_AHEAD="1", TJ_PREFIX_REPLAY_PREPARE="1",
            TJ_PREFIX_OVERLAP="1", TJ_PREFIX_OWNERSHIP="1"))
        run(["0", "0", "0", "1", "1", "0"], dict(clean_env,
            TJ_PREFIX_QUEUE_AHEAD="1", TJ_PREFIX_QUEUED_PAIR="1",
            TJ_PREFIX_OVERLAP="1", TJ_PREFIX_OWNERSHIP="1"))
        run(["0", "0", "0", "0", "1", "0"], dict(clean_env,
            TJ_PREFIX_QUEUE_AHEAD="1", TJ_PREFIX_QUEUED_PAIR="1",
            TJ_PREFIX_REPLAY_PREPARE="1", TJ_PREFIX_OWNERSHIP="1"))
        run(["0", "0", "0", "0", "0", "0"], dict(clean_env,
            TJ_PREFIX_QUEUE_AHEAD="1", TJ_PREFIX_QUEUED_PAIR="1",
            TJ_PREFIX_REPLAY_PREPARE="1", TJ_PREFIX_OVERLAP="1"))
        # Queue-ahead absent keeps the prior exact-A queued-pair behavior.
        pair_out, _ = run(["1", "1", "0", "1", "1", "0"], dict(clean_env,
            TJ_PREFIX_QUEUED_PAIR="1", TJ_PREFIX_REPLAY_PREPARE="1",
            TJ_PREFIX_OVERLAP="1", TJ_PREFIX_OWNERSHIP="1"))
        assert "0 failures" in pair_out
        print("Queued-pair only: " + pair_out)
        enabled_env = dict(clean_env, TJ_PREFIX_REPLAY_PREPARE="1",
                           TJ_PREFIX_QUEUED_PAIR="1", TJ_PREFIX_QUEUE_AHEAD="1",
                           TJ_PREFIX_OVERLAP="1", TJ_PREFIX_OWNERSHIP="1",
                           TJ_PREFIX_VERTEX_PREPARE_OVERLAP="1")
        for value in ("", "0", "1x"):
            off_vertex_env = dict(enabled_env)
            off_vertex_env["TJ_PREFIX_VERTEX_PREPARE_OVERLAP"] = value
            off_vertex_out, _ = run(["1", "1", "1", "1", "1", "0"], off_vertex_env)
            assert "0 failures" in off_vertex_out
        no_ahead_vertex_env = dict(clean_env, TJ_PREFIX_REPLAY_PREPARE="1",
                                   TJ_PREFIX_QUEUED_PAIR="1", TJ_PREFIX_QUEUE_AHEAD="0",
                                   TJ_PREFIX_OVERLAP="1", TJ_PREFIX_OWNERSHIP="1",
                                   TJ_PREFIX_VERTEX_PREPARE_OVERLAP="1")
        no_ahead_out, _ = run(["1", "1", "0", "1", "1", "0"], no_ahead_vertex_env)
        assert "0 failures" in no_ahead_out
        enabled_out, enabled_err = run(["1", "1", "1", "1", "1", "0"], enabled_env)
        print("Queue-ahead: " + enabled_out)
        probe_env = dict(enabled_env, TJ_FP_PROBE_T1="1")
        probe_out, _ = run(["0", "0", "0", "0", "1", "0", "1"], probe_env)
        assert "0 failures" in probe_out, probe_out
        cost_out, cost_err = run(["1", "1", "1", "1", "1", "1"],
                                 dict(enabled_env, TJ_PREFIX_COST="1"))
        assert cost_out == enabled_out and cost_err == enabled_err
        print("Queue-ahead + TJ_PREFIX_COST=1: " + cost_out)


if __name__ == "__main__":
    test_prefix_replay_prepare()
