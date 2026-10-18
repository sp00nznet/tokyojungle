"""Compile actual FIFO cost aggregation; no game, waits, or extra timing calls."""
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

from test_fifo_present_boundary import function

ROOT = Path(__file__).resolve().parents[1]


def main():
    backend = (ROOT.parent / "ps3recomp/libs/video/cellGcmSys.c").read_text(encoding="utf-8")
    types = "\n".join(re.search(r"typedef struct " + name + r" \{.*?\} " + name + ";", backend, re.S).group()
                      for name in ("TjFifoPerfGroup", "TjFifoPerf", "TjFifoPerfCall"))
    cap = re.search(r"#define TJ_FIFO_PERF_GROUPS .*", backend).group()
    bodies = "\n".join(function(backend, prefix) for prefix in (
        "static void tj_fifo_perf_group_add(", "static u32 tj_fifo_perf_top_group(",
        "static int tj_fifo_perf_add("))
    fast = function(backend, "static void gcm_rsx_process_fifo_unlocked_fast(void)\n{")
    profiled = function(backend, "static void gcm_rsx_process_fifo_unlocked_profiled(TjFifoPerfCall* perf_call)\n{")
    assert "tj_fifo_perf_group_add(" not in fast and "get_timestamp_ns(" not in fast
    assert profiled.count("tj_fifo_perf_group_add(") == 1
    assert "method_group_calls, elapsed);" in profiled
    assert "get_timestamp_ns(" not in function(backend, "static void tj_fifo_perf_group_add(")
    default_dispatch = function(backend, "void cellGcm_rsx_process_fifo(")
    assert "TjFifoPerf snapshot" not in default_dispatch and "get_timestamp_ns(" not in default_dispatch
    profiled_prefix = backend.split("static void gcm_rsx_process_fifo_profiled(void)", 1)[0][-220:]
    assert "__declspec(noinline)" in profiled_prefix and "__attribute__((noinline))" in profiled_prefix
    fixture = r'''
#include <stdint.h>
#include <stdio.h>
#include <string.h>
typedef uint32_t u32;
typedef uint64_t u64;
typedef uint8_t u8;
CAP
TYPES
enum { TJ_FIFO_EXIT_CAUGHT_UP, TJ_FIFO_EXIT_FLIP, TJ_FIFO_EXIT_IDLE,
       TJ_FIFO_EXIT_EA, TJ_FIFO_EXIT_BUDGET, TJ_FIFO_EXIT_NOT_INITIALIZED };
static TjFifoPerf s_tj_fifo_perf;
static u64 now;
static unsigned clocks, checks, failures;
static u64 get_timestamp_ns(void) { ++clocks; return now; }
static u64 ps3_ms_now(void) { return now / 1000000ULL; }
BODIES
static void check(int ok,const char* label)
{ ++checks; if(!ok){ ++failures; fprintf(stderr,"FAIL %s\n",label); } }
static u64 total(const TjFifoPerf* perf)
{
    u64 sum=perf->other_ns;
    for(u32 i=0;i<TJ_FIFO_PERF_GROUPS;++i)sum+=perf->group[i].ns;
    return sum;
}
int main(void)
{
    tj_fifo_perf_group_add(0,0x4097,0x1808,0x1808,0,123);
    check(total(&s_tj_fifo_perf)==0,"empty group ignored");
    tj_fifo_perf_group_add(0,0x4097,0x1D74,0x1D74,1,10000);
    for(u32 i=0;i<100;++i)tj_fifo_perf_group_add(0,0x4097,0x1808,0x1808,8,200);
    u8 selected[TJ_FIFO_PERF_GROUPS]={0};
    u32 best=tj_fifo_perf_top_group(&s_tj_fifo_perf,selected);
    TjFifoPerfGroup* group=&s_tj_fifo_perf.group[best];
    check(group->first_method==0x1808 && group->groups==100 && group->payloads==800 &&
          group->ns==20000 && group->max_ns==200,"ranking uses total cost not largest single group");
    selected[best]=1;
    u32 next=tj_fifo_perf_top_group(&s_tj_fifo_perf,selected);
    check(s_tj_fifo_perf.group[next].first_method==0x1D74,"selected rank excluded");
    selected[next]=1;
    check(tj_fifo_perf_top_group(&s_tj_fifo_perf,selected)==TJ_FIFO_PERF_GROUPS,
          "empty ranks stop");
    tj_fifo_perf_group_add(1,0x4097,0x1808,0x1808,1,333);
    tj_fifo_perf_group_add(0,0x0039,0x1808,0x1808,2,444);
    tj_fifo_perf_group_add(0,0x4097,0x1808,0x180C,2,555);
    unsigned populated=0;
    for(u32 i=0;i<TJ_FIFO_PERF_GROUPS;++i)populated+=s_tj_fifo_perf.group[i].groups!=0;
    check(populated==5 && total(&s_tj_fifo_perf)==31332,"subchannel class and actual range stay distinct");
    check(clocks==0,"group recording adds no timing calls");
    TjFifoPerfCall call={0}; TjFifoPerf snapshot={0};
    call.method_ns=31332;call.method_calls=806;call.exit_reason=TJ_FIFO_EXIT_FLIP;
    now=1000000ull;
    check(!tj_fifo_perf_add(&call,2,40000,&snapshot),"window remains open before interval");
    memset(&call,0,sizeof(call));call.exit_reason=TJ_FIFO_EXIT_IDLE;
    now=1001000001ull;
    check(tj_fifo_perf_add(&call,0,1,&snapshot),"report snapshots complete aggregate window");
    check(total(&snapshot)==snapshot.method_ns && snapshot.method_calls==806 &&
          snapshot.exit_flip==1 && snapshot.exit_idle==1,"group cost reconciles with existing totals");
    check(snapshot.report_start_ms==1 && snapshot.report_end_ms==1001,
          "report preserves sampled host tick bounds");
    check(total(&s_tj_fifo_perf)==0 && s_tj_fifo_perf.next_report_ns==now+1000000000ull,
          "report clears groups without losing next deadline");
    for(u32 i=0;i<TJ_FIFO_PERF_GROUPS;++i)
        tj_fifo_perf_group_add(0,0x4097,i*4,i*4,1,i+1);
    u64 expected=(u64)TJ_FIFO_PERF_GROUPS*(TJ_FIFO_PERF_GROUPS+1)/2;
    check(total(&s_tj_fifo_perf)==expected && !s_tj_fifo_perf.other_groups,"bounded table fills exactly");
    for(u32 i=0;i<200;++i)
        tj_fifo_perf_group_add(1,0x4097,i*4,i*4,3,700);
    check(s_tj_fifo_perf.other_groups==200 && s_tj_fifo_perf.other_payloads==600 &&
          total(&s_tj_fifo_perf)==expected+140000,"overflow retains every unranked cost");
    tj_fifo_perf_group_add(0,0x4097,0,0,2,999999);
    memset(selected,0,sizeof(selected));
    best=tj_fifo_perf_top_group(&s_tj_fifo_perf,selected);
    check(s_tj_fifo_perf.group[best].first_method==0 &&
          s_tj_fifo_perf.group[best].groups==2,"existing keys still update when table is full");
    for(u32 i=0;i<TJ_FIFO_PERF_GROUPS;++i){
        u32 chosen=tj_fifo_perf_top_group(&s_tj_fifo_perf,selected);
        check(chosen<TJ_FIFO_PERF_GROUPS && !selected[chosen],"all ranks unique across full capacity");
        if(chosen<TJ_FIFO_PERF_GROUPS)selected[chosen]=1;
    }
    check(tj_fifo_perf_top_group(&s_tj_fifo_perf,selected)==TJ_FIFO_PERF_GROUPS,"all selected terminates");
    printf("FIFO group performance: %u checks, %u failures, plus 6 opt-in integration guards\n",checks,failures);
    return failures?1:0;
}
'''.replace("CAP\n", cap + "\n").replace("TYPES\n", types + "\n").replace("BODIES\n", bodies + "\n")
    clang = shutil.which("clang") or r"C:\Program Files\LLVM\bin\clang.exe"
    with tempfile.TemporaryDirectory(prefix="tj-fifo-group-perf-") as temp:
        path, exe = Path(temp) / "fixture.c", Path(temp) / "fixture.exe"
        path.write_text(fixture, encoding="utf-8")
        subprocess.run([clang, "-std=c11", "-O2", "-D_CRT_SECURE_NO_WARNINGS", str(path), "-o", str(exe)], check=True)
        return subprocess.run([str(exe)], check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
