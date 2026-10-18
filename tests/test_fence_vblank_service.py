"""Compile the actual presenter callback used inside checked owner waits."""
from pathlib import Path
import shutil
import subprocess
import tempfile

from test_fifo_present_boundary import function

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src/tj_present.cpp"


def main():
    source = SOURCE.read_text(encoding="utf-8")
    thread = function(source, "static DWORD WINAPI tj_present_thread(")
    complete = function(source, "static int tj_present_complete_ticket(")
    service = function(source, "static uint32_t tj_present_fence_wait_service(")
    clock = function(source, "static uint64_t tj_vblank_clock_now(")
    context_start = source.index("struct TjFenceVBlankServiceContext {")
    context_end = source.index("};", context_start) + 2
    context = source[context_start:context_end]

    assert 'getenv("TJ_FENCE_VBLANK_SERVICE")' in thread
    assert 'strcmp(fence_vblank_request, "1") == 0' in thread
    assert 'getenv("TJ_FENCE_VBLANK_FIFO_PULSE")' in thread
    assert 'fence_vblank_fifo_pulse = fence_vblank_service_enabled &&' in thread
    assert 'strcmp(fifo_pulse_request, "1") == 0' in thread
    assert thread.count("tj_flip_timeline_drain();") == 3
    assert "awaiting_vblank.generation && cellGcm_flip_uses_vsync()" in thread
    outer = thread[thread.index("if (tj_present_outer_drain_allowed("):]
    assert outer.index("rsx_d3d12_backend_set_fence_wait_service(") < \
        outer.index("rsx_d3d12_backend_set_fence_wait_fifo_pulse(fence_vblank_fifo_pulse);") < \
        outer.index("tj_flip_timeline_drain();") < \
        outer.index("rsx_d3d12_backend_set_fence_wait_service(nullptr, nullptr);")
    assert thread.rindex("rsx_d3d12_backend_set_fence_wait_service(nullptr, nullptr);") < \
        thread.index("rsx_d3d12_backend_shutdown();")
    assert "tj_present_flip(" not in service
    assert "cellGcm_take_flip_ticket" not in service
    assert "cellGcm_rsx_process_fifo" not in service
    assert "rsx_d3d12_backend_present_buffer" not in service
    assert "ppu_gcm_pump" not in service and "rsx_d3d12_backend_pump_messages" not in service
    assert "tj_vblank_cadence_advance(context->cadence)" in service

    fixture = r'''#include <stdint.h>
#include <stdio.h>
#include "tj_vblank_cadence.h"
typedef struct { long long QuadPart; } LARGE_INTEGER;
struct tj_gcm_flip_ticket { uint64_t generation; uint32_t buffer_id, source; };
static volatile long g_tj_present_stop=0;
static uint64_t fake_qpc=0, fake_ms=0;
static int qpc_ok=1, qpc_calls=0, ms_calls=0, fake_vsync=1, fake_rsx_ok=1;
static int complete_result=1, tick_calls=0, completion_calls=0;
static int timeline_tick_calls=0, timeline_complete_calls=0, timeline_reject_calls=0;
static uint64_t timeline_tick_due=0, completed_generation=0;
static void tj_flip_timeline_tick(int qpc_active, uint64_t due) {
    ++timeline_tick_calls; timeline_tick_due=qpc_active ? due : 0;
}
static void tj_flip_timeline_complete(const tj_gcm_flip_ticket& ticket) {
    ++timeline_complete_calls; completed_generation=ticket.generation;
}
static void tj_flip_timeline_reject_completion(const tj_gcm_flip_ticket& ticket) {
    (void)ticket; ++timeline_reject_calls;
}
static int QueryPerformanceCounter(LARGE_INTEGER* now) {
    ++qpc_calls; if (!qpc_ok) return 0;
    now->QuadPart=(long long)fake_qpc; return 1;
}
static uint64_t GetTickCount64(void) { ++ms_calls; return fake_ms; }
static long InterlockedCompareExchange(volatile long* target,long exchange,long comparand) {
    long prior=*target; if (prior==comparand) *target=exchange; return prior;
}
static int cellGcm_flip_uses_vsync(void) { return fake_vsync; }
static void cellGcmTickVBlank(void) { ++tick_calls; }
static int cellGcm_complete_flip_ticket(const tj_gcm_flip_ticket* ticket) {
    ++completion_calls; completed_generation=ticket->generation; return complete_result;
}
CLOCK
COMPLETE
CONTEXT
SERVICE
#define CHECK(c,m) do { if (!(c)) { fprintf(stderr,"FAIL: %s\n",m); return 1; } } while(0)
static TjFenceVBlankServiceContext context_for(TjVBlankCadence* cadence,
                                               bool* qpc_active,
                                               uint64_t* frequency,
                                               tj_gcm_flip_ticket* awaiting,
                                               int* rsx_ok) {
    TjFenceVBlankServiceContext c={cadence,qpc_active,frequency,awaiting,rsx_ok,0};
    return c;
}
static void reset_counts(void) {
    qpc_calls=ms_calls=tick_calls=completion_calls=0;
    timeline_tick_calls=timeline_complete_calls=timeline_reject_calls=0;
    completed_generation=timeline_tick_due=0;
}
int main(void) {
    TjVBlankCadence cadence; bool qpc_active=true; uint64_t frequency=6000;
    tj_gcm_flip_ticket awaiting={77,2,1};
    tj_vblank_cadence_init(&cadence,1000,frequency);
    tj_vblank_cadence_advance(&cadence);
    fake_qpc=1050; reset_counts();
    TjFenceVBlankServiceContext c=context_for(&cadence,&qpc_active,&frequency,&awaiting,&fake_rsx_ok);
    CHECK(tj_present_fence_wait_service(&c,20)==9,
          "pre-deadline wait was not rounded up to the next cadence deadline");
    CHECK(!tick_calls && !completion_calls && awaiting.generation==77 &&
          cadence.deadline==1100,"pre-deadline callback consumed a guest ticket");
    CHECK(tj_present_fence_wait_service(&c,4)==4,
          "requested wait slice exceeded the backend's remaining deadline");

    fake_qpc=1100; reset_counts();
    CHECK(tj_present_fence_wait_service(&c,40)==40,
          "due service did not preserve the backend remaining wait slice");
    CHECK(tick_calls==1 && timeline_tick_calls==1 && timeline_tick_due==1100 &&
          completion_calls==1 && completed_generation==77 &&
          timeline_complete_calls==1 && !timeline_reject_calls &&
          !awaiting.generation && c.ticket_serviced && cadence.deadline==1200,
          "due VBlank did not tick and complete the exact ticket once");
    int qpc_after_service=qpc_calls;
    CHECK(tj_present_fence_wait_service(&c,39)==39 && qpc_calls==qpc_after_service &&
          tick_calls==1 && completion_calls==1,
          "repeat callback serviced an extra tick or re-read the clock");

    /* Mode changes retain immediate completion without fabricating a tick. */
    tj_vblank_cadence_init(&cadence,2000,frequency);
    tj_vblank_cadence_advance(&cadence);
    awaiting={78,3,0}; fake_vsync=0; complete_result=1;
    c=context_for(&cadence,&qpc_active,&frequency,&awaiting,&fake_rsx_ok);
    reset_counts();
    CHECK(tj_present_fence_wait_service(&c,25)==25 && !awaiting.generation &&
          completion_calls==1 && completed_generation==78 && !tick_calls &&
          !timeline_tick_calls && timeline_complete_calls==1 &&
          cadence.deadline==2100,
          "VSync mode switch failed immediate exact-ticket completion");

    /* Rejected exact completion clears and rejects; it never fabricates a row. */
    awaiting={79,3,0}; fake_vsync=0; complete_result=0;
    c=context_for(&cadence,&qpc_active,&frequency,&awaiting,&fake_rsx_ok);
    reset_counts();
    tj_present_fence_wait_service(&c,25);
    CHECK(!awaiting.generation && timeline_reject_calls==1 &&
          !timeline_complete_calls && !tick_calls,
          "rejected exact completion retained the ticket or became a completed row");

    /* Stop, backend loss, or no pending ticket makes the callback inert. */
    fake_vsync=1; complete_result=1; awaiting={80,1,1};
    c=context_for(&cadence,&qpc_active,&frequency,&awaiting,&fake_rsx_ok);
    reset_counts(); g_tj_present_stop=1;
    CHECK(tj_present_fence_wait_service(&c,30)==30 && !qpc_calls && !tick_calls &&
          !completion_calls && awaiting.generation==80,
          "stop did not disable in-wait service");
    g_tj_present_stop=0; fake_rsx_ok=0;
    CHECK(tj_present_fence_wait_service(&c,30)==30 && !qpc_calls && !tick_calls,
          "failed RSX backend did not disable in-wait service");
    fake_rsx_ok=1; awaiting.generation=0;
    CHECK(tj_present_fence_wait_service(&c,30)==30 && !qpc_calls && !tick_calls,
          "empty ticket did not disable in-wait service");

    /* Runtime QPC loss rebases to one fallback period before the first tick. */
    awaiting={81,1,1}; qpc_active=true; frequency=6000;
    tj_vblank_cadence_init(&cadence,3000,frequency);
    tj_vblank_cadence_advance(&cadence);
    c=context_for(&cadence,&qpc_active,&frequency,&awaiting,&fake_rsx_ok);
    qpc_ok=0; fake_ms=5000; fake_vsync=1; reset_counts();
    CHECK(tj_present_fence_wait_service(&c,50)==16 && !tick_calls &&
          qpc_active==false && frequency==1000 && cadence.deadline==5016,
          "QPC fallback invented a tick instead of rearming one period");
    fake_ms=5016;
    CHECK(tj_present_fence_wait_service(&c,50)==50 && tick_calls==1 &&
          !timeline_tick_due && completion_calls==1 && completed_generation==81 &&
          !awaiting.generation && cadence.deadline==5033,
          "fallback cadence did not service its exact due ticket once");

    puts("PASS: bounded wait slices, exact one-ticket VBlank service, mode switch, errors, stop, and QPC fallback");
    return 0;
}
'''.replace("CLOCK", clock).replace("COMPLETE", complete).replace(
        "CONTEXT", context).replace("SERVICE", service)

    compiler = shutil.which("clang++") or "C:/Program Files/LLVM/bin/clang++.exe"
    with tempfile.TemporaryDirectory(prefix="tj-fence-vblank-service-") as temp:
        source_path = Path(temp) / "service.cpp"
        exe = Path(temp) / "service.exe"
        source_path.write_text(fixture, encoding="utf-8")
        subprocess.run([compiler, "-std=c++17", "-O2", "-D_CRT_SECURE_NO_WARNINGS",
                        "-I", str(ROOT / "src"), str(source_path), "-o", str(exe)],
                       check=True)
        subprocess.run([str(exe)], check=True)


if __name__ == "__main__":
    main()
