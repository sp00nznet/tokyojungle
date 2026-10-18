"""Exercise production flip completion and callback ordering without a game/GPU.

The pre-fix source is supported so the same scenarios demonstrate the failure.
This proves host state transitions, not rendering or interactive gameplay.
"""
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

from test_fifo_present_boundary import function

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT.parent / "ps3recomp/libs/video/cellGcmSys.c"


def main():
    source = SOURCE.read_text(encoding="utf-8")
    publication = re.search(
        r"typedef struct GcmFlipPublication \{.*?\} GcmFlipPublication;", source, re.S
    ).group()
    signatures = [
        "static void gcm_flip_publication_lock(void)",
        "static void gcm_flip_publication_unlock(void)",
        "static int gcm_flip_publication_qpc_enabled(void)",
        "static CellGcmFlipPublicationTiming gcm_flip_publication_timestamp_locked(int pending)",
        "static void gcm_flip_publish(u32 buffer_id, int pending, u32 source)",
        "static void gcm_flip_publication_reset(void)",
        "static int gcm_flip_take_ticket(CellGcmFlipTicket* out_ticket,\n                                CellGcmFlipPublicationTiming* out_timing)",
        "int cellGcm_take_flip_ticket(CellGcmFlipTicket* out_ticket)",
        "int cellGcm_take_flip_ticket_timed(CellGcmFlipTicket* out_ticket,\n                                   CellGcmFlipPublicationTiming* out_timing)",
        "int cellGcm_flip_consumed_ticket_snapshot(CellGcmFlipTicket* out_ticket)",
        "void cellGcmResetFlipStatus(void)",
        "void cellGcm_request_tick(void)",
        "void ppu_gcm_pump(void)",
        "void cellGcmTickVBlank(void)",
        "void cellGcmTickFlip(void)",
        "static s32 cellGcmSetFlipCommand_source(u32 bufferId, u32 frame_prof_source)",
    ]
    complete = "int cellGcm_complete_flip_ticket(const CellGcmFlipTicket* ticket)"
    has_completion = complete in source
    if has_completion:
        signatures.append(complete)
    bodies = []
    for signature in signatures:
        # The source also forward-declares SetFlipCommand_source. Select its body.
        match = re.search(re.escape(signature) + r"\s*\{", source)
        bodies.append(function(source[match.start():], signature))

    fixture = r'''
#include <stdatomic.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
typedef uint32_t u32; typedef uint64_t u64; typedef int32_t s32;
typedef struct { u64 generation; u32 buffer_id, source; } CellGcmFlipTicket;
enum { CELL_GCM_FLIP_PUBLICATION_QPC_OFF=0, CELL_GCM_FLIP_PUBLICATION_QPC_VALID=1,
       CELL_GCM_FLIP_PUBLICATION_QPC_FAILED=2 };
typedef struct { u64 publication_qpc; u32 publication_qpc_state; } CellGcmFlipPublicationTiming;
typedef struct { u32 generation, opd, code, cause, sequence; } GcmUserInterrupt;
#define CELL_OK 0
#define CELL_GCM_ERROR_INVALID_VALUE (-1)
#define CELL_GCM_MAX_DISPLAY_BUFFER_NUM 8
#define CELL_GCM_FLIP_STATUS_DONE 0
#define CELL_GCM_FLIP_STATUS_WAITING 1
INSERT_PUBLICATION_STRUCT
static GcmFlipPublication s_flip_publication = { ATOMIC_FLAG_INIT, 0 };
static atomic_int s_flip_publication_qpc_enabled = ATOMIC_VAR_INIT(-1);
static atomic_uint s_current_display_buffer_id, s_flip_request_count;
static atomic_uint s_flip_callbacks_pending, s_user_interrupt_generation;
static atomic_uint s_tj_flip_reset_count, s_flip_status;
static u64 s_vblank_count, s_last_flip_time;
static int s_tj_survival_flip_diag, s_gcm_pending, callbacks, vblanks, bad_head;
static int checks, failures, s_display_buffer_set[8];
static u32 s_flip_handler_opd=0x100, s_flip_handler_code=0x200;
static u32 s_vblank_handler_opd=0x300, s_vblank_handler_code=0x400;
#define GCM_PENDING_SET(bits) (s_gcm_pending |= (bits))
#define GCM_PENDING_TAKE() take_pending()
static int take_pending(void) { int p=s_gcm_pending; s_gcm_pending=0; return p; }
static u32 flip_status_load(void) { return atomic_load(&s_flip_status); }
static void flip_status_store(u32 v) { atomic_store(&s_flip_status,v); }
static void tj_flip_trace(const char* event,u32 value) { (void)event; (void)value; }
static void tj_flip_perf_tick_done(void) {}
static void tj_flip_perf_set_waiting(void) {}
static void frame_prof_publish(u32 source) { (void)source; }
/* This default-off fixture checks flip state transitions, not event delivery. */
static void gcm_flip_kick_presenter(void) {}
static u64 get_timestamp_ns(void) { static u64 now=100; return ++now; }
typedef struct { long long QuadPart; } LARGE_INTEGER;
static int qpc_calls;
static int QueryPerformanceCounter(LARGE_INTEGER* now) {
    ++qpc_calls;
    if (getenv("TJ_TEST_QPC_FAIL")) return 0;
    now->QuadPart=100+qpc_calls; return 1;
}
static void ydkj_restore_handler_opd(u32 opd,u32 code) { (void)opd; (void)code; }
static int gcm_user_interrupt_take(GcmUserInterrupt* event) { (void)event; return 0; }
static int gcm_interrupt_dispatch_allowed(void) { return 1; } /* Ownership is covered by test_gcm_interrupt_worker.py. */
static void guest_call(u32 opd,u64 a,u64 b,u64 c,u64 d,u64 e,u64 f,u64 g,u64 h) {
    (void)b; (void)c; (void)d; (void)e; (void)f; (void)g; (void)h;
    if (opd==s_flip_handler_opd) { ++callbacks; if(a!=1) ++bad_head; }
    if (opd==s_vblank_handler_opd) ++vblanks;
}
static void (*g_ps3_guest_caller)(u32,u64,u64,u64,u64,u64,u64,u64,u64)=guest_call;
void cellGcmTickVBlank(void);
void cellGcmTickFlip(void);
int cellGcm_complete_flip_ticket(const CellGcmFlipTicket* ticket);
BODIES
COMPLETE_WRAPPER
#define CHECK(test,message) do { ++checks; if (!(test)) { ++failures; fprintf(stderr,"FAIL: %s\n",message); } } while(0)
static void reset(void) {
    gcm_flip_publication_reset();
    s_gcm_pending=callbacks=vblanks=bad_head=0;
    atomic_store(&s_flip_callbacks_pending,0);
    flip_status_store(CELL_GCM_FLIP_STATUS_DONE);
    s_flip_handler_opd=0x100;
    for(int i=0;i<8;i++) s_display_buffer_set[i]=1;
}
static CellGcmFlipTicket submit(u32 buffer) {
    CellGcmFlipTicket ticket={0};
    CHECK(cellGcmSetFlipCommand_source(buffer,1)==CELL_OK,"valid submit rejected");
    CHECK(cellGcm_take_flip_ticket(&ticket),"submitted ticket unavailable");
    return ticket;
}
int main(void) {
    CellGcmFlipTicket a={0},b={0},bad={0};
    CellGcmFlipPublicationTiming timing={UINT64_MAX,UINT32_MAX};
    reset();
    gcm_flip_publish(6,1,1);
    CHECK(cellGcm_take_flip_ticket_timed(&a,&timing),"timed take missed pending ticket");
    const int timing_enabled=getenv("TJ_FLIP_TIMELINE") &&
        !strcmp(getenv("TJ_FLIP_TIMELINE"),"1");
    const int timing_failed=getenv("TJ_TEST_QPC_FAIL")!=NULL;
    CHECK(a.buffer_id==6,"timed take changed ticket identity");
    if (!timing_enabled)
        CHECK(timing.publication_qpc==0 &&
              timing.publication_qpc_state==CELL_GCM_FLIP_PUBLICATION_QPC_OFF &&
              qpc_calls==0,"default-off timed take called QPC");
    else if (timing_failed)
        CHECK(timing.publication_qpc==0 &&
              timing.publication_qpc_state==CELL_GCM_FLIP_PUBLICATION_QPC_FAILED,
              "QPC failure did not remain an invalid timing sidecar");
    else {
        CHECK(timing.publication_qpc>0 &&
              timing.publication_qpc_state==CELL_GCM_FLIP_PUBLICATION_QPC_VALID,
              "enabled timed take missed publication QPC");
        CellGcmFlipPublicationTiming first=timing;
        CHECK(cellGcmSetFlipCommand_source(7,1)==CELL_OK &&
              cellGcmSetFlipCommand_source(0,1)==CELL_OK,
              "overwrite fixture could not publish two tickets");
        CHECK(cellGcm_take_flip_ticket_timed(&b,&timing) && b.buffer_id==0 &&
              timing.publication_qpc_state==CELL_GCM_FLIP_PUBLICATION_QPC_VALID &&
              timing.publication_qpc>first.publication_qpc,
              "latest pending ticket did not retain its own publication QPC");
    }
    reset(); timing=(CellGcmFlipPublicationTiming){UINT64_MAX,UINT32_MAX};
    CHECK(!cellGcm_take_flip_ticket_timed(&a,&timing) && !timing.publication_qpc &&
          timing.publication_qpc_state==CELL_GCM_FLIP_PUBLICATION_QPC_OFF,
          "reset left a stale pending timing sidecar");
    reset();
    cellGcmResetFlipStatus();
    cellGcmTickFlip(); ppu_gcm_pump();
    CHECK(flip_status_load()==1,"idle timer invented flip completion");
    CHECK(callbacks==0,"idle timer invented flip callback");
    cellGcm_request_tick(); ppu_gcm_pump();
    CHECK(flip_status_load()==1,"combined tick invented flip completion");
    CHECK(callbacks==0,"combined tick invented flip callback");
    CHECK(vblanks==1,"vblank was lost while no flip exists");

    reset();
    a=submit(2);
    CHECK(callbacks==0,"submission invoked callback before presentation");
    CHECK(flip_status_load()==1,"taking ticket completed presentation early");
    finish(&a);
    CHECK(flip_status_load()==0,"presented frame did not publish DONE");
    CHECK(callbacks==0,"completion ran guest callback on presenter thread");
    ppu_gcm_pump();
    CHECK(callbacks==1,"completed frame did not produce exactly one callback");
    CHECK(bad_head==0,"flip callback received wrong argument (RPCS3 uses 1)");
    finish(&a); cellGcmTickFlip(); ppu_gcm_pump();
    CHECK(callbacks==1,"duplicate completion or idle tick replayed callback");

    reset();
    a=submit(1); finish(&a);
    cellGcmResetFlipStatus();
    b=submit(2);
    ppu_gcm_pump();
    CHECK(flip_status_load()==1,"old queued callback completed newer frame");
    CHECK(callbacks==1,"old completed frame callback was lost or duplicated");
    finish(&a); ppu_gcm_pump();
    CHECK(flip_status_load()==1,"old ticket completed newer frame");
    CHECK(callbacks==1,"old ticket generated another callback");
    finish(&b); ppu_gcm_pump();
    CHECK(flip_status_load()==0 && callbacks==2,"new frame did not complete independently");

    reset();
    a=submit(3);
    CHECK(cellGcmSetFlipCommand_source(4,1)==CELL_OK,"overlapping request rejected");
    finish(&a); ppu_gcm_pump();
    CHECK(flip_status_load()==1,"older in-flight frame cleared newer pending request");
    CHECK(callbacks==1,"older in-flight frame lost its completion event");
    CHECK(cellGcm_take_flip_ticket(&b) && b.buffer_id==4,"newer pending request was lost");
    finish(&b); ppu_gcm_pump();
    CHECK(flip_status_load()==0 && callbacks==2,"overlapping frames did not retire separately");

    reset();
    a=submit(0); bad=a; ++bad.generation;
    finish(&bad); ppu_gcm_pump();
    CHECK(flip_status_load()==1 && callbacks==0,"unknown generation completed a frame");
    bad=a; ++bad.buffer_id;
    finish(&bad); ppu_gcm_pump();
    CHECK(flip_status_load()==1 && callbacks==0,"wrong buffer completed a frame");
    bad=a; ++bad.source;
    finish(&bad); ppu_gcm_pump();
    CHECK(flip_status_load()==1 && callbacks==0,"wrong source completed a frame");
    finish(&a); ppu_gcm_pump();
    CHECK(flip_status_load()==0 && callbacks==1,"valid identity was rejected after invalid tickets");

    reset();
    a=submit(0); finish(&a);
    b=submit(1); finish(&b);
    ppu_gcm_pump();
    CHECK(callbacks==2,"two completed frames coalesced into one callback");
    ppu_gcm_pump();
    CHECK(callbacks==2,"pump replayed already delivered callbacks");
    cellGcmResetFlipStatus(); cellGcmTickFlip(); ppu_gcm_pump();
    CHECK(flip_status_load()==1 && callbacks==2,"reset reused a previously completed ticket");

    reset();
    a=submit(5);
    reset(); cellGcmResetFlipStatus();
    finish(&a); ppu_gcm_pump();
    CHECK(flip_status_load()==1 && callbacks==0,"ticket survived terminate/re-init boundary");
    b=submit(5);
    CHECK(b.generation>a.generation,"generation reused after reset");
    finish(&b); ppu_gcm_pump();
    CHECK(callbacks==1 && flip_status_load()==0,"new lifetime failed to complete");

    reset(); s_flip_handler_opd=0;
    a=submit(1); finish(&a); ppu_gcm_pump();
    CHECK(flip_status_load()==0 && callbacks==0,"no-handler completion failed");
    s_flip_handler_opd=0x100; ppu_gcm_pump();
    CHECK(callbacks==0,"late handler received stale completion");
    printf("flip completion: %d/%d checks passed\n",checks-failures,checks);
    return failures ? 1 : 0;
}
'''
    fixture = fixture.replace("INSERT_PUBLICATION_STRUCT", publication).replace("BODIES", "\n".join(bodies))
    fixture = fixture.replace("COMPLETE_WRAPPER", (
        "static void finish(const CellGcmFlipTicket* t) { cellGcm_complete_flip_ticket(t); }"
        if has_completion else
        "static void finish(const CellGcmFlipTicket* t) { (void)t; cellGcmTickFlip(); }"
    ))
    compiler = shutil.which("clang") or "C:/Program Files/LLVM/bin/clang.exe"
    with tempfile.TemporaryDirectory(prefix="tj-flip-completion-") as temp:
        path = Path(temp) / "test.c"
        path.write_text(fixture, encoding="utf-8")
        exe = Path(temp) / "test.exe"
        subprocess.run([compiler, "-std=c11", "-O2", "-D_CRT_SECURE_NO_WARNINGS",
                        str(path), "-o", str(exe)], check=True)
        cases = ({}, {"TJ_FLIP_TIMELINE": "1"},
                 {"TJ_FLIP_TIMELINE": "1", "TJ_TEST_QPC_FAIL": "1"})
        for extra in cases:
            env = dict(__import__("os").environ)
            env.pop("TJ_FLIP_TIMELINE", None)
            env.pop("TJ_TEST_QPC_FAIL", None)
            env.update(extra)
            subprocess.run([str(exe)], env=env, check=True)


if __name__ == "__main__":
    main()
