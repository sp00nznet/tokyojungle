"""Compile actual host-present interval accounting with deterministic clocks."""
from pathlib import Path
import os
import re
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT.parent / "ps3recomp"


def function(text, signature):
    """Extract one C function while ignoring braces in comments and literals."""
    start = text.index(signature)
    body = text.index("{", start)
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


def run_case(exe, mode, fps, perf):
    env = dict(os.environ)
    env.pop("TJ_FPS_TRACE", None)
    env.pop("TJ_PERF_TRACE", None)
    env.pop("TJ_PRESENT_WORK_TRACE", None)
    if fps is not None:
        env["TJ_FPS_TRACE"] = fps
    if perf is not None:
        env["TJ_PERF_TRACE"] = perf
    result = subprocess.run([str(exe), mode], env=env, capture_output=True,
                            text=True, check=True)
    lines = [line for line in result.stderr.splitlines()
             if line.startswith("[TJ-PRESENT-INTERVAL]")]
    return result.stdout, lines


def fields(line):
    return dict(re.findall(r"(\w+)=([^ ]*)", line))


def main():
    backend = (RUNTIME / "libs/video/rsx_d3d12_backend.c").read_text(encoding="utf-8")
    work_types = "\n".join(type_declaration(backend, name) for name in (
        "TJPresentWorkTrace", "TJPresentWorkTicket"))
    work_signatures = (
        "static int tj_present_work_trace_enabled(",
        "static void tj_present_work_trace_reset(",
        "static void tj_present_work_trace_arm(",
        "static LONGLONG tj_present_work_trace_begin(",
        "static void tj_present_work_trace_cpu_finish(",
        "static void tj_present_work_trace_ticket(",
        "static void tj_present_work_trace_add(",
        "static void tj_present_work_trace_finish(",
        "static void tj_present_work_trace_report(",
    )
    work_funcs = "\n".join(function(backend, signature) for signature in work_signatures)
    signatures = (
        "static int tj_present_interval_trace_enabled(",
        "static void tj_present_interval_trace_init(",
        "static u64 tj_present_interval_nearest_rank_us(",
        "static void tj_present_interval_trace_report(",
        "static void tj_present_interval_trace_clear_window(",
        "static void tj_present_interval_trace_frame(",
    )
    funcs = work_funcs + "\n" + "\n".join(
        function(backend, signature) for signature in signatures)
    assert "if (tj_present_interval_trace_enabled())\n        tj_present_interval_trace_frame(rsx_survival_trace_is_armed());" in backend
    assert "tj_present_interval_trace_init();" in backend

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
typedef struct TJPresentIntervalTrace {
    u64 durations_us[TJ_PRESENT_INTERVAL_CAPACITY];
    u32 sample_count, invalid_count, overflow_count, baseline_count;
    LONGLONG previous_qpc;
    ULONGLONG window_start_ms;
    int have_previous, active;
} TJPresentIntervalTrace;
static TJPresentIntervalTrace s_tj_present_interval_trace;
static LARGE_INTEGER s_tj_present_frequency;
static int tj_present_interval_trace_enabled(void);
static unsigned qpf_calls, qpc_calls;
static ULONGLONG fake_ms;
static LONGLONG fake_qpc;
static LONGLONG fake_frequency = 1000;
static int qpf_ok = 1, qpc_ok = 1;
static int QueryPerformanceFrequency(LARGE_INTEGER* value) {
    ++qpf_calls; value->QuadPart = fake_frequency; return qpf_ok;
}
static int QueryPerformanceCounter(LARGE_INTEGER* value) {
    ++qpc_calls; value->QuadPart = fake_qpc; return qpc_ok;
}
static ULONGLONG GetTickCount64(void) { return fake_ms; }
FUNCTIONS
#define CHECK(c, label) do { if (!(c)) { fprintf(stderr,"FAIL: %s\n",label); return 1; } } while (0)
static void completed(int armed) {
    if (tj_present_interval_trace_enabled())
        tj_present_interval_trace_frame(armed);
}
int main(int argc, char** argv) {
    if (argc != 2) return 2;
    tj_present_interval_trace_init();
    CHECK(qpf_calls == 1, "QPC frequency was not initialized once");
    if (!strcmp(argv[1], "off")) {
        fake_ms=0; fake_qpc=1000; completed(1);
        fake_ms=100; fake_qpc=1100; completed(1);
        CHECK(!tj_present_interval_trace_enabled() && qpc_calls == 0,
              "default-off or detailed-trace-only mode sampled QPC");
        puts("PASS: QPC frequency initialized, default-off trace did not sample");
        return 0;
    }
    if (!strcmp(argv[1], "qpf_fail")) {
        qpf_ok=0; tj_present_interval_trace_init();
        CHECK(qpf_calls == 2 && s_tj_present_frequency.QuadPart == 0,
              "failed QPF call retained its positive output");
        fake_ms=0; completed(1);
        fake_ms=1; completed(1);
        fake_ms=2; completed(0);
        CHECK(qpc_calls == 0, "invalid QPC frequency still sampled the counter");
        puts("PASS: failed QPF initialization clears frequency and rejects samples");
        return 0;
    }
    CHECK(tj_present_interval_trace_enabled(), "light FPS trace did not enable intervals");
    const u64 ordered[] = {10000,20000,30000,40000,50000};
    CHECK(tj_present_interval_nearest_rank_us(ordered,5,50)==30000 &&
          tj_present_interval_nearest_rank_us(ordered,5,95)==50000 &&
          tj_present_interval_nearest_rank_us(ordered,5,99)==50000,
          "nearest-rank percentile selection is wrong");

    if (!strcmp(argv[1], "metrics")) {
        fake_ms=0;    fake_qpc=1000; completed(1); /* first clock stamp is baseline */
        fake_ms=100;  fake_qpc=1100; completed(1);
        fake_ms=1000; fake_qpc=2000; completed(1); /* first report boundary */
        fake_ms=1033; fake_qpc=2033; completed(1); /* previous stamp survived window */
        fake_ms=2000; fake_qpc=3000; completed(1);
        fake_ms=2033; fake_qpc=3033; completed(1);
        fake_ms=2050; completed(0); /* flush partial window on disarm */
        CHECK(qpc_calls == 6, "unexpected QPC sample count");
        puts("PASS: window boundaries preserve exact completion intervals");
        return 0;
    }
    if (!strcmp(argv[1], "invalid")) {
        fake_ms=0;  fake_qpc=1000; qpc_ok=0; completed(1);
        qpc_ok=1; fake_ms=10; fake_qpc=100; completed(1);
        fake_ms=20; fake_qpc=90; completed(1);  /* backward clock */
        fake_ms=30; fake_qpc=110; completed(1);
        fake_ms=40; completed(0);
        CHECK(qpc_calls == 4, "unexpected invalid-clock QPC count");
        puts("PASS: failed and backward clocks are counted and recover");
        return 0;
    }
    if (!strcmp(argv[1], "submicro")) {
        fake_frequency=2000000; tj_present_interval_trace_init();
        fake_ms=0; fake_qpc=100; completed(1);
        fake_ms=1; fake_qpc=101; completed(1); /* converts to zero microseconds */
        fake_ms=2; fake_qpc=103; completed(1);
        fake_ms=3; completed(0);
        CHECK(qpf_calls == 2 && qpc_calls == 3,
              "unexpected sub-microsecond clock sample count");
        puts("PASS: sub-microsecond conversion is invalid, later sample recovers");
        return 0;
    }
    if (!strcmp(argv[1], "overflow")) {
        fake_ms=0; fake_qpc=1000; completed(1);
        for (unsigned i=1; i<=513; ++i) {
            fake_ms=i; fake_qpc=1000+i; completed(1);
        }
        fake_ms=600; completed(0);
        CHECK(qpc_calls == 514, "unexpected overflow QPC count");
        puts("PASS: fixed sample buffer reports dropped intervals");
        return 0;
    }
    return 2;
}
'''.replace("WORK_TYPES", work_types).replace("FUNCTIONS", funcs)

    clang = shutil.which("clang") or r"C:\Program Files\LLVM\bin\clang.exe"
    with tempfile.TemporaryDirectory(prefix="tj-present-interval-") as temp:
        c = Path(temp) / "interval.c"
        exe = Path(temp) / "interval.exe"
        c.write_text(source, encoding="utf-8")
        subprocess.run([clang, "-std=c11", "-O2", "-D_CRT_SECURE_NO_WARNINGS",
                        str(c), "-o", str(exe)], check=True)

        stdout, lines = run_case(exe, "off", None, "1")
        assert not lines and "PASS:" in stdout

        stdout, lines = run_case(exe, "qpf_fail", "1", "0")
        assert len(lines) == 1, lines
        qpf_failure = fields(lines[0])
        assert (qpf_failure["samples"], qpf_failure["invalid"],
                qpf_failure["baseline"]) == ("0", "2", "0")
        assert "PASS:" in stdout

        stdout, lines = run_case(exe, "metrics", "1", "0")
        assert len(lines) == 3, lines
        first, second, partial = map(fields, lines)
        assert (first["window_ms"], first["samples"], first["baseline"],
                first["min_us"], first["p50_us"], first["p95_us"],
                first["p99_us"], first["max_us"], first["durations_us"]) == (
                    "1000", "2", "1", "100000", "100000", "900000",
                    "900000", "900000", "100000,900000")
        assert (second["window_ms"], second["samples"], second["baseline"],
                second["min_us"], second["p50_us"], second["p95_us"],
                second["p99_us"], second["max_us"], second["durations_us"]) == (
                    "1000", "2", "0", "33000", "33000", "967000",
                    "967000", "967000", "33000,967000")
        assert (partial["window_ms"], partial["samples"], partial["durations_us"]) == (
            "50", "1", "33000")
        assert "PASS:" in stdout

        stdout, lines = run_case(exe, "invalid", "1", "0")
        assert len(lines) == 1, lines
        invalid = fields(lines[0])
        assert (invalid["samples"], invalid["invalid"], invalid["baseline"],
                invalid["durations_us"]) == ("1", "2", "1", "20000")
        assert "PASS:" in stdout

        stdout, lines = run_case(exe, "submicro", "1", "0")
        assert len(lines) == 1, lines
        submicro = fields(lines[0])
        assert (submicro["samples"], submicro["invalid"], submicro["baseline"],
                submicro["durations_us"]) == ("1", "1", "1", "1")
        assert "PASS:" in stdout

        stdout, lines = run_case(exe, "overflow", "1", "0")
        assert len(lines) == 1, lines
        overflow = fields(lines[0])
        assert (overflow["samples"], overflow["invalid"], overflow["overflow"],
                overflow["baseline"], overflow["min_us"], overflow["max_us"]) == (
                    "512", "0", "1", "1", "1000", "1000")
        assert len(overflow["durations_us"].split(",")) == 512
        assert "PASS:" in stdout

    print("PASS: present interval telemetry, pooled samples, percentiles, windows, overflow, invalid clocks, and default-off")


if __name__ == "__main__":
    main()
