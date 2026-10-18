"""Native lifecycle/conservation proof for the observe-only per-WORK wait row."""
from pathlib import Path
import os
import re
import shutil
import subprocess
import tempfile
from test_prefix_wait_trace import definition
from test_present_interval_trace import type_declaration

ROOT = Path(__file__).resolve().parents[1]
VIDEO = ROOT.parent / "ps3recomp/libs/video"


def main():
    backend = (VIDEO / "rsx_d3d12_backend.c").read_text(encoding="utf-8")
    header = (VIDEO / "rsx_d3d12_backend.h").read_text(encoding="utf-8")
    a = header.index("typedef enum RsxD3D12CompletionReason")
    reasons = header[a:header.index("} RsxD3D12CompletionReason;", a) + len("} RsxD3D12CompletionReason;")]
    a = backend.index("enum {\n    TJ_PREFIX_WAIT_SYNC,")
    types = backend[a:backend.index("/* Exact opt-in.", a)]
    a = backend.index("static const char* const s_tj_completion_names")
    names = backend[a:backend.index("};", a) + 2]
    work_types = "\n".join(type_declaration(backend, n) for n in ("TJPresentWorkTrace", "TJPresentWorkTicket"))
    functions = "\n".join(definition(backend, s) for s in (
        "static int tj_prefix_wait_trace_enabled(",
        "static void tj_prefix_wait_frame_reset(",
        "static void tj_prefix_wait_frame_owner(",
        "static void tj_prefix_wait_frame_report(",
        "static void tj_prefix_wait_trace_owner(",
        "static void tj_present_work_trace_reset(",
        "static void tj_present_work_trace_arm(",
        "static void tj_present_work_trace_report(",
    ))
    # Both companions run before the existing raw owner totals are retired.
    report = definition(backend, "static void tj_present_work_trace_report(")
    assert report.index("tj_prefix_wait_frame_report(now_ms);") < report.index("trace->owner_wait_calls =")
    for signature in ("static void tj_present_work_trace_reset(", "static void tj_present_work_trace_arm("):
        assert "tj_prefix_wait_frame_reset();" in definition(backend, signature)
    for token in ("QueryPerformanceCounter", "QueryThreadCycleTime", "WaitForSingleObject", "GetCompletedValue"):
        assert token not in functions, token
    source = r'''
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
using u32=uint32_t; using u64=uint64_t; using DWORD=uint32_t;
using LONG=int32_t; using LONGLONG=int64_t; using ULONGLONG=uint64_t;
struct LARGE_INTEGER { LONGLONG QuadPart; };
static DWORD thread_id=1; static int thread_reads;
static DWORD GetCurrentThreadId(){++thread_reads;return thread_id;}
static LONG InterlockedExchange(volatile LONG* p,LONG v){LONG old=*p;*p=v;return old;}
static LONG InterlockedCompareExchange(volatile LONG* p,LONG v,LONG compare){LONG old=*p;if(old==compare)*p=v;return old;}
REASONS
WORK_TYPES
static TJPresentWorkTrace s_tj_present_work_trace;
static TJPresentWorkTicket s_tj_present_work_ticket;
static LARGE_INTEGER s_tj_present_frequency={3000000};
static int work_enabled=1,interval_enabled=1;
static int tj_present_work_trace_enabled(){return work_enabled;}
static int tj_present_interval_trace_enabled(){return interval_enabled;}
static void tj_prefix_wait_trace_report(ULONGLONG){}
NAMES
TRACE_TYPES
FUNCTIONS
#define CHECK(c) do{if(!(c)){fprintf(stderr,"FAIL line %d\n",__LINE__);return 1;}++checks;}while(0)
static int checks;
static void add(u32 site,u32 outcome,u64 ticks,int bad=0,int untimed=0){
    tj_prefix_wait_trace_owner(site,RSX_COMPLETION_OTHER,outcome,ticks,bad,untimed);
    if(outcome!=TJ_PREFIX_WAIT_PRECHECK_FAILURE){
        ++s_tj_present_work_trace.owner_wait_calls;
        s_tj_present_work_trace.owner_wait_ticks+=ticks;
    }
}
static void report(u64 generation){
    s_tj_present_work_trace.generation=generation;
    s_tj_present_work_trace.source=2;s_tj_present_work_trace.buffer_id=1;
    tj_present_work_trace_report(33333,2000+generation);
}
int main(int argc,char** argv){
    if(argc!=2)return 2;
    tj_present_work_trace_reset();tj_present_work_trace_arm(1);
    if(!tj_prefix_wait_trace_enabled()){
        CHECK(!s_tj_prefix_wait_frame.ready&&thread_reads==0);
        report(1);CHECK(thread_reads==0);puts("PASS OFF no thread or clock reads");return 0;
    }
    if(!strcmp(argv[1],"no_fps")){
        tj_present_work_trace_arm(0);interval_enabled=0;tj_present_work_trace_arm(1);
        CHECK(!s_tj_prefix_wait_frame.ready);report(1);puts("PASS no FPS gate");return 0;
    }
    // Old cumulative observations and disarmed waits must not leak into a new row.
    tj_present_work_trace_arm(0);
    tj_prefix_wait_trace_owner(TJ_PREFIX_WAIT_SYNC,RSX_COMPLETION_OTHER,TJ_PREFIX_WAIT_OK,1000,0,0);
    tj_present_work_trace_arm(1);
    add(TJ_PREFIX_WAIT_SYNC,TJ_PREFIX_WAIT_OK,20);
    add(TJ_PREFIX_WAIT_SYNC,TJ_PREFIX_WAIT_OK,10);
    add(TJ_PREFIX_WAIT_LOCAL_MAIN_READBACK,TJ_PREFIX_WAIT_OK,30);
    add(TJ_PREFIX_WAIT_ORDERED_FULL_DRAIN,TJ_PREFIX_WAIT_PRECHECK_FAILURE,0);
    add(TJ_PREFIX_WAIT_QUEUED_PAIR_RETIRE,TJ_PREFIX_WAIT_FENCE_FAILURE,7);
    add(TJ_PREFIX_WAIT_QUEUE_AHEAD_OLD_RETIRE,TJ_PREFIX_WAIT_POSTCHECK_FAILURE,9);
    report(101);
    CHECK(s_tj_present_work_trace.owner_wait_calls==0&&s_tj_present_work_trace.owner_wait_ticks==0);
    CHECK(s_tj_prefix_wait_trace.sites[TJ_PREFIX_WAIT_SYNC][RSX_COMPLETION_OTHER].wait_ticks==1030);
    add(TJ_PREFIX_WAIT_SYNC,TJ_PREFIX_WAIT_OK,7);report(102);
    tj_present_work_trace_arm(0);
    tj_prefix_wait_trace_owner(TJ_PREFIX_WAIT_SYNC,RSX_COMPLETION_OTHER,TJ_PREFIX_WAIT_OK,99,0,0);
    tj_present_work_trace_arm(1);report(103);
    add(TJ_PREFIX_WAIT_SYNC,TJ_PREFIX_WAIT_OK,0,1,1);
    s_tj_present_work_trace.cpu_clock_invalid=1;report(104);
    thread_id=2;add(TJ_PREFIX_WAIT_SYNC,TJ_PREFIX_WAIT_OK,5);report(105);
    CHECK(s_tj_prefix_wait_frame.thread_id==1); // A bad reporter cannot become the renderer.
    thread_id=1;report(106);add(TJ_PREFIX_WAIT_SYNC,TJ_PREFIX_WAIT_OK,3);report(107);
    ++s_tj_present_work_trace.owner_wait_calls;report(108); // Conservation failure.
    tj_present_work_trace_arm(0);tj_present_work_trace_arm(1);
    ++s_tj_prefix_wait_frame.sites[TJ_PREFIX_WAIT_SYNC][RSX_COMPLETION_OTHER].wait.calls;
    report(109); // Corrupt outcome conservation is invalid, never measured zero.
    s_tj_present_frequency.QuadPart=0;report(110);
    s_tj_present_frequency.QuadPart=3000000;
    s_tj_prefix_wait_frame.sites[TJ_PREFIX_WAIT_SYNC][RSX_COMPLETION_OTHER].wait.wait_ticks=UINT64_MAX;
    add(TJ_PREFIX_WAIT_SYNC,TJ_PREFIX_WAIT_OK,1);report(111);
    printf("PASS %d native conservation/lifecycle checks\n",checks);return 0;
}
'''.replace("REASONS", reasons).replace("WORK_TYPES", work_types).replace("NAMES", names).replace(
        "TRACE_TYPES", types).replace("FUNCTIONS", functions)
    compiler = shutil.which("clang++") or r"C:\Program Files\LLVM\bin\clang++.exe"
    with tempfile.TemporaryDirectory(prefix="tj-prefix-wait-frame-") as directory:
        cpp, exe = Path(directory)/"frame.cpp", Path(directory)/"frame.exe"
        cpp.write_text(source, encoding="utf-8")
        subprocess.run([compiler,"-std=c++17","-D_CRT_SECURE_NO_WARNINGS",str(cpp),"-o",str(exe)],check=True)
        for value in (None,"0","1x","1"):
            env = dict(os.environ)
            env.pop("TJ_PREFIX_WAIT_TRACE",None)
            if value is not None: env["TJ_PREFIX_WAIT_TRACE"]=value
            result = subprocess.run([str(exe),"normal"],env=env,capture_output=True,text=True,timeout=10)
            assert result.returncode==0,result.stdout+result.stderr
            rows=[dict(re.findall(r"(\w+)=([^ ]+)",line)) for line in result.stderr.splitlines()
                  if line.startswith("[TJ-PREFIX-WAIT-FRAME]")]
            if value != "1":
                assert not rows and "PASS OFF" in result.stdout
                continue
            assert len(rows)==11
            by_gen={int(row["generation"]):row for row in rows}
            first=by_gen[101]
            assert (first["valid"],first["calls"],first["precheck"],first["ticks"],first["owner_wait_n"])==("1","6","1","76","5")
            assert first["t_ms"]=="2101" and first["source"]=="2" and first["buffer"]=="1"
            assert "local_main_readback:other:1:30:1:0:0:0:0:0:30" in first["waits"]
            assert "sync:other:2:30:2:0:0:0:0:0:20" in first["waits"]
            assert by_gen[102]["ticks"]=="7" and by_gen[102]["calls"]=="1"
            assert by_gen[103]["valid"]=="1" and by_gen[103]["waits"]=="none"
            assert by_gen[104]["valid"]=="0" and by_gen[104]["clock_invalid"]=="1" and by_gen[104]["untimed"]=="1"
            assert by_gen[105]["valid"]=="0" and by_gen[105]["thread_mismatch"]=="1"
            assert by_gen[106]["valid"]=="0" and by_gen[107]["valid"]=="1"
            assert by_gen[108]["counts_valid"]=="0"
            assert by_gen[109]["counter_invalid"]=="1" and by_gen[109]["valid"]=="0"
            assert by_gen[110]["qpc_hz"]=="0" and by_gen[110]["valid"]=="0"
            assert by_gen[111]["overflow"]=="1" and by_gen[111]["valid"]=="0"
            assert first["entries"]=="5" and first["invalid"]=="0"
            no_fps=subprocess.run([str(exe),"no_fps"],env=env,capture_output=True,text=True,check=True)
            assert "[TJ-PREFIX-WAIT-FRAME]" not in no_fps.stderr
            # Companion identities must exactly match the untouched WORK stream.
            work=[dict(re.findall(r"(\w+)=([^ ]+)",line)) for line in result.stderr.splitlines()
                  if line.startswith("[TJ-PRESENT-WORK]")]
            assert len(work)==len(rows)
            for a,b in zip(rows,work):
                assert all(a[k]==b[k] for k in ("generation","source","buffer","t_ms"))
            print(result.stdout.strip())
    print("PASS strict OFF, clock-invalid, thread-mismatch, two-generation, rearm and WORK join checks")


if __name__=="__main__":
    main()
