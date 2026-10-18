"""Exercise the extracted owner-wait diagnostic with mocked fence/clock endpoints."""
from pathlib import Path
import os
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


def test_prefix_wait_trace():
    backend = (VIDEO / "rsx_d3d12_backend.c").read_text(encoding="utf-8")
    header = (VIDEO / "rsx_d3d12_backend.h").read_text(encoding="utf-8")
    reason_start = header.index("typedef enum RsxD3D12CompletionReason")
    reasons = header[reason_start:header.index(
        "} RsxD3D12CompletionReason;", reason_start) + len("} RsxD3D12CompletionReason;")]
    trace_start = backend.index("enum {\n    TJ_PREFIX_WAIT_SYNC,")
    trace_end = backend.index("/* Exact opt-in.", trace_start)
    trace_types = backend[trace_start:trace_end]
    names_start = backend.index("static const char* const s_tj_completion_names")
    completion_names = backend[names_start:backend.index("};", names_start) + 2]
    production = "\n".join(definition(backend, signature) for signature in (
        "static int tj_prefix_wait_trace_enabled(void)",
        "static void tj_prefix_wait_frame_owner(u32 site,",
        "static void tj_prefix_wait_trace_owner(u32 site,",
        "static void tj_prefix_wait_trace_local_main(void)",
        "static void tj_prefix_wait_trace_readback(void)",
        "static u64 tj_prefix_wait_trace_ticks_to_ns(u64 ticks)",
        "static void tj_prefix_wait_trace_report(ULONGLONG now_ms)",
        "static int d3d12_prefix_owner_wait(u32 slot,",
        "static int d3d12_prefix_owner_complete(void)",
    ))
    local_main = definition(backend, "int rsx_d3d12_backend_complete_local_main_prefix(void)")
    assert local_main.index("tj_prefix_wait_trace_local_main();") < local_main.index(
        "d3d12_prefix_overlap_drain(RSX_COMPLETION_LOCAL_MAIN_COPY)"
    )
    readback = definition(backend, "static void tj_prefix_wait_trace_readback(void)")
    assert "s_d3d.draw_count" not in readback and "s_d3d.draws" not in readback
    assert "plan->trace_source_flags" in readback
    preflight = definition(backend, "static int exact_writeback_preflight(void)")
    authority_gate = preflight.index("!dr->scaled_blit_src_authoritative")
    plan_create = preflight.index(
        "ExactBlitWritebackPlan* p = &s_exact_writeback[s_exact_writeback_count++]"
    )
    plan_reset = preflight.index("memset(p, 0, sizeof(*p));", plan_create)
    trace_flags = preflight.index("p->trace_source_flags", plan_reset)
    capture_start = preflight.rfind("if (tj_prefix_wait_trace_enabled())", plan_reset, trace_flags)
    capture = preflight[capture_start:trace_flags + 150]
    assert authority_gate < plan_create < plan_reset < capture_start < trace_flags
    assert "dr->scaled_blit_src_authoritative" in capture
    assert "b->src_local" in capture and "b->dst_local" in capture
    readback_endpoint = backend.index("tj_prefix_wait_trace_readback();")
    owner_complete_endpoint = backend.index(
        "int fence_ok = d3d12_prefix_owner_complete() == 0;", readback_endpoint
    )
    assert backend[readback_endpoint:owner_complete_endpoint].strip() == (
        "tj_prefix_wait_trace_readback();"
    ), "readback plans are sampled immediately before the existing completion fence"
    owner_complete = definition(backend, "static int d3d12_prefix_owner_complete(void)")
    selection_start = owner_complete.index("const u32 wait_site =")
    selection_end = owner_complete.index("if (result == 0)", selection_start)
    selection = owner_complete[selection_start:selection_end]
    assert "tj_prefix_wait_trace_enabled()" in selection
    assert "s_tj_completion_reason == RSX_COMPLETION_LOCAL_MAIN_COPY" in selection
    assert "s_exact_writeback_count" in selection
    assert "TJ_PREFIX_WAIT_LOCAL_MAIN_READBACK" in selection
    assert "TJ_PREFIX_WAIT_SYNC" in selection
    assert owner_complete.index("if (!s_d3d.prefix_owner_active) return wait_for_gpu_checked();") < selection_start
    assert owner_complete.index("d3d12_prefix_owner_signal();") < selection_start
    source = r'''
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
using u32=uint32_t; using u64=uint64_t; using LONGLONG=int64_t;
using ULONGLONG=uint64_t;
using DWORD=uint32_t; using LONG=int32_t;
static DWORD GetCurrentThreadId(){return 1;}
static LONG InterlockedExchange(volatile LONG* target,LONG value){LONG old=*target;*target=value;return old;}
#define MAX_DRAWS 8
struct LARGE_INTEGER { LONGLONG QuadPart; };
REASONS
TRACE_TYPES
COMPLETION_NAMES
struct D3D12PrefixOwner { int phase,signal_queued; u64 fence_value; void* completion_fence; u32 label_count; };
struct ScaledBlit { int src_local,dst_local,src_cpu_owned; };
struct D3D12DrawRecord { int is_scaled_blit; ScaledBlit scaled_blit; int scaled_blit_src_authoritative; };
struct ExactBlitWritebackPlan { u32 record; u64 packed_bytes,allocation_bytes; u32 trace_source_flags; int queued,direct_readback; };
static ExactBlitWritebackPlan s_exact_writeback[MAX_DRAWS];
static struct {
    int gcm2d_backend_halted,texture_label_armed;
    void* fence;
    u32 draw_count,texture_label_count,prefix_deferred_owner,prefix_owner_active;
    D3D12PrefixOwner prefix_owners[2];
    D3D12DrawRecord draws[MAX_DRAWS];
} s_d3d;
static u32 s_exact_writeback_count;
static RsxD3D12CompletionReason s_tj_completion_reason;
static struct { int active; u64 owner_wait_ticks,owner_wait_calls,cpu_clock_invalid; } s_tj_present_work_trace;
static LARGE_INTEGER s_tj_present_frequency={1000000};
static int interval_enabled=1,clock_bad_begin,clock_bad_finish,wait_result,completion_result;
static int wait_calls,qpc_calls,halt_calls,signal_calls,signal_result,legacy_wait_calls;
static int tj_gcm2d_backend_perf_enabled(void){return 0;}
static LONGLONG tj_gcm2d_backend_perf_now(void){return 0;}
static int tj_present_interval_trace_enabled(void){return interval_enabled;}
static int tj_present_work_trace_enabled(void){return 1;}
static void tj_prefix_gpu_wait_start(u32){}
static void gcm2d_backend_halt(const char*){++halt_calls;s_d3d.gcm2d_backend_halted=1;}
static int wait_for_fence_value_checked(u64,u32){++wait_calls;return wait_result;}
static int wait_for_gpu_checked(void){++legacy_wait_calls;return wait_result;}
static int d3d12_prefix_owner_signal(void){
    ++signal_calls;
    if(signal_result)return signal_result;
    if(s_d3d.prefix_owner_active&&s_d3d.prefix_owner_active<=2){
        D3D12PrefixOwner* owner=&s_d3d.prefix_owners[s_d3d.prefix_owner_active-1];
        owner->signal_queued=1;owner->fence_value=5;
    }
    return 0;
}
static int d3d12_prefix_owner_checked_completion(u32,int){return completion_result;}
static int tj_gpu_completion_perf_result(LONGLONG,int result){return result;}
static LONGLONG tj_present_work_trace_sync_begin(void){
    if(!s_tj_present_work_trace.active||!interval_enabled)return 0;
    ++qpc_calls;if(clock_bad_begin){++s_tj_present_work_trace.cpu_clock_invalid;return 0;}
    return 100;
}
static void tj_present_work_trace_sync_finish(LONGLONG mark,u64* ticks,u64* calls){
    if(!s_tj_present_work_trace.active||!interval_enabled)return;
    ++*calls;if(!mark)return;++qpc_calls;
    if(clock_bad_finish){++s_tj_present_work_trace.cpu_clock_invalid;return;}
    *ticks+=17;
}
PRODUCTION
static int checks,failures;
static void check(int ok,const char* label){if(!ok){++failures;fprintf(stderr,"FAIL %s\n",label);}++checks;}
static void reset_owner(void){
    memset(&s_d3d,0,sizeof(s_d3d));memset(&s_tj_prefix_wait_trace,0,sizeof(s_tj_prefix_wait_trace));
    memset(&s_tj_present_work_trace,0,sizeof(s_tj_present_work_trace));
    memset(s_exact_writeback,0,sizeof(s_exact_writeback));
    s_exact_writeback_count=0;s_tj_completion_reason=RSX_COMPLETION_OTHER;
    wait_calls=qpc_calls=halt_calls=signal_calls=legacy_wait_calls=0;signal_result=0;
    clock_bad_begin=clock_bad_finish=0;
    wait_result=completion_result=0;s_d3d.fence=(void*)1;
    s_d3d.prefix_owners[0]={2,1,5,s_d3d.fence};
}
static void set_local_main(int cpu_owned){
    memset(&s_d3d,0,sizeof(s_d3d));memset(s_exact_writeback,0,sizeof(s_exact_writeback));s_exact_writeback_count=0;
    s_d3d.draw_count=1;s_d3d.draws[0].is_scaled_blit=1;
    s_d3d.draws[0].scaled_blit={1,0,cpu_owned};
}
static void test_local_main_trace(void){
    set_local_main(1);s_d3d.draw_count=0;
    tj_prefix_wait_trace_local_main();
    check(!s_tj_prefix_wait_trace.local_main.calls,
        "empty record has no local-main candidate");

    set_local_main(1);s_d3d.draw_count=MAX_DRAWS+1;
    tj_prefix_wait_trace_local_main();
    check(!s_tj_prefix_wait_trace.local_main.calls,
        "oversized draw count is rejected before reading its tail");

    set_local_main(1);s_d3d.draw_count=2;
    s_d3d.draws[1].is_scaled_blit=1;
    s_d3d.draws[1].scaled_blit={1,1,1};
    tj_prefix_wait_trace_local_main();
    check(!s_tj_prefix_wait_trace.local_main.calls,
        "only the structural LOCAL-to-MAIN tail is sampled");

    set_local_main(1);s_d3d.draws[0].scaled_blit.src_local=0;
    tj_prefix_wait_trace_local_main();
    check(!s_tj_prefix_wait_trace.local_main.calls,
        "nonlocal source is outside the structural LOCAL-to-MAIN case");

    set_local_main(1);tj_prefix_wait_trace_local_main();
    check(s_tj_prefix_wait_trace.local_main.calls==1&&
        s_tj_prefix_wait_trace.local_main.src_cpu_owned==1&&
        !s_tj_prefix_wait_trace.local_main.src_not_cpu_proven&&
        s_tj_prefix_wait_trace.local_main.cpu_owned_unobserved==1,
        "CPU-owned source with no pending state enters the joint predicate");

    set_local_main(0);s_d3d.texture_label_count=2;s_d3d.texture_label_armed=1;
    s_d3d.prefix_deferred_owner=2;s_exact_writeback_count=4;
    s_d3d.prefix_owners[0].phase=2;s_d3d.prefix_owners[0].label_count=2;
    s_d3d.prefix_owners[1].phase=2;s_d3d.prefix_owners[1].label_count=1;
    tj_prefix_wait_trace_local_main();
    check(s_tj_prefix_wait_trace.local_main.calls==2&&
        s_tj_prefix_wait_trace.local_main.src_not_cpu_proven==1&&
        s_tj_prefix_wait_trace.local_main.global_label_pending==1&&
        s_tj_prefix_wait_trace.local_main.owner_label_pending==1&&
        s_tj_prefix_wait_trace.local_main.deferred_owner_pending==1&&
        s_tj_prefix_wait_trace.local_main.exact_writeback_pending==1&&
        s_tj_prefix_wait_trace.local_main.cpu_owned_unobserved==1,
        "non-CPU-proven call counts each pending predicate once, not each object");

    set_local_main(1);s_d3d.texture_label_armed=1;
    s_d3d.prefix_owners[0].label_count=9;
    tj_prefix_wait_trace_local_main();
    check(s_tj_prefix_wait_trace.local_main.calls==3&&
        s_tj_prefix_wait_trace.local_main.global_label_pending==2&&
        s_tj_prefix_wait_trace.local_main.owner_label_pending==1&&
        s_tj_prefix_wait_trace.local_main.cpu_owned_unobserved==1,
        "global armed labels count once and labels on free owners are ignored");

    set_local_main(1);s_d3d.texture_label_count=1;
    tj_prefix_wait_trace_local_main();
    check(s_tj_prefix_wait_trace.local_main.calls==4&&
        s_tj_prefix_wait_trace.local_main.global_label_pending==3&&
        s_tj_prefix_wait_trace.local_main.cpu_owned_unobserved==1,
        "global label count independently satisfies the pending predicate");

    set_local_main(1);s_d3d.prefix_deferred_owner=2;
    s_d3d.prefix_owners[0].phase=2;s_d3d.prefix_owners[0].label_count=1;
    tj_prefix_wait_trace_local_main();
    check(s_tj_prefix_wait_trace.local_main.calls==5&&
        s_tj_prefix_wait_trace.local_main.owner_label_pending==2&&
        s_tj_prefix_wait_trace.local_main.deferred_owner_pending==2&&
        s_tj_prefix_wait_trace.local_main.cpu_owned_unobserved==1,
        "old A owner labels are found while deferred ownership names B");

    set_local_main(1);s_exact_writeback_count=1;
    tj_prefix_wait_trace_local_main();
    check(s_tj_prefix_wait_trace.local_main.calls==6&&
        s_tj_prefix_wait_trace.local_main.exact_writeback_pending==2&&
        s_tj_prefix_wait_trace.local_main.cpu_owned_unobserved==1,
        "exact writeback suppresses the joint CPU-owned predicate");
}
static void set_readback_plans(void){
    memset(&s_d3d,0,sizeof(s_d3d));memset(s_exact_writeback,0,sizeof(s_exact_writeback));
    s_d3d.draw_count=3;s_d3d.prefix_owner_active=1;s_d3d.fence=(void*)1;
    s_d3d.prefix_owners[0]={2,0,0,s_d3d.fence};
    s_d3d.draws[0].scaled_blit={1,0,0};s_d3d.draws[0].scaled_blit_src_authoritative=1;
    s_d3d.draws[1].scaled_blit={1,1,0};s_d3d.draws[1].scaled_blit_src_authoritative=1;
    s_d3d.draws[2].scaled_blit={1,0,0};s_d3d.draws[2].scaled_blit_src_authoritative=0;
    s_exact_writeback_count=5;
    s_exact_writeback[0]={0,400,512,3,1,1};
    s_exact_writeback[1]={1,300,512,1,1,0};
    s_exact_writeback[2]={2,900,1024,0,1,1};
    s_exact_writeback[3]={0,777,1024,3,0,1};
    s_exact_writeback[4]={MAX_DRAWS,888,1024,3,1,1};
}
static int traced_owner_complete(void){
    tj_prefix_wait_trace_readback();
    return d3d12_prefix_owner_complete();
}
static void test_readback_owner_attribution(int enabled){
    const int waits_before=wait_calls,qpc_before=qpc_calls,legacy_before=legacy_wait_calls;
    if(!enabled){
        set_readback_plans();s_tj_completion_reason=RSX_COMPLETION_LOCAL_MAIN_COPY;
        s_tj_present_work_trace.active=1;
        check(traced_owner_complete()==0,"trace-disabled owner completion still succeeds");
        check(signal_calls==1&&wait_calls==waits_before+1&&legacy_wait_calls==legacy_before&&
            qpc_calls==qpc_before+2,"trace-disabled path keeps signal and checked wait");
        check(!s_tj_prefix_wait_trace.readback.calls&&
            !s_tj_prefix_wait_trace.sites[TJ_PREFIX_WAIT_LOCAL_MAIN_READBACK]
                [RSX_COMPLETION_LOCAL_MAIN_COPY].calls&&
            !s_tj_prefix_wait_trace.sites[TJ_PREFIX_WAIT_SYNC]
                [RSX_COMPLETION_LOCAL_MAIN_COPY].calls,
            "default-off path keeps readback and owner-wait counters inert");
        set_readback_plans();s_d3d.prefix_owner_active=0;
        const int signals_before=signal_calls;
        check(traced_owner_complete()==0&&legacy_wait_calls==legacy_before+1&&
            signal_calls==signals_before,"default-off no-owner path keeps the legacy fallback");
        check(qpc_calls==qpc_before+2&&wait_calls==waits_before+1,
            "default-off fallback adds no checked-owner wait or QPC");
        return;
    }

    set_readback_plans();s_tj_completion_reason=RSX_COMPLETION_LOCAL_MAIN_COPY;
    s_tj_present_work_trace.active=1;
    s_d3d.draw_count=0;memset(s_d3d.draws,0,sizeof(s_d3d.draws));
    const int qpc_before_helper=qpc_calls,waits_before_helper=wait_calls;
    tj_prefix_wait_trace_readback();
    check(qpc_calls==qpc_before_helper&&wait_calls==waits_before_helper,
        "readback attribution adds no clock reads or waits");
    const TJPrefixWaitReadbackTrace* trace=&s_tj_prefix_wait_trace.readback;
    check(trace->calls==1&&trace->plans==5&&trace->authoritative_plans==2&&
        trace->unproven_plans==3&&trace->local_main_plans==1&&trace->direct_plans==1&&
        trace->packed_bytes==700&&trace->allocation_bytes==1024&&
        trace->owner_backed_calls==1,
        "preflight flags preserve authority and MAIN attribution after draw state resets");

    signal_calls=0;wait_result=completion_result=0;
    const int qpc_before_success=qpc_calls,waits_before_success=wait_calls;
    check(d3d12_prefix_owner_complete()==0,"local-main owner completion succeeds");
    const TJPrefixWaitSiteTrace* readback_site=&s_tj_prefix_wait_trace.sites
        [TJ_PREFIX_WAIT_LOCAL_MAIN_READBACK][RSX_COMPLETION_LOCAL_MAIN_COPY];
    check(signal_calls==1&&wait_calls==waits_before_success+1&&
        readback_site->calls==1&&readback_site->outcomes[TJ_PREFIX_WAIT_OK]==1&&
        readback_site->wait_ticks==17&&qpc_calls==qpc_before_success+2,
        "exact local-main plans attribute the same signaled checked wait with no extra QPC");

    set_readback_plans();s_tj_completion_reason=RSX_COMPLETION_LOCAL_MAIN_COPY;
    s_tj_present_work_trace.active=1;signal_calls=0;wait_result=completion_result=-1;
    const int qpc_before_failure=qpc_calls,waits_before_failure=wait_calls;
    check(traced_owner_complete()<0,"failed local-main checked wait remains fail closed");
    check(signal_calls==1&&wait_calls==waits_before_failure+1&&qpc_calls==qpc_before_failure+2&&
        s_tj_prefix_wait_trace.sites[TJ_PREFIX_WAIT_LOCAL_MAIN_READBACK]
            [RSX_COMPLETION_LOCAL_MAIN_COPY].outcomes[TJ_PREFIX_WAIT_FENCE_FAILURE]==1,
        "failed wait retains its readback classification and signal/clock behavior");

    set_readback_plans();s_tj_completion_reason=RSX_COMPLETION_OTHER;
    s_tj_present_work_trace.active=1;wait_result=completion_result=0;signal_calls=0;
    const u64 readback_calls_before=s_tj_prefix_wait_trace.readback.calls;
    check(traced_owner_complete()==0&&signal_calls==1&&
        s_tj_prefix_wait_trace.readback.calls==readback_calls_before&&
        s_tj_prefix_wait_trace.sites[TJ_PREFIX_WAIT_SYNC][RSX_COMPLETION_OTHER].calls==1,
        "non-local-main reason declines readback attribution and keeps sync site");

    set_readback_plans();s_tj_completion_reason=RSX_COMPLETION_LOCAL_MAIN_COPY;
    s_exact_writeback_count=0;s_tj_present_work_trace.active=1;signal_calls=0;
    const u64 readback_calls_without_plans=s_tj_prefix_wait_trace.readback.calls;
    check(traced_owner_complete()==0&&signal_calls==1&&
        s_tj_prefix_wait_trace.readback.calls==readback_calls_without_plans+1&&
        s_tj_prefix_wait_trace.readback.plans==10&&
        s_tj_prefix_wait_trace.sites[TJ_PREFIX_WAIT_SYNC]
            [RSX_COMPLETION_LOCAL_MAIN_COPY].calls==1,
        "zero-plan local-main endpoint is counted while the owner wait keeps sync site");

    set_readback_plans();s_tj_completion_reason=RSX_COMPLETION_LOCAL_MAIN_COPY;
    s_tj_present_work_trace.active=1;s_d3d.prefix_owner_active=0;
    const int signals_before_legacy=signal_calls,owner_waits_before_legacy=wait_calls;
    check(traced_owner_complete()==0&&legacy_wait_calls==legacy_before+1&&
        signal_calls==signals_before_legacy&&wait_calls==owner_waits_before_legacy&&
        s_tj_prefix_wait_trace.readback.legacy_calls==1&&
        s_tj_prefix_wait_trace.sites[TJ_PREFIX_WAIT_LOCAL_MAIN_READBACK]
            [RSX_COMPLETION_LOCAL_MAIN_COPY].calls==2,
        "no-owner completion keeps legacy fallback while readback trace marks legacy");
    check(qpc_calls==qpc_before+8&&wait_calls==waits_before+4,
        "attribution and legacy fallback preserve the checked-wait clock count");

    set_readback_plans();s_tj_completion_reason=RSX_COMPLETION_LOCAL_MAIN_COPY;
    s_exact_writeback_count=MAX_DRAWS+1;
    const u64 plans_before_bounds=s_tj_prefix_wait_trace.readback.plans;
    tj_prefix_wait_trace_readback();
    check(s_tj_prefix_wait_trace.readback.plans==plans_before_bounds,
        "oversized plan count is rejected before indexing the plan array");
}
int main(void){
    reset_owner();const int enabled=tj_prefix_wait_trace_enabled();
    s_tj_present_work_trace.active=1;
    check(d3d12_prefix_owner_wait(0,TJ_PREFIX_WAIT_QUEUE_AHEAD_OLD_RETIRE,
        RSX_COMPLETION_LOCAL_MAIN_COPY)==0,"timed owner wait succeeds");
    check(qpc_calls==2&&s_tj_present_work_trace.owner_wait_ticks==17,
        "diagnostic reuses the existing two-read wait span");
    if(!enabled){
        check(!s_tj_prefix_wait_trace.sites[TJ_PREFIX_WAIT_QUEUE_AHEAD_OLD_RETIRE]
            [RSX_COMPLETION_LOCAL_MAIN_COPY].calls,"exact opt-in keeps counters off");
        set_local_main(1);tj_prefix_wait_trace_local_main();
        check(!s_tj_prefix_wait_trace.local_main.calls,
            "default-off local-main counters stay inert");
        test_readback_owner_attribution(0);
        check(checks==9&&failures==0,"default-off diagnostic checks");
        puts("PASS: exact opt-in off, no added QPC reads");return failures?1:0;
    }
    check(s_tj_prefix_wait_trace.sites[TJ_PREFIX_WAIT_QUEUE_AHEAD_OLD_RETIRE]
        [RSX_COMPLETION_LOCAL_MAIN_COPY].wait_ticks==17,"active wait ticks are attributed");
    wait_result=-1;completion_result=-1;
    check(d3d12_prefix_owner_wait(0,TJ_PREFIX_WAIT_QUEUE_AHEAD_OLD_RETIRE,
        RSX_COMPLETION_LOCAL_MAIN_COPY)<0,"fence failure is returned");
    wait_result=0;completion_result=-1;
    check(d3d12_prefix_owner_wait(0,TJ_PREFIX_WAIT_QUEUE_AHEAD_OLD_RETIRE,
        RSX_COMPLETION_LOCAL_MAIN_COPY)<0,"postcheck failure is returned");
    completion_result=0;s_tj_present_work_trace.active=0;
    check(d3d12_prefix_owner_wait(0,TJ_PREFIX_WAIT_QUEUE_AHEAD_OLD_RETIRE,
        RSX_COMPLETION_LOCAL_MAIN_COPY)==0,"unarmed wait succeeds");
    s_tj_present_work_trace.active=1;clock_bad_begin=1;
    check(d3d12_prefix_owner_wait(0,TJ_PREFIX_WAIT_QUEUE_AHEAD_OLD_RETIRE,
        RSX_COMPLETION_LOCAL_MAIN_COPY)==0,"invalid-clock wait succeeds");
    clock_bad_begin=0;
    check(d3d12_prefix_owner_wait(2,TJ_PREFIX_WAIT_QUEUE_AHEAD_OLD_RETIRE,
        RSX_COMPLETION_LOCAL_MAIN_COPY)<0,"precheck failure remains fail closed");
    const TJPrefixWaitSiteTrace* row=&s_tj_prefix_wait_trace.sites
        [TJ_PREFIX_WAIT_QUEUE_AHEAD_OLD_RETIRE][RSX_COMPLETION_LOCAL_MAIN_COPY];
    check(row->calls==6&&row->outcomes[TJ_PREFIX_WAIT_OK]==3&&
        row->outcomes[TJ_PREFIX_WAIT_PRECHECK_FAILURE]==1&&
        row->outcomes[TJ_PREFIX_WAIT_FENCE_FAILURE]==1&&
        row->outcomes[TJ_PREFIX_WAIT_POSTCHECK_FAILURE]==1&&row->wait_ticks==51&&
        row->clock_invalid==1&&row->untimed==2,"wait failures and untimed calls bucket correctly");
    const int waits_before_local_main=wait_calls,qpc_before_local_main=qpc_calls;
    test_local_main_trace();
    check(wait_calls==waits_before_local_main&&qpc_calls==qpc_before_local_main,
        "local-main predicate counters add no wait or QPC");
    test_readback_owner_attribution(1);
    tj_prefix_wait_trace_report(1000);tj_prefix_wait_trace_report(2001);
    check(checks==31&&failures==0,"enabled diagnostic checks");
    puts("PASS: exact opt-in on, timing, untimed and failure buckets");return failures?1:0;
}
'''.replace("REASONS", reasons).replace("TRACE_TYPES", trace_types).replace(
        "COMPLETION_NAMES", completion_names).replace("PRODUCTION", production)

    compiler = shutil.which("clang++") or r"C:\Program Files\LLVM\bin\clang++.exe"
    with tempfile.TemporaryDirectory(prefix="tj-prefix-wait-trace-") as directory:
        cpp, exe = Path(directory) / "wait.cpp", Path(directory) / "wait.exe"
        cpp.write_text(source, encoding="utf-8")
        subprocess.run([compiler, "-std=c++17", "-D_CRT_SECURE_NO_WARNINGS",
                        str(cpp), "-o", str(exe)], check=True)
        for value in (None, "0", "1x", "1"):
            env = dict(os.environ)
            env.pop("TJ_PREFIX_WAIT_TRACE", None)
            if value is not None:
                env["TJ_PREFIX_WAIT_TRACE"] = value
            result = subprocess.run([str(exe)], env=env, check=False,
                                    capture_output=True, text=True, timeout=10)
            assert result.returncode == 0, result.stdout + result.stderr
            if value == "1":
                assert "PASS: exact opt-in on" in result.stdout
                assert ("[TJ-PREFIX-WAIT-COST] t_ms=2001 cumulative=1 work_active=1 "
                        "interval_enabled=1 qpc_hz=1000000 waits=") in result.stderr, result.stderr
                assert ("queue_ahead_old_retire:local_main_copy:6:51:51000:3:1:1:1:1:2"
                        in result.stderr), result.stderr
                assert ("prepare_calls=0 prepare_disabled=0 prepare_busy=0 prepare_no_deferred=0 "
                        "prepare_bad_parity=0 prepare_overlap=0 prepare_no_retire=0 "
                        "prepare_retire_candidate=0 record_bank_calls=0 record_disabled=0 "
                        "record_unarmed=0 record_renderer_state=0 record_upload_state=0 "
                        "record_labels=0 record_writeback=0 record_epoch=0 record_slot_state=0 "
                        "record_observer=0 record_same_slot=0 record_tickets=0 record_mirrors=0 "
                        "record_current_gpu_alias=0 record_ready=0 local_main_calls=6 "
                        "local_main_src_cpu_owned=5 local_main_src_not_cpu_proven=1 "
                        "local_main_global_label_pending=3 local_main_owner_label_pending=2 "
                        "local_main_deferred_owner_pending=2 local_main_exact_writeback_pending=2 "
                        "local_main_cpu_owned_unobserved=1 readback_calls=4 readback_plans=15 "
                        "readback_authoritative_plans=6 readback_unproven_plans=9 "
                        "readback_local_main_plans=3 readback_direct_plans=3 "
                        "readback_packed_bytes=2100 readback_allocation_bytes=3072 "
                        "readback_owner_backed_calls=3 readback_legacy_calls=1") in result.stderr, result.stderr
                assert ("local_main_readback:local_main_copy:2:34:34000:1:0:1:0:0:0"
                        in result.stderr), result.stderr
            else:
                assert "PASS: exact opt-in off" in result.stdout
                assert not result.stderr


if __name__ == "__main__":
    test_prefix_wait_trace()
