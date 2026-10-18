"""Exercise production fence waits without launching a game or GPU workload."""
from pathlib import Path
import os
import shutil
import subprocess
import sys
import tempfile

from test_fifo_present_boundary import function

ROOT = Path(__file__).resolve().parents[1]


def main():
    backend = (ROOT.parent / "ps3recomp/libs/video/rsx_d3d12_backend.c").read_text(encoding="utf-8")
    header = (ROOT.parent / "ps3recomp/libs/video/rsx_d3d12_backend.h").read_text(encoding="utf-8")
    reason_start = header.index("typedef enum RsxD3D12CompletionReason")
    reasons = header[reason_start:header.index("} RsxD3D12CompletionReason;", reason_start) + len("} RsxD3D12CompletionReason;")]
    names_start = backend.index("static const char* const s_tj_completion_names[")
    names = backend[names_start:backend.index("};", names_start) + 2]
    service_state_start = backend.index(
        "static _Thread_local u32 (*s_d3d12_fence_wait_service)")
    service_state_end = backend.index(
        "void rsx_d3d12_backend_set_fence_wait_service(", service_state_start)
    # Compile the actual checked-wait helpers and exercise reuse under both
    # default-off and explicit opt-in process environments.
    bodies = [backend[service_state_start:service_state_end],
              function(backend, "void rsx_d3d12_backend_set_fence_wait_service(")]
    for name in ("fence_wait_failure", "wait_for_fence_value_checked", "tj_gpu_completion_perf_result",
                 "d3d12_command_result_checked",
                 "signal_for_gpu_checked", "d3d12_gpu_completion_reuse_enabled",
                 "wait_for_gpu_checked", "wait_for_gpu_checked_at", "move_to_next_frame"):
        marker = f"static int {name}("
        start = backend.find(marker)
        while start >= 0 and backend.find(";", start) < backend.find("{", start):
            start = backend.find(marker, start + len(marker))
        if start >= 0:
            bodies.append(function(backend[start:], marker))
    bodies.append(function(backend, "int rsx_d3d12_backend_complete_fifo_prefix_at("))
    # The executable below tests HRESULT policy. These source checks retain the
    # real submission/reset call-site wiring; they do not simulate D3D12 itself.
    for operation in ("capture command-list close", "FIFO prefix command-list close",
                      "frame command-list close"):
        where = backend.index('"' + operation + '"')
        guard = backend.rfind("if (d3d12_command_result_checked(", 0, where)
        execute = backend.index("->ExecuteCommandLists(", where)
        assert guard >= 0 and "->Close(" in backend[guard:where]
        assert "return;" in backend[where:execute]
    for operation in ("capture allocator reset", "render allocator reset"):
        where = backend.index('"' + operation + '"')
        assert '!= 0 ||' in backend[where:where + 100]
    present = backend.index('"swap-chain present"')
    present_call = backend.index("s_d3d.swap_chain->lpVtbl->Present(", present - 500)
    present_begin = backend.rfind("LONGLONG present_mark = tj_present_work_trace_sync_begin();",
                                 0, present_call)
    assert present_begin >= 0 and backend[:present_begin].rstrip().endswith(
        "s_d3d.gpu_completion_ticket = 0;"), (
            "Present must invalidate a prior exact-writeback signal before it can be queued")
    assert "return;" in backend[present:backend.index("if (move_to_next_frame()", present)]
    execute_positions = []
    search_from = 0
    while True:
        execute = backend.find("->ExecuteCommandLists(", search_from)
        if execute < 0:
            break
        execute_positions.append(execute)
        call_start = backend.rfind(";", 0, execute) + 1
        assert backend[:call_start].rstrip().endswith("s_d3d.gpu_completion_ticket = 0;") and \
               backend[call_start:execute].strip() == "s_d3d.cmd_queue->lpVtbl", (
            "every command-list execution must invalidate the reusable completion ticket first")
        search_from = execute + 1
    assert len(execute_positions) == 3, "expected capture, FIFO-prefix, and frame submissions"
    for signal_marker in ("s_d3d.cmd_queue->lpVtbl->Signal(",):
        search_from = 0
        signal_count = 0
        while True:
            signal = backend.find(signal_marker, search_from)
            if signal < 0:
                break
            signal_count += 1
            function_start = max(backend.rfind("static int move_to_next_frame(", 0, signal),
                                backend.rfind("static int signal_for_gpu_checked(", 0, signal))
            function_body = function(backend[function_start:], "static int ")
            clear = function_body.find("s_d3d.gpu_completion_ticket = 0;")
            signal_in_body = function_body.find(signal_marker)
            assert 0 <= clear < signal_in_body, "each direct queue Signal must clear an older ticket first"
            search_from = signal + 1
        assert signal_count == 2, "expected the frame and checked-prefix direct signals"
    fence_create = backend.index("/* Create fence */")
    init_end = backend.index("Create root signature", fence_create)
    fence_init = backend[fence_create:init_end]
    assert "s_d3d.gpu_completion_ticket = 0;" in fence_init
    assert "s_d3d.gpu_completion_reused = s_d3d.gpu_completion_fresh = 0;" in fence_init
    source = r'''
#define _CRT_SECURE_NO_WARNINGS
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <initializer_list>
#include <thread>
using u32=uint32_t; using u64=uint64_t; using DWORD=uint32_t; using HRESULT=int32_t;
using HANDLE=void*; using LONGLONG=int64_t;
struct LARGE_INTEGER { long long QuadPart; };
COMPLETION_REASONS
COMPLETION_NAMES
#define S_OK ((HRESULT)0)
#define FRAME_COUNT 2
#define E_FAIL ((HRESULT)0x80004005u)
#define FAILED(h) ((HRESULT)(h)<0)
#define SUCCEEDED(h) ((HRESULT)(h)>=0)
#define WAIT_OBJECT_0 0u
#define WAIT_TIMEOUT 258u
#define WAIT_FAILED 0xffffffffu
#define ERROR_SUCCESS 0u
#define HRESULT_FROM_WIN32(x) ((HRESULT)(0x80070000u|(x)))
struct Fence; struct Queue; struct Device; struct Swap;
struct FenceVtable { u64(*GetCompletedValue)(Fence*); HRESULT(*SetEventOnCompletion)(Fence*,u64,HANDLE); };
struct QueueVtable { HRESULT(*Signal)(Queue*,Fence*,u64); };
struct DeviceVtable { HRESULT(*GetDeviceRemovedReason)(Device*); };
struct SwapVtable { u32(*GetCurrentBackBufferIndex)(Swap*); };
struct Fence { FenceVtable* lpVtbl; }; struct Queue { QueueVtable* lpVtbl; };
struct Device { DeviceVtable* lpVtbl; }; struct Swap { SwapVtable* lpVtbl; };
struct Step { DWORD result,elapsed; u64 completed; };
static Step steps[8]; static unsigned step_count,waits,arms,signals,checks,failures;
static unsigned completed_reads,complete_on_read,removed_calls,tick_reads;
static u64 completed_on_read;
static u64 tick,completed,signal_value,armed_value; static DWORD last_wait;
static unsigned qpf_calls,qpc_calls,qpc_fail_call,qpc_backwards_call;
static int qpf_succeeds=1,qpc_advance_tick;
static long long qpc_frequency=10000000,qpc_value=1000000,qpc_step=25;
static long long qpc_backwards_value=1;
static int diagnostic_mode, survival_trace_armed, prepare_calls, prepare_result;
static int prepare_mutates_ticket;
static u64 prepare_ticket;
static unsigned wait_service_calls;
static u32 wait_service_requested,wait_service_remaining;
static DWORD wait_service_elapsed;
static int wait_service_completes,wait_service_removes,wait_service_reenters;
static int wait_service_nested_result;
static void* wait_service_context_seen;
static u32 wait_service_remaining_history[32];
static DWORD fake_last_error = 5;
static unsigned last_error_calls;
static RsxD3D12CompletionReason s_tj_completion_reason;
static struct {
    u64 report_start_ms, completion_calls[RSX_COMPLETION_REASON_COUNT];
    u64 completion_ticks[RSX_COMPLETION_REASON_COUNT], completion_max[RSX_COMPLETION_REASON_COUNT];
    u64 completion_failures[RSX_COMPLETION_REASON_COUNT];
} s_tj_gcm2d_backend_perf;
static int tj_gcm2d_backend_perf_enabled(){return diagnostic_mode;}
static int tj_prefix_cost_enabled(){return 0;}
static int tj_prefix_wait_trace_enabled(){return 0;}
static int tj_main_direct_readback_enabled(){return 0;}
static LONGLONG tj_gcm2d_backend_perf_now(){return (LONGLONG)tick;}
static HRESULT signal_hr,arm_hr,removed_hr;
static struct Perf { u64 fence_signal_ticks,fence_arm_ticks,fence_event_ticks,fence_event_calls; }
    perf, *s_tj_gcm2d_backend_perf_sample;
static struct PresentWorkTraceStub {
    u64 fence_checks, fence_pending;
    u64 global_wait_calls, global_wait_ticks, owner_wait_calls, owner_wait_ticks;
    u64 backbuffer_wait_calls, backbuffer_wait_ticks;
    u64 fence_arm_calls, fence_arm_ticks, fence_event_calls, fence_event_ticks;
    u64 queue_signal_calls, queue_signal_ticks, present_calls, present_ticks;
} s_tj_present_work_trace;
static LONGLONG tj_present_work_trace_sync_begin(){return 0;}
/* Deliberately clobber LastError, as a QPC/reporting helper could. */
static void tj_present_work_trace_sync_finish(LONGLONG,u64*,u64*){fake_last_error=0x162E;}
static void tj_present_work_trace_wait_check(int){}
static LONGLONG tj_gcm2d_backend_perf_subphase_begin(){
    return s_tj_gcm2d_backend_perf_sample?(LONGLONG)tick:0;
}
static void tj_gcm2d_backend_perf_subphase_end(u64* total,LONGLONG start){
    if(start)*total+=tick-(u64)start;
}
static u64 GetTickCount64(){++tick_reads;return tick;}
static DWORD GetLastError(){++last_error_calls;return fake_last_error;}
static DWORD WaitForSingleObject(HANDLE,DWORD ms){
    last_wait=ms;
    if(waits>=step_count){++waits;tick+=ms;return WAIT_TIMEOUT;}
    Step s=steps[waits++];tick+=s.result==WAIT_TIMEOUT?ms:s.elapsed;completed=s.completed;
    if(s.result==WAIT_FAILED)fake_last_error=0x4D2;
    return s.result;
}
static int QueryPerformanceFrequency(LARGE_INTEGER* value){
    ++qpf_calls;if(!qpf_succeeds)return 0;value->QuadPart=qpc_frequency;return 1;
}
static int QueryPerformanceCounter(LARGE_INTEGER* value){
    ++qpc_calls;if(qpc_advance_tick)++tick;
    if(qpc_fail_call==qpc_calls)return 0;
    qpc_value+=qpc_step;
    if(qpc_backwards_call==qpc_calls){value->QuadPart=qpc_backwards_value;return 1;}
    value->QuadPart=qpc_value;return 1;
}
static u64 get_completed(Fence*){
    ++completed_reads;
    if(complete_on_read==completed_reads)completed=completed_on_read;
    return completed;
}
static HRESULT arm(Fence*,u64 v,HANDLE){++arms;armed_value=v;return arm_hr;}
static HRESULT signal(Queue*,Fence*,u64 v){++signals;signal_value=v;return signal_hr;}
static HRESULT removed(Device*){++removed_calls;return removed_hr;}
static u32 next_buffer_index=1;
static u32 next_buffer(Swap*){return next_buffer_index;}
static FenceVtable fvt={get_completed,arm}; static QueueVtable qvt={signal};
static DeviceVtable dvt={removed}; static SwapVtable svt={next_buffer};
static Fence fence={&fvt};static Queue queue={&qvt};static Device device={&dvt};static Swap swap={&svt};
static struct { u32 frame_index; int gcm2d_backend_halted; u64 fence_values[2];
    u64 gpu_completion_ticket, gpu_completion_reused, gpu_completion_fresh;
    Fence* fence; Queue* cmd_queue;
    Device* device;Swap* swap_chain;HANDLE fence_event; } s_d3d;
int rsx_d3d12_backend_complete_fifo_prefix(void);
static int wait_for_fence_value_checked(u64 want,DWORD timeout_ms);
static void gcm2d_backend_halt(const char*) { s_d3d.gcm2d_backend_halted = 1; }
static int rsx_survival_trace_is_armed(){return survival_trace_armed;}
/* The fixture controls preparation to prove it runs before cache selection. */
static int d3d12_prefix_prepare_before_wait(){
    ++prepare_calls;
    if (prepare_mutates_ticket) s_d3d.gpu_completion_ticket=prepare_ticket;
    return prepare_result;
}
static u32 wait_service(void* context,u32 remaining_ms){
    ++wait_service_calls;wait_service_remaining=remaining_ms;
    wait_service_context_seen=context;
    if(wait_service_calls<=32)wait_service_remaining_history[wait_service_calls-1]=remaining_ms;
    tick+=wait_service_elapsed;
    if(wait_service_completes)completed=armed_value;
    if(wait_service_removes)completed=UINT64_MAX;
    if(wait_service_reenters){
        wait_service_reenters=0;
        wait_service_nested_result=wait_for_fence_value_checked(5,20);
    }
    return wait_service_requested;
}
PRODUCTION
int rsx_d3d12_backend_complete_fifo_prefix(void){return wait_for_gpu_checked();}
static void check(bool ok,const char* label){++checks;if(!ok){++failures;printf("FAIL %s\n",label);}}
static void reset(){
    memset(&s_d3d,0,sizeof(s_d3d));s_d3d.fence=&fence;s_d3d.cmd_queue=&queue;
    s_d3d.device=&device;s_d3d.swap_chain=&swap;s_d3d.fence_event=(void*)1;
    s_d3d.fence_values[0]=4;tick=100;completed=0;signal_value=armed_value=0;
    fake_last_error=5;last_error_calls=0;
    survival_trace_armed=prepare_calls=prepare_result=prepare_mutates_ticket=0;prepare_ticket=0;
    waits=arms=signals=step_count=0;signal_hr=arm_hr=removed_hr=S_OK;
    completed_reads=complete_on_read=removed_calls=0;completed_on_read=0;
    qpf_calls=qpc_calls=qpc_fail_call=qpc_backwards_call=0;
    qpf_succeeds=1;qpc_advance_tick=0;
    qpc_frequency=10000000;qpc_value=1000000;qpc_step=25;
    qpc_backwards_value=1;
    next_buffer_index=1;
    memset(&perf,0,sizeof(perf));s_tj_gcm2d_backend_perf_sample=nullptr;
    memset(&s_tj_gcm2d_backend_perf,0,sizeof(s_tj_gcm2d_backend_perf));
    s_tj_completion_reason=RSX_COMPLETION_OTHER;
    tick_reads=0;wait_service_calls=0;wait_service_requested=1;
    wait_service_remaining=0;wait_service_elapsed=0;
    wait_service_completes=wait_service_removes=wait_service_reenters=0;
    wait_service_nested_result=-99;
    wait_service_context_seen=nullptr;memset(wait_service_remaining_history,0,
        sizeof(wait_service_remaining_history));
    rsx_d3d12_backend_set_fence_wait_service(nullptr,nullptr);
    s_d3d12_fence_wait_service_busy=0;
}
static void step(DWORD result,DWORD elapsed,u64 value){steps[step_count++]={result,elapsed,value};}
static void cases(int mode){
    diagnostic_mode=mode;
    reset();u64 ticket=99;check(signal_for_gpu_checked(&ticket)==0&&ticket==5&&
        signals==1&&!arms&&!waits,"signal returns an exact ticket without waiting");
    reset();signal_hr=E_FAIL;ticket=99;check(signal_for_gpu_checked(&ticket)<0&&
        ticket==0&&signals==1&&!arms&&!waits,"failed signal cannot expose a usable ticket");
    reset();check(signal_for_gpu_checked(nullptr)<0&&!signals,"missing ticket destination rejected");
    reset();completed=UINT64_MAX-3;s_d3d.fence_values[0]=UINT64_MAX-2;
    check(move_to_next_frame()<0&&!signals&&s_d3d.frame_index==0&&
          s_d3d.fence_values[0]==UINT64_MAX-2,"frame leaves room for a distinct ticket and next counter");
    reset();completed=UINT64_MAX-3;s_d3d.fence_values[0]=UINT64_MAX-3;
    check(move_to_next_frame()==0&&signal_value==UINT64_MAX-2&&
          s_d3d.fence_values[1]==UINT64_MAX-1,"last safe frame ticket stays below removal sentinel");
    reset();s_d3d.fence_values[0]=0;completed=0;
    check(move_to_next_frame()==0&&signal_value==1,
          "first frame without a prefix gets a nonzero completion ticket");
    reset();u64 prefix_ticket=0;
    check(signal_for_gpu_checked(&prefix_ticket)==0,"prefix signal before frame submission");
    completed=prefix_ticket;
    check(move_to_next_frame()==0&&signal_value>prefix_ticket,
          "frame submission uses a later signal than completed prefix work");
    u64 first_frame_ticket=signal_value;
    next_buffer_index=0;
    step(WAIT_OBJECT_0,1,first_frame_ticket);
    check(move_to_next_frame()==0&&waits==1&&armed_value==first_frame_ticket,
          "allocator reuse waits for frame work even when its earlier prefix completed");
    for(u64 value:{UINT64_MAX-1,UINT64_MAX}){
        reset();s_d3d.fence_values[0]=value;ticket=99;
        check(signal_for_gpu_checked(&ticket)<0&&ticket==0&&!signals&&!waits&&
            s_d3d.fence_values[0]==value,"sentinel or wrap never signaled");
        check(move_to_next_frame()<0&&!signals&&s_d3d.frame_index==0,"frame counter never wraps");
    }
    reset();s_d3d.frame_index=FRAME_COUNT;ticket=99;
    check(signal_for_gpu_checked(&ticket)<0&&ticket==0&&!signals,"invalid frame ticket rejected");
    reset();check(d3d12_command_result_checked(S_OK,"fixture success")==0 &&
                  !s_d3d.gcm2d_backend_halted,"successful command continues");
    check(d3d12_command_result_checked((HRESULT)0x087A0001u,"fixture status")==0 &&
          !s_d3d.gcm2d_backend_halted,"nonfailure DXGI status continues");
    check(d3d12_command_result_checked(E_FAIL,"fixture failure")!=0 &&
          s_d3d.gcm2d_backend_halted,"failed command halts");
    reset();check(d3d12_command_result_checked((HRESULT)0x887A0005u,"fixture removal")!=0 &&
                 s_d3d.gcm2d_backend_halted,"device removal halts");
    reset();completed=5;check(wait_for_gpu_checked()==0,"already complete");
    check(signals==1&&signal_value==5&&waits==0&&arms==0,"completed fence needs no event");
    reset();step(WAIT_OBJECT_0,1,4);step(WAIT_OBJECT_0,4,5);
    check(wait_for_gpu_checked()==0,"stale event does not reject pending GPU work");
    check(waits==2&&arms==1&&armed_value==5,"wait again for the same armed target");
    reset();step(WAIT_OBJECT_0,1,0);step(WAIT_OBJECT_0,1,4);step(WAIT_OBJECT_0,1,5);
    check(wait_for_gpu_checked()==0,"multiple early wakes preserve the target");
    check(waits==3,"each early wake rechecks completion");
    reset();completed=UINT64_MAX;removed_hr=(HRESULT)0x887A0005u;
    check(wait_for_gpu_checked()!=0,"removed-device sentinel is not completion");
    reset();step(WAIT_OBJECT_0,1,UINT64_MAX);
    check(wait_for_gpu_checked()!=0,"device removal during wait is not completion");
    reset();signal_hr=E_FAIL;
    check(wait_for_gpu_checked()!=0&&signals==1&&arms==0&&waits==0,"signal failure stays closed");
    reset();arm_hr=E_FAIL;
    check(wait_for_gpu_checked()!=0&&arms==1&&waits==0,"event registration failure stays closed");
    reset();step(WAIT_TIMEOUT,6000,4);
    check(wait_for_gpu_checked()!=0,"timeout cannot authorize writeback");
    check(tick<=6100,"timeout stays within six-second budget");
    reset();step(WAIT_OBJECT_0,2000,1);step(WAIT_OBJECT_0,2000,3);step(WAIT_TIMEOUT,2000,4);
    check(wait_for_gpu_checked()!=0&&tick<=6100,"early wakes cannot extend timeout budget");
    reset();step(WAIT_TIMEOUT,6000,5);
    check(wait_for_gpu_checked()==0,"completion at timeout boundary is authoritative");
    reset();step(WAIT_FAILED,1,0);
    check(wait_for_gpu_checked()!=0&&waits==1&&last_error_calls==1&&
          fake_last_error==0x162E,
          "OS wait failure is terminal and diagnostics clobber LastError afterward");
    reset();s_d3d.fence_values[0]=8;s_d3d.fence_values[1]=7;completed=6;
    step(WAIT_OBJECT_0,1,6);step(WAIT_OBJECT_0,1,7);
    check(move_to_next_frame()==0,"frame reuse waits through stale event");
    check(waits==2&&armed_value==7&&signal_value==9&&s_d3d.fence_values[1]==10,"frame fence ownership preserved");
    reset();s_d3d.fence_values[0]=8;s_d3d.fence_values[1]=7;completed=UINT64_MAX;
    check(move_to_next_frame()!=0&&s_d3d.fence_values[1]==7,"removed device cannot reuse frame allocator");
    reset();s_d3d.fence_values[0]=8;s_d3d.fence_values[1]=7;completed=6;
    step(WAIT_TIMEOUT,2000,6);
    check(move_to_next_frame()!=0&&s_d3d.fence_values[1]==7,"frame timeout preserves required fence");
    reset();s_tj_gcm2d_backend_perf_sample=&perf;
    step(WAIT_OBJECT_0,1,4);step(WAIT_OBJECT_0,4,5);
    check(wait_for_gpu_checked()==0,"diagnostics preserve stale-event completion checks");
    check(perf.fence_event_calls==2&&perf.fence_event_ticks==5,
          "diagnostics count actual OS waits and elapsed time");
    reset();s_tj_gcm2d_backend_perf_sample=&perf;completed=5;
    check(wait_for_gpu_checked()==0&&perf.fence_event_calls==0&&arms==0,
          "diagnostics add no event for an already-complete fence");
    reset();s_tj_gcm2d_backend_perf_sample=&perf;step(WAIT_TIMEOUT,6000,4);
    check(wait_for_gpu_checked()!=0&&tick==6100&&perf.fence_event_calls==1,
          "diagnostics preserve the total deadline and reject unfinished work");
    reset();s_tj_completion_reason=RSX_COMPLETION_BACKEND_RELEASE;
    step(WAIT_OBJECT_0,1,4);step(WAIT_OBJECT_0,4,5);
    check(rsx_d3d12_backend_complete_fifo_prefix_at(RSX_COMPLETION_LOCAL_MAIN_COPY)==0,
          "scoped copy completion preserves stale-event behavior");
    check(signals==1&&arms==1&&waits==2&&s_tj_completion_reason==RSX_COMPLETION_BACKEND_RELEASE,
          "scope restores its caller without adding GPU operations");
    check(s_tj_gcm2d_backend_perf.completion_calls[RSX_COMPLETION_LOCAL_MAIN_COPY]==(u64)mode&&
          s_tj_gcm2d_backend_perf.completion_ticks[RSX_COMPLETION_LOCAL_MAIN_COPY]==(u64)(mode*5)&&
          s_tj_gcm2d_backend_perf.completion_max[RSX_COMPLETION_LOCAL_MAIN_COPY]==(u64)(mode*5),
          "copy counters are accurate and disabled when not opted in");
    reset();s_tj_completion_reason=RSX_COMPLETION_BACKEND_RELEASE;step(WAIT_TIMEOUT,6000,4);
    check(rsx_d3d12_backend_complete_fifo_prefix_at(RSX_COMPLETION_CLEAR)!=0&&tick==6100&&
          s_tj_completion_reason==RSX_COMPLETION_BACKEND_RELEASE,
          "failed scope preserves deadline, failure and caller reason");
    check(s_tj_gcm2d_backend_perf.completion_failures[RSX_COMPLETION_CLEAR]==(u64)mode,
          "failed completion is attributed only when opted in");
    reset();s_tj_completion_reason=RSX_COMPLETION_BACKEND_RELEASE;signal_hr=E_FAIL;
    check(wait_for_gpu_checked_at(RSX_COMPLETION_CAPTURE)!=0&&signals==1&&arms==0&&waits==0&&
          s_tj_completion_reason==RSX_COMPLETION_BACKEND_RELEASE,
          "nested capture failure restores outer reason without a wait");
    check(s_tj_gcm2d_backend_perf.completion_calls[RSX_COMPLETION_CAPTURE]==(u64)mode&&
          s_tj_gcm2d_backend_perf.completion_failures[RSX_COMPLETION_CAPTURE]==(u64)mode,
          "queue signal failure is attributed");
    reset();completed=5;
    check(rsx_d3d12_backend_complete_fifo_prefix_at((RsxD3D12CompletionReason)999)==0&&
          s_tj_gcm2d_backend_perf.completion_calls[RSX_COMPLETION_OTHER]==(u64)mode,
          "invalid diagnostic reason cannot change successful completion");
}
static void default_off_case(){
    reset();survival_trace_armed=1;
    check(!d3d12_gpu_completion_reuse_enabled(),"completion reuse defaults off without the environment flag");
    step(WAIT_OBJECT_0,1,5);
    check(wait_for_gpu_checked()==0&&signals==1&&signal_value==5&&
          s_d3d.gpu_completion_fresh==1&&s_d3d.gpu_completion_reused==0,
          "default-off first wait queues a fresh signal");
    step(WAIT_OBJECT_0,1,6);
    check(wait_for_gpu_checked()==0&&signals==2&&signal_value==6&&
          s_d3d.gpu_completion_fresh==2&&s_d3d.gpu_completion_reused==0,
          "default-off consecutive wait queues another signal");
    check(qpf_calls==0&&qpc_calls==0,
          "default-off fence waits make no performance-counter calls");
}
static void reuse_cases(){
    check(d3d12_gpu_completion_reuse_enabled(),"nonzero environment flag opts into completion reuse");

    reset();survival_trace_armed=1;step(WAIT_OBJECT_0,1,5);
    check(wait_for_gpu_checked()==0&&s_d3d.gpu_completion_ticket==5,
          "first opted-in wait records its actual signal ticket");
    check(wait_for_gpu_checked()==0&&signals==1&&waits==1&&
          s_d3d.gpu_completion_fresh==1&&s_d3d.gpu_completion_reused==1,
          "consecutive wait reuses a covering ticket without a new signal");

    reset();survival_trace_armed=1;s_d3d.gpu_completion_ticket=5;
    step(WAIT_OBJECT_0,1,4);step(WAIT_OBJECT_0,1,5);
    check(wait_for_gpu_checked()==0&&signals==0&&waits==2&&arms==1&&armed_value==5,
          "pending covering ticket still performs the checked fence wait");
    check(s_d3d.gpu_completion_reused==1&&s_d3d.gpu_completion_fresh==0,
          "pending ticket reuse attempt is counted while its checked wait stays active");

    reset();survival_trace_armed=1;s_d3d.gpu_completion_ticket=5;
    step(WAIT_TIMEOUT,6000,4);
    check(wait_for_gpu_checked()!=0&&signals==0&&arms==1&&waits==1&&tick==6100&&
          s_d3d.gpu_completion_ticket==5,
          "pending reused ticket timeout stays bounded and never queues a fresh signal");

    reset();survival_trace_armed=1;s_d3d.gpu_completion_ticket=5;
    step(WAIT_OBJECT_0,1,UINT64_MAX);
    check(wait_for_gpu_checked()!=0&&signals==0&&arms==1&&waits==1&&
          s_d3d.gpu_completion_ticket==5,
          "device removal sentinel rejects a reused ticket without signaling again");

    reset();survival_trace_armed=1;s_d3d.gpu_completion_ticket=5;arm_hr=E_FAIL;
    check(wait_for_gpu_checked()!=0&&signals==0&&arms==1&&waits==0&&
          s_d3d.gpu_completion_ticket==5,
          "event registration failure rejects a reused ticket without signaling again");

    reset();survival_trace_armed=1;s_d3d.gpu_completion_ticket=5;
    step(WAIT_FAILED,1,0);
    check(wait_for_gpu_checked()!=0&&signals==0&&arms==1&&waits==1&&
          s_d3d.gpu_completion_ticket==5&&last_error_calls==1&&fake_last_error==0x162E,
          "OS wait failure rejects a reused ticket without signaling again");

    for(u64 invalid_ticket:{(u64)0,UINT64_MAX}){
        reset();survival_trace_armed=1;s_d3d.gpu_completion_ticket=invalid_ticket;
        s_d3d.fence_values[0]=5;completed=5;step(WAIT_OBJECT_0,1,6);
        check(wait_for_gpu_checked()==0&&signals==1&&signal_value==6&&
              s_d3d.gpu_completion_ticket==6&&s_d3d.gpu_completion_reused==0&&
              s_d3d.gpu_completion_fresh==1,
              "zero or sentinel cache ticket falls back to a fresh valid signal");
    }

    reset();s_d3d.gpu_completion_ticket=5;s_d3d.fence_values[0]=5;completed=5;
    step(WAIT_OBJECT_0,1,6);
    check(wait_for_gpu_checked()==0&&signals==1&&armed_value==6&&
          s_d3d.gpu_completion_reused==0&&s_d3d.gpu_completion_fresh==1,
          "opt-in reuse remains gated while the Survival trace is unarmed");

    reset();survival_trace_armed=1;s_d3d.gpu_completion_ticket=5;completed=5;
    s_d3d.fence_values[0]=5;
    s_d3d.gpu_completion_ticket=0; /* Mirrors the production Execute invalidation. */
    step(WAIT_OBJECT_0,1,6);
    check(wait_for_gpu_checked()==0&&signals==1&&signal_value==6&&
          s_d3d.gpu_completion_ticket==6&&s_d3d.gpu_completion_reused==0&&
          s_d3d.gpu_completion_fresh==1,
          "a new Execute invalidation forces a fresh signal");

    reset();survival_trace_armed=0;s_d3d.gpu_completion_ticket=5;signal_hr=E_FAIL;
    check(wait_for_gpu_checked()!=0&&signals==1&&s_d3d.gpu_completion_ticket==0&&
          s_d3d.gpu_completion_reused==0,
          "failed fallback Signal clears the older reusable ticket");
    signal_hr=S_OK;survival_trace_armed=1;step(WAIT_OBJECT_0,1,6);
    check(wait_for_gpu_checked()==0&&signals==2&&signal_value==6&&
          s_d3d.gpu_completion_ticket==6&&s_d3d.gpu_completion_fresh==1&&
          s_d3d.gpu_completion_reused==0,
          "wait after failed Signal queues a new fresh ticket");

    reset();survival_trace_armed=1;
    check(move_to_next_frame()==0&&signal_value==5&&s_d3d.gpu_completion_ticket==5&&
          s_d3d.fence_values[1]==6,
          "frame Signal caches its actual ticket without caching the next counter value");
    step(WAIT_OBJECT_0,1,5);
    check(wait_for_gpu_checked()==0&&signals==1&&armed_value==5&&
          s_d3d.fence_values[1]==6&&s_d3d.gpu_completion_reused==1,
          "frame wait uses the actual Signal ticket rather than the next counter value");

    reset();survival_trace_armed=1;s_d3d.gpu_completion_ticket=5;
    prepare_mutates_ticket=1;prepare_ticket=7;step(WAIT_OBJECT_0,1,7);
    check(wait_for_gpu_checked()==0&&prepare_calls==1&&signals==0&&armed_value==7&&
          s_d3d.gpu_completion_reused==1,
          "prefix preparation runs first and its updated ticket is selected");

    reset();survival_trace_armed=1;s_d3d.gpu_completion_ticket=5;prepare_result=-1;
    check(wait_for_gpu_checked()!=0&&prepare_calls==1&&signals==0&&waits==0&&
          s_d3d.gpu_completion_ticket==5,
          "prefix preparation failure stops before ticket selection or fence operations");

    reset();survival_trace_armed=1;s_d3d.gpu_completion_ticket=5;
    s_d3d.frame_index=FRAME_COUNT;
    check(wait_for_gpu_checked()!=0&&signals==0&&waits==0&&
          s_d3d.gpu_completion_ticket==0&&s_d3d.frame_index==FRAME_COUNT,
          "corrupt frame index cannot reuse or signal a ticket");

    reset();survival_trace_armed=1;s_d3d.gpu_completion_ticket=5;
    s_d3d.fence_values[0]=UINT64_MAX-1;
    check(wait_for_gpu_checked()!=0&&signals==0&&waits==0&&
          s_d3d.gpu_completion_ticket==0&&s_d3d.fence_values[0]==UINT64_MAX-1,
          "near-overflow frame counter cannot reuse or wrap a ticket");
}
static void short_poll_disabled_case(){
    reset();survival_trace_armed=1;step(WAIT_OBJECT_0,1,5);
    check(wait_for_fence_value_checked(5,2000)==0&&arms==1&&waits==1&&
          qpf_calls==0&&qpc_calls==0,
          "unset or malformed short-poll flag keeps the original event wait");
}
static void short_poll_cases(){
    reset();survival_trace_armed=0;step(WAIT_OBJECT_0,1,5);
    check(wait_for_fence_value_checked(5,2000)==0&&arms==1&&waits==1&&
          qpf_calls==0&&qpc_calls==0,
          "unarmed Survival trace keeps the original checked event wait");

    reset();survival_trace_armed=1;
    check(wait_for_fence_value_checked(5,0)!=0&&arms==1&&waits==0&&
          qpf_calls==0&&qpc_calls==0,
          "zero timeout bypasses polling and preserves the checked timeout");

    reset();survival_trace_armed=1;complete_on_read=2;completed_on_read=5;
    check(wait_for_fence_value_checked(5,2000)==0&&completed_reads==2&&
          qpf_calls==1&&qpc_calls==2&&arms==0&&waits==0,
          "completion during polling returns only after the exact ticket is complete");

    reset();survival_trace_armed=1;complete_on_read=2;completed_on_read=UINT64_MAX;
    removed_hr=(HRESULT)0x887A0005u;
    check(wait_for_fence_value_checked(5,2000)!=0&&completed_reads==2&&
          arms==0&&waits==0&&removed_calls==1,
          "device removal during polling fails without arming or waiting");

    reset();survival_trace_armed=1;qpc_step=1000;
    complete_on_read=2;completed_on_read=4;step(WAIT_OBJECT_0,1,5);
    check(wait_for_fence_value_checked(5,2000)==0&&completed_reads==5&&
          arms==1&&waits==1&&armed_value==5,
          "a below-ticket poll falls back and accepts only the exact event-wait ticket");

    reset();survival_trace_armed=1;qpc_step=1000;
    step(WAIT_OBJECT_0,1,4);step(WAIT_OBJECT_0,4,5);
    check(wait_for_fence_value_checked(5,2000)==0&&arms==1&&waits==2&&
          armed_value==5&&completed==5,
          "pending polling preserves stale-wake checks for the exact ticket");

    reset();survival_trace_armed=1;qpc_step=1000;qpc_advance_tick=1;
    step(WAIT_TIMEOUT,6000,0);
    check(wait_for_fence_value_checked(5,6000)!=0&&arms==1&&waits==1&&
          last_wait==5997&&tick==6100,
          "poll time is included in the original total timeout deadline");

    reset();survival_trace_armed=1;qpc_step=0;step(WAIT_OBJECT_0,1,5);
    check(wait_for_fence_value_checked(5,6000)==0&&completed_reads==4099&&
          qpc_calls==4097&&arms==1&&waits==1&&armed_value==5,
          "a frozen QPC is bounded by 4096 checks before the event wait");

    reset();survival_trace_armed=1;qpc_step=1000;arm_hr=E_FAIL;
    check(wait_for_fence_value_checked(5,2000)!=0&&arms==1&&waits==0,
          "event-arm failure remains terminal after bounded polling");

    reset();survival_trace_armed=1;qpc_step=1000;step(WAIT_FAILED,1,0);
    check(wait_for_fence_value_checked(5,2000)!=0&&arms==1&&waits==1&&
          last_error_calls==1&&fake_last_error==0x162E,
          "OS wait failure remains terminal after bounded polling");

    int all_hits=1;
    for(unsigned i=0;i<256;++i){
        reset();survival_trace_armed=1;complete_on_read=2;completed_on_read=5;
        if(wait_for_fence_value_checked(5,2000)!=0||arms||waits)all_hits=0;
    }
    check(all_hits,"repeated short-poll hits stay bounded and avoid event registration");
}
static void short_poll_clock_case(int mode){
    reset();survival_trace_armed=1;step(WAIT_OBJECT_0,1,5);
    if(mode==0)qpf_succeeds=0;
    else if(mode==1)qpc_fail_call=1;
    else if(mode==2)qpc_backwards_call=2;
    else if(mode==3){qpc_fail_call=3;qpc_step=25;}
    else {qpc_backwards_call=3;qpc_step=1000;qpc_backwards_value=1001500;}
    check(wait_for_fence_value_checked(5,2000)==0&&arms==1&&waits==1&&
          (mode==0?qpf_calls==1&&qpc_calls==0:
           mode==1?qpf_calls==1&&qpc_calls==1:
           mode==2?qpf_calls==1&&qpc_calls==2:
           mode==3?qpf_calls==1&&qpc_calls==3:
                    qpf_calls==1&&qpc_calls==3),
          "QPC or frequency failure falls back to the checked event wait");
}
static void fence_wait_service_cases(){
    reset();completed=4;int context_tag=7;
    rsx_d3d12_backend_set_fence_wait_service(wait_service,&context_tag);
    wait_service_requested=2;wait_service_elapsed=0;
    check(wait_for_fence_value_checked(5,5)!=0&&wait_service_calls==3&&
          waits==3&&wait_service_remaining_history[0]==5&&
          wait_service_remaining_history[1]==3&&wait_service_remaining_history[2]==1&&
          last_wait==1&&completed==4,
          "expired slices loop while the exact fence remains pending");
    check(wait_service_context_seen==&context_tag,
          "registered service receives its same-thread context");

    reset();rsx_d3d12_backend_set_fence_wait_service(wait_service,nullptr);
    wait_service_requested=20;wait_service_elapsed=6;
    check(wait_for_fence_value_checked(5,10)!=0&&wait_service_calls==1&&
          waits==1&&last_wait==4&&tick==110,
          "callback time counts against the original deadline and slice is clamped");

    reset();rsx_d3d12_backend_set_fence_wait_service(wait_service,nullptr);
    wait_service_requested=0;
    check(wait_for_fence_value_checked(5,2)!=0&&wait_service_calls==2&&
          waits==2&&last_wait==1&&tick==102,
          "zero callback duration clamps to a one millisecond native wait");

    reset();rsx_d3d12_backend_set_fence_wait_service(wait_service,nullptr);
    wait_service_completes=1;
    check(wait_for_fence_value_checked(5,10)==0&&wait_service_calls==1&&
          completed_reads==3&&waits==0,
          "callback return is not readiness; only a checked exact ticket completes");

    reset();rsx_d3d12_backend_set_fence_wait_service(wait_service,nullptr);
    wait_service_removes=1;
    check(wait_for_fence_value_checked(5,10)!=0&&wait_service_calls==1&&
          completed_reads==3&&waits==0&&removed_calls==1,
          "device removal after callback fails before any event wait");

    reset();rsx_d3d12_backend_set_fence_wait_service(wait_service,nullptr);
    step(WAIT_OBJECT_0,1,4);step(WAIT_OBJECT_0,1,5);
    check(wait_for_fence_value_checked(5,10)==0&&wait_service_calls==2&&
          waits==2&&completed==5,
          "service preserves stale-wake and exact-ticket checks");

    reset();rsx_d3d12_backend_set_fence_wait_service(wait_service,nullptr);
    step(WAIT_FAILED,1,4);
    check(wait_for_fence_value_checked(5,10)!=0&&wait_service_calls==1&&
          waits==1&&last_error_calls==1&&fake_last_error==0x162E,
          "OS event failure still captures LastError before diagnostics");

    reset();rsx_d3d12_backend_set_fence_wait_service(wait_service,nullptr);
    wait_service_reenters=1;step(WAIT_OBJECT_0,2,5);
    check(wait_for_fence_value_checked(5,10)==0&&wait_service_calls==1&&
          wait_service_nested_result==0&&waits==1&&arms==2,
          "nested wait suppresses recursive service while retaining native checks");

    reset();rsx_d3d12_backend_set_fence_wait_service(wait_service,nullptr);
    rsx_d3d12_backend_set_fence_wait_service(nullptr,nullptr);
    step(WAIT_OBJECT_0,1,5);
    check(wait_for_fence_value_checked(5,10)==0&&wait_service_calls==0&&
          waits==1&&tick_reads==2&&completed_reads==3&&
          s_d3d12_fence_wait_service==nullptr&&
          s_d3d12_fence_wait_service_context==nullptr,
          "unregister restores the original path with no added timing or fence reads");

    reset();int cross_thread_context=9;
    rsx_d3d12_backend_set_fence_wait_service(wait_service,&cross_thread_context);
    step(WAIT_OBJECT_0,1,5);
    int cross_thread_result=-99;
    std::thread other_thread([&]{
        cross_thread_result=wait_for_fence_value_checked(5,10);
    });
    other_thread.join();
    check(cross_thread_result==0&&wait_service_calls==0&&waits==1,
          "a service registered on another thread is not visible here");

    reset();step(WAIT_OBJECT_0,1,5);
    check(wait_for_fence_value_checked(5,10)==0&&wait_service_calls==0&&
          tick_reads==2&&completed_reads==3,
          "unregistered waits retain the original clock and fence-read sequence");
}
int main(int argc,char** argv){
    if(argc>1&&strcmp(argv[1],"--reuse")==0){reuse_cases();
        printf("D3D12 fence reuse: %u checks, %u failures\n",checks,failures);
        return failures?1:0;}
    if(argc>1&&strcmp(argv[1],"--disabled")==0){default_off_case();
        printf("D3D12 fence reuse-off: %u checks, %u failures\n",checks,failures);
        return failures?1:0;}
    if(argc>1&&strcmp(argv[1],"--shortpoll-disabled")==0){short_poll_disabled_case();
        printf("D3D12 fence short-poll-off: %u checks, %u failures\n",checks,failures);
        return failures?1:0;}
    if(argc>1&&strcmp(argv[1],"--shortpoll")==0){short_poll_cases();
        printf("D3D12 fence short-poll: %u checks, %u failures\n",checks,failures);
        return failures?1:0;}
    if(argc>1&&strcmp(argv[1],"--shortpoll-clock")==0){
        if(argc<3)return 2;short_poll_clock_case(atoi(argv[2]));
        printf("D3D12 fence short-poll-clock: %u checks, %u failures\n",checks,failures);
        return failures?1:0;}
    cases(0);cases(1);fence_wait_service_cases();
    default_off_case();
    printf("D3D12 fence waits: %u checks, %u failures\n",checks,failures);
    return failures?1:0;
}
'''.replace("PRODUCTION", "\n".join(bodies)).replace("COMPLETION_REASONS", reasons).replace("COMPLETION_NAMES", names)
    clang = shutil.which("clang++") or shutil.which("g++") or r"C:\Program Files\LLVM\bin\clang++.exe"
    with tempfile.TemporaryDirectory(prefix="tj-fence-wait-") as temp:
        cpp, exe = Path(temp) / "fence.cpp", Path(temp) / "fence.exe"
        cpp.write_text(source, encoding="utf-8")
        subprocess.run([clang, "-std=c++20", "-O2", str(cpp), "-o", str(exe)], check=True)
        default_env = os.environ.copy()
        default_env.pop("TJ_GPU_COMPLETION_REUSE", None)
        default_env.pop("TJ_FENCE_SHORT_POLL", None)
        baseline = subprocess.run([str(exe)], check=False, env=default_env,
                                  capture_output=True, text=True)
        reuse_env = default_env.copy()
        reuse_env["TJ_GPU_COMPLETION_REUSE"] = "1"
        opted_in = subprocess.run([str(exe), "--reuse"], check=False, env=reuse_env,
                                  capture_output=True, text=True)
        disabled_env = default_env.copy()
        disabled_env["TJ_GPU_COMPLETION_REUSE"] = "0"
        explicit_off = subprocess.run([str(exe), "--disabled"], check=False, env=disabled_env,
                                      capture_output=True, text=True)
        short_poll_env = default_env.copy()
        short_poll_env["TJ_FENCE_SHORT_POLL"] = "1"
        short_poll = subprocess.run([str(exe), "--shortpoll"], check=False,
                                    env=short_poll_env, capture_output=True, text=True)
        poll_reports = [line for line in short_poll.stderr.splitlines()
                        if line.startswith("[D3D12-FENCE-POLL]")]
        assert len(poll_reports) == 2, short_poll.stderr
        assert "tick_ms=" in poll_reports[0] and "attempts=1 " in poll_reports[0], poll_reports
        assert "timed=1 invalid=0 qpc_hz=10000000" in poll_reports[0], poll_reports
        assert "attempts=256 " in poll_reports[1] and "hits=249 " in poll_reports[1], poll_reports
        assert "timed=256 invalid=0 qpc_hz=10000000" in poll_reports[1], poll_reports
        assert "pending=6 failures=1 clock_fallbacks=0" in poll_reports[1], poll_reports
        clock_fallbacks = []
        for mode in range(5):
            result = subprocess.run([str(exe), "--shortpoll-clock", str(mode)], check=False,
                                    env=short_poll_env, capture_output=True, text=True)
            assert result.returncode == 0, result.stdout + result.stderr
            reports = [line for line in result.stderr.splitlines()
                       if line.startswith("[D3D12-FENCE-POLL]")]
            assert len(reports) == 1 and "attempts=1 " in reports[0] and \
                "hits=0 pending=0 failures=0 clock_fallbacks=1" in reports[0], result.stderr
            assert "timed=0 invalid=1" in reports[0], reports
            clock_fallbacks.append(result)
        strict_flag_results = []
        for value in (None, "0", "01", "true", "1 "):
            env = default_env.copy()
            if value is not None:
                env["TJ_FENCE_SHORT_POLL"] = value
            strict_flag_results.append(subprocess.run(
                [str(exe), "--shortpoll-disabled"], check=False, env=env,
                capture_output=True, text=True))
        assert "hr=0x800704D2" in baseline.stderr, baseline.stderr
        assert "hr=0x800704D2" in opted_in.stderr, opted_in.stderr
        assert "[D3D12-FENCE-POLL]" not in baseline.stderr, baseline.stderr
        assert short_poll.returncode == 0, short_poll.stdout + short_poll.stderr
        assert all(result.returncode == 0 for result in strict_flag_results), [
            result.stdout + result.stderr for result in strict_flag_results if result.returncode
        ]
        sys.stdout.write(baseline.stdout)
        sys.stdout.write(opted_in.stdout)
        sys.stdout.write(explicit_off.stdout)
        sys.stdout.write(short_poll.stdout)
        for result in clock_fallbacks + strict_flag_results:
            sys.stdout.write(result.stdout)
        return (baseline.returncode or opted_in.returncode or explicit_off.returncode or
                short_poll.returncode or next((r.returncode for r in strict_flag_results
                                               if r.returncode), 0))


if __name__ == "__main__":
    raise SystemExit(main())
