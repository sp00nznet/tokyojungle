#include "jobchain_run_gate.h"
#include "jobguard_line_gate.h"

#include <stdio.h>

static int check(int condition, const char* message)
{
    if (condition) return 1;
    fprintf(stderr, "FAIL: %s\n", message);
    return 0;
}

int main(void)
{
#ifdef _WIN32
    volatile long running = 0;
    volatile long requested = 0;

    if (!check(jobchain_request_run(&running, &requested) == 1,
               "idle chain must create a worker")) return 1;
    jobchain_worker_begin(&requested);

    /* Exact lost-wakeup interleaving: the chain has parked, but its worker has
     * not yet cleared `running` when JobGuardNotify requests the rerun. */
    if (!check(jobchain_request_run(&running, &requested) == 0,
               "active worker must own the rerun")) return 1;
    if (!check(jobchain_worker_should_continue(&running, &requested) == 1,
               "parked worker must consume the latched rerun")) return 1;
    jobchain_worker_begin(&requested);

    if (!check(jobchain_worker_should_continue(&running, &requested) == 0,
               "worker must release an idle chain")) return 1;
    if (!check(running == 0 && requested == 0,
               "idle gate state must be fully released")) return 1;

    /* A request after release owns a fresh worker. */
    if (!check(jobchain_request_run(&running, &requested) == 1,
               "post-release request must create a worker")) return 1;
    jobchain_worker_begin(&requested);
    if (!check(jobchain_worker_should_continue(&running, &requested) == 0,
               "fresh worker must release normally")) return 1;

    /* Full-line JobGuard handoff: no interleaving may leave a zero-count guard
     * parked without a rerun. The production users serialize these calls with
     * the shared SPU reservation-line lock. */
    jobguard_line_state guard = { 1, 0 };
    if (!check(jobguard_line_try_park(&guard) == 1 && guard.parked == 1,
               "park-first must publish parked")) return 1;
    if (!check(jobguard_line_notify(&guard) == 1 && guard.count == 0,
               "final notify after park must request restart")) return 1;

    guard.count = 1;
    guard.parked = 0;
    if (!check(jobguard_line_notify(&guard) == 0 && guard.count == 0,
               "final notify first has no parked chain to restart")) return 1;
    if (!check(jobguard_line_try_park(&guard) == 0 && guard.parked == 0,
               "notify-first worker must open, never park")) return 1;
    if (!check(!(guard.count == 0 && guard.parked != 0),
               "zero-count guard must not remain parked without restart")) return 1;

    guard.count = 2;
    guard.parked = 1;
    if (!check(jobguard_line_notify(&guard) == 0 && guard.count == 1 &&
               guard.parked == 1,
               "non-final notify must only decrement")) return 1;
    guard.count = 0;
    if (!check(jobguard_line_notify(&guard) < 0 && guard.count == 0,
               "empty guard must reject without a restart")) return 1;

    puts("jobchain run-gate and JobGuard handoff regressions passed");
#else
    puts("jobchain run-gate regression skipped: Windows-only host path");
#endif
    return 0;
}
