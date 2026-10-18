"""Compile the actual present-thread timing helpers with fake QPF/QPC clocks."""
from pathlib import Path
import os
import re
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src/tj_present.cpp"


def production_helpers(text):
    start = text.index("/* TJ_FIFO_PERF also times present-thread segments.")
    end = text.index("/* TJ_PRESENT_HEARTBEAT=1:", start)
    return text[start:end]


def fields(line):
    return dict(re.findall(r"([A-Za-z_][A-Za-z0-9_-]*)=([^ ]*)", line))


def run_case(executable, mode, loop_trace=None, fifo_perf=None):
    env = dict(os.environ)
    env.pop("TJ_PRESENT_LOOP_TRACE", None)
    env.pop("TJ_FIFO_PERF", None)
    if loop_trace is not None:
        env["TJ_PRESENT_LOOP_TRACE"] = loop_trace
    if fifo_perf is not None:
        env["TJ_FIFO_PERF"] = fifo_perf
    result = subprocess.run([str(executable), mode], env=env, capture_output=True,
                            text=True, check=False)
    if result.returncode:
        raise AssertionError(
            f"{mode} failed ({result.returncode}):\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}")
    reports = [line for line in result.stderr.splitlines()
               if line.startswith("[TJ-PRESENT-PERF] start_ms=")]
    disabled = [line for line in result.stderr.splitlines()
                if line.startswith("[TJ-PRESENT-PERF] event=disabled")]
    return result.stdout, reports, disabled


def main():
    present = SOURCE.read_text(encoding="utf-8")
    helpers = production_helpers(present)
    assert 'getenv("TJ_PRESENT_LOOP_TRACE")' in helpers
    assert 'loop_trace && strcmp(loop_trace, "1") == 0' in helpers
    assert 'getenv("TJ_FIFO_PERF") ||' in helpers
    assert "tj_fifo_perf_elapsed_ns(perf, segment_begin)" in present
    assert "tj_fifo_perf_elapsed_ns(perf, loop_begin)" in present
    assert "tj_fifo_perf_now_ns(perf) -" not in present
    runtime = (ROOT.parent / "ps3recomp/libs/video/cellGcmSys.c").read_text(
        encoding="utf-8")
    assert 'enabled = getenv("TJ_FIFO_PERF") ? 1 : 0;' in runtime
    assert "TJ_PRESENT_LOOP_TRACE" not in runtime, \
        "host-only flag must not enable per-command FIFO group profiling"

    fixture = r'''
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <limits.h>
typedef struct { long long QuadPart; } LARGE_INTEGER;
static int qpf_ok=1, qpc_ok=1, qpf_calls=0, qpc_calls=0;
static long long fake_frequency=1000000, fake_qpc=1234567, fake_step=100;
static uint64_t fake_ms=1000;
static int QueryPerformanceFrequency(LARGE_INTEGER* value)
{
    ++qpf_calls;
    if (!qpf_ok) return 0;
    value->QuadPart=fake_frequency;
    return 1;
}
static int QueryPerformanceCounter(LARGE_INTEGER* value)
{
    ++qpc_calls;
    if (!qpc_ok) return 0;
    value->QuadPart=fake_qpc;
    if (fake_step > 0 && fake_qpc <= LLONG_MAX-fake_step)
        fake_qpc += fake_step;
    return 1;
}
static uint64_t GetTickCount64(void) { return fake_ms; }
HELPERS
#define CHECK(test,message) do { ++checks; if (!(test)) { \
    ++failures; fprintf(stderr,"FAIL: %s\n",message); } } while (0)
static unsigned checks=0, failures=0;

static int disabled_case(void)
{
    tj_fifo_perf_loop& state=tj_fifo_perf_loop_state();
    CHECK(state.enabled==-1,"disabled env should leave collector off");
    CHECK(qpf_calls==0 && qpc_calls==0,"disabled collector queried QPF/QPC");
    CHECK(tj_fifo_perf_now_ns(state)==0,"disabled now returned a timestamp");
    CHECK(tj_fifo_perf_elapsed_ns(state,123)==0,"disabled elapsed was nonzero");
    tj_fifo_perf_mark_present(state);
    CHECK(tj_fifo_perf_loop_report(state)==0,"disabled collector reported");
    CHECK(qpf_calls==0 && qpc_calls==0,"disabled helper later queried QPF/QPC");
    return 0;
}

static int valid_case(void)
{
    tj_fifo_perf_loop& state=tj_fifo_perf_loop_state();
    CHECK(state.enabled==1 && qpf_calls==1 && qpc_calls==0,
          "opt-in did not initialize only the frequency");
    CHECK(state.report_start_ms==1000,"report window missed host start time");
    uint64_t first=tj_fifo_perf_now_ns(state);
    CHECK(first==1234567000ULL,"QPC ticks were converted to nanoseconds incorrectly");

    uint64_t begin=tj_fifo_perf_now_ns(state);
    fake_qpc+=500;
    uint64_t elapsed=tj_fifo_perf_elapsed_ns(state,begin);
    CHECK(elapsed==600000ULL,"elapsed span did not include exact QPC delta");
    tj_fifo_perf_add_segment(state.sleep_ns,state.sleep_max_ns,elapsed);
    CHECK(state.sleep_ns==600000ULL && state.sleep_max_ns==600000ULL,
          "phase total/max did not record the measured duration");

    begin=tj_fifo_perf_now_ns(state);
    fake_qpc+=3400;
    elapsed=tj_fifo_perf_elapsed_ns(state,begin);
    CHECK(elapsed==3500000ULL,"second phase duration is wrong");
    tj_fifo_perf_add_segment(state.tick_present_ns,state.tick_present_max_ns,elapsed);

    uint64_t anchor=tj_fifo_perf_now_ns(state);
    state.next_report_ns=anchor+1000000000ULL;
    state.loops=3; state.ticks=2; state.drain1_ns=77000;
    state.fifo_kick_wakes=3; state.fifo_kick_timeouts=4; state.fifo_kick_fallbacks=5;
    fake_qpc+=fake_frequency;
    fake_step=2500;
    fake_ms=2000;
    uint64_t report_cost=tj_fifo_perf_loop_report(state);
    CHECK(report_cost==2500000ULL,"report walltime was not measured");
    CHECK(state.loops==0 && state.ticks==0 && state.sleep_ns==0 &&
          state.tick_present_ns==0 && state.drain1_ns==0 && state.report_ns==0 &&
          state.fifo_kick_wakes==0 && state.fifo_kick_timeouts==0 &&
          state.fifo_kick_fallbacks==0,
          "report did not reset the completed window");
    state.loops=1;
    state.loop_max_ns=report_cost;
    tj_fifo_perf_add_segment(state.report_ns,state.report_max_ns,report_cost);
    CHECK(state.report_ns==2500000ULL && state.report_max_ns==2500000ULL,
          "report cost was not carried into the next window");

    fake_qpc+=fake_frequency;
    fake_ms=3000;
    uint64_t next_cost=tj_fifo_perf_loop_report(state);
    CHECK(next_cost==2500000ULL,"second report duration was not measured");
    CHECK(state.loops==0 && state.report_ns==0 && state.report_max_ns==0,
          "second report did not clear its completed window");
    return 0;
}

static int bad_frequency_case(int query_succeeds)
{
    qpf_ok=query_succeeds;
    fake_frequency=query_succeeds ? 0 : 1000000;
    tj_fifo_perf_loop& state=tj_fifo_perf_loop_state();
    CHECK(state.enabled==-1,"bad or unavailable frequency did not disable tracing");
    CHECK(qpf_calls==1 && qpc_calls==0,"frequency failure sampled QPC");
    CHECK(tj_fifo_perf_now_ns(state)==0 && tj_fifo_perf_loop_report(state)==0,
          "frequency failure produced measurements");
    CHECK(qpc_calls==0,"disabled bad-frequency state retried QPC");
    return 0;
}

static int failed_qpc_case(void)
{
    tj_fifo_perf_loop& state=tj_fifo_perf_loop_state();
    qpc_ok=0;
    uint64_t value=tj_fifo_perf_now_ns(state);
    CHECK(value==0 && state.enabled==-1,"failed QPC did not fail closed");
    CHECK(tj_fifo_perf_elapsed_ns(state,UINT64_MAX)==0,
          "failed QPC produced an unsigned elapsed value");
    CHECK(tj_fifo_perf_loop_report(state)==0,"failed QPC emitted a report");
    tj_fifo_perf_mark_present(state);
    CHECK(qpc_calls==1,"disabled QPC failure was sampled again");
    return 0;
}

static int backward_qpc_case(void)
{
    tj_fifo_perf_loop& state=tj_fifo_perf_loop_state();
    uint64_t begin=tj_fifo_perf_now_ns(state);
    CHECK(begin>0,"initial QPC sample failed");
    fake_qpc=1;
    CHECK(tj_fifo_perf_elapsed_ns(state,begin)==0,
          "backward QPC produced an unsigned elapsed value");
    CHECK(state.enabled==-1,"backward QPC did not disable tracing");
    CHECK(tj_fifo_perf_loop_report(state)==0,"backward QPC emitted a report");
    CHECK(qpc_calls==2,"disabled backward clock was sampled again");
    return 0;
}

static int conversion_overflow_case(void)
{
    fake_frequency=1;
    fake_qpc=LLONG_MAX;
    tj_fifo_perf_loop& state=tj_fifo_perf_loop_state();
    CHECK(tj_fifo_perf_now_ns(state)==0 && state.enabled==-1,
          "nanosecond conversion overflow did not disable tracing");
    CHECK(tj_fifo_perf_elapsed_ns(state,1)==0 &&
          tj_fifo_perf_loop_report(state)==0,
          "conversion overflow generated a measurement/report");
    return 0;
}

static int report_overflow_case(void)
{
    fake_frequency=500000000;
    fake_qpc=LLONG_MAX-250000000;
    tj_fifo_perf_loop& state=tj_fifo_perf_loop_state();
    state.next_report_ns=1;
    CHECK(tj_fifo_perf_loop_report(state)==0 && state.enabled==-1,
          "report-deadline overflow did not fail closed");
    CHECK(tj_fifo_perf_now_ns(state)==0,"disabled overflow returned a timestamp");
    return 0;
}

int main(int argc,char** argv)
{
    if(argc!=2) return 2;
    const char* mode=argv[1];
    if(!strcmp(mode,"off") || !strcmp(mode,"off_after_loop_zero"))
        disabled_case();
    else if(!strcmp(mode,"valid")) valid_case();
    else if(!strcmp(mode,"zero_frequency")) bad_frequency_case(1);
    else if(!strcmp(mode,"qpf_failure")) bad_frequency_case(0);
    else if(!strcmp(mode,"qpc_failure")) failed_qpc_case();
    else if(!strcmp(mode,"backward")) backward_qpc_case();
    else if(!strcmp(mode,"conversion_overflow")) conversion_overflow_case();
    else if(!strcmp(mode,"report_overflow")) report_overflow_case();
    else return 2;
    if(failures) return 1;
    printf("PASS: %s (%u checks)\n",mode,checks);
    return 0;
}
'''.replace("HELPERS", helpers)

    compiler = shutil.which("clang++") or r"C:\Program Files\LLVM\bin\clang++.exe"
    with tempfile.TemporaryDirectory(prefix="tj-present-loop-trace-") as temp:
        source = Path(temp) / "fixture.cpp"
        executable = Path(temp) / "fixture.exe"
        source.write_text(fixture, encoding="utf-8")
        subprocess.run([compiler, "-std=c++17", "-O2", "-D_CRT_SECURE_NO_WARNINGS",
                        str(source), "-o", str(executable)], check=True)

        stdout, reports, disabled = run_case(executable, "off")
        assert not reports and not disabled and "PASS:" in stdout
        stdout, reports, disabled = run_case(executable, "off_after_loop_zero",
                                             loop_trace="0")
        assert not reports and not disabled and "PASS:" in stdout

        stdout, reports, disabled = run_case(executable, "valid", loop_trace="1")
        assert len(reports) == 2 and not disabled
        first, second = map(fields, reports)
        assert (first["start_ms"], first["end_ms"], first["loops"], first["ticks"],
                first["sleep"], first["tick-present"], first["fifo-drain-1"],
                first["fifo-kick-wakes"], first["fifo-kick-timeouts"],
                first["fifo-kick-fallbacks"], first["report-prev"]) == (
                    "1000", "2000", "3", "2", "600us/600us",
                    "3500us/3500us", "77us/0us", "3", "4", "5", "0us/0us")
        assert (second["start_ms"], second["end_ms"], second["loops"],
                second["fifo-kick-wakes"], second["fifo-kick-timeouts"],
                second["fifo-kick-fallbacks"], second["report-prev"]) == (
                    "2000", "3000", "1", "0", "0", "0", "2500us/2500us")
        assert "PASS: valid" in stdout

        stdout, reports, disabled = run_case(executable, "valid", fifo_perf="0")
        assert len(reports) == 2 and not disabled and "PASS: valid" in stdout

        for mode in ("zero_frequency", "qpf_failure", "qpc_failure", "backward",
                     "conversion_overflow", "report_overflow"):
            stdout, reports, disabled = run_case(
                executable, mode, loop_trace="1")
            assert not reports, f"{mode} emitted a timed report: {reports}"
            assert len(disabled) == 1, f"{mode} did not emit one fail-closed marker: {disabled}"
            assert "PASS:" in stdout
            expected = ("invalid-frequency" if mode in ("zero_frequency", "qpf_failure")
                        else "clock-overflow" if mode in (
                            "conversion_overflow", "report_overflow")
                        else "invalid-clock")
            assert f"reason={expected}" in disabled[0], disabled[0]

    print("Present loop trace: 10 native cases passed; env, phase, report-window, and fail-closed wiring checked")


if __name__ == "__main__":
    main()
