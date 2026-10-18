"""Compile actual clear/present routing; no GPU or game process required."""
from pathlib import Path
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
VIDEO = ROOT.parent / "ps3recomp/libs/video"


def function(text, signature):
    start = text.index(signature)
    body = text.index("{", start)
    depth = 1
    end = body + 1
    while depth:
        depth += (text[end] == "{") - (text[end] == "}")
        end += 1
    return text[start:end]


def test_flip_enqueue(gcm, compiler, temp):
    source = r'''
#include <stdint.h>
#include <stdio.h>
#include <string.h>
typedef uint32_t u32; typedef int32_t s32;
#define CELL_OK 0
#define CELL_GCM_ERROR_INVALID_VALUE (-1)
#define CELL_GCM_ERROR_FAILURE (-2)
#define CELL_GCM_MAX_DISPLAY_BUFFER_NUM 8
#define CELL_GCM_MAX_LABEL_COUNT 256u
#define GCM_LABEL_GUEST_BASE 0x800u
#define GCM_LABEL_STRIDE 0x10u
static unsigned char memory[8192];
static unsigned char* vm_base=memory;
static int s_display_buffer_set[8],immediate;
static u32 vm_read32(u32 ea) {
    return (u32)memory[ea]<<24|(u32)memory[ea+1]<<16|(u32)memory[ea+2]<<8|memory[ea+3];
}
static void vm_write32(u32 ea,u32 v) {
    for(int i=0;i<4;++i) memory[ea+i]=(unsigned char)(v>>(24-8*i));
}
static s32 cellGcmSetFlipCommand(u32 id) { (void)id; ++immediate; return 0; }
ENQUEUE
LEGACY_WAIT
WAIT_ENQUEUE
#define CHECK(c,label) do { if(!(c)) { fprintf(stderr,"FAIL: %s\n",label); return 1; } } while(0)
int main(void) {
    s_display_buffer_set[1]=1;
    vm_write32(256,512); vm_write32(260,1024); vm_write32(264,600);
    vm_write32(596,0xAABBCCDD); vm_write32(608,0x12345678);
    CHECK(_cellGcmSetFlipCommand((void*)(uintptr_t)256,1)==CELL_OK,"valid enqueue rejected");
    CHECK(!immediate,"context import published flip before FIFO reached its position");
    CHECK(vm_read32(600)==0xFEAD0001 && vm_read32(604)==0 && vm_read32(264)==608,
          "flip packet or BE context-current update incorrect");
    CHECK(vm_read32(596)==0xAABBCCDD && vm_read32(608)==0x12345678,"adjacent guest commands changed");
    CHECK(_cellGcmSetFlipCommand((void*)(uintptr_t)256,8)!=CELL_OK && vm_read32(264)==608,
          "invalid display buffer mutated context");
    vm_write32(264,1020);
    CHECK(_cellGcmSetFlipCommand((void*)(uintptr_t)256,1)!=CELL_OK && vm_read32(264)==1020,
          "short reservation overwritten");
    vm_write32(264,602);
    CHECK(_cellGcmSetFlipCommand((void*)(uintptr_t)256,1)!=CELL_OK,"unaligned command accepted");
    CHECK(_cellGcmSetFlipCommand(NULL,1)!=CELL_OK,"null context accepted");
    vm_write32(264,600); vm_write32(620,0x12345678);
    vm_write32(GCM_LABEL_GUEST_BASE+7*GCM_LABEL_STRIDE,0x55);
    CHECK(_cellGcmSetFlipCommandWithWaitLabel((void*)(uintptr_t)256,1,7,0x12345678)==CELL_OK,
          "valid wait/flip enqueue rejected");
    CHECK(!immediate && vm_read32(GCM_LABEL_GUEST_BASE+7*GCM_LABEL_STRIDE)==0x55,
          "wait/flip import published a flip or wrote its own expected label");
    CHECK(vm_read32(600)==0x00080064 && vm_read32(604)==0x70 && vm_read32(608)==0x12345678 &&
          vm_read32(612)==0xFEAD0001 && vm_read32(616)==0 && vm_read32(264)==620,
          "wait/acquire/flip packet or BE context-current update incorrect");
    CHECK(vm_read32(596)==0xAABBCCDD && vm_read32(620)==0x12345678,"wait/flip changed adjacent commands");
    for (int bad=0;bad<7;++bad) {
        unsigned char before[sizeof(memory)];
        vm_write32(264,bad==2?1008:(bad==3?602:(bad==6?508:600)));
        memcpy(before,memory,sizeof(memory));
        void* ctx=bad==4?NULL:(void*)(uintptr_t)(bad==5?258:256);
        CHECK(_cellGcmSetFlipCommandWithWaitLabel(ctx,bad==0?8:1,bad==1?256:7,9)!=CELL_OK,
              "invalid wait/flip request accepted");
        CHECK(!memcmp(before,memory,sizeof(memory)) && !immediate,"failed wait/flip partially mutated guest state");
    }
    vm_write32(264,1004); vm_write32(1024,0x87654321);
    CHECK(_cellGcmSetFlipCommandWithWaitLabel((void*)(uintptr_t)256,1,255,0xA1B2C3D4)==CELL_OK &&
          vm_read32(264)==1024 && vm_read32(1008)==0xff0 && vm_read32(1012)==0xA1B2C3D4 &&
          vm_read32(1024)==0x87654321,"exact-fit wait/flip failed or crossed context end");
    puts("PASS: actual flip import enqueues without early publish or adjacent writes");
    return 0;
}
'''.replace("\nENQUEUE\n", "\n" + function(gcm, "s32 _cellGcmSetFlipCommand(") + "\n")
    source = source.replace("LEGACY_WAIT\n", function(gcm, "s32 cellGcmSetFlipCommandWithWaitLabel(") + "\n")
    source = source.replace("WAIT_ENQUEUE\n", function(gcm, "s32 _cellGcmSetFlipCommandWithWaitLabel(") + "\n")
    test = Path(temp) / "enqueue.c"
    test.write_text(source, encoding="utf-8")
    exe = Path(temp) / "enqueue.exe"
    subprocess.run([compiler, "-std=c11", str(test), "-o", str(exe)], check=True)
    subprocess.run([str(exe)], check=True)


def main():
    backend = (VIDEO / "rsx_d3d12_backend.c").read_text(encoding="utf-8")
    source = r'''
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include "rsx_boundary_policy.h"
typedef uint8_t u8; typedef uint32_t u32; typedef uint64_t u64;
#define MAX_DRAWS 16
#define RSX_CLEAR_COLOR_MASK 0xF0u
#define RSX_CLEAR_DEPTH_STENCIL_MASK 3u
#define CELL_GCM_FLIP_SOURCE_FIFO 2u
typedef struct { u64 generation; u32 buffer_id, source; } CellGcmFlipTicket;
typedef struct {
    int rt_is_display,is_vp,is_clear,tex_slot;
    u32 clear_flags,rt_off,rt_mrt[3],rt_w,rt_h,rt_fmt;
    u8 clear_stencil,rt_local,rt_mrt_local[3]; float clear_depth,cc[4];
} D3D12DrawRecord;
typedef struct { u32 surface_format; } State;
static struct {
    int gcm2d_backend_halted,initialized;
    u32 draw_count,vb_offset,vp_vb_offset,texture_label_flush_kind;
    u64 submitted_boundary_generation;
    D3D12DrawRecord draws[MAX_DRAWS]; State* current_rsx_state;
} s_d3d;
static u32 s_dbg_clears_since_present,s_clear_presents;
static CellGcmFlipTicket consumed;
static u32 presentations,last_draws,last_buffer,last_raw;
static u64 work_generation;
static u32 work_source;
static void tj_present_work_trace_ticket(u64 generation,u32 source) {
    work_generation=generation; work_source=source;
}
enum { TJ_BOUNDARY_OFFSCREEN_CLEAR, TJ_BOUNDARY_DISPLAY_CLEAR,
       TJ_BOUNDARY_REJECT_NO_DISPLAY,TJ_BOUNDARY_REJECT_SAME_FLIP,TJ_BOUNDARY_ACCEPT };
static void tj_boundary_perf_note(int unused,...) { (void)unused; }
static void tj_survival_pending_trace(const char* unused) { (void)unused; }
static int blink_dbg(void) { return 0; }
static void gcm2d_discard_records(void) { s_d3d.draw_count=0; }
static void gpu_blit_record_snapshots_release(void) {}
static int rsx_d3d12_backend_flush_texture_labels(void) { return 0; }
static void record_rt_registers(D3D12DrawRecord* unused) { (void)unused; }
static u32 current_rt_off(u32* w,u32* h,u32* mrt,u8* local,u8* ml) {
    *w=1280; *h=720; (void)mrt; (void)local; (void)ml; return 0;
}
static int cellGcm_flip_consumed_ticket_snapshot(CellGcmFlipTicket* out) {
    *out=consumed; return consumed.generation!=0;
}
static int selected_display_raw_for_id(u32 id,u32* raw) { *raw=id*0x100000; return 1; }
static void render_frame(u32 id,u32 raw,int valid,int prefix) {
    (void)valid; (void)prefix; ++presentations;
    last_buffer=id; last_raw=raw; last_draws=s_d3d.draw_count; s_d3d.draw_count=0;
}
static void d3d12_record_display_clear(const float* cc,u32 w,u32 h,u32 f,float d,u8 s) {
    (void)cc;(void)w;(void)h;(void)f;(void)d;(void)s;
    s_d3d.draws[s_d3d.draw_count++].is_clear=1;
}
CLEAR
PRESENT
#define CHECK(c,label) do { if (!(c)) { fprintf(stderr,"FAIL: %s\n",label); return 1; } } while(0)
int main(void) {
    s_d3d.initialized=1; s_clear_presents=1;
    s_d3d.submitted_boundary_generation=40;
    consumed=(CellGcmFlipTicket){41,1,CELL_GCM_FLIP_SOURCE_FIFO};
    s_d3d.draws[0].rt_is_display=1; s_d3d.draw_count=1;
    d3d12_clear(NULL,RSX_CLEAR_DEPTH_STENCIL_MASK,0,1.0f,0);
    CHECK(presentations==0 && s_d3d.draw_count==2,"in-frame clear presented before tutorial/UI tail");
    s_d3d.draws[s_d3d.draw_count++].rt_is_display=1;
    consumed.generation=42;
    rsx_d3d12_backend_present_buffer(42,1);
    CHECK(presentations==1 && last_draws==3 && last_buffer==1 && last_raw==0x100000,
          "ordered flip did not present complete selected frame");
    CHECK(work_generation==42 && work_source==CELL_GCM_FLIP_SOURCE_FIFO,
          "FIFO presentation trace attributed the wrong consumed ticket");
    /* Prefix completion discarded records, not the executed surface. */
    consumed.generation=43;
    rsx_d3d12_backend_present_buffer(43,2);
    CHECK(presentations==2 && last_draws==0 && last_buffer==2,
          "prefix-only frame was dropped at its flip");
    CHECK(work_generation==43 && work_source==CELL_GCM_FLIP_SOURCE_FIFO,
          "prefix-only presentation lost its consumed ticket");
    rsx_d3d12_backend_present_buffer(43,2);
    CHECK(presentations==2,"same flip presented twice");
    consumed=(CellGcmFlipTicket){44,1,1};
    s_d3d.draws[0].rt_is_display=1; s_d3d.draw_count=1;
    d3d12_clear(NULL,RSX_CLEAR_COLOR_MASK,0,1.0f,0);
    CHECK(presentations==3,"direct-HLE legacy boundary changed");
    CHECK(work_generation==44 && work_source==1,
          "legacy presentation trace lost its consumed ticket");
    s_d3d.gcm2d_backend_halted=1;
    consumed=(CellGcmFlipTicket){45,1,CELL_GCM_FLIP_SOURCE_FIFO};
    rsx_d3d12_backend_present_buffer(45,1);
    CHECK(presentations==3,"halted backend presented");
    puts("PASS: actual FIFO clear/present routing, UI tail, prefix-only frame, legacy and halt");
    return 0;
}
'''.replace("CLEAR\n", function(backend, "static void d3d12_clear(") + "\n")
    source = source.replace("PRESENT\n", function(backend, "void rsx_d3d12_backend_present_buffer(") + "\n")
    compiler = shutil.which("clang") or shutil.which("gcc") or r"C:\Program Files\LLVM\bin\clang.exe"
    gcm = (VIDEO / "cellGcmSys.c").read_text(encoding="utf-8")
    with tempfile.TemporaryDirectory(prefix="tj-fifo-boundary-") as temp:
        test = Path(temp) / "test.c"
        test.write_text(source, encoding="utf-8")
        exe = Path(temp) / "test.exe"
        subprocess.run([compiler, "-std=c11", "-I", str(VIDEO), str(test), "-o", str(exe)], check=True)
        subprocess.run([str(exe)], check=True)
        test_flip_enqueue(gcm, compiler, temp)
    assert "gcm_flip_publish(bufferId, 1, frame_prof_source)" in gcm
    assert "u32 source;" in (VIDEO / "cellGcmSys.h").read_text(encoding="utf-8")
    assert "uint32_t source;" in (ROOT / "src/tj_present.cpp").read_text(encoding="utf-8")


if __name__ == "__main__":
    main()
