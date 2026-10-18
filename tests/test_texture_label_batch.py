"""Native production batching checks: no publication before checked completion."""
from pathlib import Path
import os
import shutil
import subprocess
import tempfile
from test_fifo_present_boundary import function

ROOT = Path(__file__).resolve().parents[1]


def main():
    text = (ROOT.parent / "ps3recomp/libs/video/rsx_d3d12_backend.c").read_text(encoding="utf-8")
    header = (ROOT.parent / "ps3recomp/libs/video/rsx_d3d12_backend.h").read_text(encoding="utf-8")
    reason_start = header.index("typedef enum RsxD3D12CompletionReason")
    reasons = header[reason_start:header.index("} RsxD3D12CompletionReason;", reason_start) + len("} RsxD3D12CompletionReason;")]
    blit_start = header.index("typedef struct {")
    blit_struct = header[blit_start:header.index("} RsxD3D12ExactBlit;", blit_start) + len("} RsxD3D12ExactBlit;")]
    assert "label_limit_edges" in text
    bodies = [function(text, marker) for marker in (
        "static int gpu_blit_record_mapping_valid(",
        "static int d3d12_texture_copy_label_batch_enabled(",
        "static int d3d12_texture_batch_has_copy(",
        "static int vp_record_texture_is_gpu_owned(",
        "static void d3d12_publish_texture_label_batch(",
        "static void d3d12_publish_texture_labels(",
        "int rsx_d3d12_backend_complete_fifo_prefix(",
        "int rsx_d3d12_backend_complete_fifo_prefix_at(",
        "int rsx_d3d12_backend_flush_texture_labels_at(",
        "int rsx_d3d12_backend_flush_texture_labels(",
        "static int d3d12_texture_label_batch_enabled(",
        "static int d3d12_texture_prefix_can_batch(",
        "int rsx_d3d12_backend_prepare_texture_label(",
        "static int d3d12_defer_texture_label(",
        "static int d3d12_texture_batch_input_needs_flush(",
        "static int d3d12_texture_copy_cpu_inputs_need_flush(",
        "static u32 d3d12_texture_batch_fragment_mask(",
        "static int d3d12_texture_batch_before_draw(",
    )]
    fixture = r'''
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <vector>
#include <utility>
#include "rsx_scaled_blit.h"
using u32=uint32_t; using u64=uint64_t; using u8=uint8_t; using u16=uint16_t; using LONGLONG=int64_t;
typedef struct {u32 offset,value;} D3D12TextureLabel;
struct D3D12PrefixOwner {int phase,signal_queued;u32 label_count;D3D12TextureLabel labels[8];};
COMPLETION_REASONS
EXACT_BLIT_STRUCT
static RsxD3D12CompletionReason s_tj_completion_reason;
static int diagnostics;
#define MAX_DRAWS 32
#define MAX_VERTICES 512
#define VP_VERT_STRIDE 256
#define VERTEX_STRIDE 36
#define RSX_MAX_TEXTURES 16
#define RSX_MAX_VERTEX_TEXTURES 4
#define RSX_MAX_VERTEX_ATTRIBS 16
struct Tex {
    int set; u32 raw, location;
    u32 w,h,depth,fmt,pitch,dimension,mips; int cube; u8 border_type;
};
struct D3D12DrawRecord {
    int is_vp,is_clear,is_scaled_blit,fp_program_valid;
    struct { u32 texture_read_mask; } fp_program;
    Tex tex[16],vtex[4]; void* tex_snapshot[16]; void* vtex_snapshot[4];
    u32 tex_snapshot_fault_mask,vtex_snapshot_fault_mask;
    u32 rt_off,rt_mrt[3],rt_raw[4],rt_pitch[4],zeta_raw,zeta_pitch;
    u8 rt_local,rt_mrt_local[3],rt_raw_local[4],zeta_local;
    RsxD3D12ExactBlit scaled_blit;
};
static struct {
    int initialized,gcm2d_backend_halted,fifo_prefix_outstanding;
    u64 fifo_prefix_serial;
    u32 prefix_deferred_owner;
    D3D12PrefixOwner prefix_owners[2];
    u32 draw_count,vp_vb_offset,vb_offset;
    D3D12DrawRecord draws[MAX_DRAWS]; Tex cur_texs[16],cur_vtexs[4];
    D3D12TextureLabel texture_labels[8];
    u32 texture_label_count,texture_label_flush_kind; int texture_label_armed,texture_release_nowait_request;
    struct rsx_state* current_rsx_state;
} s_d3d;
struct rsx_state {
    u32 shader_program,vertex_attrib_input_mask,index_array_offset;
    struct { int enabled; u32 stride; } vertex_attribs[16];
    u32* immediate_index_data;
} current_state;
struct RSXFPStructuralProgram { u32 texture_read_mask; };
#define RSX_FP_MAX_PROGRAM_BYTES 512
static uint8_t bytes[512]; static uint8_t* vm_base=bytes;
static u32 program_mask; static int program_fails,location_fails,mapping_fails;
static u32 ppu_vm_size,alias_raw,alias_ea; static u64 generation=2;
static int generation_changes,span_fails; static u32 span_bytes=16;
static u64 cellGcmMainMappingGeneration(){u64 result=generation;generation+=generation_changes?2:0;return result;}
static int rsx_shader_program_location_is_local(u32,int* local){*local=0;return !location_fails;}
static int cellGcmResolveLocatedChecked(int local,u32 raw,u32* off){
    *off=raw==0x100?0:(!local&&raw==alias_raw?alias_ea:raw+(local?0x40000000u:0));
    return !mapping_fails;
}
static int vp_tex_source_has_border(u32,u8){return 0;}
/* Layout arithmetic has its own native fixture. This fixture varies the exact
 * span supplied by it and compiles the production range/observer guards. */
static int vp_tex_source_span(u32,u32,u32,u32,u32,int,u32,u32,int,u32* bytes,u64*){
    if(span_fails)return -1;*bytes=span_bytes;return 0;
}
static int rsx_fp_capture_structural(const uint8_t*,u32,RSXFPStructuralProgram* p){
    p->texture_read_mask=program_mask;return program_fails?-1:0;
}
static int checks,failures,renders,waits,render_fail,wait_fail,overlap_drain_fail,overlap_drains;
static std::vector<std::pair<u32,u32>> writes;
#define CHECK(c,m) do{++checks;if(!(c)){++failures;fprintf(stderr,"FAIL %s line %d\n",m,__LINE__);}}while(0)
static void vm_write32(u32 a,u32 v){writes.push_back({a,v});}
static int tj_gcm2d_backend_perf_enabled(){return diagnostics;}
static int tj_prefix_cost_enabled(){return 0;}
static int tj_main_direct_readback_enabled(){return 0;}
static u64 GetTickCount64(){return 0;}
/* Prefix timing is orthogonal to this barrier-ordering fixture and stays off. */
static int tj_present_work_trace_enabled(){return 0;}
static int rsx_survival_trace_is_armed(){return 0;}
static void tj_present_work_trace_arm(int){}
static LONGLONG tj_present_work_trace_begin(){return 0;}
static void tj_present_work_trace_cpu_finish(LONGLONG,int){}
static void gcm2d_backend_halt(const char*){s_d3d.gcm2d_backend_halted=1;}
static int wait_for_gpu_checked(){++waits;return wait_fail?-1:0;}
static void render_frame(u32,u32,u32,int prefix){
    CHECK(prefix==1,"batch uses normal Present");
    CHECK(writes.empty(),"label published before completion");
    ++renders;
    if(render_fail){s_d3d.gcm2d_backend_halted=1;return;}
    s_d3d.draw_count=s_d3d.vp_vb_offset=s_d3d.vb_offset=0;
    s_d3d.fifo_prefix_outstanding=0;
}
static int cellGcmOffsetIsDisplay(u32 raw){return raw==0x10000;}
static int off_rt_find(u32 raw,u8 local){return raw==0x20000&&local?0:-1;}
static int dr_rt_attachment_enabled(const D3D12DrawRecord*,int){return 1;}
/* This fixture isolates label ordering; real owned-prefix contexts have a
 * separate extracted-production fixture. */
static int d3d12_prefix_ownership_enabled(){return 0;}
static int d3d12_texture_release_nowait_requested(){return 0;}
static int d3d12_prefix_overlap_drain(RsxD3D12CompletionReason){
    if(!s_d3d.prefix_deferred_owner)return 0;
    ++overlap_drains;
    if(overlap_drain_fail){gcm2d_backend_halt("fixture drain");return -1;}
    s_d3d.prefix_deferred_owner=0;s_d3d.fifo_prefix_outstanding=0;return 0;
}
/* Replay preparation is disabled in this label-ordering fixture. */
static int d3d12_prefix_prepare_admit(){return 0;}
static int d3d12_prefix_queue_ahead_prepare(RsxD3D12CompletionReason){return 0;}
static int tj_prefix_wait_trace_enabled(){return 0;}
static int d3d12_prefix_overlap_eligible(){return 0;}
static int d3d12_prefix_overlap_submit(int,RsxD3D12CompletionReason){return -1;}
static void render_fifo_prefix_owned(){render_frame(0,0,0,1);}
int rsx_d3d12_backend_complete_fifo_prefix(void);
int rsx_d3d12_backend_flush_texture_labels(void);
PRODUCTION
static void reset(){
    memset(&s_d3d,0,sizeof(s_d3d));s_d3d.initialized=1;
    renders=waits=render_fail=wait_fail=overlap_drain_fail=overlap_drains=0;writes.clear();
    program_mask=0xFFFF;program_fails=location_fails=mapping_fails=0;
    ppu_vm_size=alias_raw=alias_ea=generation_changes=span_fails=0;generation=2;span_bytes=16;
    memset(&current_state,0,sizeof(current_state));
    current_state.shader_program=0x100;s_d3d.current_rsx_state=&current_state;
    s_d3d.draw_count=1;
    s_d3d.draws[0].is_vp=s_d3d.draws[0].fp_program_valid=1;
    auto& dr=s_d3d.draws[0];dr.tex[0]={1,0x30000,2};dr.tex_snapshot[0]=(void*)1;
}
static void queue(u32 offset,u32 value){
    CHECK(rsx_d3d12_backend_prepare_texture_label()==0,"prepare failed");
    CHECK(d3d12_defer_texture_label(nullptr,offset,value)==1,"armed label not consumed");
}
static void copy_record(){
    auto& dr=s_d3d.draws[0];memset(&dr,0,sizeof(dr));dr.is_vp=dr.is_scaled_blit=1;
    auto& b=dr.scaled_blit;b.src_raw=0x30000;b.dst_raw=0x50000;
    b.src_ea=0x30000;b.dst_ea=0x40050000;b.mapping_generation=2;
    b.src_pitch=32;b.dst_pitch=64;b.src_w=8;b.src_h=16;b.dst_w=16;b.dst_h=16;
    b.src_x=1;b.src_y=1;b.dst_x=1;b.dst_y=2;b.copy_w=4;b.copy_h=2;
    b.dst_local=1;b.src_snapshot=(u8*)1;b.src_snapshot_bytes=512;
}
int main(int argc,char** argv){
    const int enabled=argc>1&&atoi(argv[1]);diagnostics=argc>2&&atoi(argv[2]);reset();
    const int copy_enabled=argc>3&&atoi(argv[3]);
    CHECK(d3d12_texture_label_batch_enabled()==enabled,"option semantics");
    CHECK(d3d12_texture_copy_label_batch_enabled()==copy_enabled,"copy option semantics");
    if(!enabled){
        CHECK(rsx_d3d12_backend_prepare_texture_label()==0&&renders==1,"disabled completion changed");
        CHECK(!d3d12_defer_texture_label(nullptr,4,7)&&writes.empty(),"disabled label intercepted");
    }else{
        // A previous owned submission must not force a new snapshot-safe block
        // to submit solely because its outstanding flag is still set.
        s_d3d.prefix_deferred_owner=1;s_d3d.fifo_prefix_outstanding=1;
        CHECK(rsx_d3d12_backend_prepare_texture_label()==0&&overlap_drains==1&&
              renders==0&&s_d3d.texture_label_armed==1&&s_d3d.draw_count==1,
              "prior overlap fragments the next batch");
        CHECK(d3d12_defer_texture_label(nullptr,4,7)==1&&writes.empty()&&
              s_d3d.texture_label_count==1,"next batch label published early");
        reset();s_d3d.prefix_deferred_owner=1;s_d3d.fifo_prefix_outstanding=1;overlap_drain_fail=1;
        CHECK(rsx_d3d12_backend_prepare_texture_label()<0&&!renders&&!s_d3d.texture_label_armed&&
              s_d3d.prefix_deferred_owner==1&&s_d3d.fifo_prefix_outstanding==1,
              "failed prior overlap discarded or current batch armed");
        CHECK(!d3d12_defer_texture_label(nullptr,4,7)&&writes.empty(),"failed prior overlap published next label");
        reset();
        CHECK(!d3d12_defer_texture_label(nullptr,4,7),"unprepared direct dispatch intercepted");
        queue(0x14,0x11223344);queue(0x14,0x55667788);queue(0x18,3);
        CHECK(writes.empty()&&renders==0,"queued label published early");
        CHECK(rsx_d3d12_backend_flush_texture_labels()==0&&renders==1,"did not combine three releases");
        CHECK(writes.size()==3&&writes[0]==std::make_pair(0x03000014u,0x11223344u)&&
              writes[1].second==0x55667788&&writes[2].first==0x03000018,"order, address, byte order");
        CHECK(!s_d3d.texture_label_count,"successful queue not drained");
        CHECK(rsx_d3d12_backend_flush_texture_labels()==0&&renders==1,"empty flush adds GPU work");
        reset();for(u32 i=0;i<8;++i)queue(4*i,i);
        CHECK(writes.empty()&&s_d3d.texture_label_count==8,"queue capacity incorrect");
        queue(32,8);CHECK(renders==1&&writes.size()==8&&s_d3d.texture_label_count==1,"full queue ordering");
        CHECK(rsx_d3d12_backend_flush_texture_labels()==0&&writes.size()==9,"empty-prefix label stranded");
        reset();for(u32 i=0;i<8;++i)queue(4*i,i);render_fail=1;
        CHECK(rsx_d3d12_backend_prepare_texture_label()<0&&writes.empty(),"failed cap flush published label");
        reset();queue(4,9);render_fail=1;
        CHECK(rsx_d3d12_backend_flush_texture_labels()<0&&writes.empty(),"failed render published label");
        CHECK(rsx_d3d12_backend_flush_texture_labels()<0&&writes.empty(),"halted retry published label");
        reset();queue(4,9);s_d3d.draw_count=0;s_d3d.fifo_prefix_outstanding=1;wait_fail=1;
        CHECK(rsx_d3d12_backend_flush_texture_labels()<0&&waits==1&&writes.empty(),"failed outstanding fence published label");
        reset();queue(4,9);s_d3d.draw_count=0;s_d3d.fifo_prefix_outstanding=1;
        CHECK(rsx_d3d12_backend_flush_texture_labels()==0&&waits==1&&writes.size()==1,"outstanding completion lost label");
        reset();s_d3d.texture_label_count=8;s_d3d.texture_label_armed=1;
        CHECK(d3d12_defer_texture_label(nullptr,0,1)&&s_d3d.gcm2d_backend_halted&&writes.empty(),"overflow failed open");
        for(int reason=0;reason<9;++reason){
            reset();auto& dr=s_d3d.draws[0];
            if(reason==0)dr.is_vp=0;if(reason==1)dr.is_clear=1;
            if(reason==2)dr.is_scaled_blit=1;if(reason==3)dr.fp_program_valid=0;
            if(reason==4)dr.tex_snapshot[0]=nullptr;if(reason==5)dr.tex_snapshot_fault_mask=1;
            if(reason==6)dr.tex[0]={1,0x10000,1};
            if(reason==7)dr.vtex[2]={1,0x30000,2};
            if(reason==8)s_d3d.fifo_prefix_outstanding=1;
            CHECK(!d3d12_texture_prefix_can_batch(),"unsafe prefix allowed");
            CHECK(rsx_d3d12_backend_prepare_texture_label()==0&&renders==1&&!s_d3d.texture_label_armed,"unsafe prefix deferred");
        }
        reset();s_d3d.draws[0].tex[15]={1,0x30000,2};
        CHECK(d3d12_texture_prefix_can_batch(),"unused fragment binding blocks batch");
        s_d3d.draws[0].fp_program.texture_read_mask=1u<<15;
        CHECK(!d3d12_texture_prefix_can_batch(),"new sampled texture lacks snapshot");
        for(int hazard=0;hazard<8;++hazard){
            reset();queue(4,7);
            if(hazard==0)s_d3d.draw_count=MAX_DRAWS;
            if(hazard==1)s_d3d.vp_vb_offset=MAX_VERTICES*VP_VERT_STRIDE-1;
            if(hazard==2)s_d3d.vb_offset=MAX_VERTICES*VERTEX_STRIDE-1;
            if(hazard==3)s_d3d.cur_texs[15]={1,0x10000,1};
            if(hazard==4)s_d3d.cur_texs[0]={1,0x20000,1};
            if(hazard==5){s_d3d.draws[0].rt_raw[3]=0x40000;s_d3d.draws[0].rt_pitch[3]=256;s_d3d.cur_texs[0]={1,0x40000,2};}
            if(hazard==6){s_d3d.draws[0].zeta_raw=0x40000;s_d3d.draws[0].zeta_pitch=256;s_d3d.cur_vtexs[3]={1,0x40000,2};}
            CHECK(d3d12_texture_batch_before_draw(3,0)==0,"pre-draw flush failed");
            CHECK(renders==(hazard==7?0:1)&&writes.size()==(hazard==7?0:1),"capacity or GPU input boundary");
        }
        reset();queue(4,7);render_fail=1;s_d3d.draw_count=MAX_DRAWS;
        CHECK(d3d12_texture_batch_before_draw(3,0)<0&&writes.empty(),"failed pre-draw flush continued");
        reset();queue(4,7);program_mask=1;s_d3d.cur_texs[15]={1,0x10000,1};
        CHECK(d3d12_texture_batch_before_draw(3,0)==0&&renders==0,"unused stale GPU binding flushes batch");
        program_mask=1u<<15;
        CHECK(d3d12_texture_batch_before_draw(3,0)==0&&renders==1,"changed program did not flush new GPU input");
        reset();program_mask=1u<<3;
        CHECK(d3d12_texture_batch_fragment_mask()==9,"fallback t0 missing");
        for(int reason=0;reason<4;++reason){
            reset();program_mask=0;
            if(reason==0)program_fails=1;if(reason==1)location_fails=1;
            if(reason==2)mapping_fails=1;if(reason==3)s_d3d.current_rsx_state=nullptr;
            CHECK(d3d12_texture_batch_fragment_mask()==0xFFFF,"unknown program failed open");
        }
        reset();copy_record();
        CHECK(d3d12_texture_prefix_can_batch()==copy_enabled,"copy requires separate opt-in");
        if(copy_enabled){
            queue(4,7);CHECK(renders==0&&writes.empty(),"copy label published early");
            CHECK(rsx_d3d12_backend_flush_texture_labels()==0&&renders==1&&writes.size()==1,
                  "copy label missing checked completion");
            for(int bad=0;bad<15;++bad){
                reset();copy_record();auto& b=s_d3d.draws[0].scaled_blit;
                if(bad==0)b.src_local=1;if(bad==1)b.dst_local=0;
                if(bad==2)b.src_snapshot=nullptr;if(bad==3)--b.src_snapshot_bytes;
                if(bad==4)b.dst_snapshot=(u8*)1;if(bad==5)b.dst_post_snapshot_bytes=1;
                if(bad==6)b.mapping_generation=4;if(bad==7)generation=3;
                if(bad==8)mapping_fails=1;if(bad==9)b.dst_ea++;
                if(bad==10)b.copy_w=0;if(bad==11)b.dst_pitch=63;
                if(bad==12)b.src_cpu_owned=1;if(bad==13)b.src_x=8;
                if(bad==14)b.dst_y=16;
                CHECK(!d3d12_texture_prefix_can_batch(),"invalid copy eligible");
            }
            /* Dirty rows: [EA+132,EA+148), [EA+196,EA+212).
             * The guard deliberately encloses the pitch hole too. */
            for(int main_alias=0;main_alias<2;++main_alias){
                for(u32 offset=112;offset<228;++offset){
                    reset();copy_record();queue(4,7);
                    const u32 ea=0x40050000+offset;
                    const u32 raw=main_alias?0x90000:0x50000+offset;
                    if(main_alias){alias_raw=raw;alias_ea=ea;}
                    s_d3d.cur_texs[0]={1,raw,main_alias?2u:1u};
                    CHECK(d3d12_texture_batch_before_draw(3,0)==0,"alias guard failed");
                    const bool overlaps=offset<212&&132<offset+span_bytes;
                    CHECK(renders==overlaps&&writes.size()==(overlaps?1u:0u),
                          "partial/containing alias missed or disjoint flushed");
                }
            }
            reset();copy_record();queue(4,7);alias_raw=0x90000;alias_ea=0x40050000+120;
            span_bytes=100;s_d3d.cur_vtexs[3]={1,alias_raw,2};
            CHECK(d3d12_texture_batch_before_draw(3,0)==0&&renders==1,"containing vertex alias missed");
            reset();copy_record();queue(4,7);program_mask=1;
            s_d3d.cur_texs[15]={1,0x50000+132,1};
            CHECK(d3d12_texture_batch_before_draw(3,0)==0&&renders==0,"unused alias flushes copy");
            for(int bad=0;bad<7;++bad){
                reset();copy_record();queue(4,7);s_d3d.cur_texs[0]={1,0x60000,2};
                if(bad==0)span_fails=1;if(bad==1)mapping_fails=1;
                if(bad==2)generation=3;if(bad==3)generation=4;
                if(bad==4)generation_changes=1;if(bad==5)s_d3d.cur_texs[0].location=0;
                if(bad==6)ppu_vm_size=0x50000;
                CHECK(d3d12_texture_batch_before_draw(3,0)==0&&renders==1,"unknown copy input failed open");
            }
            reset();copy_record();queue(4,7);render_fail=1;
            s_d3d.cur_texs[0]={1,0x50000+132,1};
            CHECK(d3d12_texture_batch_before_draw(3,0)<0&&writes.empty(),"failed copy-input completion published");
            for(int source=0;source<6;++source){
                reset();copy_record();queue(4,7);
                if(source==0)s_d3d.current_rsx_state=nullptr;
                if(source==1)current_state.shader_program=0;
                if(source==2)location_fails=1;
                if(source==3){current_state.shader_program=0x90000;alias_raw=0x90000;alias_ea=0x40050000+100;}
                if(source==4){current_state.vertex_attrib_input_mask=1;current_state.vertex_attribs[0]={1,16};}
                if(source==5)current_state.index_array_offset=0x70000;
                CHECK(d3d12_texture_batch_before_draw(3,source==5)==0&&renders==1,"copy CPU reader failed open");
            }
            reset();copy_record();queue(4,7);current_state.index_array_offset=0x70000;
            current_state.immediate_index_data=(u32*)1;
            CHECK(d3d12_texture_batch_before_draw(3,1)==0&&renders==0,"host index binding flushed copy");
            reset();copy_record();queue(4,7);
            CHECK(d3d12_texture_batch_before_draw(3,1)==0&&renders==1,"zero index address failed open");
        }
    }
    printf("enabled=%d: %d checks, %d failures\n",enabled,checks,failures);
    return failures?1:0;
}
'''.replace("PRODUCTION", "\n".join(bodies)).replace("COMPLETION_REASONS", reasons).replace("EXACT_BLIT_STRUCT", blit_struct)
    compiler = shutil.which("clang++") or shutil.which("g++") or r"C:\Program Files\LLVM\bin\clang++.exe"
    with tempfile.TemporaryDirectory(prefix="tj-label-batch-") as directory:
        source, exe = Path(directory) / "test.cpp", Path(directory) / "test.exe"
        source.write_text(fixture, encoding="utf-8")
        subprocess.run([compiler, "-std=c++17", "-D_CRT_SECURE_NO_WARNINGS", "-I", str(ROOT.parent / "ps3recomp/libs/video"), str(source), "-o", str(exe)], check=True)
        for value, probe, expected in [(None, False, 0), ("0", False, 0),
                                       ("", False, 0), ("1", False, 1),
                                       ("1", True, 0)]:
            env = {k:v for k,v in os.environ.items() if k not in (
                "TJ_TEXTURE_LABEL_BATCH", "FP_FORCE", "TJ_FP_PROBE_T0", "TJ_FP_PROBE_T1",
                "TJ_FP_PROBE_STAGEGRID", "TJ_FP_PROBE_LATEGRID")}
            if value is not None:
                env["TJ_TEXTURE_LABEL_BATCH"] = value
            if probe:
                env["FP_FORCE"] = "1"
            for copy_value in (None, "0", "1"):
                env.pop("TJ_TEXTURE_COPY_LABEL_BATCH", None)
                if copy_value is not None:
                    env["TJ_TEXTURE_COPY_LABEL_BATCH"] = copy_value
                for diagnostic in (0, 1):
                    subprocess.run([str(exe), str(expected), str(diagnostic), str(int(copy_value == "1"))], env=env, check=True, timeout=10)


if __name__ == "__main__":
    main()
