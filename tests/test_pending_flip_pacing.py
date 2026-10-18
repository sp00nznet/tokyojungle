"""Exercise pending-VSync outer-drain pacing with the production flip/gate helpers.

The simulated label drain checks ordering only; this is not a GPU or game test.
"""
from pathlib import Path
import shutil
import subprocess
import tempfile

from test_fifo_present_boundary import function

ROOT = Path(__file__).resolve().parents[1]


def main():
    source = (ROOT / "src/tj_present.cpp").read_text(encoding="utf-8")
    thread = function(source, "static DWORD WINAPI tj_present_thread(")
    complete = function(source, "static int tj_present_complete_ticket(")
    flip = function(source, "static int tj_present_flip(int vblank,")
    gate = function(source, "static int tj_present_outer_drain_allowed(")

    compact_thread = " ".join(thread.split())
    assert 'getenv("TJ_PENDING_FLIP_PACING")' in compact_thread
    assert 'pacing_request && strcmp(pacing_request, "1") == 0' in compact_thread
    assert "Sleep(1);" in thread
    assert "InterlockedCompareExchange(&g_tj_present_stop, 0, 0)" in thread
    assert thread.index("InterlockedCompareExchange(&g_tj_present_stop, 0, 0)") < thread.index("Sleep(1);")
    assert "tj_fifo_kick_wait_once(" in thread
    assert thread.index("InterlockedCompareExchange(&g_tj_present_stop, 0, 0)") < thread.index("tj_fifo_kick_wait_once(")
    assert thread.count("tj_flip_timeline_drain();") == 3
    tick_drain = thread.index("tj_flip_timeline_drain();")
    advance = thread.index("tj_vblank_cadence_advance(&vblank_cadence);")
    outer_flip = thread.index("tj_present_flip(0, &awaiting_vblank)")
    outer_guard = thread.index("tj_present_outer_drain_allowed(pending_flip_pacing, &awaiting_vblank)")
    outer_drain = thread.index("tj_flip_timeline_drain();", tick_drain + 1)
    message_pump = thread.index("rsx_d3d12_backend_pump_messages()", outer_drain)
    assert tick_drain < advance < outer_flip < outer_guard < outer_drain < message_pump, \
        "tick drain/advance and outer flip/gated drain/message-pump order changed"
    assert "return !pacing_enabled || !awaiting_vblank->generation ||" in gate
    assert "!cellGcm_flip_uses_vsync();" in gate

    fixture = r'''#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include "tj_vblank_cadence.h"
struct tj_gcm_flip_ticket { uint64_t generation; uint32_t buffer_id,source; };
struct tj_gcm_flip_publication_timing { uint64_t publication_qpc; uint32_t publication_qpc_state; };
static tj_gcm_flip_ticket pending={},last_presented={};
static int has_pending=0,vsync=1;
static struct { int enabled; } s_tj_flip_timeline={};
static unsigned takes=0,presents=0,completions=0,failures=0;
static uint64_t fake_now=0,completion_time=0;
static unsigned drains_before_completion=0,drains_after_completion=0;
static unsigned simulated_tick_label_releases=0,event_count=0;
static char events[16];
static void event(char e) { if(event_count<sizeof(events)) events[event_count++]=e; }
static int cellGcm_flip_uses_vsync(void) { return vsync; }
static void tj_flip_timeline_take(const tj_gcm_flip_ticket&,const tj_gcm_flip_publication_timing* =nullptr) {}
static void tj_flip_timeline_present_return(void) {}
static void tj_flip_timeline_complete(const tj_gcm_flip_ticket&) {}
static void tj_flip_timeline_reject_completion(const tj_gcm_flip_ticket&) {}
static int cellGcm_take_flip_ticket(tj_gcm_flip_ticket* out) {
    if(!has_pending) return 0;
    *out=pending; has_pending=0; ++takes; return 1;
}
static int cellGcm_take_flip_ticket_timed(tj_gcm_flip_ticket* out,tj_gcm_flip_publication_timing* timing) {
    if(timing) *timing={}; return cellGcm_take_flip_ticket(out);
}
static void rsx_d3d12_backend_present_buffer(uint64_t gen,uint32_t buffer) {
    ++presents; last_presented={gen,buffer,1};
}
static int cellGcm_complete_flip_ticket(const tj_gcm_flip_ticket* t) {
    if(t->generation!=last_presented.generation || t->buffer_id!=last_presented.buffer_id)
        ++failures;
    ++completions; completion_time=fake_now; event('C'); return 1;
}
static void request(uint64_t gen) {
    pending={gen,(uint32_t)(gen%2),1}; has_pending=1;
}
/* Simulates only the blocking duration/call order of an outer FIFO drain. */
static void simulated_outer_drain(void) {
    if(completions) ++drains_after_completion; else ++drains_before_completion;
    event('O'); fake_now+=1200; /* 20 ms at 60,000 QPC ticks/second */
}
/* Explicit label-release stub: verifies production tick ordering, not GPU progress. */
static void simulated_tick_label_drain(void) {
    if(!completions) ++failures;
    ++simulated_tick_label_releases; event('T');
}
COMPLETE_TICKET
FLIP
GATE
#define CHECK(c,m) do { if(!(c)) { fprintf(stderr,"FAIL: %s\n",m); return 1; } } while(0)
static void reset(void) {
    pending={}; last_presented={}; has_pending=0; vsync=1;
    takes=presents=completions=failures=0; fake_now=completion_time=0;
    drains_before_completion=drains_after_completion=0;
    simulated_tick_label_releases=event_count=0;
}
static int timing_case(int pacing, uint64_t* completed_at, char* trace) {
    reset();
    TjVBlankCadence cadence;
    tj_vblank_cadence_init(&cadence,0,60000); /* exactly 1,000 QPC ticks per 60 Hz step */
    fake_now=0;
    if(!tj_vblank_cadence_due(&cadence,fake_now)) return 10;
    tj_vblank_cadence_advance(&cadence); /* initial VBlank; next due at 16.667 ms */

    fake_now=60; /* off-tick present at 1 ms */
    request(1);
    tj_gcm_flip_ticket awaiting={};
    if(tj_present_flip(0,&awaiting)!=1 || awaiting.generation!=1 ||
       presents!=1 || completions!=0) return 11;
    if(tj_present_outer_drain_allowed(pacing,&awaiting))
        simulated_outer_drain();

    /* Poll to the next deadline using the actual rational cadence header. */
    if(fake_now<cadence.deadline) fake_now=cadence.deadline;
    if(!tj_vblank_cadence_due(&cadence,fake_now)) return 12;
    if(tj_present_flip(1,&awaiting)!=0 || awaiting.generation!=0 || completions!=1)
        return 13;
    *completed_at=completion_time;
    simulated_tick_label_drain(); /* production tick drain follows exact completion */
    tj_vblank_cadence_advance(&cadence);

    /* The post-tick outer drain is allowed once the pending ticket is complete. */
    tj_present_flip(0,&awaiting);
    if(!tj_present_outer_drain_allowed(pacing,&awaiting)) return 14;
    simulated_outer_drain();
    for(unsigned i=0;i<event_count;i++) trace[i]=events[i];
    trace[event_count]='\0';
    return failures ? 15 : 0;
}
int main(void) {
    tj_gcm_flip_ticket awaiting={1,0,1};
    vsync=1;
    CHECK(tj_present_outer_drain_allowed(0,&awaiting),"default-off mode blocked a pending VSync drain");
    CHECK(!tj_present_outer_drain_allowed(1,&awaiting),"enabled pacing did not defer pending VSync drain");
    awaiting.generation=0;
    CHECK(tj_present_outer_drain_allowed(1,&awaiting),"no-ticket state blocked the outer drain");
    awaiting.generation=1; vsync=0;
    CHECK(tj_present_outer_drain_allowed(1,&awaiting),"non-VSync mode was unnecessarily paced");

    uint64_t off_complete=0,on_complete=0; char off_trace[16]={},on_trace[16]={};
    CHECK(timing_case(0,&off_complete,off_trace)==0,"OFF timing simulation failed");
    CHECK(timing_case(1,&on_complete,on_trace)==0,"ON timing simulation failed");
    CHECK(off_complete==1260 && on_complete==1000 && on_complete<off_complete,
          "pending ticket was not completed before the expensive outer drain");
    CHECK(!strcmp(off_trace,"OCTO") && !strcmp(on_trace,"CTO"),
          "simulated outer/tick label drain order changed");

    reset(); request(10);
    awaiting={};
    CHECK(tj_present_flip(0,&awaiting)==1 && takes==1 && awaiting.generation==10,
          "first off-tick request was not held for VSync");
    request(11);
    CHECK(tj_present_flip(0,&awaiting)==0 && takes==1 && has_pending,
          "a second ticket was consumed before the awaited VBlank");
    CHECK(!tj_present_outer_drain_allowed(1,&awaiting),
          "pending ticket did not keep the paced outer drain deferred");
    CHECK(tj_present_flip(1,&awaiting)==0 && completions==1 && takes==1 && has_pending,
          "VBlank completion consumed a second ticket on the same tick");
    CHECK(tj_present_flip(0,&awaiting)==1 && takes==2 && awaiting.generation==11,
          "queued ticket did not resume after the prior completion");

    reset(); request(20); awaiting={};
    CHECK(tj_present_flip(0,&awaiting)==1 && awaiting.generation==20,
          "VSync-switch case did not present a pending ticket");
    vsync=0;
    CHECK(tj_present_outer_drain_allowed(1,&awaiting),
          "VSync mode switch failed to re-enable outer drain");
    tj_present_flip(0,&awaiting);
    CHECK(completions==1 && awaiting.generation==0 &&
          tj_present_outer_drain_allowed(1,&awaiting),
          "mode switch stranded the pending ticket or drain");
    puts("PASS: pending-VSync drain pacing, rational deadline timing, ticket order, and simulated label-release order");
    return 0;
}
'''.replace("COMPLETE_TICKET", complete).replace("FLIP", flip).replace("GATE", gate)

    compiler = shutil.which("clang++") or r"C:\Program Files\LLVM\bin\clang++.exe"
    with tempfile.TemporaryDirectory(prefix="tj-pending-flip-pacing-") as temp:
        test_source = Path(temp) / "pending_pacing.cpp"
        executable = Path(temp) / "pending_pacing.exe"
        test_source.write_text(fixture, encoding="utf-8")
        subprocess.run([
            compiler, "-std=c++17", "-O2", "-I", str((ROOT / "src").resolve()),
            str(test_source), "-o", str(executable),
        ], check=True)
        subprocess.run([str(executable)], check=True)


if __name__ == "__main__":
    main()
