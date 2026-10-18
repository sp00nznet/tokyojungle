"""Prove SetPrepareFlip completion is ordered with ticket publication.

The source-extracted fixture checks host flip state and callback arguments;
it does not model RSX execution, guest command streams, or FPS.
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
    publish_signature = "static void gcm_flip_publish(u32 buffer_id, int pending, u32 source)"
    prepare_signature = "s32 cellGcmSetPrepareFlip(void* ctx, u32 bufferId)"
    take_signature = (
        "static int gcm_flip_take_ticket(CellGcmFlipTicket* out_ticket,"
    )
    publish = function(source, publish_signature)
    prepare = function(source, prepare_signature)
    take = function(source, take_signature)
    take_public = function(source, "int cellGcm_take_flip_ticket(CellGcmFlipTicket* out_ticket)")
    complete = function(source, "int cellGcm_complete_flip_ticket(const CellGcmFlipTicket* ticket)")

    locked_done = re.compile(
        r"    /\* Immediate completion belongs to this generation too\. Publish it before\s+"
        r"\* unlocking so a later pending request cannot be cleared by its caller\. \*/\s+"
        r"if \(!pending\) flip_status_store\(CELL_GCM_FLIP_STATUS_DONE\);\s+"
    )
    assert len(locked_done.findall(publish)) == 1, "locked immediate DONE source seam changed"
    assert "flip_status_store(CELL_GCM_FLIP_STATUS_DONE);" not in prepare, \
        "SetPrepareFlip still has a caller-tail completion store"
    baseline_publish = locked_done.sub("", publish, count=1)
    assert "if (!pending) flip_status_store(CELL_GCM_FLIP_STATUS_DONE);" not in baseline_publish

    candidate_publish = publish.replace(
        publish_signature, "static void candidate_gcm_flip_publish(u32 buffer_id, int pending, u32 source)", 1
    )
    baseline_publish = baseline_publish.replace(
        publish_signature, "static void baseline_gcm_flip_publish(u32 buffer_id, int pending, u32 source)", 1
    )
    candidate_prepare = prepare.replace(
        prepare_signature, "s32 candidate_set_prepare_flip(void* ctx, u32 bufferId)", 1
    ).replace("gcm_flip_publish(bufferId, 0, FRAME_PROF_DIRECT_HLE);",
              "candidate_gcm_flip_publish(bufferId, 0, FRAME_PROF_DIRECT_HLE);", 1)
    baseline_prepare = prepare.replace(
        prepare_signature, "s32 baseline_set_prepare_flip(void* ctx, u32 bufferId)", 1
    ).replace("gcm_flip_publish(bufferId, 0, FRAME_PROF_DIRECT_HLE);",
              "baseline_gcm_flip_publish(bufferId, 0, FRAME_PROF_DIRECT_HLE);\n"
              "    flip_status_store(CELL_GCM_FLIP_STATUS_DONE);", 1)
    assert baseline_prepare.count("flip_status_store(CELL_GCM_FLIP_STATUS_DONE);") == 1

    publication = re.search(
        r"typedef struct GcmFlipPublication\s*\{.*?\}\s*GcmFlipPublication;", source, re.S
    ).group()
    bodies = "\n".join((candidate_publish, baseline_publish, candidate_prepare,
                         baseline_prepare, take, take_public, complete))

    fixture = r'''
#include <stdatomic.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
typedef uint32_t u32; typedef uint64_t u64; typedef int32_t s32;
typedef struct { u64 generation; u32 buffer_id, source; } CellGcmFlipTicket;
enum { CELL_GCM_FLIP_PUBLICATION_QPC_OFF=0, CELL_GCM_FLIP_PUBLICATION_QPC_VALID=1,
       CELL_GCM_FLIP_PUBLICATION_QPC_FAILED=2 };
typedef struct { u64 publication_qpc; u32 publication_qpc_state; } CellGcmFlipPublicationTiming;
enum { CELL_OK=0, CELL_GCM_ERROR_INVALID_VALUE=-1, CELL_GCM_MAX_DISPLAY_BUFFER_NUM=8,
       CELL_GCM_FLIP_STATUS_DONE=0, CELL_GCM_FLIP_STATUS_WAITING=1,
       FRAME_PROF_DIRECT_HLE=0xD1, FRAME_PROF_FIFO_DISPATCH=0xF1 };
INSERT_PUBLICATION
static GcmFlipPublication s_flip_publication = { ATOMIC_FLAG_INIT, 0 };
static atomic_uint s_current_display_buffer_id, s_flip_request_count;
static atomic_uint s_flip_callbacks_pending, s_flip_status;
static int s_display_buffer_set[8], s_tj_survival_flip_diag, s_gcm_pending;
static u64 s_last_flip_time;
static u32 s_flip_handler_opd=0x1234;
static int publication_locked, unlock_hook_armed, unlock_hook_calls, lock_errors;
static int frame_prof_calls, frame_prof_last, timestamp_calls, handler_calls, bad_handler_args;
static int kick_calls, checks, failures;
static u64 synthetic_qpc;
static void (*hook_pending_publish)(u32,int,u32);
static void (*g_ps3_guest_caller)(u32,u64,u64,u64,u64,u64,u64,u64,u64);

static void candidate_gcm_flip_publish(u32,int,u32);
static void baseline_gcm_flip_publish(u32,int,u32);
static void gcm_flip_publication_lock(void) {
    if (publication_locked) ++lock_errors;
    publication_locked=1;
}
static void gcm_flip_publication_unlock(void) {
    if (!publication_locked) ++lock_errors;
    publication_locked=0;
    if (unlock_hook_armed) {
        unlock_hook_armed=0; /* Avoid recursion when the new pending ticket unlocks. */
        ++unlock_hook_calls;
        hook_pending_publish(2,1,0xA5);
    }
}
static CellGcmFlipPublicationTiming gcm_flip_publication_timestamp_locked(int pending) {
    CellGcmFlipPublicationTiming t={0,CELL_GCM_FLIP_PUBLICATION_QPC_OFF};
    if (!publication_locked) ++lock_errors;
    if (pending) { t.publication_qpc=++synthetic_qpc; t.publication_qpc_state=CELL_GCM_FLIP_PUBLICATION_QPC_VALID; }
    return t;
}
static void gcm_flip_kick_presenter(void) { ++kick_calls; }
static u32 flip_status_load(void) { return atomic_load_explicit(&s_flip_status,memory_order_acquire); }
static void flip_status_store(u32 v) { atomic_store_explicit(&s_flip_status,v,memory_order_release); }
static void frame_prof_publish(u32 source) { ++frame_prof_calls; frame_prof_last=(int)source; }
static u64 get_timestamp_ns(void) { return 7000u+(u64)++timestamp_calls; }
static void tj_flip_trace(const char* event,u32 value) { (void)event; (void)value; }
static void tj_flip_perf_tick_done(void) {}
#define GCM_PENDING_SET(bits) (s_gcm_pending |= (bits))
#define CHECK(c,label) do { ++checks; if(!(c)) { ++failures; fprintf(stderr,"FAIL: %s\n",label); } } while(0)

static void guest_call(u32 opd,u64 a0,u64 a1,u64 a2,u64 a3,u64 a4,u64 a5,u64 a6,u64 a7) {
    ++handler_calls;
    if (opd!=s_flip_handler_opd || a0 || a1 || a2 || a3 || a4 || a5 || a6 || a7)
        ++bad_handler_args;
}

BODIES

static void reset_state(void) {
    unlock_hook_armed=0; publication_locked=0; unlock_hook_calls=lock_errors=0;
    hook_pending_publish=NULL; synthetic_qpc=0;
    atomic_flag_clear_explicit(&s_flip_publication.lock,memory_order_release);
    s_flip_publication.next_generation=0;
    s_flip_publication.pending=(CellGcmFlipTicket){0,0,0};
    s_flip_publication.consumed=(CellGcmFlipTicket){0,0,0};
    s_flip_publication.pending_timing=(CellGcmFlipPublicationTiming){0,CELL_GCM_FLIP_PUBLICATION_QPC_OFF};
    s_flip_publication.consumed_timing=(CellGcmFlipPublicationTiming){0,CELL_GCM_FLIP_PUBLICATION_QPC_OFF};
    s_flip_publication.last_publication_qpc=0;
    s_flip_publication.pending_valid=0; s_flip_publication.completed_generation=0;
    atomic_store(&s_current_display_buffer_id,0); atomic_store(&s_flip_request_count,0);
    atomic_store(&s_flip_callbacks_pending,0); flip_status_store(CELL_GCM_FLIP_STATUS_DONE);
    s_last_flip_time=0; s_gcm_pending=0; s_tj_survival_flip_diag=0;
    frame_prof_calls=frame_prof_last=timestamp_calls=handler_calls=bad_handler_args=kick_calls=0;
    s_flip_handler_opd=0x1234;
    for (int i=0;i<8;i++) s_display_buffer_set[i]=1;
}
static int same_ticket(CellGcmFlipTicket a,CellGcmFlipTicket b) {
    return a.generation==b.generation && a.buffer_id==b.buffer_id && a.source==b.source;
}

int main(void) {
    CellGcmFlipTicket old={0}, latest={0};
    g_ps3_guest_caller=guest_call;

    /* Immediate prepare still publishes DONE, consumes its identity, and calls
       the guest handler once with the existing all-zero argument tuple. */
    reset_state(); flip_status_store(CELL_GCM_FLIP_STATUS_WAITING);
    CHECK(candidate_set_prepare_flip(NULL,3)==CELL_OK,"valid SetPrepareFlip rejected");
    CHECK(flip_status_load()==CELL_GCM_FLIP_STATUS_DONE,"immediate prepare did not publish DONE");
    CHECK(s_flip_publication.next_generation==1 && !s_flip_publication.pending_valid &&
          s_flip_publication.consumed.generation==1 && s_flip_publication.consumed.buffer_id==3 &&
          s_flip_publication.consumed.source==FRAME_PROF_DIRECT_HLE,
          "immediate prepare did not publish the consumed ticket identity");
    CHECK(s_flip_publication.consumed_timing.publication_qpc==0 &&
          s_flip_publication.consumed_timing.publication_qpc_state==CELL_GCM_FLIP_PUBLICATION_QPC_OFF,
          "immediate prepare changed its timing sidecar");
    CHECK(atomic_load(&s_current_display_buffer_id)==3 && atomic_load(&s_flip_request_count)==1,
          "immediate prepare changed current buffer/request count incorrectly");
    CHECK(handler_calls==1 && bad_handler_args==0,"immediate flip callback tuple or count changed");
    CHECK(frame_prof_calls==1 && frame_prof_last==FRAME_PROF_DIRECT_HLE &&
          timestamp_calls==1 && s_last_flip_time==7001,
          "immediate prepare profiling/timestamp behavior changed");

    /* Invalid and absent display-buffer calls leave a live pending request intact. */
    reset_state();
    candidate_gcm_flip_publish(6,1,FRAME_PROF_FIFO_DISPATCH);
    s_display_buffer_set[2]=0;
    CellGcmFlipTicket prior=s_flip_publication.pending;
    CellGcmFlipPublicationTiming prior_timing=s_flip_publication.pending_timing;
    CHECK(candidate_set_prepare_flip(NULL,8)==CELL_GCM_ERROR_INVALID_VALUE,
          "out-of-range prepare buffer accepted");
    CHECK(candidate_set_prepare_flip(NULL,2)==CELL_GCM_ERROR_INVALID_VALUE,
          "unregistered display buffer accepted");
    CHECK(flip_status_load()==CELL_GCM_FLIP_STATUS_WAITING &&
          s_flip_publication.next_generation==1 && s_flip_publication.pending_valid &&
          same_ticket(prior,s_flip_publication.pending) &&
          s_flip_publication.pending_timing.publication_qpc==prior_timing.publication_qpc &&
          s_flip_publication.pending_timing.publication_qpc_state==prior_timing.publication_qpc_state &&
          atomic_load(&s_current_display_buffer_id)==6 && atomic_load(&s_flip_request_count)==1,
          "invalid prepare mutated request/status/timing state");
    CHECK(handler_calls==0 && frame_prof_calls==0 && timestamp_calls==0 && s_last_flip_time==0,
          "invalid prepare invoked callbacks or profiling");

    /* Reconstruct only the former unlocked caller-tail DONE store. The hook
       publishes a newer pending request immediately after helper unlock. */
    reset_state(); hook_pending_publish=baseline_gcm_flip_publish; unlock_hook_armed=1;
    CHECK(baseline_set_prepare_flip(NULL,1)==CELL_OK,"baseline valid prepare rejected");
    CHECK(unlock_hook_calls==1 && s_flip_publication.pending_valid &&
          s_flip_publication.pending.generation==2 && s_flip_publication.pending.buffer_id==2 &&
          s_flip_publication.pending.source==0xA5 && atomic_load(&s_flip_request_count)==2,
          "baseline interleaving did not publish the newer ticket");
    CHECK(flip_status_load()==CELL_GCM_FLIP_STATUS_DONE,
          "reconstructed caller-tail store no longer reproduces WAITING clobber");

    /* Exact current bodies preserve the newer request because immediate DONE
       is serialized before unlock; the new request then publishes WAITING. */
    reset_state(); hook_pending_publish=candidate_gcm_flip_publish; unlock_hook_armed=1;
    CHECK(candidate_set_prepare_flip(NULL,1)==CELL_OK,"candidate valid prepare rejected");
    CHECK(unlock_hook_calls==1 && lock_errors==0,"unlock interleaving hook was not deterministic");
    CHECK(flip_status_load()==CELL_GCM_FLIP_STATUS_WAITING &&
          s_flip_publication.next_generation==2 && s_flip_publication.pending_valid &&
          s_flip_publication.pending.generation==2 && s_flip_publication.pending.buffer_id==2 &&
          s_flip_publication.pending.source==0xA5 &&
          atomic_load(&s_current_display_buffer_id)==2 && atomic_load(&s_flip_request_count)==2,
          "newer request did not retain WAITING and its published identity");
    CHECK(s_flip_publication.consumed.generation==1 && s_flip_publication.consumed.buffer_id==1 &&
          s_flip_publication.consumed.source==FRAME_PROF_DIRECT_HLE &&
          s_flip_publication.consumed_timing.publication_qpc_state==CELL_GCM_FLIP_PUBLICATION_QPC_OFF,
          "immediate consumed identity/timing changed under newer request");
    CHECK(s_flip_publication.pending_timing.publication_qpc==1 &&
          s_flip_publication.pending_timing.publication_qpc_state==CELL_GCM_FLIP_PUBLICATION_QPC_VALID,
          "newer request timing was lost during immediate publication");

    /* The immediate consumed ticket becomes stale once the newer pending
       ticket is consumed. Only completion of that exact latest ticket clears WAITING. */
    old=s_flip_publication.consumed;
    CHECK(cellGcm_take_flip_ticket(&latest) && latest.generation==2 && latest.buffer_id==2 &&
          latest.source==0xA5,"latest pending ticket was not consumed intact");
    CHECK(s_flip_publication.consumed_timing.publication_qpc==1 &&
          s_flip_publication.consumed_timing.publication_qpc_state==CELL_GCM_FLIP_PUBLICATION_QPC_VALID,
          "latest ticket timing sidecar was not retained on consume");
    CHECK(!cellGcm_complete_flip_ticket(&old) && flip_status_load()==CELL_GCM_FLIP_STATUS_WAITING,
          "stale immediate ticket cleared the latest pending status");
    CHECK(cellGcm_complete_flip_ticket(&latest) && flip_status_load()==CELL_GCM_FLIP_STATUS_DONE,
          "exact latest completion did not clear WAITING");
    CHECK(handler_calls==1 && atomic_load(&s_flip_callbacks_pending)==1 && s_gcm_pending==2,
          "presenter completion ran or lost the deferred guest callback");

    CHECK(lock_errors==0,"publication lock/unlock contract was violated");
    printf("prepare flip publication: %d/%d checks passed\n",checks-failures,checks);
    return failures ? 1 : 0;
}
'''.replace("INSERT_PUBLICATION", publication).replace("BODIES", bodies)

    compiler = shutil.which("clang") or "C:/Program Files/LLVM/bin/clang.exe"
    with tempfile.TemporaryDirectory(prefix="tj-prepare-flip-publication-") as temp:
        c_path = Path(temp) / "test.c"
        exe_path = Path(temp) / "test.exe"
        c_path.write_text(fixture, encoding="utf-8")
        subprocess.run([compiler, "-std=c11", "-O2", "-D_CRT_SECURE_NO_WARNINGS",
                        str(c_path), "-o", str(exe_path)], check=True)
        subprocess.run([str(exe_path)], check=True)


if __name__ == "__main__":
    main()
