"""Compile actual Present-work accounting with deterministic clocks."""
from pathlib import Path
import os
import re
import shutil
import subprocess
import tempfile

from test_present_interval_trace import function, type_declaration


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT.parent / "ps3recomp"


def fields(line):
    return dict(re.findall(r"(\w+)=([^ ]*)", line))


def run_case(exe, mode, fps, work):
    env = dict(os.environ)
    env.pop("TJ_FPS_TRACE", None)
    env.pop("TJ_PERF_TRACE", None)
    env.pop("TJ_PRESENT_WORK_TRACE", None)
    if fps is not None:
        env["TJ_FPS_TRACE"] = fps
    if work is not None:
        env["TJ_PRESENT_WORK_TRACE"] = work
    result = subprocess.run([str(exe), mode], env=env, capture_output=True,
                            text=True, check=True)
    lines = [line for line in result.stderr.splitlines()
             if line.startswith(("[TJ-PRESENT-WORK]", "[TJ-PRESENT-INTERVAL]"))]
    return result.stdout, lines


def main():
    backend = (RUNTIME / "libs/video/rsx_d3d12_backend.c").read_text(encoding="utf-8")
    work_types = "\n".join(type_declaration(backend, name) for name in (
        "TJPresentWorkTrace", "TJPresentWorkTicket", "TJPresentIntervalTrace"))
    work_signatures = (
        "static int tj_present_work_trace_enabled(",
        "static void tj_present_work_trace_reset(",
        "static void tj_present_work_trace_arm(",
        "static LONGLONG tj_present_work_trace_begin(",
        "static void tj_present_work_trace_cpu_finish(",
        "static LONGLONG tj_present_work_trace_sync_begin(",
        "static void tj_present_work_trace_sync_finish(",
        "static void tj_present_work_trace_wait_check(",
        "static void tj_present_work_trace_ticket(",
        "static void tj_present_work_trace_add(",
        "static void tj_present_work_trace_finish(",
        "static void tj_present_work_trace_report(",
    )
    interval_signatures = (
        "static int tj_present_interval_trace_enabled(",
        "static void tj_present_interval_trace_init(",
        "static u64 tj_present_interval_nearest_rank_us(",
        "static void tj_present_interval_trace_report(",
        "static void tj_present_interval_trace_clear_window(",
        "static void tj_present_interval_trace_frame(",
    )
    helpers = "\n".join(function(backend, signature) for signature in (
        *work_signatures, *interval_signatures))
    complete_prefix = function(backend, "int rsx_d3d12_backend_complete_fifo_prefix(")

    def production_function(signature):
        start = backend.find(signature)
        while start >= 0:
            body = backend.find("{", start)
            end_decl = backend.find(";", start)
            if body >= 0 and (end_decl < 0 or body < end_decl):
                return function(backend[start:], signature)
            start = backend.find(signature, start + len(signature))
        raise AssertionError("production function not found: " + signature)

    core = complete_prefix
    assert core.index("if (!s_d3d.initialized || s_d3d.gcm2d_backend_halted) return -1;") < core.index(
        "work_start = tj_present_work_trace_begin();")
    assert core.index("d3d12_publish_texture_labels();") < core.index(
        "tj_present_work_trace_cpu_finish(work_start, 1);") < core.rindex("return 0;")
    assert core.count("tj_present_work_trace_cpu_finish(work_start, 1);") == 1
    assert core.index("if (wait_for_gpu_checked() != 0)") < core.index(
        "tj_present_work_trace_cpu_finish(work_start, 1);")
    render = function(backend, "static void render_frame(")
    assert render.count("tj_present_work_trace_cpu_finish(work_final_start, 0);") == 1
    assert render.index("work_final_start = tj_present_work_trace_begin();") < render.index(
        "s_d3d.swap_chain->lpVtbl->Present(")
    present_call = render.index("s_d3d.swap_chain->lpVtbl->Present(")
    assert render.rfind("present_mark = tj_present_work_trace_sync_begin();", 0,
                        present_call) >= 0
    assert render.index("tj_present_work_trace_sync_finish(present_mark,", present_call) > present_call
    assert render.index("if (move_to_next_frame() != 0)") < render.index(
        "tj_present_work_trace_cpu_finish(work_final_start, 0);") < render.index(
            "tj_present_interval_trace_frame(rsx_survival_trace_is_armed());")

    assert "tj_present_work_trace_report(frame_duration_us, now_ms);" in backend
    checked_wait = production_function("static int wait_for_fence_value_checked(")
    assert "tj_present_work_trace_wait_check(completed < want);" in checked_wait
    assert checked_wait.index("tj_present_work_trace_wait_check(completed < want);") < checked_wait.index(
        "if (completed == UINT64_MAX)")

    def assert_sync_span(body, api_call, finish_marker, field):
        call = body.index(api_call)
        begin = body.rfind("sync_mark = tj_present_work_trace_sync_begin();", 0, call)
        finish = body.index(finish_marker, call)
        assert begin >= 0 and finish > call and field in body[finish:finish + 150]

    assert_sync_span(checked_wait, "SetEventOnCompletion(",
                     "tj_present_work_trace_sync_finish(sync_mark,", "fence_arm_ticks")
    assert_sync_span(checked_wait, "WaitForSingleObject(",
                     "tj_present_work_trace_sync_finish(sync_mark,", "fence_event_ticks")
    for signature in ("static int move_to_next_frame(", "static int signal_for_gpu_checked("):
        signal_body = production_function(signature)
        assert_sync_span(signal_body, "->Signal(",
                         "tj_present_work_trace_sync_finish(sync_mark,", "queue_signal_ticks")
    for signature, field in (
        ("static int wait_for_gpu_checked(", "global_wait_ticks"),
        ("static int d3d12_prefix_owner_wait(u32 slot,", "owner_wait_ticks"),
        ("static int move_to_next_frame(", "backbuffer_wait_ticks"),
    ):
        wait_body = production_function(signature)
        wait_call = wait_body.index("wait_for_fence_value_checked(")
        wait_begin = wait_body.rfind("wait_mark = tj_present_work_trace_sync_begin();", 0, wait_call)
        wait_finish = wait_body.index(
            "tj_present_work_trace_sync_finish(wait_mark, &s_tj_present_work_trace." + field,
            wait_call)
        assert wait_begin >= 0 and wait_finish > wait_call
    # Caller wait totals include their nested SetEvent/OS wait submetrics.
    # Keep the report categories separate; do not add those values together.
    render = function(backend, "static void render_frame(")
    assert ("work_ticket = s_tj_present_work_ticket;\n"
            "            memset(&s_tj_present_work_ticket, 0, sizeof(s_tj_present_work_ticket));") in render
    assert render.index("if (!wb_ok) {") < render.rindex(
        "tj_present_work_trace_add(work_rasters, work_blits, 1);")
    overlap = render[render.index('if (overlap_ready) {'):render.index('ExactWritebackFailure failure', render.index('if (overlap_ready) {'))]
    assert overlap.index('d3d12_prefix_owner_signal()') < overlap.index('tj_present_work_trace_add(')
    # No overlapping spans: pending waits finish before either outer timer starts.
    assert render.index('d3d12_prefix_overlap_drain(') < render.index('work_final_start = tj_present_work_trace_begin()')
    assert core.index('d3d12_prefix_overlap_drain(') < core.index('work_start = tj_present_work_trace_begin()')
    assert render.index("tj_present_work_trace_add(work_rasters, work_blits, 0);") < render.index(
        "tj_present_work_trace_finish(work_ticket,")
    work_counts = render[render.index("/* Completion is execution-backed."):render.index(
        "if (perf) {", render.index("/* Completion is execution-backed."))]
    assert "if (dr->display_color_produced)" in work_counts
    assert "if (work_trace && ds >= 0) ++work_rasters;" in work_counts
    assert "dr->scaled_blit_executed" in work_counts
    assert "dr->scaled_blit_dst_kind == GPU_BLIT_DISPLAY" in work_counts
    assert "if (work_trace) ++work_blits;" in work_counts

    source = r'''
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
typedef uint32_t u32;
typedef uint64_t u64;
typedef int64_t LONGLONG;
typedef uint64_t ULONGLONG;
typedef struct { LONGLONG QuadPart; } LARGE_INTEGER;
#define TJ_PRESENT_INTERVAL_CAPACITY 512u
WORK_TYPES
static TJPresentWorkTrace s_tj_present_work_trace;
static TJPresentWorkTicket s_tj_present_work_ticket;
static TJPresentIntervalTrace s_tj_present_interval_trace;
static LARGE_INTEGER s_tj_present_frequency;
static int tj_present_interval_trace_enabled(void);
static unsigned qpf_calls, qpc_calls;
static ULONGLONG fake_ms;
static LONGLONG fake_qpc;
static LONGLONG fake_frequency = 1000;
static int qpf_ok = 1, qpc_ok = 1;
typedef struct TestD3D {
    int initialized, gcm2d_backend_halted;
    u32 draw_count;
    int fifo_prefix_outstanding;
    u64 fifo_prefix_serial;
} TestD3D;
static TestD3D s_d3d;
static int survival_armed = 1;
static int mock_wait_result, mock_render_fail;
static LONGLONG mock_render_ticks = 10, mock_wait_ticks = 10,
                 mock_publish_ticks = 1;
static unsigned render_calls, wait_calls, publish_calls, halt_calls;
static void render_frame(u32 selected_buffer_id, u32 selected_display_raw,
                         int selected_display_valid, int fifo_prefix);
static int d3d12_prefix_ownership_enabled(void) { return 0; }
static void render_fifo_prefix_owned(void) { render_frame(0,0,0,1); }
static int wait_for_gpu_checked(void);
static int s_tj_completion_reason;
static int d3d12_prefix_overlap_drain(int) { return 0; }
static void gcm2d_backend_halt(const char* reason);
static void d3d12_publish_texture_labels(void);
static int rsx_survival_trace_is_armed(void);
static int QueryPerformanceFrequency(LARGE_INTEGER* value) {
    ++qpf_calls; value->QuadPart = fake_frequency; return qpf_ok;
}
static int QueryPerformanceCounter(LARGE_INTEGER* value) {
    ++qpc_calls; value->QuadPart = fake_qpc; return qpc_ok;
}
static ULONGLONG GetTickCount64(void) { return fake_ms; }
static void tj_prefix_wait_trace_report(ULONGLONG now_ms) { (void)now_ms; }
static void tj_prefix_wait_frame_reset(void) {}
static void tj_prefix_wait_frame_report(ULONGLONG now_ms) { (void)now_ms; }
FUNCTIONS
CORE_FUNCTION
#define CHECK(c, label) do { if (!(c)) { fprintf(stderr,"FAIL: %s\n",label); return 1; } } while (0)
static int sync_fields_clear(void) {
    TJPresentWorkTrace* t = &s_tj_present_work_trace;
    return !(t->fence_checks || t->fence_pending ||
        t->global_wait_calls || t->global_wait_ticks ||
        t->owner_wait_calls || t->owner_wait_ticks ||
        t->backbuffer_wait_calls || t->backbuffer_wait_ticks ||
        t->fence_arm_calls || t->fence_arm_ticks ||
        t->fence_event_calls || t->fence_event_ticks ||
        t->queue_signal_calls || t->queue_signal_ticks ||
        t->present_calls || t->present_ticks);
}
static void seed_sync_fields(void) {
    TJPresentWorkTrace* t = &s_tj_present_work_trace;
    t->fence_checks = t->fence_pending = 1;
    t->global_wait_calls = t->global_wait_ticks = 1;
    t->owner_wait_calls = t->owner_wait_ticks = 1;
    t->backbuffer_wait_calls = t->backbuffer_wait_ticks = 1;
    t->fence_arm_calls = t->fence_arm_ticks = 1;
    t->fence_event_calls = t->fence_event_ticks = 1;
    t->queue_signal_calls = t->queue_signal_ticks = 1;
    t->present_calls = t->present_ticks = 1;
}
static void completed(int armed) {
    if (tj_present_interval_trace_enabled())
        tj_present_interval_trace_frame(armed);
}
static int rsx_survival_trace_is_armed(void) { return survival_armed; }
static void render_frame(u32 selected_buffer_id, u32 selected_display_raw,
                         int selected_display_valid, int fifo_prefix) {
    (void)selected_buffer_id; (void)selected_display_raw;
    (void)selected_display_valid;
    ++render_calls;
    fake_qpc += mock_render_ticks;
    if (mock_render_fail) {
        s_d3d.gcm2d_backend_halted = 1;
        return;
    }
    if (fifo_prefix) {
        tj_present_work_trace_add(2, 3, 1);
        s_d3d.draw_count = 0;
        s_d3d.fifo_prefix_outstanding = 0;
    }
}
static int wait_for_gpu_checked(void) {
    ++wait_calls;
    fake_qpc += mock_wait_ticks;
    return mock_wait_result;
}
static void gcm2d_backend_halt(const char* reason) {
    (void)reason;
    ++halt_calls;
    s_d3d.gcm2d_backend_halted = 1;
}
static void d3d12_publish_texture_labels(void) {
    ++publish_calls;
    fake_qpc += mock_publish_ticks;
}
int main(int argc, char** argv) {
    if (argc != 2) return 2;
    tj_present_interval_trace_init();
    if (!strcmp(argv[1], "no_fps")) {
        tj_present_work_trace_arm(1);
        LONGLONG start = tj_present_work_trace_begin();
        tj_present_work_trace_cpu_finish(start, 1);
        LONGLONG sync_start = tj_present_work_trace_sync_begin();
        tj_present_work_trace_wait_check(1);
        tj_present_work_trace_sync_finish(sync_start,
            &s_tj_present_work_trace.fence_event_ticks,
            &s_tj_present_work_trace.fence_event_calls);
        s_d3d.initialized = 1; s_d3d.draw_count = 1;
        CHECK(rsx_d3d12_backend_complete_fifo_prefix() == 0,
              "work-only completion failed");
        completed(1);
        CHECK(tj_present_work_trace_enabled() && !tj_present_interval_trace_enabled(),
              "work-only trace did not stay independent of FPS reporting");
        CHECK(qpc_calls == 0 && s_tj_present_work_trace.prefix_calls == 0 &&
              !s_tj_present_work_trace.prefix_cpu_ticks &&
              !s_tj_present_work_trace.fence_checks &&
              !s_tj_present_work_trace.fence_pending &&
              !s_tj_present_work_trace.fence_event_calls &&
              !s_tj_present_work_trace.fence_event_ticks,
              "work-only diagnostics sampled QPC or accumulated without an FPS reporting path");
        puts("PASS: work-only timing stays gated with no FPS report path");
        return 0;
    }
    if (!strcmp(argv[1], "default_off")) {
        s_d3d.initialized = 1; s_d3d.draw_count = 1;
        CHECK(!tj_present_work_trace_enabled(), "work tracing enabled by default");
        CHECK(rsx_d3d12_backend_complete_fifo_prefix() == 0,
              "default-off completion failed");
        CHECK(qpc_calls == 0 && !s_tj_present_work_trace.prefix_calls &&
              !s_tj_present_work_trace.prefix_cpu_ticks,
              "default-off completion sampled QPC");
        puts("PASS: completion timing remains default-off");
        return 0;
    }
    if (!strcmp(argv[1], "timing")) {
        tj_present_work_trace_arm(1);
        s_d3d.initialized = 1; s_d3d.draw_count = 1;
        fake_qpc = 100; mock_render_ticks = 10;
        CHECK(rsx_d3d12_backend_complete_fifo_prefix() == 0,
              "first successful prefix failed");
        s_d3d.draw_count = 1; mock_render_ticks = 20;
        CHECK(rsx_d3d12_backend_complete_fifo_prefix() == 0,
              "second successful prefix failed");
        CHECK(s_tj_present_work_trace.prefix_calls == 2 &&
              s_tj_present_work_trace.replay_prefixes == 2 &&
              s_tj_present_work_trace.display_rasters == 4 &&
              s_tj_present_work_trace.display_blits == 6 &&
              s_tj_present_work_trace.prefix_cpu_ticks == 32,
              "multiple prefix calls/work or elapsed ticks did not accumulate");

        /* A successful no-draw/no-fence edge still costs a host call, but did
         * not replay a prefix. Keep its elapsed span separate from work count. */
        CHECK(rsx_d3d12_backend_complete_fifo_prefix() == 0,
              "successful no-op prefix completion failed");
        CHECK(s_tj_present_work_trace.prefix_calls == 3 &&
              s_tj_present_work_trace.replay_prefixes == 2 &&
              s_tj_present_work_trace.prefix_cpu_ticks == 33,
              "successful no-op completion was confused with replay work");

        LONGLONG final_start = tj_present_work_trace_begin();
        fake_qpc += 40;
        tj_present_work_trace_cpu_finish(final_start, 0);
        TJPresentWorkTicket ticket = {21, 2};
        tj_present_work_trace_finish(ticket, 3, 0x4321, 1, 1);
        fake_ms = 0; completed(1); /* baseline reports and retires these costs */
        CHECK(s_tj_present_work_trace.active &&
              !s_tj_present_work_trace.prefix_calls &&
              !s_tj_present_work_trace.prefix_cpu_ticks &&
              !s_tj_present_work_trace.final_cpu_ticks,
              "report did not reset CPU totals while preserving the active arm");

        s_d3d.draw_count = 1; mock_render_ticks = 7;
        CHECK(rsx_d3d12_backend_complete_fifo_prefix() == 0,
              "post-reset prefix failed");
        final_start = tj_present_work_trace_begin();
        fake_qpc += 9;
        tj_present_work_trace_cpu_finish(final_start, 0);
        tj_present_work_trace_finish(ticket, 3, 0x4321, 1, 1);
        fake_ms = 50; completed(1);
        CHECK(!s_tj_present_work_trace.prefix_calls &&
              !s_tj_present_work_trace.prefix_cpu_ticks &&
              !s_tj_present_work_trace.final_cpu_ticks,
              "second report leaked CPU totals into the next interval");
        puts("PASS: successful prefix timing accumulates, final timing is disjoint, report resets per-present totals");
        return 0;
    }
    if (!strcmp(argv[1], "failure")) {
        tj_present_work_trace_arm(1);
        s_d3d.initialized = 1; s_d3d.fifo_prefix_outstanding = 1;
        mock_wait_result = -1; fake_qpc = 1000;
        CHECK(rsx_d3d12_backend_complete_fifo_prefix() == -1,
              "failed outstanding fence was accepted");
        CHECK(s_tj_present_work_trace.prefix_calls == 0 &&
              s_tj_present_work_trace.prefix_cpu_ticks == 0,
              "failed checked wait was recorded as successful completion timing");

        s_d3d.gcm2d_backend_halted = 0; s_d3d.draw_count = 1;
        mock_render_fail = 1;
        CHECK(rsx_d3d12_backend_complete_fifo_prefix() == -1,
              "failed prefix replay was accepted");
        CHECK(s_tj_present_work_trace.prefix_calls == 0 &&
              s_tj_present_work_trace.prefix_cpu_ticks == 0 &&
              !s_tj_present_work_trace.replay_prefixes,
              "failed replay was recorded as successful prefix timing/work");
        puts("PASS: checked-wait and replay failures do not finish prefix timing");
        return 0;
    }
    if (!strcmp(argv[1], "clock_edges")) {
        tj_present_work_trace_arm(1);
        LONGLONG start;

        /* Failed begin is counted once; its zero token must not trigger a
         * second clock read when the matching completion hook runs. */
        qpc_ok = 0; fake_qpc = 100;
        start = tj_present_work_trace_begin();
        CHECK(start == 0 && s_tj_present_work_trace.cpu_clock_invalid == 1,
              "failed begin was not counted as an invalid clock");
        unsigned after_failed_begin = qpc_calls;
        tj_present_work_trace_cpu_finish(start, 1);
        CHECK(qpc_calls == after_failed_begin &&
              s_tj_present_work_trace.prefix_calls == 1,
              "zero begin token sampled again or lost successful-call count");

        /* End-query failure and a regressed QPC each count once and add no
         * elapsed ticks. */
        qpc_ok = 1; fake_qpc = 1000;
        start = tj_present_work_trace_begin();
        qpc_ok = 0;
        tj_present_work_trace_cpu_finish(start, 1);
        CHECK(s_tj_present_work_trace.cpu_clock_invalid == 2 &&
              s_tj_present_work_trace.prefix_calls == 2 &&
              !s_tj_present_work_trace.prefix_cpu_ticks,
              "failed end query did not count invalid without elapsed time");

        qpc_ok = 1; fake_qpc = 2000;
        start = tj_present_work_trace_begin();
        fake_qpc = 1999;
        tj_present_work_trace_cpu_finish(start, 1);
        CHECK(s_tj_present_work_trace.cpu_clock_invalid == 3 &&
              s_tj_present_work_trace.prefix_calls == 3 &&
              !s_tj_present_work_trace.prefix_cpu_ticks,
              "regressed end clock did not count invalid without elapsed time");

        fake_qpc = 3000;
        start = tj_present_work_trace_begin();
        fake_qpc = 3015;
        tj_present_work_trace_cpu_finish(start, 0);
        tj_present_work_trace_ticket(31, 2);
        TJPresentWorkTicket ticket = s_tj_present_work_ticket;
        tj_present_work_trace_finish(ticket, 4, 0x4321, 1, 0);
        fake_ms = 0; completed(1);
        CHECK(s_tj_present_work_trace.active &&
              !s_tj_present_work_trace.cpu_clock_invalid &&
              !s_tj_present_work_trace.prefix_calls &&
              !s_tj_present_work_trace.prefix_cpu_ticks &&
              !s_tj_present_work_trace.final_cpu_ticks,
              "work report did not reset clock errors and CPU totals");
        puts("PASS: failed begin/end and regressed clocks count once, retire, and reset");
        return 0;
    }
    if (!strcmp(argv[1], "disabled")) {
        tj_present_work_trace_arm(1);
        tj_present_work_trace_add(3, 4, 1);
        LONGLONG sync_start = tj_present_work_trace_sync_begin();
        tj_present_work_trace_wait_check(1);
        tj_present_work_trace_sync_finish(sync_start,
            &s_tj_present_work_trace.fence_arm_ticks,
            &s_tj_present_work_trace.fence_arm_calls);
        CHECK(qpc_calls == 0 && !s_tj_present_work_trace.fence_checks &&
              !s_tj_present_work_trace.fence_pending &&
              !s_tj_present_work_trace.fence_arm_calls &&
              !s_tj_present_work_trace.fence_arm_ticks,
              "disabled diagnostics sampled QPC or accumulated counters");
        fake_ms = 10; fake_qpc = 1000; completed(1);
        CHECK(!tj_present_work_trace_enabled() && qpc_calls == 1,
              "disabled work trace altered interval sampling");
        CHECK(s_tj_present_work_trace.display_rasters == 3 &&
              s_tj_present_work_trace.display_blits == 4,
              "disabled work helper unexpectedly retired its counters");
        puts("PASS: work telemetry is independently default-off");
        return 0;
    }
    if (!strcmp(argv[1], "diagnostics")) {
        tj_present_work_trace_arm(1);
        fake_qpc = 100;
        LONGLONG start = tj_present_work_trace_sync_begin();
        fake_qpc += 3;
        tj_present_work_trace_sync_finish(start,
            &s_tj_present_work_trace.fence_arm_ticks,
            &s_tj_present_work_trace.fence_arm_calls);

        start = tj_present_work_trace_sync_begin();
        fake_qpc += 5;
        tj_present_work_trace_sync_finish(start,
            &s_tj_present_work_trace.fence_event_ticks,
            &s_tj_present_work_trace.fence_event_calls);
        start = tj_present_work_trace_sync_begin();
        fake_qpc += 2;
        tj_present_work_trace_sync_finish(start,
            &s_tj_present_work_trace.fence_event_ticks,
            &s_tj_present_work_trace.fence_event_calls);

        start = tj_present_work_trace_sync_begin();
        fake_qpc += 7;
        tj_present_work_trace_sync_finish(start,
            &s_tj_present_work_trace.queue_signal_ticks,
            &s_tj_present_work_trace.queue_signal_calls);
        start = tj_present_work_trace_sync_begin();
        fake_qpc += 13;
        tj_present_work_trace_sync_finish(start,
            &s_tj_present_work_trace.global_wait_ticks,
            &s_tj_present_work_trace.global_wait_calls);
        start = tj_present_work_trace_sync_begin();
        fake_qpc += 17;
        tj_present_work_trace_sync_finish(start,
            &s_tj_present_work_trace.owner_wait_ticks,
            &s_tj_present_work_trace.owner_wait_calls);
        start = tj_present_work_trace_sync_begin();
        fake_qpc += 19;
        tj_present_work_trace_sync_finish(start,
            &s_tj_present_work_trace.backbuffer_wait_ticks,
            &s_tj_present_work_trace.backbuffer_wait_calls);
        start = tj_present_work_trace_sync_begin();
        fake_qpc += 11;
        tj_present_work_trace_sync_finish(start,
            &s_tj_present_work_trace.present_ticks,
            &s_tj_present_work_trace.present_calls);

        tj_present_work_trace_wait_check(0);
        tj_present_work_trace_wait_check(1);
        tj_present_work_trace_wait_check(1);
        start = tj_present_work_trace_sync_begin();
        fake_qpc = start - 1;
        tj_present_work_trace_sync_finish(start,
            &s_tj_present_work_trace.present_ticks,
            &s_tj_present_work_trace.present_calls);
        CHECK(s_tj_present_work_trace.fence_checks == 3 &&
              s_tj_present_work_trace.fence_pending == 2 &&
              s_tj_present_work_trace.fence_arm_calls == 1 &&
              s_tj_present_work_trace.fence_arm_ticks == 3 &&
              s_tj_present_work_trace.fence_event_calls == 2 &&
              s_tj_present_work_trace.fence_event_ticks == 7 &&
              s_tj_present_work_trace.queue_signal_calls == 1 &&
              s_tj_present_work_trace.queue_signal_ticks == 7 &&
              s_tj_present_work_trace.global_wait_calls == 1 &&
              s_tj_present_work_trace.global_wait_ticks == 13 &&
              s_tj_present_work_trace.owner_wait_calls == 1 &&
              s_tj_present_work_trace.owner_wait_ticks == 17 &&
              s_tj_present_work_trace.backbuffer_wait_calls == 1 &&
              s_tj_present_work_trace.backbuffer_wait_ticks == 19 &&
              s_tj_present_work_trace.present_calls == 2 &&
              s_tj_present_work_trace.present_ticks == 11 &&
              s_tj_present_work_trace.cpu_clock_invalid == 1,
              "API spans, pending checks, or regressed-clock accounting changed");
        CHECK(qpc_calls == 18 && !s_tj_present_work_trace.prefix_cpu_ticks &&
              !s_tj_present_work_trace.final_cpu_ticks,
              "sequential API spans sampled an unexpected clock or changed outer CPU totals");

        tj_present_work_trace_report(123, 7);
        CHECK(s_tj_present_work_trace.active && sync_fields_clear() &&
              !s_tj_present_work_trace.cpu_clock_invalid,
              "report did not reset diagnostic counters and elapsed spans");

        seed_sync_fields();
        tj_present_work_trace_arm(0);
        CHECK(!s_tj_present_work_trace.active && sync_fields_clear(),
              "disarm did not reset diagnostic counters and spans");
        tj_present_work_trace_arm(1);
        seed_sync_fields();
        tj_present_interval_trace_init();
        CHECK(!s_tj_present_work_trace.active && sync_fields_clear(),
              "trace initialization did not reset diagnostic counters");
        puts("PASS: fence checks, diagnostic clock spans, invalid clock, and resets");
        return 0;
    }
    if (strcmp(argv[1], "sequence")) return 2;

    tj_present_work_trace_arm(1);
    tj_present_work_trace_ticket(11, 2);
    TJPresentWorkTicket first = s_tj_present_work_ticket;
    tj_present_work_trace_add(2, 3, 1);
    tj_present_work_trace_add(4, 5, 1);
    tj_present_work_trace_finish(first, 4, 0x1234, 1, 0);
    fake_ms = 0; fake_qpc = 1000; completed(1); /* baseline still retires work */
    CHECK(s_tj_present_work_trace.active &&
          !s_tj_present_work_trace.display_rasters &&
          !s_tj_present_work_trace.display_blits &&
          !s_tj_present_work_trace.replay_prefixes,
          "baseline Present did not clear only accumulated work");
    CHECK(s_tj_present_work_trace.generation == 11 &&
          s_tj_present_work_trace.source == 2 &&
          s_tj_present_work_trace.buffer_id == 4 &&
          s_tj_present_work_trace.selected_raw == 0x1234,
          "work clear discarded frame identity");

    tj_present_work_trace_ticket(12, 1);
    TJPresentWorkTicket second = s_tj_present_work_ticket;
    tj_present_work_trace_add(7, 8, 1);
    tj_present_work_trace_finish(second, 5, 0x5678, 0, 1);
    fake_ms = 50; fake_qpc = 1100; completed(1);

    tj_present_work_trace_ticket(13, 3);
    TJPresentWorkTicket invalid = s_tj_present_work_ticket;
    tj_present_work_trace_add(9, 10, 1);
    tj_present_work_trace_finish(invalid, 6, 0x9ABC, 1, 1);
    fake_ms = 75; qpc_ok = 0; completed(1); /* invalid QPC still retires work */
    CHECK(!s_tj_present_work_trace.display_rasters &&
          !s_tj_present_work_trace.display_blits &&
          !s_tj_present_work_trace.replay_prefixes,
          "invalid QPC did not retire the successful Present's work");

    tj_present_work_trace_ticket(14, 4);
    TJPresentWorkTicket after_invalid = s_tj_present_work_ticket;
    tj_present_work_trace_add(1, 2, 0);
    tj_present_work_trace_finish(after_invalid, 7, 0xDEF0, 0, 0);
    qpc_ok = 1; fake_ms = 100; fake_qpc = 2000; completed(1);
    CHECK(s_tj_present_work_trace.active &&
          !s_tj_present_work_trace.display_rasters &&
          !s_tj_present_work_trace.display_blits,
          "baseline after invalid clock failed to preserve active trace");

    tj_present_work_trace_ticket(15, 5);
    TJPresentWorkTicket window_edge = s_tj_present_work_ticket;
    tj_present_work_trace_add(3, 4, 1);
    tj_present_work_trace_finish(window_edge, 8, 0x1111, 1, 1);
    fake_ms = 1000; fake_qpc = 3000; completed(1); /* reports interval window */
    CHECK(s_tj_present_work_trace.active &&
          !s_tj_present_work_trace.display_rasters &&
          !s_tj_present_interval_trace.sample_count &&
          s_tj_present_interval_trace.have_previous,
          "window clear reset work arm or lost interval boundary state");

    tj_present_work_trace_ticket(16, 6);
    TJPresentWorkTicket after_window = s_tj_present_work_ticket;
    tj_present_work_trace_add(5, 6, 1);
    tj_present_work_trace_finish(after_window, 9, 0x2222, 0, 1);
    fake_ms = 1050; fake_qpc = 3050; completed(1);

    tj_present_work_trace_add(99, 88, 7);
    tj_present_work_trace_arm(0);
    CHECK(!s_tj_present_work_trace.active &&
          !s_tj_present_work_trace.display_rasters &&
          !s_tj_present_work_trace.display_blits &&
          !s_tj_present_work_trace.replay_prefixes &&
          !s_tj_present_work_trace.generation &&
          !s_tj_present_work_trace.source,
          "disarm left old work or ticket identity behind");
    tj_present_work_trace_arm(1);
    CHECK(s_tj_present_work_trace.active &&
          !s_tj_present_work_trace.display_rasters &&
          !s_tj_present_work_trace.replay_prefixes,
          "re-arm leaked prior menu work");
    puts("PASS: work accumulation, metadata, invalid/baseline retirement, windows, and arm transitions");
    return 0;
}
'''.replace("WORK_TYPES", work_types).replace("FUNCTIONS", helpers).replace(
    "CORE_FUNCTION", complete_prefix)

    clang = shutil.which("clang") or r"C:\Program Files\LLVM\bin\clang.exe"
    with tempfile.TemporaryDirectory(prefix="tj-present-work-") as temp:
        c = Path(temp) / "work.c"
        exe = Path(temp) / "work.exe"
        c.write_text(source, encoding="utf-8")
        subprocess.run([clang, "-std=c11", "-O2", "-D_CRT_SECURE_NO_WARNINGS",
                        str(c), "-o", str(exe)], check=True)

        stdout, lines = run_case(exe, "no_fps", None, "1")
        assert not lines and "PASS:" in stdout

        stdout, lines = run_case(exe, "default_off", None, None)
        assert not lines and "PASS:" in stdout

        stdout, lines = run_case(exe, "timing", "1", "1")
        work_lines = [line for line in lines if line.startswith("[TJ-PRESENT-WORK]")]
        assert len(work_lines) == 2, work_lines
        timed_rows = list(map(fields, work_lines))
        assert (timed_rows[0]["replay_prefixes"], timed_rows[0]["prefix_calls"],
                timed_rows[0]["prefix_cpu_us"], timed_rows[0]["final_cpu_us"],
                timed_rows[0]["cpu_clock_invalid"], timed_rows[0]["duration_us"]) == (
                    "2", "3", "33000", "40000", "0", "0")
        assert (timed_rows[1]["replay_prefixes"], timed_rows[1]["prefix_calls"],
                timed_rows[1]["prefix_cpu_us"], timed_rows[1]["final_cpu_us"],
                timed_rows[1]["cpu_clock_invalid"], timed_rows[1]["duration_us"]) == (
                    "1", "1", "8000", "9000", "0", "17000")
        assert "PASS:" in stdout

        stdout, lines = run_case(exe, "diagnostics", "1", "1")
        work_lines = [line for line in lines if line.startswith("[TJ-PRESENT-WORK]")]
        assert len(work_lines) == 1, work_lines
        diagnostic_row = fields(work_lines[0])
        assert (diagnostic_row["fence_checks"], diagnostic_row["fence_pending"],
                diagnostic_row["global_wait_n"], diagnostic_row["global_wait_us"],
                diagnostic_row["owner_wait_n"], diagnostic_row["owner_wait_us"],
                diagnostic_row["backbuffer_wait_n"], diagnostic_row["backbuffer_wait_us"],
                diagnostic_row["fence_arm_n"], diagnostic_row["fence_arm_us"],
                diagnostic_row["fence_event_n"], diagnostic_row["fence_event_us"],
                diagnostic_row["queue_signal_n"], diagnostic_row["queue_signal_us"],
                diagnostic_row["present_n"], diagnostic_row["present_us"],
                diagnostic_row["cpu_clock_invalid"]) == (
                    "3", "2", "1", "13000", "1", "17000", "1", "19000",
                    "1", "3000", "2", "7000", "1", "7000", "2", "11000", "1")
        assert "PASS:" in stdout

        stdout, lines = run_case(exe, "failure", "1", "1")
        assert not lines and "PASS:" in stdout

        stdout, lines = run_case(exe, "clock_edges", "1", "1")
        work_lines = [line for line in lines if line.startswith("[TJ-PRESENT-WORK]")]
        assert len(work_lines) == 1, work_lines
        clock_row = fields(work_lines[0])
        assert (clock_row["prefix_calls"], clock_row["prefix_cpu_us"],
                clock_row["final_cpu_us"], clock_row["cpu_clock_invalid"],
                clock_row["duration_us"]) == ("3", "0", "15000", "3", "0")
        assert "PASS:" in stdout

        stdout, lines = run_case(exe, "disabled", "1", "0")
        assert not [line for line in lines if line.startswith("[TJ-PRESENT-WORK]")]
        assert len([line for line in lines if line.startswith("[TJ-PRESENT-INTERVAL]")]) == 0
        assert "PASS:" in stdout

        stdout, lines = run_case(exe, "sequence", "1", "1")
        work_lines = [line for line in lines if line.startswith("[TJ-PRESENT-WORK]")]
        interval_lines = [line for line in lines if line.startswith("[TJ-PRESENT-INTERVAL]")]
        assert len(work_lines) == 6, work_lines
        rows = list(map(fields, work_lines))
        assert (rows[0]["generation"], rows[0]["source"], rows[0]["buffer"],
                rows[0]["raw"], rows[0]["selected_copied"], rows[0]["selected_updated"],
                rows[0]["display_rasters"], rows[0]["display_blits"],
                rows[0]["replay_prefixes"], rows[0]["duration_us"]) == (
                    "11", "2", "4", "00001234", "1", "0", "6", "8", "2", "0")
        assert (rows[1]["generation"], rows[1]["source"], rows[1]["buffer"],
                rows[1]["raw"], rows[1]["selected_copied"], rows[1]["selected_updated"],
                rows[1]["display_rasters"], rows[1]["display_blits"],
                rows[1]["replay_prefixes"], rows[1]["duration_us"]) == (
                    "12", "1", "5", "00005678", "0", "1", "7", "8", "1", "100000")
        assert (rows[2]["generation"], rows[2]["display_rasters"],
                rows[2]["display_blits"], rows[2]["replay_prefixes"],
                rows[2]["duration_us"]) == ("13", "9", "10", "1", "0")
        assert (rows[3]["generation"], rows[3]["display_rasters"],
                rows[3]["display_blits"], rows[3]["replay_prefixes"],
                rows[3]["duration_us"]) == ("14", "1", "2", "0", "0")
        assert (rows[4]["generation"], rows[4]["display_rasters"],
                rows[4]["display_blits"], rows[4]["replay_prefixes"]) == (
                    "15", "3", "4", "1")
        assert (rows[5]["generation"], rows[5]["display_rasters"],
                rows[5]["display_blits"], rows[5]["replay_prefixes"],
                rows[5]["duration_us"]) == ("16", "5", "6", "1", "50000")
        assert len(interval_lines) == 1, interval_lines
        assert lines.index(work_lines[4]) < lines.index(interval_lines[0]), lines
        assert "PASS:" in stdout

    print("PASS: Present-work helper output, ticket metadata, CPU spans, success guards, per-present reset, and interval gate")


if __name__ == "__main__":
    main()
