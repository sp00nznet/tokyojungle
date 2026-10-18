"""Compile production host-cost accounting with deterministic clocks, no GPU."""
from pathlib import Path
import os
import re
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def function(text, signature):
    """Extract one C function while ignoring braces in comments and literals."""
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


def main():
    backend = (ROOT.parent / 'ps3recomp/libs/video/rsx_d3d12_backend.c').read_text(encoding='utf-8')
    header = (ROOT.parent / 'ps3recomp/libs/video/rsx_d3d12_backend.h').read_text(encoding='utf-8')
    a = header.index('typedef enum RsxD3D12CompletionReason')
    reasons = header[a:header.index('} RsxD3D12CompletionReason;', a) + len('} RsxD3D12CompletionReason;')]
    a = backend.index('static const char* const s_tj_completion_names[')
    names_end = backend.index('};', a) + len('};')
    cost_start = backend.index('enum { TJ_COST_SETUP', names_end)
    cost_end = backend.index('static int tj_gcm2d_backend_perf_enabled(', cost_start)
    # Keep unrelated prefix-wait tracing out of this cost-helper fixture.
    code = backend[a:names_end] + '\n' + backend[cost_start:cost_end]

    render = function(backend, 'static void render_frame(')
    opening = render.index('prefix_cost = tj_prefix_cost_mark(prefix_cost, TJ_COST_OPENING,')
    sample_start = render.index('record_cost.mark = prefix_cost;', opening)
    phase_calls = (
        'tj_record_cost_mark(&record_cost, TJ_RECORD_TARGET_SETUP);',
        'tj_record_cost_mark(&record_cost, TJ_RECORD_ENDPOINTS);',
        'tj_record_cost_mark(&record_cost, TJ_RECORD_PREFLIGHT);',
        'tj_record_cost_mark(&record_cost, TJ_RECORD_SCRATCH);',
        'tj_record_cost_mark(&record_cost, TJ_RECORD_TEXTURE_FP_PREPARE);',
        'tj_record_cost_mark(&record_cost, TJ_RECORD_MANIFEST_REPLAY);',
    )
    phase_positions = [render.index(call) for call in phase_calls]
    assert opening < sample_start < phase_positions[0]
    assert phase_positions == sorted(phase_positions)
    assert render.index('&gcm2d_backend_sample.depth_prepare_ticks') < phase_positions[0]
    assert phase_positions[0] < render.index('&gcm2d_backend_sample.endpoint_ticks')
    assert render.index('exact_writeback_preflight() != 0') < phase_positions[2]
    assert phase_positions[2] < render.index('scratch_need[MAX_OFF_RT_SCRATCH]')
    assert render.index('off_rt_scratch_prepare(') < phase_positions[3]
    assert phase_positions[3] < render.index('/* Per-batch VP textures')
    assert render.index('&gcm2d_backend_sample.texture_prepare_ticks') < phase_positions[4]
    assert phase_positions[4] < render.index('/* Survival-only render manifest') < phase_positions[5]
    assert render.index('&gcm2d_backend_sample.draw_replay_ticks') < phase_positions[5]
    record_ends = re.findall(
        r'prefix_cost = tj_prefix_cost_mark\(prefix_cost, TJ_COST_RECORD,[^;]+;\s*'
        r'tj_record_cost_finish\(&record_cost, prefix_cost\);', render)
    assert len(record_ends) == 2, 'both existing RECORD endpoints must finish the nested sample'
    finish = function(code, 'static void tj_record_cost_finish(')
    assert 'QueryPerformanceCounter' not in finish, 'parent endpoint must be reused without another QPC'
    for signature in (
        'static int d3d12_prefix_overlap_eligible(',
        'static int d3d12_prefix_queue_ahead_record_bank_ready(',
        'static int d3d12_texture_release_nowait_ready(',
    ):
        assert 'tj_record_cost_' not in function(backend, signature)
    mirror = function(backend, 'static int gpu_mirror_get(')
    map_failure = mirror.index('if (FAILED(m->up->lpVtbl->Map(m->up, 0, &nr, &mapped)) || !mapped) {')
    map_cleanup = mirror[map_failure:mirror.index('s_d3d.gcm2d_endpoint_reason = "mirror-map"', map_failure)]
    assert 'if (mapped) m->up->lpVtbl->Unmap(m->up, 0, NULL);' in map_cleanup
    assert map_cleanup.index('if (mapped) m->up->lpVtbl->Unmap') < map_cleanup.index('m->up->lpVtbl->Release')

    source = r'''
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
using u32=uint32_t;using u64=uint64_t;using LONGLONG=int64_t;
struct LARGE_INTEGER {LONGLONG QuadPart;};
static LONGLONG clock_tick=100,frequency=1000000;
static u64 wall=1000;static int armed=1,clock_ok=1,clock_reads,checks,failures;
static int QueryPerformanceCounter(LARGE_INTEGER* p){++clock_reads;p->QuadPart=clock_tick;return clock_ok;}
static int QueryPerformanceFrequency(LARGE_INTEGER* p){p->QuadPart=frequency;return 1;}
static u64 GetTickCount64(){return wall;}
static unsigned long GetCurrentProcessId(){return 1234;}
static int rsx_survival_trace_is_armed(){return armed;}
static void tj_prefix_gpu_cost_report(u64,u64){}
#define CHECK(c) do{++checks;if(!(c)){++failures;fprintf(stderr,"FAIL line %d: %s\n",__LINE__,#c);}}while(0)
REASONS
static RsxD3D12CompletionReason s_tj_completion_reason=RSX_COMPLETION_OTHER;
static struct {u32 prefix_owner_active,prefix_prepare_owner;} s_d3d;
PRODUCTION
int main(int argc,char** argv){
    int enabled=argc>1&&atoi(argv[1]);
    CHECK(tj_prefix_cost_enabled()==enabled);
    if(!enabled){CHECK(!tj_prefix_cost_begin()&&!clock_reads);CHECK(!tj_main_cost_mark(0,0,0)&&!clock_reads);CHECK(!tj_prefix_sampler_cost_enabled()&&!clock_reads);TJRecordCostSample off={};off.reason=tj_record_cost_reason(999);CHECK(off.reason==RSX_COMPLETION_OTHER);CHECK(!tj_record_cost_mark(&off,TJ_RECORD_TARGET_SETUP)&&!clock_reads);tj_record_cost_finish(&off,0);TJMirrorUploadCostSample upload_off={};tj_prefix_mirror_upload_cost_begin(&upload_off);tj_prefix_mirror_upload_cost_phase(&upload_off,TJ_MIRROR_UPLOAD_OWNER_GUARD);tj_prefix_mirror_upload_cost_finish(&upload_off,640,0,1);CHECK(clock_reads==0&&!s_tj_mirror_upload_cost.calls[RSX_COMPLETION_OTHER]);CHECK(!s_tj_prefix_cost.record_samples[RSX_COMPLETION_OTHER]&&!s_tj_prefix_cost.record_invalid[RSX_COMPLETION_OTHER]&&!clock_reads);tj_prefix_cost_report(3000);return failures?1:0;}
    armed=0;s_tj_prefix_cost.start_ms=1000;CHECK(!tj_prefix_cost_begin()&&!clock_reads);CHECK(!tj_prefix_sampler_cost_enabled()&&!clock_reads);TJRecordCostSample unarmed={};unarmed.reason=RSX_COMPLETION_INPUT;unarmed.mark=tj_prefix_cost_begin();CHECK(!unarmed.mark);CHECK(!tj_record_cost_mark(&unarmed,TJ_RECORD_TARGET_SETUP)&&!clock_reads);tj_record_cost_finish(&unarmed,0);TJMirrorUploadCostSample upload_unarmed={};tj_prefix_mirror_upload_cost_begin(&upload_unarmed);tj_prefix_mirror_upload_cost_phase(&upload_unarmed,TJ_MIRROR_UPLOAD_OWNER_GUARD);tj_prefix_mirror_upload_cost_finish(&upload_unarmed,640,0,1);CHECK(clock_reads==0&&!s_tj_mirror_upload_cost.calls[RSX_COMPLETION_INPUT]);CHECK(!s_tj_prefix_cost.record_samples[RSX_COMPLETION_INPUT]&&!s_tj_prefix_cost.record_invalid[RSX_COMPLETION_INPUT]&&!clock_reads);armed=1;s_tj_prefix_cost.start_ms=0;
    CHECK(!tj_prefix_sampler_cost_enabled()&&!clock_reads);
    CHECK(tj_prefix_sampler_cost_heap()==TJ_SAMPLER_HEAP_ORDINARY);
    s_d3d.prefix_owner_active=1;CHECK(tj_prefix_sampler_cost_heap()==TJ_SAMPLER_HEAP_OWNER0);
    s_d3d.prefix_owner_active=2;CHECK(tj_prefix_sampler_cost_heap()==TJ_SAMPLER_HEAP_OWNER1);
    s_d3d.prefix_owner_active=0;
    LONGLONG start=tj_prefix_cost_begin();CHECK(tj_prefix_sampler_cost_enabled());clock_tick=175;
    start=tj_prefix_cost_mark(start,TJ_COST_OPENING,RSX_COMPLETION_TEXTURE_RELEASE);
    clock_tick=250;start=tj_prefix_cost_mark(start,TJ_COST_RECORD,RSX_COMPLETION_TEXTURE_RELEASE);
    CHECK(s_tj_prefix_cost.ticks[RSX_COMPLETION_TEXTURE_RELEASE][TJ_COST_OPENING]==75);
    CHECK(s_tj_prefix_cost.ticks[RSX_COMPLETION_TEXTURE_RELEASE][TJ_COST_RECORD]==75);
    clock_tick=280;tj_prefix_cost_mark(start,TJ_COST_DEFERRED,RSX_COMPLETION_INPUT);
    CHECK(s_tj_prefix_cost.calls[RSX_COMPLETION_INPUT][TJ_COST_DEFERRED]==1);
    LONGLONG main_start=tj_prefix_cost_begin(),parent_start=main_start;
    clock_tick=290;main_start=tj_main_cost_mark(main_start,TJ_MAIN_MAP_CONVERT,RSX_COMPLETION_LOCAL_MAIN_COPY);
    clock_tick=310;main_start=tj_main_cost_mark(main_start,TJ_MAIN_WORLD_COMMIT,RSX_COMPLETION_LOCAL_MAIN_COPY);
    clock_tick=340;tj_main_cost_mark(main_start,TJ_MAIN_CLEANUP,RSX_COMPLETION_LOCAL_MAIN_COPY);
    clock_tick=350;tj_prefix_cost_mark(parent_start,TJ_COST_COMMIT,RSX_COMPLETION_LOCAL_MAIN_COPY);
    CHECK(s_tj_prefix_cost.main_ticks[RSX_COMPLETION_LOCAL_MAIN_COPY][TJ_MAIN_MAP_CONVERT]==10);
    CHECK(s_tj_prefix_cost.main_ticks[RSX_COMPLETION_LOCAL_MAIN_COPY][TJ_MAIN_WORLD_COMMIT]==20);
    CHECK(s_tj_prefix_cost.main_ticks[RSX_COMPLETION_LOCAL_MAIN_COPY][TJ_MAIN_CLEANUP]==30);
    CHECK(s_tj_prefix_cost.ticks[RSX_COMPLETION_LOCAL_MAIN_COPY][TJ_COST_COMMIT]==70);
    auto complete_record = [&](unsigned reason,LONGLONG base){
        clock_tick=base;LONGLONG parent=tj_prefix_cost_begin();
        clock_tick=base+10;parent=tj_prefix_cost_mark(parent,TJ_COST_OPENING,reason);
        TJRecordCostSample sample={};sample.mark=parent;sample.reason=tj_record_cost_reason(reason);
        clock_tick=base+20;tj_record_cost_mark(&sample,TJ_RECORD_TARGET_SETUP);
        clock_tick=base+25;tj_record_cost_mark(&sample,TJ_RECORD_ENDPOINTS);
        clock_tick=base+35;tj_record_cost_mark(&sample,TJ_RECORD_PREFLIGHT);
        clock_tick=base+42;tj_record_cost_mark(&sample,TJ_RECORD_SCRATCH);
        clock_tick=base+58;tj_record_cost_mark(&sample,TJ_RECORD_TEXTURE_FP_PREPARE);
        clock_tick=base+81;tj_record_cost_mark(&sample,TJ_RECORD_MANIFEST_REPLAY);
        clock_tick=base+90;const int reads_before_end=clock_reads;
        parent=tj_prefix_cost_mark(parent,TJ_COST_RECORD,reason);
        const int reads_after_end=clock_reads;
        tj_record_cost_finish(&sample,parent);
        CHECK(clock_reads==reads_after_end&&reads_after_end==reads_before_end+1);
        CHECK(!sample.mark&&!sample.invalid&&sample.valid_mask==((1u<<TJ_RECORD_COUNT)-1u));
        CHECK(s_tj_prefix_cost.record_samples[sample.reason]==1);
        CHECK(s_tj_prefix_cost.ticks[sample.reason][TJ_COST_RECORD]==80);
        const u64 expected[TJ_RECORD_COUNT]={10,5,10,7,16,23,9};
        u64 total=0;
        for(unsigned p=0;p<TJ_RECORD_COUNT;p++){
            CHECK(s_tj_prefix_cost.record_calls[sample.reason][p]==1);
            CHECK(s_tj_prefix_cost.record_ticks[sample.reason][p]==expected[p]);
            total+=s_tj_prefix_cost.record_ticks[sample.reason][p];
        }
        CHECK(total==s_tj_prefix_cost.ticks[sample.reason][TJ_COST_RECORD]);
    };
    complete_record(RSX_COMPLETION_ACQUIRE,600);
    complete_record(999,800);
    CHECK(tj_record_cost_reason(999)==RSX_COMPLETION_OTHER);
    const u64 aborted_samples=s_tj_prefix_cost.record_samples[RSX_COMPLETION_CAPTURE];
    {
        TJRecordCostSample aborted={};aborted.mark=900;aborted.reason=RSX_COMPLETION_CAPTURE;
        clock_tick=910;tj_record_cost_mark(&aborted,TJ_RECORD_TARGET_SETUP);
    }
    CHECK(s_tj_prefix_cost.record_samples[RSX_COMPLETION_CAPTURE]==aborted_samples);
    CHECK(!s_tj_prefix_cost.record_calls[RSX_COMPLETION_CAPTURE][TJ_RECORD_TARGET_SETUP]);
    TJRecordCostSample bad_clock={};bad_clock.mark=920;bad_clock.reason=RSX_COMPLETION_INPUT;
    clock_ok=0;clock_tick=930;CHECK(!tj_record_cost_mark(&bad_clock,TJ_RECORD_TARGET_SETUP));clock_ok=1;
    CHECK(bad_clock.invalid&&!bad_clock.mark&&s_tj_prefix_cost.record_invalid[RSX_COMPLETION_INPUT]==1);
    const int reads_before_bad_finish=clock_reads;tj_record_cost_finish(&bad_clock,940);
    CHECK(clock_reads==reads_before_bad_finish&&s_tj_prefix_cost.record_invalid[RSX_COMPLETION_INPUT]==1);
    TJRecordCostSample backwards={};backwards.mark=950;backwards.reason=RSX_COMPLETION_CAPACITY;
    clock_tick=949;CHECK(!tj_record_cost_mark(&backwards,TJ_RECORD_TARGET_SETUP));
    CHECK(s_tj_prefix_cost.record_invalid[RSX_COMPLETION_CAPACITY]==1&&!s_tj_prefix_cost.record_samples[RSX_COMPLETION_CAPACITY]);
    TJRecordCostSample bad_phase={};bad_phase.mark=960;bad_phase.reason=RSX_COMPLETION_CLEAR;
    const int reads_before_bad_phase=clock_reads;
    CHECK(!tj_record_cost_mark(&bad_phase,TJ_RECORD_COUNT));
    CHECK(clock_reads==reads_before_bad_phase&&s_tj_prefix_cost.record_invalid[RSX_COMPLETION_CLEAR]==1);
    TJRecordCostSample missing_phase={};missing_phase.mark=970;missing_phase.reason=RSX_COMPLETION_DRAIN;
    clock_tick=980;tj_record_cost_mark(&missing_phase,TJ_RECORD_TARGET_SETUP);
    const int reads_before_missing_finish=clock_reads;tj_record_cost_finish(&missing_phase,990);
    CHECK(clock_reads==reads_before_missing_finish&&s_tj_prefix_cost.record_invalid[RSX_COMPLETION_DRAIN]==1);
    TJRecordCostSample bad_parent_end={};bad_parent_end.mark=1000;bad_parent_end.reason=RSX_COMPLETION_DRAIN;
    for(unsigned p=0;p<TJ_RECORD_CLOSE_TAIL;p++){
        clock_tick=1001+p;tj_record_cost_mark(&bad_parent_end,p);
    }
    const int reads_before_bad_parent=clock_reads;
    tj_record_cost_finish(&bad_parent_end,bad_parent_end.mark-1);
    CHECK(clock_reads==reads_before_bad_parent);
    CHECK(s_tj_prefix_cost.record_invalid[RSX_COMPLETION_DRAIN]==2&&!s_tj_prefix_cost.record_samples[RSX_COMPLETION_DRAIN]);
    s_tj_completion_reason=RSX_COMPLETION_TEXTURE_RELEASE;
    TJMirrorUploadCostSample upload={};clock_tick=1000;tj_prefix_mirror_upload_cost_begin(&upload);
    CHECK(upload.active&&!upload.clock_invalid&&upload.path==TJ_MIRROR_UPLOAD_INLINE);
    clock_tick=1010;tj_prefix_mirror_upload_cost_phase(&upload,TJ_MIRROR_UPLOAD_OWNER_GUARD);
    clock_tick=1030;tj_prefix_mirror_upload_cost_phase(&upload,TJ_MIRROR_UPLOAD_MAP);
    clock_tick=1180;tj_prefix_mirror_upload_cost_phase(&upload,TJ_MIRROR_UPLOAD_CONVERT);
    clock_tick=1200;tj_prefix_mirror_upload_cost_phase(&upload,TJ_MIRROR_UPLOAD_UNMAP);
    clock_tick=1250;tj_prefix_mirror_upload_cost_phase(&upload,TJ_MIRROR_UPLOAD_TRANSITION_COPY);
    tj_prefix_mirror_upload_cost_finish(&upload,640,0,720);
    CHECK(s_tj_mirror_upload_cost.calls[RSX_COMPLETION_TEXTURE_RELEASE]==1);
    CHECK(s_tj_mirror_upload_cost.success[RSX_COMPLETION_TEXTURE_RELEASE]==1);
    CHECK(s_tj_mirror_upload_cost.samples[RSX_COMPLETION_TEXTURE_RELEASE]==1);
    CHECK(s_tj_mirror_upload_cost.total_ticks[RSX_COMPLETION_TEXTURE_RELEASE]==250);
    const u64 upload_expected[TJ_MIRROR_UPLOAD_PHASE_COUNT]={10,20,150,20,50};u64 upload_sum=0;
    for(unsigned p=0;p<TJ_MIRROR_UPLOAD_PHASE_COUNT;p++){
        CHECK(s_tj_mirror_upload_cost.phase_calls[RSX_COMPLETION_TEXTURE_RELEASE][p]==1);
        CHECK(s_tj_mirror_upload_cost.phase_ticks[RSX_COMPLETION_TEXTURE_RELEASE][p]==upload_expected[p]);
        upload_sum+=s_tj_mirror_upload_cost.phase_ticks[RSX_COMPLETION_TEXTURE_RELEASE][p];
    }
    CHECK(upload_sum==s_tj_mirror_upload_cost.total_ticks[RSX_COMPLETION_TEXTURE_RELEASE]);
    CHECK(s_tj_mirror_upload_cost.whole_max_ticks[RSX_COMPLETION_TEXTURE_RELEASE]==250);
    CHECK(s_tj_mirror_upload_cost.path_samples[RSX_COMPLETION_TEXTURE_RELEASE][TJ_MIRROR_UPLOAD_INLINE]==1);
    CHECK(s_tj_mirror_upload_cost.path_ticks[RSX_COMPLETION_TEXTURE_RELEASE][TJ_MIRROR_UPLOAD_INLINE]==250);
    CHECK(s_tj_mirror_upload_cost.physical_bytes[RSX_COMPLETION_TEXTURE_RELEASE]==640ULL*720*4);
    CHECK(s_tj_mirror_upload_cost.converted_bytes[RSX_COMPLETION_TEXTURE_RELEASE]==640ULL*720*4);
    clock_tick=2000;tj_prefix_mirror_upload_cost_begin(&upload);
    tj_prefix_mirror_upload_cost_skip(&upload,TJ_MIRROR_UPLOAD_OWNER_GUARD);
    tj_prefix_mirror_upload_cost_skip(&upload,TJ_MIRROR_UPLOAD_MAP);
    tj_prefix_mirror_upload_cost_skip(&upload,TJ_MIRROR_UPLOAD_CONVERT);
    tj_prefix_mirror_upload_cost_skip(&upload,TJ_MIRROR_UPLOAD_UNMAP);
    clock_tick=2020;tj_prefix_mirror_upload_cost_phase(&upload,TJ_MIRROR_UPLOAD_TRANSITION_COPY);
    tj_prefix_mirror_upload_cost_finish(&upload,640,640,2);
    CHECK(s_tj_mirror_upload_cost.samples[RSX_COMPLETION_TEXTURE_RELEASE]==2);
    CHECK(s_tj_mirror_upload_cost.phase_calls[RSX_COMPLETION_TEXTURE_RELEASE][TJ_MIRROR_UPLOAD_MAP]==2);
    CHECK(s_tj_mirror_upload_cost.phase_ticks[RSX_COMPLETION_TEXTURE_RELEASE][TJ_MIRROR_UPLOAD_MAP]==20);
    CHECK(s_tj_mirror_upload_cost.converted_bytes[RSX_COMPLETION_TEXTURE_RELEASE]==640ULL*720*4);
    CHECK(s_tj_mirror_upload_cost.path_samples[RSX_COMPLETION_TEXTURE_RELEASE][TJ_MIRROR_UPLOAD_INLINE]==2);
    s_d3d.prefix_prepare_owner=1;clock_tick=3000;tj_prefix_mirror_upload_cost_begin(&upload);
    CHECK(upload.path==TJ_MIRROR_UPLOAD_PREPARED);
    clock_tick=3001;tj_prefix_mirror_upload_cost_phase(&upload,TJ_MIRROR_UPLOAD_OWNER_GUARD);
    clock_tick=3003;tj_prefix_mirror_upload_cost_phase(&upload,TJ_MIRROR_UPLOAD_MAP);
    clock_tick=3008;tj_prefix_mirror_upload_cost_phase(&upload,TJ_MIRROR_UPLOAD_CONVERT);
    clock_tick=3009;tj_prefix_mirror_upload_cost_phase(&upload,TJ_MIRROR_UPLOAD_UNMAP);
    clock_tick=3012;tj_prefix_mirror_upload_cost_phase(&upload,TJ_MIRROR_UPLOAD_TRANSITION_COPY);
    tj_prefix_mirror_upload_cost_finish(&upload,640,0,1);s_d3d.prefix_prepare_owner=0;
    CHECK(s_tj_mirror_upload_cost.path_samples[RSX_COMPLETION_TEXTURE_RELEASE][TJ_MIRROR_UPLOAD_PREPARED]==1);
    CHECK(s_tj_mirror_upload_cost.path_ticks[RSX_COMPLETION_TEXTURE_RELEASE][TJ_MIRROR_UPLOAD_PREPARED]==12);
    clock_tick=4000;tj_prefix_mirror_upload_cost_begin(&upload);
    clock_tick=4010;tj_prefix_mirror_upload_cost_phase(&upload,TJ_MIRROR_UPLOAD_OWNER_GUARD);
    clock_tick=4020;tj_prefix_mirror_upload_cost_phase(&upload,TJ_MIRROR_UPLOAD_MAP);
    tj_prefix_mirror_upload_cost_abort(&upload,1);
    CHECK(s_tj_mirror_upload_cost.calls[RSX_COMPLETION_TEXTURE_RELEASE]==4);
    CHECK(s_tj_mirror_upload_cost.aborted[RSX_COMPLETION_TEXTURE_RELEASE]==1);
    CHECK(s_tj_mirror_upload_cost.failures[RSX_COMPLETION_TEXTURE_RELEASE]==1);
    clock_tick=5000;tj_prefix_mirror_upload_cost_begin(&upload);
    clock_tick=5004;tj_prefix_mirror_upload_cost_phase(&upload,TJ_MIRROR_UPLOAD_OWNER_GUARD);
    tj_prefix_mirror_upload_cost_abort(&upload,0);
    CHECK(s_tj_mirror_upload_cost.aborted[RSX_COMPLETION_TEXTURE_RELEASE]==2);
    clock_tick=6000;tj_prefix_mirror_upload_cost_begin(&upload);clock_ok=0;
    tj_prefix_mirror_upload_cost_phase(&upload,TJ_MIRROR_UPLOAD_OWNER_GUARD);clock_ok=1;
    tj_prefix_mirror_upload_cost_finish(&upload,640,0,1);
    CHECK(s_tj_mirror_upload_cost.invalid[RSX_COMPLETION_TEXTURE_RELEASE]==1);
    CHECK(s_tj_mirror_upload_cost.samples[RSX_COMPLETION_TEXTURE_RELEASE]==3);
    clock_tick=7000;tj_prefix_mirror_upload_cost_begin(&upload);
    clock_tick=7010;tj_prefix_mirror_upload_cost_phase(&upload,TJ_MIRROR_UPLOAD_OWNER_GUARD);
    clock_tick=7009;tj_prefix_mirror_upload_cost_phase(&upload,TJ_MIRROR_UPLOAD_MAP);
    tj_prefix_mirror_upload_cost_finish(&upload,640,0,1);
    CHECK(s_tj_mirror_upload_cost.invalid[RSX_COMPLETION_TEXTURE_RELEASE]==2);
    CHECK(s_tj_mirror_upload_cost.samples[RSX_COMPLETION_TEXTURE_RELEASE]==3);
    CHECK(s_tj_mirror_upload_cost.total_ticks[RSX_COMPLETION_TEXTURE_RELEASE]==282);
    tj_prefix_cost_report(1999);CHECK(s_tj_prefix_cost.start_ms==1000);
    tj_prefix_cost_report(2000);CHECK(s_tj_prefix_cost.start_ms==2000);
    CHECK(!s_tj_prefix_cost.calls[RSX_COMPLETION_TEXTURE_RELEASE][TJ_COST_RECORD]);
    CHECK(!s_tj_prefix_cost.main_calls[RSX_COMPLETION_LOCAL_MAIN_COPY][TJ_MAIN_MAP_CONVERT]);
    CHECK(!s_tj_prefix_cost.record_samples[RSX_COMPLETION_ACQUIRE]);
    CHECK(!s_tj_prefix_cost.record_calls[RSX_COMPLETION_ACQUIRE][TJ_RECORD_TARGET_SETUP]);
    CHECK(!s_tj_prefix_cost.record_ticks[RSX_COMPLETION_ACQUIRE][TJ_RECORD_TARGET_SETUP]);
    CHECK(!s_tj_prefix_cost.record_invalid[RSX_COMPLETION_DRAIN]);
    CHECK(!s_tj_mirror_upload_cost.calls[RSX_COMPLETION_TEXTURE_RELEASE]);
    CHECK(!s_tj_mirror_upload_cost.samples[RSX_COMPLETION_TEXTURE_RELEASE]);
    start=tj_prefix_cost_begin();clock_tick=279;
    CHECK(!tj_main_cost_mark(start,TJ_MAIN_MAP_CONVERT,0)&&s_tj_prefix_cost.invalid==1);
    CHECK(!tj_prefix_cost_mark(start,TJ_COST_EXECUTE,0)&&s_tj_prefix_cost.invalid==2);
    clock_ok=0;CHECK(!tj_prefix_cost_begin()&&s_tj_prefix_cost.invalid==3);clock_ok=1;
    s_tj_prefix_cost_frequency.QuadPart=0;
    CHECK(!tj_prefix_cost_begin()&&s_tj_prefix_cost.invalid==4);
    s_tj_prefix_cost_frequency.QuadPart=1000000;clock_tick=400;start=tj_prefix_cost_begin();clock_tick=410;
    tj_prefix_cost_mark(start,TJ_COST_EXECUTE,999);
    CHECK(s_tj_prefix_cost.calls[RSX_COMPLETION_OTHER][TJ_COST_EXECUTE]==1);
    const unsigned sampler_reason=RSX_COMPLETION_TEXTURE_RELEASE;
    const unsigned ordinary=TJ_SAMPLER_HEAP_ORDINARY,owner0=TJ_SAMPLER_HEAP_OWNER0,owner1=TJ_SAMPLER_HEAP_OWNER1;
    clock_tick=500;LONGLONG ordinary_start=tj_prefix_sampler_cost_lookup_begin(sampler_reason,ordinary);clock_tick=510;
    tj_prefix_sampler_cost_finish(sampler_reason,ordinary,ordinary_start,0);
    clock_tick=520;LONGLONG owner0_start=tj_prefix_sampler_cost_lookup_begin(sampler_reason,owner0);clock_tick=530;
    LONGLONG descriptor_start=tj_prefix_sampler_cost_qpc();clock_tick=540;
    tj_prefix_sampler_cost_finish(sampler_reason,owner0,owner0_start,descriptor_start);
    clock_tick=550;LONGLONG owner1_start=tj_prefix_sampler_cost_lookup_begin(sampler_reason,owner1);clock_tick=560;
    tj_prefix_sampler_cost_finish(sampler_reason,owner1,owner1_start,0);
    CHECK(s_tj_prefix_sampler_cost.lookup_total==3);
    CHECK(s_tj_prefix_sampler_cost.lookup_ticks[sampler_reason][ordinary]==10);
    CHECK(s_tj_prefix_sampler_cost.lookup_ticks[sampler_reason][owner0]==20);
    CHECK(s_tj_prefix_sampler_cost.descriptor_build_ticks[sampler_reason][owner0]==10);
    CHECK(s_tj_prefix_sampler_cost.lookup_ticks[sampler_reason][owner1]==10);
    ++s_tj_prefix_sampler_cost.exact_hits[sampler_reason][owner0];
    ++s_tj_prefix_sampler_cost.new_tables[sampler_reason][owner0];
    s_tj_prefix_sampler_cost.create_sampler_writes[sampler_reason][owner0]=20;
    ++s_tj_prefix_sampler_cost.capacity_failures[sampler_reason][owner1];
    ++s_tj_prefix_sampler_cost.heap_failures[sampler_reason][ordinary];
    clock_ok=0;CHECK(!tj_prefix_sampler_cost_qpc()&&s_tj_prefix_sampler_cost.invalid==1);clock_ok=1;
    clock_tick=570;LONGLONG regressing_start=tj_prefix_sampler_cost_qpc();clock_tick=569;
    const u64 owner1_ticks=s_tj_prefix_sampler_cost.lookup_ticks[sampler_reason][owner1];
    tj_prefix_sampler_cost_finish(sampler_reason,owner1,regressing_start,0);
    CHECK(s_tj_prefix_sampler_cost.invalid==2);
    CHECK(s_tj_prefix_sampler_cost.lookup_ticks[sampler_reason][owner1]==owner1_ticks);
    const u64 owner0_build=s_tj_prefix_sampler_cost.descriptor_build_ticks[sampler_reason][owner0];
    clock_ok=0;LONGLONG failed_lookup=tj_prefix_sampler_cost_lookup_begin(sampler_reason,owner0);
    CHECK(!failed_lookup&&s_tj_prefix_sampler_cost.invalid==3);clock_ok=1;
    clock_tick=580;LONGLONG unpaired_build=tj_prefix_sampler_cost_qpc();
    const int reads_before_unpaired_finish=clock_reads;
    tj_prefix_sampler_cost_finish(sampler_reason,owner0,failed_lookup,unpaired_build);
    CHECK(clock_reads==reads_before_unpaired_finish);
    CHECK(s_tj_prefix_sampler_cost.descriptor_build_ticks[sampler_reason][owner0]==owner0_build);
    clock_tick=580;LONGLONG descriptor_before_lookup=tj_prefix_sampler_cost_qpc();clock_tick=590;
    LONGLONG later_lookup=tj_prefix_sampler_cost_qpc();
    const u64 owner1_build=s_tj_prefix_sampler_cost.descriptor_build_ticks[sampler_reason][owner1];
    const int reads_before_order_check=clock_reads;
    tj_prefix_sampler_cost_finish(sampler_reason,owner1,later_lookup,descriptor_before_lookup);
    CHECK(clock_reads==reads_before_order_check&&s_tj_prefix_sampler_cost.invalid==4);
    CHECK(s_tj_prefix_sampler_cost.descriptor_build_ticks[sampler_reason][owner1]==owner1_build);
    tj_prefix_cost_report(2999);CHECK(s_tj_prefix_sampler_cost.lookup_total==4);
    tj_prefix_cost_report(3000);CHECK(!s_tj_prefix_cost.invalid);
    CHECK(!s_tj_prefix_sampler_cost.lookup_total);
    complete_record(RSX_COMPLETION_INPUT,1200);
    CHECK(s_tj_prefix_cost.record_samples[RSX_COMPLETION_INPUT]==1);
    armed=0;tj_prefix_cost_report(3100);CHECK(!s_tj_prefix_cost.start_ms);
    CHECK(!s_tj_prefix_cost.record_samples[RSX_COMPLETION_INPUT]);
    armed=1;const int reads_before_rearm=clock_reads;
    CHECK(!tj_prefix_sampler_cost_enabled()&&clock_reads==reads_before_rearm);
    CHECK(tj_prefix_cost_begin()&&tj_prefix_sampler_cost_enabled());
    armed=0;tj_prefix_cost_report(3200);CHECK(!s_tj_prefix_cost.start_ms);
    CHECK(!tj_prefix_sampler_cost_enabled());
    printf("Prefix cost: %d checks, %d failures\n",checks,failures);return failures?1:0;
}
'''.replace('REASONS', reasons).replace('PRODUCTION', code)
    with tempfile.TemporaryDirectory(prefix='tj-prefix-cost-') as directory:
        cpp, exe = Path(directory)/'cost.cpp', Path(directory)/'cost.exe'
        cpp.write_text(source, encoding='utf-8')
        compiler = shutil.which('clang++') or r'C:\Program Files\LLVM\bin\clang++.exe'
        subprocess.run([compiler, '-std=c++17', '-D_CRT_SECURE_NO_WARNINGS', str(cpp), '-o', str(exe)], check=True)
        env = dict(os.environ)
        env.pop('TJ_PREFIX_COST', None)
        for value in (None, '', '0', '1x', '1'):
            case = dict(env)
            if value is not None:
                case['TJ_PREFIX_COST'] = value
            result = subprocess.run([str(exe), str(int(value == '1'))], env=case, capture_output=True, text=True)
            assert result.returncode == 0, result.stdout + result.stderr
            if value != '1':
                assert not result.stderr
                continue
            rows = [dict(re.findall(r'(\w+)=([^ ]+)', line)) for line in result.stderr.splitlines() if line.startswith('[TJ-PREFIX-COST]')]
            assert len(rows) == 6
            texture = next(r for r in rows if r['reason'] == 'texture_release')
            deferred = next(r for r in rows if r['reason'] == 'input')
            parent = next(r for r in rows if r['reason'] == 'local_main_copy')
            acquire = next(r for r in rows if r['reason'] == 'acquire')
            fallback = next(r for r in rows if r['reason'] == 'other')
            invalid = next(r for r in rows if r['invalid'] == '4')
            assert texture['opening_us'] == '75' and texture['record_us'] == '75'
            assert deferred['deferred_us'] == '30'
            assert acquire['record_us'] == '80' and fallback['record_us'] == '80'
            assert invalid['execute_us'] == '10'
            upload_rows = [dict(re.findall(r'(\w+)=([^ ]+)', line)) for line in result.stderr.splitlines() if line.startswith('[TJ-MIRROR-UPLOAD-COST]')]
            assert len(upload_rows) == 1
            upload = upload_rows[0]
            assert upload['pid'] == '1234' and upload['start_ms'] == '1000' and upload['end_ms'] == '2000'
            assert upload['reason'] == 'texture_release'
            assert [upload[name] for name in ('calls', 'success', 'samples', 'invalid', 'aborted', 'failures')] == ['7', '5', '3', '2', '2', '1']
            assert upload['calls'] == str(int(upload['success']) + int(upload['aborted']))
            assert [upload[name] for name in ('rows', 'bytes_physical', 'bytes_converted')] == ['725', '1856000', '1850880']
            assert [upload[name] for name in ('inline_n', 'inline_ticks', 'inline_max_ticks', 'prepared_n', 'prepared_ticks', 'prepared_max_ticks')] == ['2', '270', '250', '1', '12', '12']
            upload_phases = ('owner_guard', 'map', 'convert', 'unmap', 'transition_copy')
            upload_phase_ticks = [int(upload[name + '_ticks']) for name in upload_phases]
            assert [int(upload[name + '_n']) for name in upload_phases] == [3] * 5
            assert upload_phase_ticks == [11, 22, 155, 21, 73]
            assert sum(upload_phase_ticks) == int(upload['total_ticks']) == 282
            assert upload['total_us'] == '282' and upload['whole_max_us'] == '250'
            record_rows = [dict(re.findall(r'(\w+)=([^ ]+)', line)) for line in result.stderr.splitlines() if line.startswith('[TJ-RECORD-COST]')]
            assert len(record_rows) == 6
            record = {row['reason']: row for row in record_rows}
            assert set(record) == {'acquire', 'other', 'input', 'capacity', 'clear', 'drain'}
            assert all(row['scope'] == 'within_record' and row['start_ms'] == '1000' and row['end_ms'] == '2000' for row in record_rows)
            for reason in ('acquire', 'other'):
                assert record[reason]['samples'] == '1' and record[reason]['invalid'] == '0'
                assert [record[reason][name + '_n'] for name in (
                    'target_setup', 'endpoints', 'preflight', 'scratch',
                    'texture_fp_prepare', 'manifest_replay', 'close_tail')] == ['1'] * 7
                assert [record[reason][name + '_us'] for name in (
                    'target_setup', 'endpoints', 'preflight', 'scratch',
                    'texture_fp_prepare', 'manifest_replay', 'close_tail')] == ['10', '5', '10', '7', '16', '23', '9']
                assert sum(int(record[reason][name + '_us']) for name in (
                    'target_setup', 'endpoints', 'preflight', 'scratch',
                    'texture_fp_prepare', 'manifest_replay', 'close_tail')) == 80
            assert record['input']['samples'] == '0' and record['input']['invalid'] == '1'
            assert record['capacity']['samples'] == '0' and record['capacity']['invalid'] == '1'
            assert record['clear']['samples'] == '0' and record['clear']['invalid'] == '1'
            assert record['drain']['samples'] == '0' and record['drain']['invalid'] == '2'
            assert not any(row['start_ms'] == '3000' for row in record_rows)
            main_rows = [dict(re.findall(r'(\w+)=([^ ]+)', line)) for line in result.stderr.splitlines() if line.startswith('[TJ-MAIN-COST]')]
            assert len(main_rows) == 1
            assert main_rows[0]['map_convert_us'] == '10' and main_rows[0]['world_commit_us'] == '20' and main_rows[0]['cleanup_us'] == '30'
            assert main_rows[0]['cleanup_n'] == '1' and parent['commit_us'] == '70'
            sampler_rows = [dict(re.findall(r'(\w+)=([^ ]+)', line)) for line in result.stderr.splitlines() if line.startswith('[TJ-SAMPLER-COST]')]
            assert len(sampler_rows) == 3
            sampler = {row['heap']: row for row in sampler_rows}
            assert set(sampler) == {'ordinary', 'owner0', 'owner1'}
            assert all(row['start_ms'] == '2000' and row['end_ms'] == '3000' for row in sampler_rows)
            assert sampler['ordinary']['lookups_n'] == '1' and sampler['ordinary']['heap_failures_n'] == '1'
            assert sampler['ordinary']['lookup_us'] == '10'
            assert sampler['owner0']['lookups_n'] == '2'
            assert sampler['owner0']['exact_hits_n'] == '1' and sampler['owner0']['new_tables_n'] == '1'
            assert sampler['owner0']['create_sampler_writes_n'] == '20'
            assert sampler['owner0']['lookup_us'] == '20' and sampler['owner0']['descriptor_build_us'] == '10'
            assert sampler['owner1']['capacity_failures_n'] == '1' and sampler['owner1']['lookup_us'] == '10'
            assert all(row['qpc_invalid'] == '4' for row in sampler_rows)
            assert '[TJ-PREFIX-COST-INVALID]' in result.stderr
            print(result.stdout.strip())
        print('PASS: strict opt-in, nested MAIN/sampler/record attribution, exact parent partition, invalid sample rejection, heap split, and window reset')


if __name__ == '__main__':
    main()
