"""Exercise the real due-aware presenter wait and clock fallback, without a game."""
from pathlib import Path
import shutil
import subprocess
import tempfile

from test_fifo_present_boundary import function

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src/tj_present.cpp"
CADENCE = ROOT / "src/tj_vblank_cadence.h"
BASELINE = (ROOT.parents[2] / "work/backups/20261006-present-due-before-wait-9d36"
            / "tj_present.cpp")
LOOP_BASELINE = (ROOT.parents[2] / "work/backups/20261006-vblank-refresh-1720"
                 / "tj_present.cpp")


def control_block(source, marker):
    start = source.index(marker)
    brace = source.index("{", start)
    depth = 0
    for index in range(brace, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[start:index + 1]
    raise AssertionError(f"unterminated control block: {marker}")


def cap_clause(thread):
    start = thread.index("if (fired >= 240")
    end = thread.index(";", start) + 1
    return thread[start:end].strip()


def main():
    present = SOURCE.read_text(encoding="utf-8")
    baseline = BASELINE.read_text(encoding="utf-8")
    loop_baseline = LOOP_BASELINE.read_text(encoding="utf-8")
    current_thread = function(present, "static DWORD WINAPI tj_present_thread(")
    baseline_thread = function(baseline, "static DWORD WINAPI tj_present_thread(")
    loop_baseline_thread = function(loop_baseline, "static DWORD WINAPI tj_present_thread(")
    wait_helper = function(present, "static void tj_fifo_kick_wait_once(")
    clock_helper = function(present, "static uint64_t tj_vblank_clock_now(")
    export_helper = function(present, "static void tj_flip_timeline_export_poll(")
    export_helper = export_helper.replace(
        "tj_flip_timeline_export_poll", "tj_flip_timeline_export_poll_impl", 1)
    baseline_loop = control_block(
        loop_baseline_thread, "while (tj_vblank_cadence_due(&vblank_cadence, now)")
    current_loop = control_block(
        current_thread, "while (tj_vblank_cadence_due(&vblank_cadence, now)")
    baseline_cap = cap_clause(loop_baseline_thread)
    current_cap = cap_clause(current_thread)

    # Tie the executable fixture to the production call order. The old source
    # always slept/waited before sampling its VBlank clock.
    normalized = " ".join(current_thread.split())
    assert normalized.index("uint64_t before_wait = tj_vblank_clock_now(") < \
        normalized.index("const DWORD wait_timeout_ms = tj_vblank_cadence_due(") < \
        normalized.index("tj_fifo_kick_wait_once(") < \
        normalized.index("uint64_t now = tj_vblank_clock_now(") < \
        normalized.index("while (tj_vblank_cadence_due(&vblank_cadence, now)")
    assert "tj_vblank_cadence_due( &vblank_cadence, before_wait) ? 0 : 1" in normalized
    assert "wait_timeout_ms);" in normalized
    assert "if (wait_timeout_ms) Sleep(1);" in normalized
    assert "tj_vblank_cadence_advance(&vblank_cadence);" in current_thread
    assert "tj_vblank_cadence_advance" not in clock_helper
    assert "tj_vblank_cadence_rearm_after_clock_switch(cadence, now, *frequency);" in clock_helper
    baseline_loop_normalized = " ".join(baseline_loop.split())
    current_loop_normalized = " ".join(current_loop.split())
    assert "now = tj_vblank_clock_now(" not in baseline_loop_normalized
    assert current_loop_normalized.index("tj_vblank_cadence_advance(&vblank_cadence);") < \
        current_loop_normalized.index("fired++;") < \
        current_loop_normalized.index("rsx_d3d12_backend_pump_messages()") < \
        current_loop_normalized.index("now = tj_vblank_clock_now(")
    assert "fired < 240" in current_loop_normalized
    assert "fired < 240" in baseline_loop_normalized
    assert " ".join(baseline_cap.split()) == \
        "if (fired >= 240) tj_vblank_cadence_resync(&vblank_cadence, now);"
    assert " ".join(current_cap.split()) == \
        "if (fired >= 240 && tj_vblank_cadence_due(&vblank_cadence, now)) " \
        "tj_vblank_cadence_resync(&vblank_cadence, now);"
    assert current_loop_normalized.index("rsx_d3d12_backend_pump_messages()") < \
        current_loop_normalized.index("tj_flip_timeline_export_poll();") < \
        current_loop_normalized.index("now = tj_vblank_clock_now(")
    assert "if (!s_tj_flip_timeline.enabled || s_tj_flip_timeline.dumped ||" in \
        " ".join(export_helper.split())
    assert "tj_flip_timeline_dump();" in export_helper and \
        "if (s_tj_flip_dump_complete) SetEvent(s_tj_flip_dump_complete);" in export_helper
    assert "tj_flip_timeline_export_poll();" not in baseline_loop_normalized
    assert "tj_flip_timeline_export_poll();" in current_thread
    outer_export = current_thread.index("tj_flip_timeline_export_poll();",
                                       current_thread.index("tj_dump_threads();"))
    assert outer_export > current_thread.index("while (tj_vblank_cadence_due(")
    baseline_normalized = " ".join(baseline_thread.split())
    baseline_sleep = baseline_normalized.index("Sleep(1);")
    baseline_clock = baseline_normalized.index("if (vblank_qpc_active) {", baseline_sleep)
    assert baseline_sleep < baseline_clock, "baseline no longer waits before its VBlank clock sample"

    loop_support = r'''struct tj_gcm_flip_ticket { uint64_t generation; uint32_t buffer_id, source; };
struct tj_fifo_perf_loop {
    uint64_t ticks, tick_present_ns, tick_present_max_ns, drain1_ns, drain1_max_ns;
};
static uint64_t loop_drain_step;
static unsigned loop_drain_budget, loop_drain_calls, loop_tick_calls, loop_pump_calls;
static unsigned loop_fail_qpc_at_call, loop_drain_stop_at_call, loop_export_calls;
static int loop_drain_every;
static uint64_t loop_fallback_tick;
static uint64_t tj_fifo_perf_now_ns(tj_fifo_perf_loop&) { return 0; }
static uint64_t tj_fifo_perf_elapsed_ns(tj_fifo_perf_loop&, uint64_t) { return 0; }
static void tj_fifo_perf_add_segment(uint64_t&, uint64_t&, uint64_t) {}
static void tj_fifo_perf_mark_present(tj_fifo_perf_loop&) {}
static void tj_flip_timeline_tick(bool, uint64_t) { order_push('I'); }
static void cellGcmTickVBlank(void) { ++loop_tick_calls; order_push('T'); }
static int tj_present_flip(int, tj_gcm_flip_ticket*) { order_push('P'); return 0; }
static void tj_flip_timeline_drain(void) {
    order_push('D');
    if (loop_fail_qpc_at_call && loop_drain_calls + 1 == loop_fail_qpc_at_call) {
        qpc_succeeds = 0; qpc_domain = 0; tick_now = loop_fallback_tick;
    }
    int should_advance = loop_drain_every || loop_drain_calls < loop_drain_budget;
    if (loop_drain_stop_at_call && loop_drain_calls + 1 == loop_drain_stop_at_call)
        should_advance = 0;
    if (should_advance)
        qpc_now += loop_drain_step;
    ++loop_drain_calls;
}
static int rsx_d3d12_backend_pump_messages(void) {
    ++loop_pump_calls; order_push('M'); return 0;
}
static void tj_flip_timeline_export_poll(void) {
    ++loop_export_calls; order_push('E'); tj_flip_timeline_export_poll_impl();
}
struct LoopResult {
    unsigned fired, ticks, drains, pumps, exports, outer_polls, pending;
    unsigned export_waits, export_dumps, export_acks, dumped_when_set, bad_set_event;
    unsigned qpc_reads, tick_reads;
    uint64_t now, deadline, frequency;
    int qpc_active;
    TjVBlankCadence cadence;
    char order[64];
};
static uint64_t physical_now(void) { return qpc_succeeds ? qpc_now : tick_now; }
static unsigned count_due(TjVBlankCadence cadence, uint64_t now) {
    unsigned count = 0;
    while (tj_vblank_cadence_due(&cadence, now) && count < 241) {
        ++count; tj_vblank_cadence_advance(&cadence);
    }
    return count;
}
'''
    runner_template = r'''static LoopResult @@NAME@@(uint64_t start, uint64_t drain_step,
                                unsigned drain_budget, int drain_every,
                                unsigned fail_qpc_on_drain,
                                uint64_t fallback_tick, unsigned drain_stop_at_call,
                                int request_export=0) {
    reset_io(); qpc_frequency=60000; qpc_now=start; tick_now=0;
    qpc_succeeds=1; qpc_domain=1;
    s_tj_flip_timeline.enabled=request_export;
    s_tj_flip_timeline.dumped=0;
    s_tj_flip_dump_request=expected_event;
    s_tj_flip_dump_complete=expected_complete_event;
    wait_result=request_export ? WAIT_OBJECT_0 : WAIT_TIMEOUT;
    loop_drain_step=drain_step; loop_drain_budget=drain_budget;
    loop_drain_every=drain_every; loop_fail_qpc_at_call=fail_qpc_on_drain;
    loop_drain_stop_at_call=drain_stop_at_call;
    loop_fallback_tick=fallback_tick;
    loop_drain_calls=loop_tick_calls=loop_pump_calls=loop_export_calls=0;
    TjVBlankCadence vblank_cadence;
    tj_vblank_cadence_init(&vblank_cadence, start, qpc_frequency);
    bool vblank_qpc_active=true; uint64_t vblank_clock_frequency=qpc_frequency;
    uint64_t now=tj_vblank_clock_now(&vblank_cadence, &vblank_qpc_active,
                                    &vblank_clock_frequency);
    order_length=0; order[0]=0;
    int fired=0, rsx_ok=1, heartbeat_enabled=0;
    uint64_t heartbeat_consumed=0, segment_begin=0;
    bool perf_enabled=false;
    tj_fifo_perf_loop perf={};
    tj_gcm_flip_ticket awaiting_vblank={};
    @@LOOP@@
    @@CAP@@
    unsigned inner_exports=loop_export_calls;
    tj_flip_timeline_export_poll(); /* outer-thread poll after the in-burst poll */
    LoopResult result={};
    result.fired=(unsigned)fired; result.ticks=loop_tick_calls;
    result.drains=loop_drain_calls; result.pumps=loop_pump_calls;
    result.exports=inner_exports; result.outer_polls=loop_export_calls-inner_exports;
    result.export_waits=wait_calls; result.export_dumps=export_dump_calls;
    result.export_acks=export_set_calls; result.dumped_when_set=dumped_when_set;
    result.bad_set_event=bad_set_event;
    result.now=now; result.deadline=vblank_cadence.deadline;
    result.frequency=vblank_clock_frequency; result.qpc_active=vblank_qpc_active;
    result.cadence=vblank_cadence; result.pending=count_due(vblank_cadence, physical_now());
    result.qpc_reads=qpc_calls; result.tick_reads=tick_calls;
    memcpy(result.order, order, sizeof(result.order));
    return result;
}
'''
    loop_runners = "\n".join(
        runner_template.replace("@@NAME@@", name)
        .replace("@@LOOP@@", loop)
        .replace("@@CAP@@", cap)
        for name, loop, cap in (
            ("run_stale_loop", baseline_loop, baseline_cap),
            ("run_fresh_loop", current_loop, current_cap),
        )
    )
    loop_tests = r'''static void test_inner_loop_refresh_and_cap(void) {
    TjVBlankCadence boundary;
    tj_vblank_cadence_init(&boundary, 100, 60000);
    CHECK(!tj_vblank_cadence_due(&boundary, 99) &&
          tj_vblank_cadence_due(&boundary, 100),
          "due boundary was not strict-before/inclusive-at deadline");

    for (unsigned crossed=0; crossed<=2; ++crossed) {
        uint64_t step=crossed ? crossed*1000+1 : 0;
        unsigned budget=crossed ? 1 : 0;
        LoopResult stale=run_stale_loop(600000,step,budget,0,0,0,0);
        LoopResult fresh=run_fresh_loop(600000,step,budget,0,0,0,0);
        CHECK(stale.fired==1 && fresh.fired==crossed+1,
              "inner loop did not service the crossed deadlines on the refreshed clock");
        CHECK(stale.ticks==stale.fired && fresh.ticks==fresh.fired &&
              stale.drains==stale.fired && fresh.drains==fresh.fired &&
              stale.pumps==stale.fired && fresh.pumps==fresh.fired &&
              stale.exports==0 && fresh.exports==fresh.fired,
              "tick/present/drain/pump/export work count diverged from fired cadence ticks");
        CHECK(fresh.export_waits==0 && fresh.export_dumps==0 && fresh.export_acks==0,
              "disabled timeline export touched its wait, dump, or acknowledgement API");
        CHECK(stale.pending==crossed && fresh.pending==0,
              "stale snapshot did not defer exactly the crossed deadlines");
        if (crossed==0) {
            CHECK(stale.deadline==fresh.deadline &&
                  !strncmp(stale.order,"ITPDM",5) &&
                  !strncmp(fresh.order,"ITPDMEQ",7),
                  "no-work tick order changed or export/clock checks moved around the pump");
        }
    }

    LoopResult stale_fail=run_stale_loop(100000,0,0,0,1,10000000,0);
    LoopResult fresh_fail=run_fresh_loop(100000,0,0,0,1,10000000,0);
    CHECK(stale_fail.qpc_active && stale_fail.frequency==60000 &&
          !fresh_fail.qpc_active && fresh_fail.frequency==1000,
          "clock failure was not rearmed inside the refreshed loop iteration");
    CHECK(fresh_fail.fired==1 && fresh_fail.now==10000000 &&
          fresh_fail.deadline==10000016 && fresh_fail.pending==0,
          "fallback rearm manufactured a tick before its next period");

    LoopResult stale_overload=run_stale_loop(600000,2001,0,1,0,0,0);
    CHECK(stale_overload.fired==1 && stale_overload.pending==2,
          "fixed snapshot failed to demonstrate overload backlog after one costly drain");
    LoopResult overload=run_fresh_loop(600000,2001,0,1,0,0,0);
    CHECK(overload.fired==240 && overload.ticks==240 && overload.drains==240 &&
          overload.pumps==240 && overload.exports==240,
          "continuous-overload catch-up exceeded or bypassed its 240-tick bound");
    CHECK(overload.deadline==overload.now && overload.cadence.remainder_accumulator==0 &&
          overload.pending==1,
          "240-tick recovery did not resync to the freshest checked clock");
    TjVBlankCadence resumed=overload.cadence;
    tj_vblank_cadence_advance(&resumed);
    CHECK(!tj_vblank_cadence_due(&resumed,overload.now),
          "post-cap resync did not resume a future cadence period");

    LoopResult failed_240=run_fresh_loop(600000,2001,0,1,240,10000000,0);
    CHECK(failed_240.fired==240 && !failed_240.qpc_active &&
          failed_240.frequency==1000 && failed_240.now==10000000,
          "QPC failure on tick 240 did not rearm the clock inside the catch-up loop");
    CHECK(failed_240.deadline==10000016 && failed_240.pending==0 &&
          failed_240.exports==240,
          "240 cap resync overwrote the fresh fallback deadline after clock failure");

    LoopResult healthy_240=run_fresh_loop(600000,1000,0,1,0,0,240);
    CHECK(healthy_240.fired==240 && healthy_240.exports==240 &&
          healthy_240.deadline==healthy_240.now+1000 && healthy_240.pending==0,
          "healthy 240th tick with no remaining due work was spuriously resynced");

    LoopResult export_burst=run_fresh_loop(600000,1001,1,0,0,0,0,1);
    CHECK(export_burst.fired==2 && export_burst.exports==2 &&
          export_burst.outer_polls==1,
          "requested export burst did not poll in each inner iteration before the outer poll");
    CHECK(export_burst.export_waits==1 && export_burst.export_dumps==1 &&
          export_burst.export_acks==1 && export_burst.dumped_when_set==1 &&
          export_burst.bad_set_event==0,
          "in-burst export did not dump once and acknowledge only after dumped was set");
    CHECK(strstr(export_burst.order,"MEWBAQ")!=NULL,
          "in-burst export/ack moved after the fresh clock sample or before message pumping");
    CHECK(export_burst.export_waits==1 && export_burst.export_dumps==1 &&
          export_burst.export_acks==1,
          "outer poll duplicated the already completed in-burst dump or acknowledgement");
}
'''

    fixture = r'''#include <stdint.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdarg.h>
#include <string.h>
#include "tj_vblank_cadence.h"

typedef void* HANDLE;
typedef uint32_t DWORD;
typedef struct { int64_t QuadPart; } LARGE_INTEGER;
#define WAIT_OBJECT_0 0u
#define WAIT_TIMEOUT 258u
#define WAIT_FAILED 0xffffffffu

static uint64_t qpc_frequency = 60000;
static uint64_t qpc_now, tick_now;
static int qpc_succeeds = 1;
static int qpc_domain = 1;
static uint32_t sleep_quantum_ms = 1;
static uint32_t wait_elapsed_ms = 1;
static DWORD wait_result = WAIT_TIMEOUT, last_error = 0;
static HANDLE expected_event = (HANDLE)0x1234;
static HANDLE expected_complete_event = (HANDLE)0x5678;
static unsigned qpc_calls, tick_calls, wait_calls, sleep_calls;
static unsigned error_calls, log_calls, flush_calls;
static DWORD wait_timeouts[16], sleep_args[16];
static unsigned bad_wait_event, bad_sleep_arg;
static char order[64];
static unsigned order_length;
static char log_text[256];
struct FlipTimelineState { int enabled, dumped; };
static FlipTimelineState s_tj_flip_timeline;
static HANDLE s_tj_flip_dump_request, s_tj_flip_dump_complete;
static unsigned export_dump_calls, export_set_calls, dumped_when_set, bad_set_event;

static void order_push(char c) {
    if (order_length + 1 < sizeof(order)) {
        order[order_length++] = c;
        order[order_length] = 0;
    }
}
static int SetEvent(HANDLE event) {
    order_push('A'); ++export_set_calls;
    if (event != expected_complete_event) ++bad_set_event;
    if (s_tj_flip_timeline.dumped) ++dumped_when_set;
    return 1;
}
static void tj_flip_timeline_dump(void) {
    order_push('B'); ++export_dump_calls; s_tj_flip_timeline.dumped=1;
}
static void advance_ms(uint32_t ms) {
    if (qpc_domain)
        qpc_now += (qpc_frequency * (uint64_t)ms) / 1000;
    else
        tick_now += ms;
}
static int fixture_qpc(LARGE_INTEGER* sample) {
    order_push('Q');
    ++qpc_calls;
    if (!qpc_succeeds) return 0;
    sample->QuadPart = (int64_t)qpc_now;
    return 1;
}
static uint64_t fixture_tick(void) { order_push('C'); ++tick_calls; return tick_now; }
static DWORD fixture_wait(HANDLE event, DWORD timeout_ms) {
    order_push('W');
    if (wait_calls < sizeof(wait_timeouts) / sizeof(wait_timeouts[0]))
        wait_timeouts[wait_calls] = timeout_ms;
    ++wait_calls;
    if (event != expected_event) ++bad_wait_event;
    if (timeout_ms && wait_result == WAIT_TIMEOUT) advance_ms(wait_elapsed_ms);
    return wait_result;
}
static DWORD fixture_error(void) { ++error_calls; order_push('E'); return last_error; }
static void fixture_sleep(DWORD ms) {
    order_push('S');
    if (sleep_calls < sizeof(sleep_args) / sizeof(sleep_args[0]))
        sleep_args[sleep_calls] = ms;
    ++sleep_calls;
    if (ms != 1) ++bad_sleep_arg;
    advance_ms(sleep_quantum_ms);
}
static int fixture_fprintf(void* stream, const char* format, ...) {
    (void)stream; ++log_calls; order_push('L');
    va_list args; va_start(args, format);
    vsnprintf(log_text, sizeof(log_text), format, args);
    va_end(args); return 0;
}
static int fixture_fflush(void* stream) { (void)stream; ++flush_calls; order_push('F'); return 0; }
#define QueryPerformanceCounter fixture_qpc
#define GetTickCount64 fixture_tick
#define WaitForSingleObject fixture_wait
#define GetLastError fixture_error
#define Sleep fixture_sleep
#define fprintf fixture_fprintf
#define fflush fixture_fflush
#undef stderr
#define stderr ((void*)0)
WAIT_HELPER
CLOCK_HELPER
EXPORT_HELPER

static unsigned checks, failures;
#define CHECK(test, message) do { ++checks; if (!(test)) { ++failures; printf("FAIL: %s\n", message); } } while (0)
static void reset_io(void) {
    qpc_calls = tick_calls = wait_calls = sleep_calls = 0;
    error_calls = log_calls = flush_calls = 0;
    bad_wait_event = bad_sleep_arg = 0;
    export_dump_calls = export_set_calls = dumped_when_set = bad_set_event = 0;
    memset(wait_timeouts, 0, sizeof(wait_timeouts));
    memset(sleep_args, 0, sizeof(sleep_args));
    order_length = 0; order[0] = 0; log_text[0] = 0;
}
LOOP_SUPPORT

static void test_due_wait_against_baseline(uint32_t scheduler_ms) {
    qpc_domain = 1; qpc_succeeds = 1; sleep_quantum_ms = scheduler_ms;
    qpc_now = 600000; tick_now = 0; reset_io();
    TjVBlankCadence cadence;
    tj_vblank_cadence_init(&cadence, qpc_now, qpc_frequency);
    uint64_t deadline = cadence.deadline;
    uint64_t before = qpc_now;

    /* This is the pre-change branch from the frozen presenter source. */
    Sleep(1);
    bool qpc_active = true; uint64_t frequency = qpc_frequency;
    uint64_t baseline_now = tj_vblank_clock_now(&cadence, &qpc_active, &frequency);
    uint64_t baseline_elapsed_ms = qpc_now - before;
    CHECK(baseline_elapsed_ms == scheduler_ms * (qpc_frequency / 1000),
          "baseline fake Sleep(1) did not reproduce the configured scheduler delay");
    CHECK(tj_vblank_cadence_due(&cadence, baseline_now),
          "baseline failed to service the already-due cadence after sleeping");
    CHECK(cadence.deadline == deadline, "clock sampling advanced cadence in baseline case");
    CHECK(sleep_calls == 1 && sleep_args[0] == 1,
          "baseline did not execute its unconditional Sleep(1)");

    qpc_now = 600000; tick_now = 0; reset_io();
    tj_vblank_cadence_init(&cadence, qpc_now, qpc_frequency);
    deadline = cadence.deadline;
    qpc_active = true; frequency = qpc_frequency;
    uint64_t candidate_before = tj_vblank_clock_now(&cadence, &qpc_active, &frequency);
    DWORD timeout_ms = tj_vblank_cadence_due(&cadence, candidate_before) ? 0 : 1;
    int active = 1, failure_logged = 0;
    uint64_t wakes = 0, timeouts = 0, fallbacks = 0;
    tj_fifo_kick_wait_once(NULL, &active, &failure_logged,
                           &wakes, &timeouts, &fallbacks, timeout_ms);
    uint64_t candidate_after = tj_vblank_clock_now(&cadence, &qpc_active, &frequency);
    CHECK(timeout_ms == 0 && tj_vblank_cadence_due(&cadence, candidate_before),
          "candidate did not choose zero timeout for a due VBlank");
    CHECK(sleep_calls == 0 && wait_calls == 0 && qpc_now == 600000,
          "candidate due path slept or blocked before ticking");
    CHECK(tj_vblank_cadence_due(&cadence, candidate_after) && cadence.deadline == deadline,
          "candidate clock/wait changed or concealed cadence due state");
}

static void test_future_deadline_still_waits(void) {
    qpc_domain = 1; qpc_succeeds = 1; sleep_quantum_ms = 3;
    qpc_now = 1200000; tick_now = 0; reset_io();
    TjVBlankCadence cadence;
    tj_vblank_cadence_init(&cadence, qpc_now, qpc_frequency);
    tj_vblank_cadence_advance(&cadence); /* consume initial tick; next is 1000 QPC ticks away */
    qpc_now = cadence.deadline - 1;
    bool qpc_active = true; uint64_t frequency = qpc_frequency;
    uint64_t before = tj_vblank_clock_now(&cadence, &qpc_active, &frequency);
    DWORD timeout_ms = tj_vblank_cadence_due(&cadence, before) ? 0 : 1;
    int active = 1, failure_logged = 0;
    uint64_t wakes = 0, timeouts = 0, fallbacks = 0;
    tj_fifo_kick_wait_once(NULL, &active, &failure_logged,
                           &wakes, &timeouts, &fallbacks, timeout_ms);
    uint64_t after = tj_vblank_clock_now(&cadence, &qpc_active, &frequency);
    CHECK(timeout_ms == 1 && sleep_calls == 1 && sleep_args[0] == 1,
          "future deadline no longer uses the bounded one-millisecond fallback");
    CHECK(qpc_now - before == 180, "fake Sleep(1) did not model a 3ms scheduler quantum");
    CHECK(tj_vblank_cadence_due(&cadence, after) && timeouts == 0,
          "future wait failed to let the next deadline become due");
    CHECK(wait_calls == 0 && cadence.deadline == before + 1,
          "future clock/wait advanced cadence instead of the tick loop");

    /* With the opt-in event, timeout zero still observes a signaled event and
     * treats a normal zero-timeout miss as a timeout, not a fallback/error. */
    reset_io(); qpc_now = cadence.deadline - 1;
    before = tj_vblank_clock_now(&cadence, &qpc_active, &frequency);
    timeout_ms = tj_vblank_cadence_due(&cadence, before) ? 0 : 1;
    wait_result = WAIT_TIMEOUT; active = 1; failure_logged = 0;
    wakes = timeouts = fallbacks = 0;
    tj_fifo_kick_wait_once(expected_event, &active, &failure_logged,
                           &wakes, &timeouts, &fallbacks, timeout_ms);
    CHECK(timeout_ms == 1 && wait_calls == 1 && wait_timeouts[0] == 1 &&
          timeouts == 1 && active && fallbacks == 0 && error_calls == 0,
          "future event timeout was not a normal bounded wait");
    CHECK(sleep_calls == 0 && tj_vblank_cadence_due(
              &cadence, tj_vblank_clock_now(&cadence, &qpc_active, &frequency)),
          "future event wait failed to become due without a helper Sleep");
}

static void test_clock_switch_rearms_without_a_tick(void) {
    qpc_domain = 0; qpc_succeeds = 0; tick_now = 10000000; qpc_now = 7;
    reset_io();
    TjVBlankCadence cadence;
    tj_vblank_cadence_init(&cadence, 100, qpc_frequency);
    bool qpc_active = true; uint64_t frequency = qpc_frequency;
    uint64_t fallback_now = tj_vblank_clock_now(&cadence, &qpc_active, &frequency);
    CHECK(!qpc_active && frequency == 1000 && fallback_now == tick_now,
          "QPC failure did not switch explicitly to GetTickCount64 units");
    CHECK(qpc_calls == 1 && tick_calls == 1,
          "clock switch did not sample each source exactly once");
    CHECK(cadence.deadline == fallback_now + 16 && cadence.remainder_accumulator == 40,
          "fallback cadence was not rearmed one 60Hz period after its new epoch");
    CHECK(!tj_vblank_cadence_due(&cadence, fallback_now) &&
          !tj_vblank_cadence_due(&cadence, fallback_now + 15),
          "stale QPC deadline manufactured a VBlank in the fallback clock domain");
    uint64_t deadline = cadence.deadline;
    tick_now = fallback_now + 15;
    uint64_t later = tj_vblank_clock_now(&cadence, &qpc_active, &frequency);
    CHECK(!tj_vblank_cadence_due(&cadence, later) && cadence.deadline == deadline,
          "fallback clock sample advanced cadence before its deadline");
    tick_now = deadline;
    later = tj_vblank_clock_now(&cadence, &qpc_active, &frequency);
    CHECK(tj_vblank_cadence_due(&cadence, later) && cadence.deadline == deadline,
          "fallback clock sample did not expose the deadline without consuming it");
    CHECK(qpc_calls == 1 && tick_calls == 3,
          "fallback continued querying QPC or skipped a GetTickCount64 sample");
}

static void test_zero_timeout_event_and_error_paths(void) {
    qpc_domain = 1; qpc_succeeds = 1; qpc_now = 900000; reset_io();
    int active = 1, failure_logged = 0;
    uint64_t wakes = 0, timeouts = 0, fallbacks = 0;
    wait_result = WAIT_TIMEOUT;
    tj_fifo_kick_wait_once(expected_event, &active, &failure_logged,
                           &wakes, &timeouts, &fallbacks, 0);
    CHECK(wait_calls == 1 && wait_timeouts[0] == 0 && active && timeouts == 1 &&
          fallbacks == 0 && error_calls == 0 && sleep_calls == 0 && log_calls == 0,
          "zero-timeout miss incorrectly disabled, errored, logged, or slept");

    wait_result = WAIT_OBJECT_0;
    tj_fifo_kick_wait_once(expected_event, &active, &failure_logged,
                           &wakes, &timeouts, &fallbacks, 0);
    CHECK(wait_calls == 2 && wakes == 1 && active && sleep_calls == 0,
          "zero-timeout poll failed to preserve the signaled-event predicate");

    wait_result = WAIT_FAILED; last_error = 0x4567;
    tj_fifo_kick_wait_once(expected_event, &active, &failure_logged,
                           &wakes, &timeouts, &fallbacks, 0);
    CHECK(!active && failure_logged && fallbacks == 1 && error_calls == 1 &&
          log_calls == 1 && flush_calls == 1 && sleep_calls == 0,
          "zero-timeout wait failure did not disable once without adding a due-path sleep");
    CHECK(!strcmp(order + order_length - 4, "WELF") &&
          strstr(log_text, "fallback_wait_ms=0") != NULL,
          "wait failure error was not captured before its single zero-timeout log");
    tj_fifo_kick_wait_once(expected_event, &active, &failure_logged,
                           &wakes, &timeouts, &fallbacks, 0);
    CHECK(wait_calls == 3 && fallbacks == 2 && error_calls == 1 &&
          log_calls == 1 && sleep_calls == 0,
          "disabled zero-timeout event retried or repeated its error log");
    CHECK(!bad_wait_event && !bad_sleep_arg,
          "helper passed an unexpected event handle or Sleep duration");
}

LOOP_RUNNERS

int main(void) {
    test_due_wait_against_baseline(1);
    test_due_wait_against_baseline(3);
    test_future_deadline_still_waits();
    test_clock_switch_rearms_without_a_tick();
    test_zero_timeout_event_and_error_paths();
    test_inner_loop_refresh_and_cap();
    if (failures) {
        printf("present due wait: %u checks, %u failures\n", checks, failures);
        return 1;
    }
    printf("PASS: %u checks; due wait, clock fallback, stale/fresh inner-loop deadlines, and 240 cap\n", checks);
    return 0;
}
'''.replace("WAIT_HELPER", wait_helper).replace("CLOCK_HELPER", clock_helper)
    fixture = fixture.replace("LOOP_SUPPORT", loop_support)
    fixture = fixture.replace("EXPORT_HELPER", export_helper)
    fixture = fixture.replace("LOOP_RUNNERS", loop_runners + "\n" + loop_tests)

    compiler = shutil.which("clang++") or r"C:\Program Files\LLVM\bin\clang++.exe"
    with tempfile.TemporaryDirectory(prefix="tj-present-due-wait-") as temp:
        temp = Path(temp)
        source = temp / "fixture.cpp"
        executable = temp / "fixture.exe"
        source.write_text(fixture, encoding="utf-8")
        subprocess.run([
            compiler, "-std=c++17", "-O2", "-I", str(CADENCE.parent),
            str(source), "-o", str(executable),
        ], check=True)
        result = subprocess.run([str(executable)], capture_output=True, text=True,
                                timeout=15, check=False)
        if result.returncode:
            raise AssertionError(f"fixture failed:\n{result.stdout}\n{result.stderr}")
        print(result.stdout.strip())


if __name__ == "__main__":
    main()
