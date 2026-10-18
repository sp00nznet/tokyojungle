"""Production overlap admission/label dispatch, with simulated GPU endpoints.

The ownership and fence fixtures separately exercise actual retention and waits.
This fixture proves opt-in policy and observer/callback wiring, not GPU speed.
"""
from pathlib import Path
import os
import re
import shutil
import subprocess
import tempfile
from test_fifo_present_boundary import function

ROOT = Path(__file__).resolve().parents[1]


def main():
    backend = (ROOT.parent / 'ps3recomp/libs/video/rsx_d3d12_backend.c').read_text(encoding='utf-8')
    gate = function(backend, 'static int d3d12_prefix_overlap_enabled(')
    observer_array = gate[gate.index('observers[]'):gate.index('};', gate.index('observers[]'))]
    observers = re.findall(r'"([A-Z0-9_]+)"', observer_array)
    functions = '\n'.join(function(backend, name) for name in (
        'static int d3d12_prefix_ownership_enabled(void)\n{',
        'static int d3d12_prefix_overlap_enabled(',
        'static int d3d12_prefix_overlap_eligible(',
        'static int d3d12_prefix_overlap_submit(',
        'int rsx_d3d12_backend_flush_texture_labels_at(',
        'static int d3d12_defer_texture_label(',
    ))
    header = (ROOT.parent / 'ps3recomp/libs/video/rsx_d3d12_backend.h').read_text(encoding='utf-8')
    start = header.index('typedef enum RsxD3D12CompletionReason')
    reasons = header[start:header.index('} RsxD3D12CompletionReason;', start) + len('} RsxD3D12CompletionReason;')]
    render = function(backend, 'static void render_frame(')
    assert render.index('d3d12_prefix_overlap_drain(') < render.index('local_gpu_claim_pending()')
    assert render.index('d3d12_prefix_overlap_drain(') < render.index('gpu_mirror_reset_batch()')
    assert 's_d3d.prefix_owner_active && !s_exact_writeback_count && !gcm2d_backend_perf' in render
    assert '!gcm2d_gpu_timing && !tj_compose_target_enabled()' in render
    assert 's_d3d.gpu_mirror[i].immutable_source' in render
    branch = render[render.index('if (overlap_ready) {'):]
    assert branch.index('d3d12_prefix_owner_signal()') < branch.index('prefix_deferred_owner =')
    assert branch.index('return;') < branch.index('ExactWritebackFailure failure')
    for name in ('int rsx_d3d12_backend_complete_fifo_prefix(',
                 'int rsx_d3d12_backend_complete_local_main_prefix(',
                 'int rsx_d3d12_backend_flush_texture_labels_at('):
        body = function(backend, name)
        assert 'd3d12_prefix_overlap_drain(' in body
    # Existing FIFO barriers must still route idle/acquire/non-raster observers
    # through the extended drain, including the fast and profiled walkers.
    frontend = (ROOT.parent / 'ps3recomp/libs/video/cellGcmSys.c').read_text(encoding='utf-8')
    assert function(frontend, 'void cellGcm_rsx_process_fifo(').index(
        'rsx_d3d12_backend_flush_texture_labels()') < function(
        frontend, 'void cellGcm_rsx_process_fifo(').index('ReleaseSRWLockExclusive(')
    sem = function(frontend, 'static int gcm_fifo_semaphore_method(')
    assert sem.index('rsx_d3d12_backend_flush_texture_labels_at(') < sem.index('vm_read32(label_ea)')
    source = r'''
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
using u32=uint32_t;using u64=uint64_t;using LONGLONG=int64_t;
COMPLETION_REASONS
static RsxD3D12CompletionReason s_tj_completion_reason;
#define MAX_DRAWS 4
typedef struct {u32 offset,value;} D3D12TextureLabel;
struct D3D12PrefixOwner {int phase,signal_queued;u32 label_count;D3D12TextureLabel labels[8];};
struct D3D12DrawRecord {int is_vp,is_clear,is_scaled_blit,fp_program_valid;};
static struct {u32 draw_count,texture_label_count,texture_label_flush_kind,prefix_deferred_owner;
    int texture_label_armed,dump_frames_left,gcm2d_backend_halted,prefix_overlap_request;
    D3D12DrawRecord draws[MAX_DRAWS];D3D12PrefixOwner prefix_owners[2];D3D12TextureLabel texture_labels[8];} s_d3d;
static int armed=1,indexed,compose,perf,drain_fail,render_fail,synchronous,drains,renders;
static int checks,failures;
static int completions;
#define CHECK(c,m) do{++checks;if(!(c)){++failures;fprintf(stderr,"FAIL %s:%d\n",m,__LINE__);}}while(0)
static int rsx_survival_trace_is_armed(){return armed;}
static int tj_indexed_diag_has_records(){return indexed;}
static int tj_compose_target_enabled(){return compose;}
static int tj_gcm2d_backend_perf_enabled(){return perf;}
static int tj_present_work_trace_enabled(){return 0;}
static LONGLONG tj_present_work_trace_begin(){return 0;}
static void tj_present_work_trace_cpu_finish(LONGLONG,int){}
static int d3d12_prefix_overlap_drain(RsxD3D12CompletionReason){++drains;return drain_fail?-1:0;}
/* Replay preparation is disabled in this overlap-only fixture. */
static int d3d12_prefix_prepare_admit(){return 0;}
static int d3d12_prefix_queue_ahead_prepare(RsxD3D12CompletionReason){return 0;}
static int rsx_d3d12_backend_complete_fifo_prefix_at(RsxD3D12CompletionReason){++completions;return 0;}
static int d3d12_texture_copy_label_batch_enabled(){return 0;}
static int d3d12_texture_batch_has_copy(){return 0;}
static void gcm2d_backend_halt(const char*){s_d3d.gcm2d_backend_halted=1;}
static void render_fifo_prefix_owned(){
    ++renders;CHECK(drains>=1&&s_d3d.prefix_overlap_request,"overlap request not bounded by drain/render");
    if(render_fail){gcm2d_backend_halt("fixture");return;}
    if(synchronous)return;
    s_d3d.prefix_deferred_owner=1;s_d3d.prefix_owners[0].phase=2;s_d3d.prefix_owners[0].signal_queued=1;
    s_d3d.prefix_owners[0].label_count=s_d3d.texture_label_count;s_d3d.texture_label_count=0;
}
PRODUCTION
static int d3d12_prefix_overlap_texture_release(){return d3d12_prefix_overlap_submit(1,RSX_COMPLETION_TEXTURE_RELEASE);}
static void reset(){memset(&s_d3d,0,sizeof(s_d3d));armed=1;indexed=compose=perf=drain_fail=render_fail=synchronous=drains=renders=completions=0;
    s_d3d.draw_count=1;s_d3d.draws[0]={1,0,0,1};}
int main(int argc,char** argv){
    int enabled=argc>1&&atoi(argv[1]);reset();
    CHECK(d3d12_prefix_overlap_enabled()==enabled,"opt-in/observer policy");
    CHECK(d3d12_prefix_overlap_eligible()==enabled,"valid raster eligibility");
    if(!enabled)return failures?1:0;
    for(int reason=0;reason<RSX_COMPLETION_REASON_COUNT;++reason){
        reset();s_d3d.texture_label_count=1;
        CHECK(rsx_d3d12_backend_flush_texture_labels_at((RsxD3D12CompletionReason)reason)==0,"boundary flush failed");
        int preparation=reason==RSX_COMPLETION_INPUT||reason==RSX_COMPLETION_CAPACITY||reason==RSX_COMPLETION_CLEAR;
        CHECK((preparation&&renders==1&&completions==0&&s_d3d.prefix_deferred_owner==1&&
            !s_d3d.texture_label_armed&&!s_d3d.texture_label_count&&s_d3d.prefix_owners[0].label_count==1)||
            (!preparation&&!renders&&completions==1),"guest observer deferred or preparation lost old labels");
    }
    for(int why=0;why<12;++why){reset();
        switch(why){case 0:armed=0;break;case 1:s_d3d.draw_count=0;break;
        case 2:s_d3d.draw_count=MAX_DRAWS+1;break;case 3:s_d3d.texture_label_count=8;break;
        case 4:s_d3d.texture_label_armed=1;break;case 5:s_d3d.dump_frames_left=1;break;
        case 6:indexed=1;break;case 7:compose=1;break;case 8:perf=1;break;
        case 9:s_d3d.draws[0].is_vp=0;break;case 10:s_d3d.draws[0].is_clear=1;break;
        case 11:s_d3d.draws[0].is_scaled_blit=1;break;}
        CHECK(d3d12_prefix_overlap_eligible()==(why==10),"observer/unsupported prefix admitted or owned clear rejected");
    }
    reset();s_d3d.draws[0].fp_program_valid=0;CHECK(!d3d12_prefix_overlap_eligible(),"invalid program admitted");
    s_d3d.draws[0].is_clear=1;CHECK(d3d12_prefix_overlap_eligible(),"owned clear needs no fragment program");
    reset();CHECK(d3d12_prefix_overlap_texture_release()==0&&!s_d3d.prefix_overlap_request&&
        s_d3d.texture_label_armed==2,"deferred label not armed");
    CHECK(d3d12_defer_texture_label(nullptr,4,27)==1&&!s_d3d.texture_label_armed&&
        s_d3d.prefix_owners[0].label_count==1&&s_d3d.prefix_owners[0].labels[0].value==27&&
        !s_d3d.texture_label_count,"label not attached to exact owner");
    CHECK(d3d12_defer_texture_label(nullptr,4,28)==0&&s_d3d.prefix_owners[0].label_count==1,"callback consumed twice");
    for(int why=0;why<6;++why){reset();d3d12_prefix_overlap_texture_release();
        switch(why){case 0:s_d3d.prefix_deferred_owner=0;break;case 1:s_d3d.prefix_deferred_owner=3;break;
        case 2:s_d3d.prefix_owners[0].phase=3;break;case 3:s_d3d.prefix_owners[0].signal_queued=0;break;
        case 4:s_d3d.prefix_owners[0].label_count=8;break;case 5:s_d3d.gcm2d_backend_halted=1;break;}
        CHECK(d3d12_defer_texture_label(nullptr,4,99)==1&&s_d3d.gcm2d_backend_halted&&
            !s_d3d.texture_label_count&&s_d3d.prefix_owners[0].labels[0].value!=99,
            "invalid owner published/lost label instead of consuming on failure");
    }
    reset();synchronous=1;CHECK(d3d12_prefix_overlap_texture_release()==0&&!s_d3d.texture_label_armed&&
        d3d12_defer_texture_label(nullptr,4,30)==0,"synchronous fallback swallowed current label");
    reset();drain_fail=1;CHECK(d3d12_prefix_overlap_texture_release()<0&&!renders&&!s_d3d.prefix_overlap_request,"failed drain submitted more GPU work");
    reset();render_fail=1;CHECK(d3d12_prefix_overlap_texture_release()<0&&!s_d3d.prefix_overlap_request&&!s_d3d.texture_label_armed,"failed recording left request armed");
    printf("Prefix overlap: %d checks, %d failures\n",checks,failures);return failures?1:0;
}
'''.replace('PRODUCTION', functions).replace('COMPLETION_REASONS', reasons)
    compiler = shutil.which('clang++') or r'C:\Program Files\LLVM\bin\clang++.exe'
    with tempfile.TemporaryDirectory(prefix='tj-prefix-overlap-') as directory:
        cpp, exe = Path(directory)/'test.cpp', Path(directory)/'test.exe'
        cpp.write_text(source, encoding='utf-8')
        subprocess.run([compiler, '-std=c++17', '-D_CRT_SECURE_NO_WARNINGS', str(cpp), '-o', str(exe)], check=True)
        env = dict(os.environ)
        for key in observers + ['TJ_PREFIX_OWNERSHIP', 'TJ_PREFIX_OVERLAP']:
            env.pop(key, None)
        for value in (None, '', '0', '1x', '1'):
            case = dict(env, TJ_PREFIX_OWNERSHIP='1')
            if value is not None: case['TJ_PREFIX_OVERLAP'] = value
            subprocess.run([str(exe), str(int(value == '1'))], env=case, check=True)
        subprocess.run([str(exe), '0'], env=dict(env, TJ_PREFIX_OVERLAP='1'), check=True)
        for observer in observers:
            subprocess.run([str(exe), '0'], env=dict(env, TJ_PREFIX_OVERLAP='1', TJ_PREFIX_OWNERSHIP='1', **{observer:'1'}), check=True)
        print(f'PASS: {len(observers)} inherited observer flags and FIFO/replay observer wiring')


if __name__ == '__main__':
    main()
