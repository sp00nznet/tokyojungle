"""Production owned-query lifecycle and calibration with deterministic COM/clocks."""
from pathlib import Path
import os
import shutil
import subprocess
import tempfile
from test_fifo_present_boundary import function

ROOT = Path(__file__).resolve().parents[1]


def main():
    backend = (ROOT.parent / 'ps3recomp/libs/video/rsx_d3d12_backend.c').read_text(encoding='utf-8')
    a = backend.index('/* Optional owned-prefix timestamps.')
    code = backend[a:backend.index('/* Renderer-thread-only boundary acceptance diagnostic.', a)]
    header = (ROOT.parent / 'ps3recomp/libs/video/rsx_d3d12_backend.h').read_text(encoding='utf-8')
    a = header.index('typedef enum RsxD3D12CompletionReason')
    reasons = header[a:header.index('} RsxD3D12CompletionReason;', a) + len('} RsxD3D12CompletionReason;')]
    a = backend.index('static const char* const s_tj_completion_names[')
    names = backend[a:backend.index('};', a) + 2]
    # Hooks must retain the actual successful-ticket and unsubmitted-destroy guards.
    checked = function(backend, 'static int d3d12_prefix_owner_checked_completion(')
    assert checked.index('completed < owner->fence_value') < checked.index('tj_prefix_gpu_collect(slot)')
    assert checked.index('owner->completion_checked = 1') < checked.index('tj_prefix_gpu_collect(slot)')
    destroy = function(backend, 'static int d3d12_prefix_owner_destroy(')
    assert destroy.index('owner->phase >= 2') < destroy.index('tj_prefix_gpu_release(slot)')
    render = function(backend, 'static void render_frame(')
    assert render.index('tj_prefix_gpu_begin(') < render.index('tj_prefix_gpu_end(')
    tail = render[render.index('tj_prefix_gpu_end('):]
    assert tail.index('->Close(') < tail.index('d3d12_prefix_owner_submit()') < tail.index('tj_prefix_gpu_submit(') < tail.index('->ExecuteCommandLists(')
    assert 'wait_for_' not in code and '->Signal(' not in code and 'ExecuteCommandLists' not in code
    source = r'''
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
using u32=uint32_t;using u64=uint64_t;using UINT64=uint64_t;using LONGLONG=int64_t;
using HRESULT=int32_t;using SIZE_T=size_t;
#define SUCCEEDED(x) ((x)>=0)
#define FAILED(x) ((x)<0)
#define E_FAIL (-1)
#define D3D12_QUERY_HEAP_TYPE_TIMESTAMP 1
#define D3D12_QUERY_TYPE_TIMESTAMP 1
#define D3D12_HEAP_TYPE_READBACK 1
#define D3D12_RESOURCE_DIMENSION_BUFFER 1
#define D3D12_TEXTURE_LAYOUT_ROW_MAJOR 1
#define D3D12_HEAP_FLAG_NONE 0
#define D3D12_RESOURCE_STATE_COPY_DEST 1
static int IID_ID3D12QueryHeap,IID_ID3D12Resource;
struct LARGE_INTEGER {LONGLONG QuadPart;};
static LARGE_INTEGER s_tj_prefix_cost_frequency={1000000};
struct D3D12_QUERY_HEAP_DESC {int Type;u32 Count;};
struct D3D12_HEAP_PROPERTIES {int Type;};
struct D3D12_RESOURCE_DESC {int Dimension;u64 Width;u32 Height,DepthOrArraySize,MipLevels;struct{u32 Count;}SampleDesc;int Layout;};
struct D3D12_RANGE {SIZE_T Begin,End;};
struct ID3D12QueryHeap;struct ID3D12Resource;struct Queue;struct Device;struct List;
struct HeapVT {void(*Release)(ID3D12QueryHeap*);};
struct ResourceVT {void(*Release)(ID3D12Resource*);HRESULT(*Map)(ID3D12Resource*,int,D3D12_RANGE*,void**);void(*Unmap)(ID3D12Resource*,int,D3D12_RANGE*);};
struct QueueVT {HRESULT(*GetTimestampFrequency)(Queue*,UINT64*);HRESULT(*GetClockCalibration)(Queue*,UINT64*,UINT64*);};
struct DeviceVT {HRESULT(*CreateQueryHeap)(Device*,D3D12_QUERY_HEAP_DESC*,void*,void**);HRESULT(*CreateCommittedResource)(Device*,D3D12_HEAP_PROPERTIES*,int,D3D12_RESOURCE_DESC*,int,void*,void*,void**);};
struct ListVT {void(*EndQuery)(List*,ID3D12QueryHeap*,int,u32);void(*ResolveQueryData)(List*,ID3D12QueryHeap*,int,u32,u32,ID3D12Resource*,u64);};
struct ID3D12QueryHeap {HeapVT*lpVtbl;u64 stamps[2];};
struct ID3D12Resource {ResourceVT*lpVtbl;u64 stamps[2];};
struct Queue {QueueVT*lpVtbl;};struct Device {DeviceVT*lpVtbl;};struct List {ListVT*lpVtbl;};
struct D3D12PrefixOwner {int phase,completion_checked,signal_queued;u64 fence_value,sequence;};
static struct {Queue*cmd_queue;Device*device;List*cmd_list;D3D12PrefixOwner prefix_owners[2];}s_d3d;
static int armed=1,cost_gate=1,owner_gate=1,clock_ok=1,cal_ok=1,create_fail;
static int map_fail,map_failed_output_nonnull,map_success_null;
static int clock_reads,queries,resolves,maps,unmaps,releases,heap_created,resource_created,checks,failures;
static u64 gpu_stamp,cal_gpu,cal_cpu;static LONGLONG clock_tick;
static int QueryPerformanceCounter(LARGE_INTEGER*p){++clock_reads;p->QuadPart=clock_tick;return clock_ok;}
static int rsx_survival_trace_is_armed(){return armed;}
static int tj_prefix_cost_enabled(){return cost_gate;}
static int d3d12_prefix_ownership_enabled(){return owner_gate;}
#define CHECK(x) do{++checks;if(!(x)){++failures;fprintf(stderr,"FAIL line %d\n",__LINE__);}}while(0)
static void heap_release(ID3D12QueryHeap*){++releases;}
static void resource_release(ID3D12Resource*){++releases;}
static HRESULT map(ID3D12Resource*r,int,D3D12_RANGE*range,void**data){++maps;CHECK(range->Begin==0&&range->End==16);*data=map_fail?(map_failed_output_nonnull?r->stamps:nullptr):(map_success_null?nullptr:r->stamps);return map_fail?-1:0;}
static void unmap(ID3D12Resource*,int,D3D12_RANGE*r){++unmaps;CHECK(r->Begin==0&&r->End==0);}
static HeapVT hvt={heap_release};static ResourceVT rvt={resource_release,map,unmap};
static ID3D12QueryHeap heaps[16];static ID3D12Resource resources[16];
static HRESULT freq(Queue*,u64*p){*p=1000000;return 0;}
static HRESULT calibrate(Queue*,u64*g,u64*c){*g=cal_gpu;*c=cal_cpu;return cal_ok?0:-1;}
static HRESULT heap_create(Device*,D3D12_QUERY_HEAP_DESC*q,void*,void**p){CHECK(q->Count==2&&q->Type==1);auto*h=&heaps[heap_created++];h->lpVtbl=&hvt;*p=h;return 0;}
static HRESULT resource_create(Device*,D3D12_HEAP_PROPERTIES*,int,D3D12_RESOURCE_DESC*r,int,void*,void*,void**p){CHECK(r->Width==16);if(create_fail)return -1;auto*b=&resources[resource_created++];b->lpVtbl=&rvt;*p=b;return 0;}
static void end_query(List*,ID3D12QueryHeap*h,int,u32 index){++queries;CHECK(index<2);h->stamps[index]=gpu_stamp;}
static void resolve(List*,ID3D12QueryHeap*h,int,u32 start,u32 count,ID3D12Resource*r,u64 offset){++resolves;CHECK(!start&&count==2&&!offset);memcpy(r->stamps,h->stamps,16);}
static QueueVT qvt={freq,calibrate};static DeviceVT dvt={heap_create,resource_create};static ListVT lvt={end_query,resolve};
static Queue queue={&qvt};static Device device={&dvt};static List list={&lvt};
REASONS
NAMES
static unsigned s_tj_completion_reason=RSX_COMPLETION_TEXTURE_RELEASE;
PRODUCTION
static void record(u32 slot,u64 base,u64 start,u64 end,u64 sequence){
    auto&o=s_d3d.prefix_owners[slot];o={};o.phase=1;
    gpu_stamp=base+start;for(int i=0;i<1000&&!s_tj_prefix_gpu[slot].phase;++i)tj_prefix_gpu_begin(slot);
    CHECK(s_tj_prefix_gpu[slot].phase==1);gpu_stamp=base+end;tj_prefix_gpu_end(slot);
    o.phase=2;o.sequence=sequence;o.signal_queued=1;o.fence_value=sequence;
    clock_tick=base;cal_gpu=base-100;cal_cpu=base-100;tj_prefix_gpu_submit(slot);
}
static void collect(u32 slot,u64 base,u64 wait,u64 observed,u64 drift=0){
    clock_tick=base+wait;tj_prefix_gpu_wait_start(slot);clock_tick=base+observed;
    cal_gpu=base+observed+100;cal_cpu=cal_gpu+drift;
    s_d3d.prefix_owners[slot].completion_checked=1;tj_prefix_gpu_collect(slot);
}
int main(int argc,char**argv){
    s_d3d.cmd_queue=&queue;s_d3d.device=&device;s_d3d.cmd_list=&list;
    cost_gate=atoi(argv[2]);owner_gate=atoi(argv[3]);int enabled=atoi(argv[1]);
    CHECK(tj_prefix_gpu_cost_enabled()==enabled);
    if(!enabled){s_d3d.prefix_owners[0].phase=1;tj_prefix_gpu_begin(0);tj_prefix_gpu_end(0);tj_prefix_gpu_submit(0);tj_prefix_gpu_wait_start(0);tj_prefix_gpu_collect(0);tj_prefix_gpu_cost_report(1,2);CHECK(!queries&&!clock_reads&&!maps);return failures?1:0;}
    armed=0;s_d3d.prefix_owners[0].phase=1;tj_prefix_gpu_begin(0);CHECK(!queries);armed=1;
    record(0,1000,100,200,1);record(1,2000,100,600,2);
    CHECK(s_tj_prefix_gpu[0].heap!=s_tj_prefix_gpu[1].heap&&s_tj_prefix_gpu[0].readback!=s_tj_prefix_gpu[1].readback);
    int before=queries;tj_prefix_gpu_begin(0);tj_prefix_gpu_begin(1);CHECK(queries==before&&!maps);
    tj_prefix_gpu_collect(0);CHECK(!maps&&s_tj_prefix_gpu[0].phase==3);
    s_d3d.prefix_owners[0].completion_checked=1;s_d3d.prefix_owners[0].sequence=9;
    tj_prefix_gpu_collect(0);CHECK(!maps&&s_tj_prefix_gpu[0].phase==3);
    s_d3d.prefix_owners[0].sequence=1;
    collect(0,1000,300,400);collect(1,2000,200,800);
    auto*c=&s_tj_prefix_gpu_cost[RSX_COMPLETION_TEXTURE_RELEASE];
    CHECK(c->samples==2&&c->calibrated==2&&c->invalid==2);
    CHECK(c->gpu_us==600&&c->submitted_us==1200&&c->wait_us==700);
    CHECK(c->queue_us==200&&c->observer_us==400&&c->exposed_tail_us==300&&c->finished_before_wait==1);
    tj_prefix_gpu_collect(0);CHECK(maps==2&&unmaps==2&&c->samples==2);
    tj_prefix_gpu_release(0);tj_prefix_gpu_release(1);CHECK(!releases);
    s_d3d.prefix_owners[0].phase=0;s_d3d.prefix_owners[1].phase=0;
    tj_prefix_gpu_release(0);tj_prefix_gpu_release(1);CHECK(releases==4&&!s_tj_prefix_gpu[0].readback);
    // Integer-domain subtraction must preserve short deltas at large counters.
    u64 large=1ULL<<62;record(0,large,100,200,3);collect(0,large,150,400);
    CHECK(c->gpu_us==700&&c->queue_us==300&&c->observer_us==600&&c->exposed_tail_us==500);
    // Drift/calibration failures retain GPU samples but cannot create queue/wake evidence.
    auto calibrated=c->calibrated;record(0,4000,100,200,4);collect(0,4000,150,400,1000);
    CHECK(c->samples==4&&c->calibrated==calibrated);
    cal_ok=0;record(0,5000,100,200,5);collect(0,5000,150,400);cal_ok=1;
    CHECK(c->samples==5&&c->calibrated==calibrated);
    // Successful Map owns a view even if its output is null; failed Map does not.
    auto invalid_before=c->invalid;auto unmaps_before=unmaps;map_fail=1;record(0,6000,100,200,6);collect(0,6000,150,400);map_fail=0;
    CHECK(c->samples==5&&s_tj_prefix_gpu[0].phase==0&&c->invalid==invalid_before+1&&unmaps==unmaps_before);
    invalid_before=c->invalid;unmaps_before=unmaps;map_success_null=1;record(0,6500,100,200,9);collect(0,6500,150,400);map_success_null=0;
    CHECK(c->samples==5&&s_tj_prefix_gpu[0].phase==0&&c->invalid==invalid_before+1&&unmaps==unmaps_before+1);
    invalid_before=c->invalid;unmaps_before=unmaps;map_fail=1;map_failed_output_nonnull=1;record(0,6750,100,200,10);collect(0,6750,150,400);map_fail=0;map_failed_output_nonnull=0;
    CHECK(c->samples==5&&s_tj_prefix_gpu[0].phase==0&&c->invalid==invalid_before+1&&unmaps==unmaps_before);
    // Reversed timestamps are rejected after checked completion.
    record(0,7000,300,200,7);collect(0,7000,150,400);CHECK(c->samples==5);
    // Invalid host clock cannot Map; renderer ownership remains unaffected.
    record(0,8000,100,200,8);clock_ok=0;collect(0,8000,150,400);clock_ok=1;
    CHECK(c->samples==5&&s_d3d.prefix_owners[0].phase==2);
    tj_prefix_gpu_cost_report(1000,2000);CHECK(!c->samples&&!c->invalid);
    s_d3d.prefix_owners[0].phase=0;tj_prefix_gpu_release(0);
    create_fail=1;s_d3d.prefix_owners[0].phase=1;before=releases;
    for(int i=0;i<1000&&!s_tj_prefix_gpu[0].failed;++i)tj_prefix_gpu_begin(0);
    CHECK(s_tj_prefix_gpu[0].failed&&releases==before+1&&!s_tj_prefix_gpu[0].heap&&!s_tj_prefix_gpu[0].readback);
    before=heap_created;tj_prefix_gpu_begin(0);CHECK(heap_created==before);
    // Real sampling gate: broad coverage without a fixed two-copy alias pattern.
    s_d3d.prefix_owners[0].phase=0;tj_prefix_gpu_release(0);create_fail=0;
    memset(s_tj_prefix_gpu_cost,0,sizeof(s_tj_prefix_gpu_cost));
    s_tj_prefix_gpu_sample_state=0x723AD381u;int even=0,odd=0;
    for(int i=0;i<1000;++i){
        s_d3d.prefix_owners[0].phase=1;gpu_stamp=10000;tj_prefix_gpu_begin(0);
        if(s_tj_prefix_gpu[0].phase==1){if(i&1)++odd;else ++even;
            gpu_stamp=10100;tj_prefix_gpu_end(0);s_d3d.prefix_owners[0].phase=2;
            s_d3d.prefix_owners[0].sequence=100+i;s_d3d.prefix_owners[0].signal_queued=1;
            s_d3d.prefix_owners[0].fence_value=100+i;clock_tick=9900;cal_gpu=cal_cpu=9800;
            tj_prefix_gpu_submit(0);collect(0,9900,150,400);}
    }
    CHECK(even>10&&odd>10&&even+odd>40&&even+odd<90);
    CHECK(c->candidates==1000&&c->samples==(u64)(even+odd));
    printf("Owned prefix GPU timing: %d checks, %d failures\n",checks,failures);return failures?1:0;
}
'''.replace('REASONS', reasons).replace('NAMES', names).replace('PRODUCTION', code)
    with tempfile.TemporaryDirectory(prefix='tj-prefix-gpu-cost-') as directory:
        cpp, exe = Path(directory)/'gpu.cpp', Path(directory)/'gpu.exe'
        cpp.write_text(source, encoding='utf-8')
        compiler = shutil.which('clang++') or r'C:\Program Files\LLVM\bin\clang++.exe'
        subprocess.run([compiler, '-std=c++17', '-D_CRT_SECURE_NO_WARNINGS', str(cpp), '-o', str(exe)], check=True)
        base = dict(os.environ)
        base.pop('TJ_PREFIX_GPU_COST', None)
        for value, cost, owner in [(None,1,1),('',1,1),('0',1,1),('1x',1,1),('1',0,1),('1',1,0),('1',1,1)]:
            env = dict(base)
            if value is not None:
                env['TJ_PREFIX_GPU_COST'] = value
            enabled = value == '1' and cost and owner
            result = subprocess.run([str(exe),str(int(enabled)),str(cost),str(owner)], env=env, capture_output=True, text=True)
            if result.returncode:
                print(result.stdout, result.stderr)
                result.check_returncode()
            if enabled:
                assert '[TJ-PREFIX-GPU-COST]' in result.stderr
                print(result.stdout.strip())
            else:
                assert not result.stderr
        print('PASS: strict flags, private pending slots, checked-ticket readback, drift rejection and no added waits')


if __name__ == '__main__':
    main()
