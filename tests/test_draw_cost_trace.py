"""Compile renderer draw-cost helpers and check their production callsites."""
from pathlib import Path
import os
import re
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT.parent / "ps3recomp/libs/video/rsx_d3d12_backend.c"
HEADER = ROOT.parent / "ps3recomp/libs/video/rsx_d3d12_backend.h"


def function(text, signature):
    """Extract a C function while ignoring braces in comments and literals."""
    start = text.index(signature)
    while True:
        body = text.index("{", start)
        prototype_end = text.find(";", start)
        if prototype_end < 0 or body < prototype_end:
            break
        start = text.index(signature, start + len(signature))
    depth = 0
    i = body
    state = "code"
    while i < len(text):
        ch = text[i]
        nxt = text[i + 1] if i + 1 < len(text) else ""
        if state == "code":
            if ch == "/" and nxt == "/":
                state = "line_comment"
                i += 2
                continue
            if ch == "/" and nxt == "*":
                state = "block_comment"
                i += 2
                continue
            if ch == '"':
                state = "string"
            elif ch == "'":
                state = "char"
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return text[start:i + 1]
        elif state == "line_comment":
            if ch == "\n":
                state = "code"
        elif state == "block_comment":
            if ch == "*" and nxt == "/":
                state = "code"
                i += 2
                continue
        elif state in ("string", "char"):
            if ch == "\\":
                i += 2
                continue
            if (state == "string" and ch == '"') or (state == "char" and ch == "'"):
                state = "code"
        i += 1
    raise ValueError(f"unterminated function: {signature}")


def type_declaration(text, name):
    match = re.search(
        rf"typedef struct {re.escape(name)}\s*\{{.*?\}}\s*{re.escape(name)};",
        text, re.DOTALL)
    if not match:
        raise ValueError(f"missing struct typedef: {name}")
    return match.group(0)


def case_env(exe, mode, draw="1", work="1", fps="1"):
    env = dict(os.environ)
    for key in ("TJ_DRAW_COST_TRACE", "TJ_PRESENT_WORK_TRACE", "TJ_FPS_TRACE"):
        env.pop(key, None)
    if draw is not None:
        env["TJ_DRAW_COST_TRACE"] = draw
    if work is not None:
        env["TJ_PRESENT_WORK_TRACE"] = work
    if fps is not None:
        env["TJ_FPS_TRACE"] = fps
    return subprocess.run([str(exe), mode], env=env, capture_output=True,
                          text=True, check=True)


def main():
    backend = BACKEND.read_text(encoding="utf-8")
    header = HEADER.read_text(encoding="utf-8")
    work_type = type_declaration(backend, "TJPresentWorkTrace")
    signatures = (
        "static int tj_present_interval_trace_enabled(",
        "static int tj_present_work_trace_enabled(",
        "static int tj_draw_cost_trace_enabled(",
        "static LONGLONG tj_present_work_trace_begin(",
        "static LONGLONG tj_present_work_trace_sync_begin(",
        "static void tj_present_work_trace_sync_finish(",
        "static LONGLONG tj_draw_cost_trace_begin(",
        "static void tj_draw_cost_trace_vertex_finish(",
        "static void tj_draw_cost_trace_record_cb_finish(",
        "static void tj_present_work_trace_report(",
        "void rsx_d3d12_backend_set_close_observer(",
        "static void d3d12_notify_close_observer(",
    )
    funcs = "\n".join(function(backend, signature) for signature in signatures)
    observer_global = re.search(
        r"static void \(\*s_d3d12_close_observer\)\(void\);", backend).group(0)
    macro_start = backend.index("#define TJ_DRAW_COST_VERTEX_UPLOAD(")
    macro_end = backend.index("\n\n", macro_start)
    macro = backend[macro_start:macro_end]

    arrays = function(backend, "static void d3d12_draw_arrays(")
    indexed = function(backend, "static void d3d12_draw_indexed(")
    for name, body in (("arrays", arrays), ("indexed", indexed)):
        uploads = re.findall(r"\bupload_[A-Za-z0-9_]+\s*\(", body)
        assert len(uploads) == body.count("TJ_DRAW_COST_VERTEX_UPLOAD("), (
            f"{name}: every vertex conversion call must have one coarse draw scope")
        record_calls = body.count("vp_record_cb(")
        assert record_calls == body.count("LONGLONG record_cb_start = record_cb_trace")
        assert record_calls == body.count("tj_draw_cost_trace_record_cb_finish(")
        assert record_calls == len(re.findall(
            r"LONGLONG record_cb_start = record_cb_trace\s*\?\s*"
            r"tj_draw_cost_trace_begin\(\) : 0;\s*"
            r"vp_record_cb\([^;]+;\s*if \(record_cb_trace\)\s*"
            r"tj_draw_cost_trace_record_cb_finish\(", body))

    snapshot = function(backend, "static int vp_capture_tex_snapshot(")
    assert snapshot.count("vp_capture_tex_snapshot_impl(") == 2
    assert snapshot.count("tj_present_work_trace_sync_finish(draw_cost_start,") == 2
    assert "int draw_cost_trace = tj_draw_cost_trace_enabled();" in snapshot

    fp = function(backend, "static ID3D12PipelineState* vp_get_fp_pso(")
    assert fp.index("s_tj_perf_phase.fp_hash_ticks +=") < fp.index("fp_search_start")
    assert fp.index("fp_search_start") < fp.index("for (int i = 0; i < s_d3d.vp_fp_n; i++)")
    assert fp.index("for (int i = 0; i < s_d3d.vp_fp_n; i++)") < fp.index("static char hlsl[32768]")
    assert fp.count("fp_cache_search_ticks,") == 2

    wndproc = function(backend, "static LRESULT CALLBACK d3d12_wndproc_body(")
    close = wndproc[wndproc.index("case WM_CLOSE:"):wndproc.index("case WM_DESTROY:")]
    assert close.index("d3d12_notify_close_observer();") < close.index("DestroyWindow(hwnd);")
    assert "void rsx_d3d12_backend_set_close_observer(void (*observer)(void));" in header

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
WORK_TYPE
static TJPresentWorkTrace s_tj_present_work_trace;
static LARGE_INTEGER s_tj_present_frequency = {1000};
static int survival_armed = 1;
static int rsx_survival_trace_is_armed(void) { return survival_armed; }
static void tj_prefix_wait_trace_report(ULONGLONG now_ms) { (void)now_ms; }
static void tj_prefix_wait_frame_report(ULONGLONG now_ms) { (void)now_ms; }
static unsigned qpc_calls;
static int qpc_fail_on = -1, qpc_back_on = -1;
static LONGLONG clock_values[16] = {100,130,200,210,300,350};
static unsigned clock_index;
static int QueryPerformanceCounter(LARGE_INTEGER* value) {
    ++qpc_calls;
    if ((int)qpc_calls == qpc_fail_on) return 0;
    value->QuadPart = clock_values[clock_index++];
        if ((int)qpc_calls == qpc_back_on)
            value->QuadPart = clock_values[clock_index - 2] - 1;
    return 1;
}
FUNCTIONS
MACRO
static int close_calls;
static void close_callback(void) { ++close_calls; }
static u32 upload_nine(void) { return 9; }
static u32 upload_zero(void) { return 0; }
#define CHECK(c, label) do { if (!(c)) { fprintf(stderr,"FAIL: %s\n",label); return 1; } } while (0)
int main(int argc, char** argv) {
    if (argc != 2) return 2;
    s_tj_present_work_trace.active = 1;
    if (!strcmp(argv[1], "close")) {
        CHECK(s_d3d12_close_observer == NULL, "observer is not initially NULL");
        d3d12_notify_close_observer();
        CHECK(close_calls == 0, "default close observer was not inert");
        rsx_d3d12_backend_set_close_observer(close_callback);
        d3d12_notify_close_observer();
        CHECK(close_calls == 1, "registered close observer was not called");
        rsx_d3d12_backend_set_close_observer(NULL);
        d3d12_notify_close_observer();
        CHECK(close_calls == 1, "reset close observer was not inert");
        puts("PASS: default-null, registered, and reset close observer");
        return 0;
    }
    if (!strcmp(argv[1], "missing-fps")) survival_armed = 1;
    if (!strcmp(argv[1], "missing-work")) survival_armed = 1;
    if (!strcmp(argv[1], "unarmed")) survival_armed = 0;
    if (!strcmp(argv[1], "inactive")) s_tj_present_work_trace.active = 0;
    if (!strcmp(argv[1], "off") || !strcmp(argv[1], "bad-value") ||
        !strcmp(argv[1], "bad-zero") || !strcmp(argv[1], "bad-prefix") ||
        !strcmp(argv[1], "missing-fps") || !strcmp(argv[1], "missing-work") ||
        !strcmp(argv[1], "unarmed") ||
        !strcmp(argv[1], "inactive")) {
        u32 emitted = 0;
        TJ_DRAW_COST_VERTEX_UPLOAD(5, emitted, upload_nine());
        CHECK(qpc_calls == 0, "disabled diagnostic performed a QPC read");
        CHECK(s_tj_present_work_trace.vertex_convert_calls == 0 &&
              s_tj_present_work_trace.record_cb_calls == 0,
              "disabled diagnostic accumulated counters");
        puts("PASS: strict default-off gate performs zero QPC reads");
        return 0;
    }
    if (!strcmp(argv[1], "begin-fail")) qpc_fail_on = 1;
    if (!strcmp(argv[1], "end-fail")) qpc_fail_on = 2;
    if (!strcmp(argv[1], "backwards")) qpc_back_on = 2;
    if (!strcmp(argv[1], "begin-fail") || !strcmp(argv[1], "end-fail") ||
        !strcmp(argv[1], "backwards")) {
        u32 emitted = 0;
        TJ_DRAW_COST_VERTEX_UPLOAD(4, emitted, upload_zero());
        CHECK(s_tj_present_work_trace.vertex_convert_calls == 1 &&
              s_tj_present_work_trace.vertex_input_items == 4 &&
              s_tj_present_work_trace.vertex_convert_failures == 1,
              "conversion counts/failure were not retained across clock faults");
        CHECK(s_tj_present_work_trace.cpu_clock_invalid == 1,
              "invalid QPC sample was not counted");
        CHECK(s_tj_present_work_trace.vertex_convert_ticks == 0,
              "invalid QPC span produced elapsed ticks");
        puts("PASS: begin/end/backward QPC faults preserve counts and invalidate elapsed span");
        return 0;
    }
    CHECK(!strcmp(argv[1], "metrics"), "unknown mode");
    u32 emitted = 0;
    TJ_DRAW_COST_VERTEX_UPLOAD(5, emitted, upload_nine());
    TJ_DRAW_COST_VERTEX_UPLOAD(4, emitted, upload_zero());
    LONGLONG start = tj_draw_cost_trace_begin();
    tj_draw_cost_trace_record_cb_finish(start, 13);
    CHECK(qpc_calls == 6, "coarse spans did not use two QPC reads each");
    CHECK(s_tj_present_work_trace.draw_cost_trace == 1 &&
          s_tj_present_work_trace.vertex_convert_calls == 2 &&
          s_tj_present_work_trace.vertex_input_items == 9 &&
          s_tj_present_work_trace.vertex_emitted_vertices == 9 &&
          s_tj_present_work_trace.vertex_convert_failures == 1 &&
          s_tj_present_work_trace.vertex_convert_ticks == 40,
          "vertex counters or valid elapsed ticks are wrong");
    CHECK(s_tj_present_work_trace.record_cb_calls == 1 &&
          s_tj_present_work_trace.record_cb_vertices == 13 &&
          s_tj_present_work_trace.record_cb_ticks == 50,
          "record callback counters or valid elapsed ticks are wrong");
    tj_present_work_trace_report(1000, 2000);
    puts("PASS: draw counters, nested spans, valid QPC timing, and WORK fields");
    return 0;
}
'''.replace("WORK_TYPE", work_type).replace("FUNCTIONS", funcs).replace("MACRO", macro)
    source = source.replace("static LARGE_INTEGER s_tj_present_frequency = {1000};",
                            "static LARGE_INTEGER s_tj_present_frequency = {1000};\n" + observer_global)

    clang = shutil.which("clang") or r"C:\Program Files\LLVM\bin\clang.exe"
    with tempfile.TemporaryDirectory(prefix="tj-draw-cost-") as temp:
        c = Path(temp) / "draw_cost.c"
        exe = Path(temp) / "draw_cost.exe"
        c.write_text(source, encoding="utf-8")
        subprocess.run([clang, "-std=c11", "-O2", "-D_CRT_SECURE_NO_WARNINGS",
                        "-Wall", "-Werror=format",
                        str(c), "-o", str(exe)], check=True)

        for mode, draw, work, fps in (
            ("off", None, "1", "1"), ("bad-value", "true", "1", "1"),
            ("bad-zero", "0", "1", "1"), ("bad-prefix", "1x", "1", "1"),
            ("missing-fps", "1", "1", None), ("unarmed", "1", "1", "1"),
            ("missing-work", "1", None, "1"),
            ("inactive", "1", "1", "1"),
        ):
            result = case_env(exe, mode, draw, work, fps)
            assert "PASS:" in result.stdout

        for mode in ("begin-fail", "end-fail", "backwards"):
            result = case_env(exe, mode, "1", "1", "1")
            assert "PASS:" in result.stdout

        result = case_env(exe, "metrics", "1", "1", "1")
        row = next(line for line in result.stderr.splitlines()
                   if line.startswith("[TJ-PRESENT-WORK]"))
        fields = dict(re.findall(r"(\w+)=([^ ]*)", row))
        assert fields["draw_cost_trace"] == "1"
        assert (fields["vertex_convert_n"], fields["vertex_input_items"],
                fields["vertex_emitted"], fields["vertex_convert_failures"],
                fields["vertex_convert_us"]) == ("2", "9", "9", "1", "40000")
        assert (fields["record_cb_n"], fields["record_cb_vertices"],
                fields["record_cb_us"]) == ("1", "13", "50000")
        assert "texture_snapshot_bytes=" in row and "fp_cache_search_us=" in row

        result = case_env(exe, "close", None, None, None)
        assert "PASS:" in result.stdout

    print("PASS: draw-cost opt-in, zero-QPC off path, counters, QPC faults, callsite coverage, WORK fields, and close observer")


if __name__ == "__main__":
    main()
