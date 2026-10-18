"""Compile the production texture-release gate and completion path with fake GPU endpoints.

This proves the scoped fallback and checked-wait ordering in the backend; it
does not claim a real D3D12 device or in-game result.
"""
from pathlib import Path
import os
import re
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
VIDEO = ROOT.parent / "ps3recomp" / "libs" / "video"


def function(text, signature):
    """Return one actual C function, skipping a matching forward declaration."""
    start = 0
    while True:
        start = text.index(signature, start)
        brace = text.index("{", start)
        if text.find(";", start, brace) < 0:
            break
        start += len(signature)
    depth = 1
    end = brace + 1
    while depth:
        depth += (text[end] == "{") - (text[end] == "}")
        end += 1
    return text[start:end]


def enum(text):
    start = text.index("typedef enum RsxD3D12CompletionReason")
    end = text.index("} RsxD3D12CompletionReason;", start)
    return text[start:end + len("} RsxD3D12CompletionReason;")]


def main():
    backend_path = VIDEO / "rsx_d3d12_backend.c"
    header_path = VIDEO / "rsx_d3d12_backend.h"
    backend = backend_path.read_text(encoding="utf-8")
    header = header_path.read_text(encoding="utf-8")

    production = "\n".join(function(backend, signature) for signature in (
        "static int d3d12_prefix_ownership_enabled(void)",
        "static int tj_main_direct_readback_enabled(void)",
        "static int d3d12_prefix_overlap_enabled(void)",
        "static int d3d12_texture_label_batch_enabled(void)",
        "static int d3d12_texture_copy_label_batch_enabled(void)",
        "static int d3d12_texture_release_nowait_requested(void)",
        "static int d3d12_texture_release_nowait_cpu_cost_allowed(void)",
        "static void d3d12_texture_release_nowait_report(",
        "static int d3d12_texture_release_nowait_observer_blocked(void)",
        "static int d3d12_texture_release_nowait_ready(",
        "static int d3d12_texture_release_nowait_after_submit(",
        "static void d3d12_publish_texture_label_batch(",
        "static void d3d12_publish_texture_labels(void)",
        "int rsx_d3d12_backend_complete_fifo_prefix(void)",
        "int rsx_d3d12_backend_complete_fifo_prefix_at(",
        "int rsx_d3d12_backend_prepare_texture_label(void)",
    ))
    reasons = enum(header)

    render = function(backend, "static void render_frame(")
    execute = render.index("ExecuteCommandLists")
    outstanding = render.index("s_d3d.fifo_prefix_outstanding = 1;", execute)
    skip = render.index("d3d12_texture_release_nowait_after_submit(", outstanding)
    owner_complete = render.index("d3d12_prefix_owner_complete()", skip)
    assert execute < outstanding < skip < owner_complete
    assert render.index("return;", skip) < owner_complete
    opening = render.index("if (wait_for_gpu_checked() != 0)")
    for reuse in (
        "gpu_mirror_reset_batch()",
        "render_allocator->lpVtbl->Reset(render_allocator)",
        "display_surface_reclaim_stale()",
        "vp_texture_cache_reset_usage()",
    ):
        assert opening < render.index(reuse), f"opening wait must precede {reuse}"
    checked_wait = function(backend, "static int wait_for_gpu_checked(void)")
    assert checked_wait.index("signal_for_gpu_checked(&want)") < checked_wait.index(
        "wait_for_fence_value_checked(want, 6000)")
    shutdown = function(backend, "void rsx_d3d12_backend_shutdown(void)")
    assert shutdown.index("wait_for_gpu_checked()") < shutdown.index("d3d12_prefix_owner_destroy")
    complete = function(backend, "int rsx_d3d12_backend_complete_fifo_prefix(void)")
    edge_wait = complete.index("wait_for_gpu_checked()")
    assert edge_wait < complete.index("s_d3d.fifo_prefix_outstanding = 0;", edge_wait)
    assert edge_wait < complete.index("s_d3d.fifo_prefix_serial++", edge_wait)
    prepare = function(backend, "int rsx_d3d12_backend_prepare_texture_label(void)")
    request_set = prepare.index("s_d3d.texture_release_nowait_request =")
    request_run = prepare.index("rsx_d3d12_backend_complete_fifo_prefix_at(", request_set)
    request_clear = prepare.index("s_d3d.texture_release_nowait_request = 0;", request_run)
    assert request_set < request_run < request_clear
    ready = function(backend, "static int d3d12_texture_release_nowait_ready(")
    assert "tj_main_direct_readback_enabled()" in ready
    assert "d3d12_texture_release_nowait_observer_blocked()" in ready
    observer_body = function(backend, "static int d3d12_texture_release_nowait_observer_blocked(void)")
    observer_names = re.findall(r'"([A-Z0-9_]+)"', observer_body[observer_body.index("observers[]"):])

    fixture = r'''
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
typedef uint32_t u32; typedef uint64_t u64; typedef uint64_t ULONGLONG;
typedef int64_t LONGLONG;
PRODUCTION_REASONS
#define MAX_DRAWS 8
#define MAX_GPU_MIRRORS 4
typedef struct { u32 offset, value; } D3D12TextureLabel;
typedef struct { int is_vp, is_clear, is_scaled_blit, fp_program_valid;
    u32 tex_snapshot_fault_mask, vtex_snapshot_fault_mask; } D3D12DrawRecord;
typedef struct { int valid, local, immutable_source; } GpuMirror;
typedef struct {
    int initialized, gcm2d_backend_halted, fifo_prefix_outstanding;
    int texture_release_nowait_request, prefix_owner_active, prefix_overlap_request;
    u32 prefix_deferred_owner, texture_label_count, texture_label_flush_kind;
    int texture_label_armed, draw_count, dump_frames_left;
    u64 fifo_prefix_serial;
    D3D12DrawRecord draws[MAX_DRAWS + 1];
    GpuMirror gpu_mirror[MAX_GPU_MIRRORS];
    D3D12TextureLabel texture_labels[8];
} D3D12State;
static D3D12State s_d3d;
static const char* s_tj_texture_release_nowait_reject = "not_requested";
static u32 s_exact_writeback_count;
static int s_tj_completion_reason, survival_armed, indexed, compose, compose_capture;
static int backend_perf, prefix_cost, fixture_prefix_cost, trace_active, wait_calls, wait_fail_on;
static int renders, executes, opening_waits, reused_after_open, trailing_waits;
static int after_skips, exact_releases, trace_adds, resource_resets, failures, checks;
static int last_ready, recorded_request, write_count;
static u32 writes_addr[16], writes_value[16];
static u64 trace_rasters, trace_blits, trace_submits;
#define CHECK(c, m) do { ++checks; if (!(c)) { ++failures; fprintf(stderr,"FAIL %s:%d: %s\n",__FILE__,__LINE__,m); } } while (0)
static ULONGLONG GetTickCount64(void) { return 100; }
static void vm_write32(uint32_t a, uint32_t v) {
    if (write_count < 16) { writes_addr[write_count]=a; writes_value[write_count]=v; }
    ++write_count;
}
static int rsx_survival_trace_is_armed(void) { return survival_armed; }
static int tj_indexed_diag_has_records(void) { return indexed; }
static int tj_compose_target_enabled(void) { return compose; }
static int tj_compose_capture_enabled(void) { return compose_capture; }
static int tj_gcm2d_backend_perf_enabled(void) { return backend_perf; }
static int tj_prefix_cost_enabled(void) { return prefix_cost; }
static int tj_present_work_trace_enabled(void) { return trace_active; }
static void tj_present_work_trace_arm(int armed) { (void)armed; }
static LONGLONG tj_present_work_trace_begin(void) { return 1; }
static void tj_present_work_trace_cpu_finish(LONGLONG start, int submitted) { (void)start; (void)submitted; }
static void tj_present_work_trace_add(u64 rasters, u64 blits, u64 submitted) {
    ++trace_adds; trace_rasters += rasters; trace_blits += blits; trace_submits += submitted;
}
static void exact_writeback_release(void) { ++exact_releases; }
static int d3d12_prefix_overlap_drain(RsxD3D12CompletionReason reason) { (void)reason; return 0; }
static int d3d12_prefix_overlap_eligible(void) { return 0; }
static int d3d12_prefix_overlap_submit(int attach, RsxD3D12CompletionReason reason) {
    (void)attach; (void)reason; return -1;
}
static int d3d12_texture_prefix_can_batch(void) { return 0; }
static int wait_for_gpu_checked(void);
static int d3d12_prefix_owner_complete(void) { ++trailing_waits; return wait_for_gpu_checked(); }
static int wait_for_gpu_checked(void) {
    ++wait_calls; return wait_calls == wait_fail_on ? -1 : 0;
}
static void gcm2d_backend_halt(const char* why) { (void)why; s_d3d.gcm2d_backend_halted=1; }
static void render_frame(u32, u32, int, int);
static void render_fifo_prefix_owned(void) { render_frame(0,0,0,1); }
static int d3d12_texture_release_nowait_ready(int,int,int);
static int d3d12_texture_release_nowait_after_submit(int,int,u64,u64);
static void reset(void) {
    memset(&s_d3d,0,sizeof(s_d3d)); s_d3d.initialized=1;
    s_exact_writeback_count=0; survival_armed=1; indexed=compose=compose_capture=0;
    backend_perf=trace_active=0; prefix_cost=fixture_prefix_cost; wait_calls=wait_fail_on=0;
    renders=executes=opening_waits=reused_after_open=trailing_waits=after_skips=0;
    exact_releases=trace_adds=resource_resets=last_ready=recorded_request=write_count=0;
    trace_rasters=trace_blits=trace_submits=0; memset(writes_addr,0,sizeof(writes_addr)); memset(writes_value,0,sizeof(writes_value));
}
static void candidate(void) {
    s_d3d.draw_count=1; s_d3d.draws[0].is_vp=1; s_d3d.draws[0].fp_program_valid=1;
}
/* This endpoint mirrors the production order around the extracted helpers.
 * The actual render_frame order is asserted against source above. */
static void render_frame(u32 selected, u32 raw, int valid, int fifo_prefix) {
    (void)selected; (void)raw; (void)valid; ++renders;
    if (fifo_prefix && !s_d3d.draw_count && !s_d3d.fifo_prefix_outstanding) return;
    ++opening_waits;
    if (wait_for_gpu_checked()!=0) { gcm2d_backend_halt("fixture opening fence"); return; }
    if (s_d3d.fifo_prefix_outstanding) ++reused_after_open;
    s_d3d.fifo_prefix_outstanding=0;
    ++resource_resets;
    recorded_request=s_d3d.texture_release_nowait_request;
    last_ready=d3d12_texture_release_nowait_ready(fifo_prefix,backend_perf,0);
    s_d3d.draw_count=0;
    ++executes; s_d3d.fifo_prefix_outstanding=1;
    if (d3d12_texture_release_nowait_after_submit(last_ready,trace_active,3,2)) {
        ++after_skips; return;
    }
    if (d3d12_prefix_owner_complete()!=0) { gcm2d_backend_halt("fixture completion fence"); return; }
    s_d3d.fifo_prefix_outstanding=0; ++s_d3d.fifo_prefix_serial;
}
PRODUCTION
static void queue_old_label(u32 offset, u32 value) {
    s_d3d.texture_labels[s_d3d.texture_label_count++]=(D3D12TextureLabel){offset,value};
}
static void publish_current_label(u32 offset,u32 value) {
    /* Models the caller's direct label write after a successful prepare edge. */
    vm_write32(0x03000000u+offset,value);
}
static void check_dynamic_gates(void) {
    for (int why=0; why<17; ++why) {
        reset(); candidate(); s_d3d.texture_release_nowait_request=1;
        switch(why) {
        case 0: survival_armed=0; break;
        case 1: s_d3d.prefix_overlap_request=1; break;
        case 2: s_d3d.prefix_owner_active=1; break;
        case 3: s_d3d.prefix_deferred_owner=1; break;
        case 4: s_d3d.fifo_prefix_outstanding=1; break;
        case 5: s_d3d.draw_count=0; break;
        case 6: s_d3d.draw_count=MAX_DRAWS+1; break;
        case 7: s_d3d.texture_label_count=1; break;
        case 8: s_d3d.texture_label_armed=1; break;
        case 9: s_d3d.dump_frames_left=1; break;
        case 10: indexed=1; break;
        case 11: compose=1; break;
        case 12: compose_capture=1; break;
        case 13: backend_perf=1; break;
        case 14: prefix_cost=1; break;
        case 15: s_exact_writeback_count=1; break;
        case 16: s_d3d.gcm2d_backend_halted=1; break;
        }
        const int expected = why == 14
            ? d3d12_texture_release_nowait_cpu_cost_allowed()
            : 0;
        CHECK(d3d12_texture_release_nowait_ready(1,0,0)==expected,
              "unsafe state or CPU PrefixCost opt-in gate mismatch");
    }
    for (int why=0; why<7; ++why) {
        reset(); candidate(); s_d3d.texture_release_nowait_request=1;
        if (why==0) s_d3d.draws[0].is_clear=1, s_d3d.draws[0].fp_program_valid=0;
        if (why==1) s_d3d.draws[0].is_vp=0;
        if (why==2) s_d3d.draws[0].fp_program_valid=0;
        if (why==3) s_d3d.draws[0].is_scaled_blit=1;
        if (why==4) s_d3d.draws[0].tex_snapshot_fault_mask=1;
        if (why==5) s_d3d.draws[0].vtex_snapshot_fault_mask=1;
        if (why==6) s_d3d.gpu_mirror[0]=(GpuMirror){1,0,0};
        CHECK(d3d12_texture_release_nowait_ready(1,0,0)==(why==0),"record or MAIN mirror gate changed");
    }
    reset(); candidate(); s_d3d.texture_release_nowait_request=1;
    s_d3d.gpu_mirror[0]=(GpuMirror){1,1,1};
    CHECK(!d3d12_texture_release_nowait_ready(1,0,0),"immutable mirror admitted");
    reset(); candidate(); s_d3d.texture_release_nowait_request=1;
    CHECK(!d3d12_texture_release_nowait_ready(0,0,0),"non-FIFO replay admitted");
    reset(); candidate(); s_d3d.texture_release_nowait_request=1;
    CHECK(!d3d12_texture_release_nowait_ready(1,1,0),"backend performance argument admitted");
    reset(); candidate(); s_d3d.texture_release_nowait_request=1;
    CHECK(!d3d12_texture_release_nowait_ready(1,0,1),"GPU timing admitted");
    reset(); candidate(); s_d3d.texture_release_nowait_request=1;
    s_d3d.gpu_mirror[0]=(GpuMirror){1,1,0};
    CHECK(d3d12_texture_release_nowait_ready(1,0,0),"mutable local mirror was over-rejected");
}
static void check_consecutive_and_edges(void) {
    reset(); trace_active=1; candidate();
    CHECK(rsx_d3d12_backend_prepare_texture_label()==0,"first release failed");
    CHECK(recorded_request && last_ready && after_skips==1 && trailing_waits==0 &&
          s_d3d.fifo_prefix_outstanding && !s_d3d.texture_release_nowait_request,
          "first eligible release did not skip only trailing completion in scoped request");
    CHECK(write_count==0,"current guest label became visible before its release callback");
    publish_current_label(4,0x11223344);
    candidate();
    CHECK(rsx_d3d12_backend_prepare_texture_label()==0,"second release failed");
    CHECK(opening_waits==2 && reused_after_open==1 && after_skips==2 && trailing_waits==0 &&
          s_d3d.fifo_prefix_outstanding && resource_resets==2,
          "next opening fence did not retire first prefix before reuse");
    publish_current_label(8,0x55667788);
    CHECK(write_count==2 && writes_value[0]==0x11223344 && writes_value[1]==0x55667788,
          "labels were not published after each successful submit");
    const int renders_before= renders, executes_before=executes;
    const u64 serial_before=s_d3d.fifo_prefix_serial;
    s_d3d.draw_count=0;
    CHECK(rsx_d3d12_backend_complete_fifo_prefix()==0,"empty reference edge failed");
    CHECK(!s_d3d.fifo_prefix_outstanding && wait_calls==3 && renders==renders_before &&
          executes==executes_before && s_d3d.fifo_prefix_serial==serial_before+1,
          "empty CPU/reference edge did not retire outstanding submit without rendering");
    CHECK(trace_adds==2 && trace_rasters==6 && trace_blits==4 && trace_submits==2,
          "submitted work trace missed nowait prefixes");
}
static void check_opening_failure(void) {
    reset(); candidate();
    CHECK(rsx_d3d12_backend_prepare_texture_label()==0 && s_d3d.fifo_prefix_outstanding,
          "setup prefix did not remain in flight");
    publish_current_label(4,17);
    candidate(); queue_old_label(8,29);
    wait_fail_on=wait_calls+1;
    const int prior_writes=write_count, prior_resets=resource_resets;
    CHECK(rsx_d3d12_backend_prepare_texture_label()<0,"opening fence failure was hidden");
    CHECK(s_d3d.gcm2d_backend_halted && s_d3d.fifo_prefix_outstanding &&
          s_d3d.draw_count==1 && resource_resets==prior_resets &&
          s_d3d.texture_label_count==1 && write_count==prior_writes,
          "failed opening fence published a label or recycled retained state");
}
static void check_snapshot_fault_fallback(void) {
    reset(); candidate(); s_d3d.draws[0].tex_snapshot_fault_mask=1;
    CHECK(rsx_d3d12_backend_prepare_texture_label()==0 && !last_ready &&
          trailing_waits==1 && !s_d3d.fifo_prefix_outstanding,
          "unresolved texture snapshot fault skipped checked completion");
    CHECK(write_count==0,"snapshot-fault label published before checked completion");
    publish_current_label(12,41);
    CHECK(write_count==1 && writes_value[0]==41,"label callback lost after synchronous completion");
}
static void check_synchronous_path(void) {
    reset(); candidate();
    const int result=rsx_d3d12_backend_prepare_texture_label();
    CHECK(result==0 && after_skips==0 && trailing_waits==1 &&
          !s_d3d.fifo_prefix_outstanding && !s_d3d.texture_release_nowait_request,
          "disabled or excluded mode changed synchronous completion");
}
int main(int argc,char** argv) {
    const int expected_request=argc>1?atoi(argv[1]):0;
    const int expected_ready=argc>2?atoi(argv[2]):0;
    fixture_prefix_cost=argc>3?atoi(argv[3]):0;
    reset(); candidate();
    CHECK(d3d12_texture_release_nowait_requested()==expected_request,"option parse changed");
    s_d3d.texture_release_nowait_request=expected_request;
    CHECK(d3d12_texture_release_nowait_ready(1,0,0)==expected_ready,"production eligibility/config mismatch");
    if (expected_request && expected_ready) {
        check_dynamic_gates(); check_consecutive_and_edges(); check_opening_failure();
        check_snapshot_fault_fallback();
    } else check_synchronous_path();
    printf("nowait request=%d ready=%d: %d checks, %d failures\n",
           expected_request,expected_ready,checks,failures);
    return failures?1:0;
}
'''.replace("PRODUCTION_REASONS", reasons).replace("PRODUCTION", production)

    compiler = shutil.which("clang") or shutil.which("gcc") or r"C:\Program Files\LLVM\bin\clang.exe"
    with tempfile.TemporaryDirectory(prefix="tj-texture-release-nowait-") as directory:
        source = Path(directory) / "fixture.c"
        exe = Path(directory) / "fixture.exe"
        source.write_text(fixture.replace("PRODUCTION_REASONS", reasons).replace("PRODUCTION", production), encoding="utf-8")
        subprocess.run([compiler, "-std=c11", "-D_CRT_SECURE_NO_WARNINGS", str(source), "-o", str(exe)], check=True)
        controlled = {
            "TJ_TEXTURE_RELEASE_NOWAIT", "TJ_PREFIX_OWNERSHIP", "TJ_PREFIX_OVERLAP",
            "TJ_TEXTURE_LABEL_BATCH", "TJ_TEXTURE_COPY_LABEL_BATCH",
            "TJ_MAIN_DIRECT_READBACK", "TJ_PREFIX_COST", "TJ_PREFIX_COST_NOWAIT",
            "TJ_PREFIX_GPU_COST", *observer_names,
        }
        base = {key: value for key, value in os.environ.items() if key not in controlled}
        cases = [
            ("enabled", {"TJ_TEXTURE_RELEASE_NOWAIT": "1"}, 1, 1),
            ("disabled", {}, 0, 0),
            ("zero", {"TJ_TEXTURE_RELEASE_NOWAIT": "0"}, 0, 0),
            ("malformed", {"TJ_TEXTURE_RELEASE_NOWAIT": "1x"}, 0, 0),
            ("CPU PrefixCost defaults to synchronous", {
                "TJ_TEXTURE_RELEASE_NOWAIT": "1", "TJ_PREFIX_COST": "1"}, 1, 0),
            ("CPU PrefixCost override zero", {
                "TJ_TEXTURE_RELEASE_NOWAIT": "1", "TJ_PREFIX_COST": "1",
                "TJ_PREFIX_COST_NOWAIT": "0"}, 1, 0),
            ("CPU PrefixCost override wrong", {
                "TJ_TEXTURE_RELEASE_NOWAIT": "1", "TJ_PREFIX_COST": "1",
                "TJ_PREFIX_COST_NOWAIT": "true"}, 1, 0),
            ("CPU PrefixCost override malformed", {
                "TJ_TEXTURE_RELEASE_NOWAIT": "1", "TJ_PREFIX_COST": "1",
                "TJ_PREFIX_COST_NOWAIT": "1x"}, 1, 0),
            ("CPU PrefixCost explicit override", {
                "TJ_TEXTURE_RELEASE_NOWAIT": "1", "TJ_PREFIX_COST": "1",
                "TJ_PREFIX_COST_NOWAIT": "1"}, 1, 1),
            ("GPU PrefixCost blocks CPU override", {
                "TJ_TEXTURE_RELEASE_NOWAIT": "1", "TJ_PREFIX_COST": "1",
                "TJ_PREFIX_COST_NOWAIT": "1", "TJ_PREFIX_GPU_COST": "1"}, 1, 0),
            ("prefix ownership", {"TJ_TEXTURE_RELEASE_NOWAIT": "1", "TJ_PREFIX_OWNERSHIP": "1"}, 1, 0),
            ("prefix overlap", {"TJ_TEXTURE_RELEASE_NOWAIT": "1", "TJ_PREFIX_OWNERSHIP": "1", "TJ_PREFIX_OVERLAP": "1"}, 1, 0),
            ("label batch", {"TJ_TEXTURE_RELEASE_NOWAIT": "1", "TJ_TEXTURE_LABEL_BATCH": "1"}, 1, 0),
            ("copy batch", {"TJ_TEXTURE_RELEASE_NOWAIT": "1", "TJ_TEXTURE_COPY_LABEL_BATCH": "1"}, 1, 0),
            ("MAIN direct readback", {"TJ_TEXTURE_RELEASE_NOWAIT": "1", "TJ_MAIN_DIRECT_READBACK": "1"}, 1, 0),
        ]
        cases.extend((name, {"TJ_TEXTURE_RELEASE_NOWAIT": "1", name: "1"}, 1, 0)
                     for name in observer_names)
        for label, additions, requested, ready in cases:
            env = dict(base, **additions)
            prefix_cost = int(additions.get("TJ_PREFIX_COST") == "1")
            subprocess.run([str(exe), str(requested), str(ready), str(prefix_cost)],
                           env=env, check=True, timeout=10)
    print(f"PASS: production request/predicate/prepare/complete hooks; CPU-cost override variants; {len(observer_names)} observer flags; source ordering")
if __name__ == "__main__":
    main()
