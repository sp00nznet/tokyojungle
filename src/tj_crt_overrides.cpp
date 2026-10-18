/* CRT bring-up overrides.
 *
 * These five guest functions are the PS3 libc/CRT init chain. Their real
 * bodies make lv2 calls and touch kernel structures the HLE runtime does not
 * model, so the stdio initialiser abort()s partway through _start. Skipping
 * them (returning 0) is what the old generated/ppu_stubs.c did by replacing
 * the symbols at link time; the runtime is not yet far enough along to run
 * them for real.
 *
 * The CRT chain calls them DIRECTLY (`func_0024DE14(ctx)` in the lifted
 * code), so registering an override with ppu_register_function does not
 * help -- that only intercepts indirect calls through ppu_lookup. Instead
 * scripts/post_lift.py rewrites each of these bodies into a one-line
 * call to tj_crt_skip(), the same body-redirect patch Rubber Ducky uses for
 * its allocator overrides.
 *
 * ponytail: drop entries from post_lift.py's CRT_SKIPS as the runtime grows
 * to support them; each one removed is real CRT code that starts running.
 */
#include "recomp_bridge.h"
#include "ps3emu/guest_call.h"
#include <stdio.h>
#include <stdlib.h>
#include <windows.h>
#include <string.h>
#include <map>
#include <new>

extern "C" uint64_t spu_taskset_arm_signal_generation(
    uint32_t owner_ea, uint32_t taskset_ea, uint32_t task_ids_ea,
    uint32_t count, int expected_image_id, uint32_t completion_ea,
    int create_ca9c_substitution_debt);
extern "C" uint64_t spu_taskset_current_signal_generation(
    uint32_t owner_ea, uint32_t taskset_ea);
extern "C" int spu_taskset_retry_signal_generation(
    uint32_t owner_ea, uint32_t taskset_ea, uint32_t task_ids_ea,
    uint32_t count, int expected_image_id, uint32_t completion_ea,
    uint64_t generation, int create_ca9c_substitution_debt,
    int followup_recovery);
extern "C" int spu_taskset_consume_ca9c_substitution(
    uint32_t taskset_ea, uint32_t task_id);
extern "C" void spu_taskset_signal_snapshot(FILE* out);
extern "C" void spu_completion_atomic_history_snapshot(FILE* out,
                                                         uint32_t completion_ea);
extern "C" void spu_reservation_notify_write(uint32_t ea, uint32_t size);
extern "C" void tj_jobchain_trace_arm_after_scene(void);
extern "C" void spu_taskset_signal_trace_arm_after_scene(void);
extern "C" volatile LONG g_tj_geometry_trace_armed;

/* Read-only producer provenance for the matching FIFO depth-state trace. */
void tj_depth_mask_emit_trace(ppu_context* ctx, uint32_t header_ea,
                              uint32_t value, uint32_t caller)
{
    static const bool enabled = getenv("TJ_DEPTH_PASS_TRACE") != nullptr;
    if (!enabled) return;
    struct Seen { uint32_t caller, page, value; };
    static thread_local Seen seen[128] = {};
    static thread_local unsigned seen_count = 0, late_count = 0;
    const LONG phase = InterlockedCompareExchange(&g_tj_geometry_trace_armed, 0, 0);
    if (phase < 2) {
        const uint32_t page = header_ea >> 12;
        for (unsigned i = 0; i < seen_count; ++i)
            if (seen[i].caller == caller && seen[i].page == page &&
                seen[i].value == value) return;
        if (seen_count == 128) return;
        seen[seen_count++] = {caller, page, value};
    } else if (late_count++ >= 256) return;
    fprintf(stderr,
        "[TJ-DEPTH-EMIT] phase=%ld tid=%u lr=%08X header=%08X value=%08X context=%08X\n",
        phase, ctx->thread_id, caller, header_ea, value, (uint32_t)ctx->gpr[31]);
    static thread_local bool owner_logged = false;
    if (phase == 2 && caller == 0x0019AD2Cu && !owner_logged) {
        owner_logged = true;
        uint32_t sp = (uint32_t)ctx->gpr[1];
        fprintf(stderr, "[TJ-DEPTH-OWNER] header=%08X", header_ea);
        /* Walk only the committed main stack. The ELFv1 saved LR belongs
         * to the linkage area at the parent stack pointer plus16. */
        for (unsigned frame = 0; frame < 10 && sp >= 0xD0000000u &&
             sp <= 0xD00FFFE0u && !(sp & 15u); ++frame) {
            const uint64_t parent = vm_read64(sp);
            if (parent <= sp || parent > 0xD00FFFE0u || (parent & 15u)) break;
            fprintf(stderr, " sp=%08X/lr=%08X", sp,
                    (uint32_t)vm_read64((uint32_t)parent + 16u));
            sp = (uint32_t)parent;
        }
        for (unsigned r = 14; r < 30; ++r)
            fprintf(stderr, " r%u=%08X", r, (uint32_t)ctx->gpr[r]);
        fputc('\n', stderr);
    }
}

extern "C" {
volatile LONG g_tj_survival_scene_ready = 0;
volatile LONG g_tj_last_task_manager_ea = 0;
}

/* Process-local evidence only.  These are raw object identities, not menu
 * state names and not input actions.  The gate requires the pending scene to
 * pass the subsequent state-zero write before it becomes evidence. */
static uint32_t tj_title_gate_pending_scene_ea = 0;
static uint32_t tj_title_gate_evidence_scene_ea = 0;
static uint32_t tj_title_gate_current_scene_ea = 0;
static uint32_t tj_title_gate_idle_menu_ea = 0;
static uint32_t tj_title_gate_title_ready_menu_ea = 0;

/* C904 publishes a new manager generation while CA50 waits for the preceding
 * one. Keep that boundary per manager: C904 holds this lock from its generated
 * pre-clear hook through generation arm, and CA50 advertises an active zero wait before
 * sleeping. No epoch or recovery predicate is changed here. */
struct tj_spu_manager_lifecycle_state {
    SRWLOCK lock;
    CONDITION_VARIABLE cv;
    uint32_t active_ca50_waiters;
};

static SRWLOCK tj_spu_manager_lifecycle_table_lock = SRWLOCK_INIT;
static std::map<uint32_t, tj_spu_manager_lifecycle_state*>
    tj_spu_manager_lifecycle_table;
static thread_local tj_spu_manager_lifecycle_state*
    tj_spu_c904_lifecycle_state = nullptr;
static thread_local uint32_t tj_spu_c904_lifecycle_manager_ea = 0;

static tj_spu_manager_lifecycle_state*
tj_spu_manager_lifecycle_get(uint32_t manager_ea)
{
    if (!manager_ea) return nullptr;
    AcquireSRWLockExclusive(&tj_spu_manager_lifecycle_table_lock);
    auto found = tj_spu_manager_lifecycle_table.find(manager_ea);
    if (found != tj_spu_manager_lifecycle_table.end()) {
        ReleaseSRWLockExclusive(&tj_spu_manager_lifecycle_table_lock);
        return found->second;
    }

    auto* state = new (std::nothrow) tj_spu_manager_lifecycle_state{};
    if (state) {
        InitializeSRWLock(&state->lock);
        InitializeConditionVariable(&state->cv);
        tj_spu_manager_lifecycle_table.emplace(manager_ea, state);
    }
    ReleaseSRWLockExclusive(&tj_spu_manager_lifecycle_table_lock);
    return state;
}

/* Atomically perform CA50's first completion read and register ownership only
 * when that read is zero. C904 cannot clear/arm between those two operations. */
uint32_t tj_spu_completion_wait_begin(uint32_t manager_ea)
{
    tj_spu_manager_lifecycle_state* state = tj_spu_manager_lifecycle_get(manager_ea);
    if (!state)
        return manager_ea ? vm_read32(manager_ea + 0x2Cu) : 0;

    AcquireSRWLockExclusive(&state->lock);
    uint32_t completion = vm_read32(manager_ea + 0x2Cu);
    if (completion == 0)
        ++state->active_ca50_waiters;
    ReleaseSRWLockExclusive(&state->lock);
    return completion;
}

void tj_spu_completion_wait_end(uint32_t manager_ea)
{
    tj_spu_manager_lifecycle_state* state = tj_spu_manager_lifecycle_get(manager_ea);
    if (!state) return;

    AcquireSRWLockExclusive(&state->lock);
    if (state->active_ca50_waiters && --state->active_ca50_waiters == 0)
        WakeAllConditionVariable(&state->cv);
    ReleaseSRWLockExclusive(&state->lock);
}

static bool tj_spu_completion_wait_diagnostics_enabled(void);

static bool tj_spu_completion_wait_recovery_enabled(void)
{
    static const bool enabled =
        getenv("TJ_SPU_COMPLETION_WAIT_RECOVERY") != nullptr;
    return enabled;
}

/* Enter the injected observer for either read-only diagnostics or the
 * explicit recovery experiment. Normal execution preserves the guest's CA9C
 * callback as the sole signal owner. */
bool tj_spu_completion_wait_trace_enabled(void)
{
    return tj_spu_completion_wait_diagnostics_enabled() ||
           tj_spu_completion_wait_recovery_enabled();
}

static bool tj_spu_completion_wait_diagnostics_enabled(void)
{
    static const bool enabled =
        getenv("TJ_SPU_COMPLETION_WAIT_TRACE") != nullptr;
    return enabled;
}

struct tj_spu_completion_wait_trace_state {
    bool active;
    uint32_t manager_ea;
    uint32_t taskset_ea;
    uint64_t generation;
    uint32_t caller;
    DWORD thread_id;
    ULONGLONG started_ms;
    ULONGLONG last_stuck_ms;
    bool reported_stuck;
    bool snapshot_reported;
    bool recovery_terminal;
};

static volatile LONG64 tj_spu_completion_wait_trace_sequence = 0;
static thread_local tj_spu_completion_wait_trace_state
    tj_spu_completion_wait_trace_tls = {};

static void tj_spu_completion_wait_trace_emit(const char* state,
                                              const tj_spu_completion_wait_trace_state& trace,
                                              uint32_t completion,
                                              ULONGLONG waited_ms)
{
    unsigned long long sequence = (unsigned long long)
        InterlockedIncrement64(&tj_spu_completion_wait_trace_sequence);
    fprintf(stderr,
            "[TJ-SPU-COMPLETION-WAIT] seq=%llu state=%s caller=%08X "
            "manager=0x%08X completion_ea=0x%08X completion=0x%08X "
            "waited_ms=%llu thread=%lu\n",
            sequence, state, trace.caller, trace.manager_ea,
            trace.manager_ea + 0x2Cu, completion,
            (unsigned long long)waited_ms, (unsigned long)trace.thread_id);
    fflush(stderr);
}

/* `site` is 0 on the initial zero completion and 1 after each existing
 * syscall-then-read loop iteration. The entry site captures the caller
 * before wait; the poll site keeps reporting even after frame production
 * stops because it runs inside the guest completion loop. */
void tj_spu_completion_wait_trace_record(ppu_context* ctx, uint32_t manager_ea,
                                         uint32_t completion, uint32_t site)
{
    const bool diagnostics = tj_spu_completion_wait_diagnostics_enabled();
    const bool recovery_enabled = tj_spu_completion_wait_recovery_enabled();

    tj_spu_completion_wait_trace_state& trace = tj_spu_completion_wait_trace_tls;
    ULONGLONG now_ms = GetTickCount64();
    if (site == 0) {
        trace.active = true;
        trace.manager_ea = manager_ea;
        trace.taskset_ea = manager_ea ? vm_read32(manager_ea + 0x08) : 0;
        trace.generation = spu_taskset_current_signal_generation(
            manager_ea, trace.taskset_ea);
        trace.caller = ctx ? (uint32_t)ctx->lr : 0;
        trace.thread_id = GetCurrentThreadId();
        trace.started_ms = now_ms;
        trace.last_stuck_ms = 0;
        trace.reported_stuck = false;
        trace.snapshot_reported = false;
        trace.recovery_terminal = false;

        /* C904 armed this exact generation after clearing completion. CA50 is
         * now the owning waiter, so dispatch here instead of paying the
         * 250-ms watchdog delay on every batch. If workers are not parked yet,
         * leave the existing poll-side guarded recovery to retry later. */
        uint32_t count = vm_read32(trace.manager_ea + 0x00);
        uint32_t taskset_ea = vm_read32(trace.manager_ea + 0x08);
        uint32_t task_ids_ea = vm_read32(trace.manager_ea + 0x0C);
        int recovery = recovery_enabled
            ? spu_taskset_retry_signal_generation(
                  trace.manager_ea, taskset_ea, task_ids_ea, count, 15,
                  trace.manager_ea + 0x2Cu, trace.generation, 1, 0)
            : 0;
        if (recovery == 1 && diagnostics) {
            fprintf(stderr,
                    "[TJ-SPU-COMPLETION-RECOVER] manager=0x%08X "
                    "completion_ea=0x%08X taskset=0x%08X count=%u "
                    "generation=%llu mode=wait-entry waited_ms=0\n",
                    trace.manager_ea, trace.manager_ea + 0x2Cu, taskset_ea,
                    count, (unsigned long long)trace.generation);
            fflush(stderr);
        }
        return;
    }

    if (!trace.active) return;
    ULONGLONG waited_ms = now_ms - trace.started_ms;
    if (completion != 0) {
        if (diagnostics && trace.reported_stuck)
            tj_spu_completion_wait_trace_emit("exit", trace, completion, waited_ms);
        trace.active = false;
        return;
    }
    if (recovery_enabled && waited_ms >= 250 && !trace.recovery_terminal) {
        uint32_t count = vm_read32(trace.manager_ea + 0x00);
        uint32_t taskset_ea = vm_read32(trace.manager_ea + 0x08);
        uint32_t task_ids_ea = vm_read32(trace.manager_ea + 0x0C);
        int followup_recovery = 0;
        int recovery = spu_taskset_retry_signal_generation(
            trace.manager_ea, taskset_ea, task_ids_ea, count, 15,
            trace.manager_ea + 0x2Cu, trace.generation, 1, 0);
        if (recovery < 0) {
            followup_recovery = 1;
            recovery = spu_taskset_retry_signal_generation(
                trace.manager_ea, taskset_ea, task_ids_ea, count, 15,
                trace.manager_ea + 0x2Cu, trace.generation, 0, 1);
        }
        if (recovery == 1) {
            if (followup_recovery) trace.recovery_terminal = true;
            if (diagnostics) {
                fprintf(stderr,
                        "[TJ-SPU-COMPLETION-RECOVER] manager=0x%08X "
                        "completion_ea=0x%08X taskset=0x%08X count=%u "
                        "generation=%llu mode=%s waited_ms=%llu\n",
                        trace.manager_ea, trace.manager_ea + 0x2Cu, taskset_ea,
                        count, (unsigned long long)trace.generation,
                        followup_recovery ? "post-primary" : "wait-owned",
                        (unsigned long long)waited_ms);
                fflush(stderr);
            }
        } else if (recovery < 0) {
            trace.recovery_terminal = true;
        }
    }
    if (diagnostics && waited_ms > 250 &&
        (!trace.last_stuck_ms || now_ms - trace.last_stuck_ms >= 1000)) {
        if (!trace.snapshot_reported) {
            fprintf(stderr,
                    "[TJ-SPU-COMPLETION-SNAPSHOT] state=BEGIN caller=%08X "
                    "manager=0x%08X completion_ea=0x%08X waited_ms=%llu\n",
                    trace.caller, trace.manager_ea, trace.manager_ea + 0x2Cu,
                    (unsigned long long)waited_ms);
            spu_taskset_signal_snapshot(stderr);
            spu_completion_atomic_history_snapshot(
                stderr, trace.manager_ea + 0x2Cu);
            fprintf(stderr, "[TJ-SPU-COMPLETION-SNAPSHOT] state=END\n");
            fflush(stderr);
            trace.snapshot_reported = true;
        }
        tj_spu_completion_wait_trace_emit("stuck", trace, completion, waited_ms);
        trace.last_stuck_ms = now_ms;
        trace.reported_stuck = true;
    }
}

/* TJ_TASK_WAKE_PERF=1: aggregate bridge activity at most once per second.
 * The sequence is the total bridge-call sequence; calls/signals cover the
 * elapsed reporting interval. No per-task/token logging is emitted. */
static void tj_task_wake_perf_record(uint32_t manager_ea, uint32_t count,
                                     uint32_t taskset_ea,
                                     uint32_t task_ids_ea,
                                     uint32_t signaled)
{
    static const bool enabled = getenv("TJ_TASK_WAKE_PERF") != nullptr;
    if (!enabled) return;

    static SRWLOCK lock = SRWLOCK_INIT;
    static unsigned long long sequence = 0;
    static unsigned long long interval_calls = 0;
    static unsigned long long interval_signals = 0;
    static ULONGLONG last_report_ms = 0;

    uint32_t spurs_ea = manager_ea ? vm_read32(manager_ea + 0x04) : 0;
    uint32_t completion = manager_ea ? vm_read32(manager_ea + 0x2C) : 0;
    uint32_t task_id_0 = task_ids_ea && count > 0 && count <= 128
        ? vm_read32(task_ids_ea + 0x00) : 0xFFFFFFFFu;
    uint32_t task_id_1 = task_ids_ea && count > 1 && count <= 128
        ? vm_read32(task_ids_ea + 0x04) : 0xFFFFFFFFu;
    AcquireSRWLockExclusive(&lock);
    ULONGLONG now_ms = GetTickCount64();
    unsigned long long call_sequence = ++sequence;
    ++interval_calls;
    interval_signals += signaled;
    ULONGLONG elapsed_ms = last_report_ms ? now_ms - last_report_ms : 0;
    if (!last_report_ms || elapsed_ms >= 1000) {
        fprintf(stderr,
                "[TJ-TASK-WAKE-PERF] seq=%llu interval_ms=%llu "
                "calls=%llu signals=%llu manager=0x%08X spurs=0x%08X "
                "taskset=0x%08X count=%u task_ids=0x%08X ids=%u,%u "
                "completion=%u\n",
                call_sequence, (unsigned long long)elapsed_ms,
                interval_calls, interval_signals, manager_ea, spurs_ea,
                taskset_ea, count, task_ids_ea, task_id_0, task_id_1,
                completion);
        fflush(stderr);
        interval_calls = 0;
        interval_signals = 0;
        last_report_ms = now_ms;
    }
    ReleaseSRWLockExclusive(&lock);
}

/* C904 holds this per-manager lock from pre-clear through the arm seam. A CA50
 * zero waiter blocks new C904 publication until it exits; stale GETLLAR is
 * invalidated while the publishing lock is held. */
void tj_publish_spu_task_group_write(uint32_t manager_ea)
{
    tj_spu_manager_lifecycle_state* state = tj_spu_manager_lifecycle_get(manager_ea);
    if (!state) return;

    AcquireSRWLockExclusive(&state->lock);
    while (state->active_ca50_waiters) {
        SleepConditionVariableSRW(&state->cv, &state->lock, INFINITE, 0);
    }
    tj_spu_c904_lifecycle_state = state;
    tj_spu_c904_lifecycle_manager_ea = manager_ea;
    spu_reservation_notify_write(manager_ea, 0x30u);
}

static void tj_finish_spu_task_group_publish(uint32_t manager_ea)
{
    if (tj_spu_c904_lifecycle_state &&
        tj_spu_c904_lifecycle_manager_ea == manager_ea) {
        tj_spu_manager_lifecycle_state* state = tj_spu_c904_lifecycle_state;
        tj_spu_c904_lifecycle_state = nullptr;
        tj_spu_c904_lifecycle_manager_ea = 0;
        ReleaseSRWLockExclusive(&state->lock);
    }
}

/* Arm one persistent-SPURS task-group generation. The manager layout is:
 *   +0x00 task count, +0x08 taskset EA, +0x0C task-ID array EA.
 * func_0025C904 publishes a descriptor batch and clears +0x2C (completion).
 * The generated pre-clear bridge owns reservation invalidation. This seam
 * records exact task/epoch ownership. The F5AAC0DE callback at CA9C owns the
 * normal task signal; CA50 owns only the exact-generation missed-edge recovery. */
/* Exact generated CA9C send: atomically consume recovery debt or deliver the
 * normal task signal. Unregistered tasksets fall through to generated HLE. */
bool tj_consume_spu_task_signal_substitution(uint32_t taskset_ea,
                                             uint32_t task_id)
{
    return spu_taskset_consume_ca9c_substitution(taskset_ea, task_id) != 0;
}

/* Diagnostic: TJ_DISABLE_MLAA=1 skips the whole per-frame Edge MLAA wrapper
 * (func_00153834: LOCAL->MAIN readback, SPU MLAA kick, MAIN->LOCAL upload).
 * The raw current color buffer then flips without anti-aliasing. Read once. */
bool tj_mlaa_skip(void)
{
    static const bool enabled = [] {
        const char* value = getenv("TJ_DISABLE_MLAA");
        bool on = value && value[0] == '1' && value[1] == '\0';
        if (on) fprintf(stderr, "[tj-mlaa] TJ_DISABLE_MLAA=1: per-frame MLAA skipped\n");
        return on;
    }();
    return enabled;
}

void tj_signal_spu_task_group(uint32_t manager_ea)
{
    static unsigned hits;
    if (!manager_ea) {
        if (hits++ < 16) fprintf(stderr, "[tj-task-wake] null manager\n");
        tj_task_wake_perf_record(0, 0, 0, 0, 0);
        return;
    }

    InterlockedExchange(&g_tj_last_task_manager_ea, (LONG)manager_ea);

    uint32_t count = vm_read32(manager_ea + 0x00);
    uint32_t taskset_ea = vm_read32(manager_ea + 0x08);
    uint32_t task_ids_ea = vm_read32(manager_ea + 0x0C);
    if (hits++ < 16) {
        fprintf(stderr,
                "[tj-task-wake] manager=0x%08X count=%u spurs=0x%08X "
                "taskset=0x%08X task_ids=0x%08X ids=%u,%u completion=%u\n",
                manager_ea, count, vm_read32(manager_ea + 0x04), taskset_ea,
                task_ids_ea,
                task_ids_ea ? vm_read32(task_ids_ea + 0x00) : 0xFFFFFFFFu,
                task_ids_ea ? vm_read32(task_ids_ea + 0x04) : 0xFFFFFFFFu,
                vm_read32(manager_ea + 0x2C));
        fflush(stderr);
    }
    if (!taskset_ea || !task_ids_ea || count > 128) {
        tj_task_wake_perf_record(manager_ea, count, taskset_ea,
                                 task_ids_ea, 0);
        tj_finish_spu_task_group_publish(manager_ea);
        return;
    }

    uint64_t generation = spu_taskset_arm_signal_generation(
        manager_ea, taskset_ea, task_ids_ea, count, 15,
        manager_ea + 0x2Cu, 1);
    if (hits <= 16) {
        fprintf(stderr,
                "[tj-task-wake] armed manager=0x%08X taskset=0x%08X "
                "generation=%llu mode=arm-only\n",
                manager_ea, taskset_ea,
                (unsigned long long)generation);
        fflush(stderr);
    }
    tj_task_wake_perf_record(manager_ea, count, taskset_ea,
                             task_ids_ea, 0);
    tj_finish_spu_task_group_publish(manager_ea);
}

/* TJ_TITLE_STATE=1: report post-update changes in the title/menu controller
 * reached by func_001E7404. The fields are raw observations, deliberately not
 * named as menu states until an input-free trace correlates guest behavior.
 * scripts/post_lift.py injects at the sole common return seam, after the
 * caller LR and result are restored while r31 still holds the menu object.
 * TJ_TITLE_GATE=1 also records only the exact prior idle tuple for the later
 * input-poll diagnostic; it does not emit raw title-state logging. */
void tj_title_menu_state_trace(ppu_context* ctx, uint32_t menu_ea,
                               uint32_t result)
{
    static const bool trace_state = getenv("TJ_TITLE_STATE") != nullptr;
    static const bool title_gate = getenv("TJ_TITLE_GATE") != nullptr;
    if (trace_state && ctx && ctx->thread_id != 1 &&
        (uint32_t)ctx->lr == 0x001F10E8u) {
        static thread_local unsigned rejected_actor_hits = 0;
        if (rejected_actor_hits++ < 2)
            fprintf(stderr, "[TJ-TITLE-ACTOR] ignored tid=%llu menu=%08X result=%08X\n",
                    (unsigned long long)ctx->thread_id, menu_ea, result);
    }
    if ((!trace_state && !title_gate) || !ctx || ctx->thread_id != 1 ||
        (uint32_t)ctx->lr != 0x001F10E8u) return;

    const uint32_t toc = (uint32_t)ctx->gpr[2];
    if (!toc || !menu_ea || menu_ea != vm_read32(toc + 0x135C)) return;

    struct snapshot {
        uint32_t result;
        uint32_t field_08;
        uint8_t field_14;
        uint8_t field_15;
        uint32_t field_18;
        uint32_t field_1c;
        uint32_t field_20;
        uint32_t field_2c;
        uint32_t field_30;
        uint32_t field_34;
        uint32_t field_38;
        uint32_t field_1ec;
        uint8_t field_1f4;
    } now = {};

    now.result = result;
    now.field_08 = vm_read32(menu_ea + 0x08);
    now.field_14 = vm_read8(menu_ea + 0x14);
    now.field_15 = vm_read8(menu_ea + 0x15);
    now.field_18 = vm_read32(menu_ea + 0x18);
    now.field_1c = vm_read32(menu_ea + 0x1C);
    now.field_20 = vm_read32(menu_ea + 0x20);
    now.field_2c = vm_read32(menu_ea + 0x2C);
    now.field_30 = vm_read32(menu_ea + 0x30);
    now.field_34 = vm_read32(menu_ea + 0x34);
    now.field_38 = vm_read32(menu_ea + 0x38);
    now.field_1ec = vm_read32(menu_ea + 0x1EC);
    now.field_1f4 = vm_read8(menu_ea + 0x1F4);

    const bool exact_idle_tuple = result == 0xFFFFFFFFu &&
        now.field_08 == 0 && now.field_14 == 0 && now.field_15 == 0 &&
        now.field_18 == 0x0Du && now.field_1c == 1 && now.field_20 == 0 &&
        now.field_2c == 0x14 && now.field_30 == 1 &&
        now.field_1ec == 0 && now.field_1f4 == 1;
    if (title_gate) {
        if (exact_idle_tuple) {
            tj_title_gate_idle_menu_ea = menu_ea;
        } else if (tj_title_gate_idle_menu_ea == menu_ea) {
            tj_title_gate_idle_menu_ea = 0;
        }
    }

    if (!trace_state) return;

    static snapshot previous = {};
    static uint32_t previous_menu = 0;
    static bool have_previous = false;
    static uint32_t consecutive = 0;
    static bool stable_reported = false;
    const bool identical = have_previous && previous_menu == menu_ea &&
        now.result == previous.result && now.field_08 == previous.field_08 &&
        now.field_14 == previous.field_14 && now.field_15 == previous.field_15 &&
        now.field_18 == previous.field_18 && now.field_1c == previous.field_1c &&
        now.field_20 == previous.field_20 && now.field_2c == previous.field_2c &&
        now.field_30 == previous.field_30 && now.field_34 == previous.field_34 &&
        now.field_38 == previous.field_38 &&
        now.field_1ec == previous.field_1ec && now.field_1f4 == previous.field_1f4;

    if (!identical) {
        consecutive = 1;
        stable_reported = false;
        fprintf(stderr,
                "[TJ-TITLE-STATE] phase=changed menu=%08X result=%08X "
                "f08=%08X f14=%02X f15=%02X f18=%08X f1c=%08X "
                "f20=%08X f2c=%08X f30=%08X f34=%08X f38=%08X f1ec=%08X f1f4=%02X\n",
                menu_ea, now.result, now.field_08, now.field_14, now.field_15,
                now.field_18, now.field_1c, now.field_20, now.field_2c,
                now.field_30, now.field_34, now.field_38, now.field_1ec,
                now.field_1f4);
        fflush(stderr);
    } else {
        consecutive++;
        if (!stable_reported && consecutive == 30) {
            fprintf(stderr,
                    "[TJ-TITLE-CONTROLLER-STABLE] stable=30 menu=%08X result=%08X\n",
                    menu_ea, now.result);
            fflush(stderr);
            stable_reported = true;
        }
    }
    previous = now;
    previous_menu = menu_ea;
    have_previous = true;
}

/* TJ_TITLE_GATE=1: This runs on the f30==1 title-controller path immediately
 * after f34's modulo-100 update and before the dynamic readiness branches.
 * It is intentionally more restrictive than the raw TJ_TITLE_STATE diagnostic
 * and emits once only. */
void tj_title_ready_input_poll(ppu_context* ctx, uint32_t menu_ea,
                               uint32_t input_ea)
{
    static const bool enabled = getenv("TJ_TITLE_GATE") != nullptr;
    struct progression {
        uint32_t menu_ea;
        uint32_t input_ea;
        uint32_t field_34;
        uint32_t consecutive;
        bool have_previous;
        bool emitted;
    };
    static progression state = {};

    const auto reset = [&]() {
        state.menu_ea = 0;
        state.input_ea = 0;
        state.field_34 = 0;
        state.consecutive = 0;
        state.have_previous = false;
    };

    if (!enabled || !ctx || ctx->thread_id != 1 || !menu_ea || !input_ea) {
        reset();
        return;
    }

    const uint32_t toc = (uint32_t)ctx->gpr[2];
    if (!toc || menu_ea != vm_read32(toc + 0x135C) ||
        tj_title_gate_current_scene_ea == 0 ||
        tj_title_gate_current_scene_ea != tj_title_gate_evidence_scene_ea ||
        tj_title_gate_idle_menu_ea != menu_ea ||
        vm_read32(menu_ea + 0x2C) != 0x14 ||
        vm_read32(menu_ea + 0x30) != 1 ||
        vm_read8(menu_ea + 0x1F4) != 1 ||
        vm_read32(menu_ea + 0x08) != 0 ||
        vm_read8(menu_ea + 0x14) != 0 ||
        vm_read8(menu_ea + 0x15) != 0 ||
        vm_read32(menu_ea + 0x18) != 0x0D ||
        vm_read32(menu_ea + 0x1C) != 1 ||
        vm_read32(menu_ea + 0x20) != 0 ||
        vm_read32(menu_ea + 0x1EC) != 0 ||
        vm_read8(input_ea + 0x5) != 0 ||
        vm_read8(input_ea + 0xE) != 0) {
        reset();
        return;
    }

    const uint32_t field_34 = vm_read32(menu_ea + 0x34);
    if (!state.have_previous || state.menu_ea != menu_ea ||
        state.input_ea != input_ea) {
        state.consecutive = 1;
    } else if (field_34 == ((state.field_34 + 1) % 100)) {
        state.consecutive++;
    } else {
        state.consecutive = 1;
    }

    state.menu_ea = menu_ea;
    state.input_ea = input_ea;
    state.field_34 = field_34;
    state.have_previous = true;
    if (!state.emitted && state.consecutive >= 30) {
        fprintf(stderr, "[TJ-HIDDEN-GATE] state=TITLE_READY\n");
        fflush(stderr);
        state.emitted = true;
        tj_title_gate_title_ready_menu_ea = menu_ea;
    }
}

static bool tj_title_save_probe_root_allowed(const char* root)
{
    char path[1024];
    if (!root || !_fullpath(path, root, sizeof(path))) return false;
    char* leaf = strrchr(path, '\\');
    if (!leaf || _strnicmp(leaf + 1, "savedata-menu-probe-", 20) != 0)
        return false;
    *leaf = 0;
    char* parent = strrchr(path, '\\');
    return parent && _stricmp(parent + 1, "candidate-runtime") == 0;
}

/* Test-only request through the original save starter, never the worker entry
 * (the worker exits its own PPU thread). ELF OPD0034C210 ->001AC41C creates
 * worker0034C248 ->001ACEB8 with the title's priority/stack/flags. Argument1
 * selects the new-save path in the title's stat callback. No progress flags or
 * serialized bytes are supplied by this probe. */
static int tj_title_save_probe_request(const char* mode, const char* root)
{
    if (!mode || strcmp(mode, "1") != 0) return 0;
    if (!tj_title_save_probe_root_allowed(root)) return -1;
    if (!g_ps3_guest_caller) return -2;
    if (vm_read32(0x0034C210) != 0x001AC41C ||
        vm_read32(0x0034C214) != 0x00359220 ||
        vm_read32(0x003591F8) != 0x0034C248 ||
        vm_read32(0x0034C248) != 0x001ACEB8 ||
        vm_read32(0x0034C24C) != 0x00359220) return -3;
    char payload[1200];
    int n = snprintf(payload, sizeof(payload), "%s/NPUA80523-AUTO-/SYS-DATA", root);
    if (n < 0 || n >= (int)sizeof(payload) ||
        GetFileAttributesA(payload) != INVALID_FILE_ATTRIBUTES) return -4;
    /* The save worker serializes memory; it does not initialize a profile.
     * Refuse the pre-new-game zero blob. Original initializer001AE2C4 sets
     * bit2 for the two starter animals at savedata offsets128 and12C. */
    if (vm_read32(0x0035925C) != 0x00BCC1E0 ||
        !(vm_read8(0x00BCC308) & 4) ||
        !(vm_read8(0x00BCC30C) & 4)) return -5;
    ps3_invoke_guest(0x0034C210, 1, 0, 0, 0, 0, 0, 0, 0);
    return 1;
}

/* TJ_TITLE_GATE=1: This runs only in the f2c==0x1E/f30==3 leaf after its
 * readiness calls have completed and before that leaf reads navigation input.
 * The marker states the observed default index only; it does not name a mode. */
void tj_main_menu_ready_input_poll(ppu_context* ctx, uint32_t menu_ea,
                                   uint32_t input_ea)
{
    static const bool enabled = getenv("TJ_TITLE_GATE") != nullptr;
    struct progression {
        uint32_t menu_ea;
        uint32_t input_ea;
        uint32_t confirm_input_ea;
        uint32_t consecutive;
        bool have_previous;
        bool emitted;
    };
    static progression state = {};

    const auto reset = [&]() {
        state.menu_ea = 0;
        state.input_ea = 0;
        state.confirm_input_ea = 0;
        state.consecutive = 0;
        state.have_previous = false;
    };

    if (!enabled || !ctx || ctx->thread_id != 1 || !menu_ea || !input_ea) {
        reset();
        return;
    }

    const uint32_t toc = (uint32_t)ctx->gpr[2];
    const uint32_t confirm_map_ea = toc ? vm_read32(toc + 0x8CC) : 0;
    const int32_t confirm_offset = confirm_map_ea ?
        (int32_t)vm_read32(confirm_map_ea) : 0;
    const uint32_t confirm_input_ea =
        input_ea + (uint32_t)confirm_offset;
    if (!toc || !confirm_map_ea || menu_ea != vm_read32(toc + 0x135C) ||
        tj_title_gate_current_scene_ea == 0 ||
        tj_title_gate_current_scene_ea != tj_title_gate_evidence_scene_ea ||
        tj_title_gate_title_ready_menu_ea != menu_ea ||
        vm_read32(menu_ea + 0x2C) != 0x1E ||
        vm_read32(menu_ea + 0x30) != 3 ||
        vm_read32(menu_ea + 0x34) != 0 ||
        vm_read32(menu_ea + 0x38) != 0 ||
        vm_read8(input_ea + 0x0) != 0 ||
        vm_read8(input_ea + 0x1) != 0 ||
        vm_read8(input_ea + 0x40) != 0 ||
        vm_read8(input_ea + 0x41) != 0 ||
        vm_read8(confirm_input_ea) != 0) {
        reset();
        return;
    }

    /* All accepted snapshots have the exact fixed tuple above, so matching
     * menu/input/confirm EAs makes this a consecutive identical seam
     * observation. */
    if (!state.have_previous || state.menu_ea != menu_ea ||
        state.input_ea != input_ea ||
        state.confirm_input_ea != confirm_input_ea) {
        state.consecutive = 1;
    } else {
        state.consecutive++;
    }
    state.menu_ea = menu_ea;
    state.input_ea = input_ea;
    state.confirm_input_ea = confirm_input_ea;
    state.have_previous = true;
    if (!state.emitted && state.consecutive >= 30) {
        fprintf(stderr,
                "[TJ-HIDDEN-GATE] state=MAIN_MENU_READY selection=0\n");
        fflush(stderr);
        state.emitted = true;
        int probe = tj_title_save_probe_request(getenv("TJ_TITLE_SAVE_PROBE"),
                                                getenv("PS3_SAVEDATA_ROOT"));
        if (probe) {
            fprintf(stderr, "[TJ-SAVEDATA-PROBE] state=%s result=%d starter=0034C210 arg=1\n",
                    probe == 1 ? "REQUESTED" : "REFUSED", probe);
            fflush(stderr);
        }
    }
}

/* Original image-17 producer: TOC-0x6764 -> binary EA 0x002B8900.
 * Observe render passes, populated groups, and descriptor construction.
 * Guest registers and memory remain untouched. */
extern "C" volatile LONG g_tj_geometry_trace_armed;
void tj_geometry_producer_trace(ppu_context* ctx, uint32_t site)
{
    static const bool enabled = getenv("TJ_GEOMETRY_STAGE_TRACE") != nullptr;
    if (!enabled || site > 3) return;
    LONG phase = InterlockedCompareExchange(&g_tj_geometry_trace_armed, 0, 0);
    if (phase < 0 || phase > 2) return;
    if (site == 3) {
        /* Match the first captured city triangle draw, then correlate its
         * emitted BEGIN address with the FIFO trace. No global draw spam. */
        if (phase != 2 || ctx->gpr[4] != 5 || ctx->gpr[5] != 138 ||
            ctx->gpr[6] != 1 || ctx->gpr[7] != 1) return;
        static volatile LONG city_draw_hits;
        const LONG city_hit = InterlockedIncrement(&city_draw_hits);
        if (city_hit > 2) return;
        fprintf(stderr, "[TJ-CITY-DRAW-PRODUCER] hit=%ld lr=%08X context=%08X begin=%08X indices=%08X",
            city_hit, (uint32_t)ctx->lr, (uint32_t)ctx->gpr[3],
            vm_read32((uint32_t)ctx->gpr[3] + 8) + 0x14, (uint32_t)ctx->gpr[8]);
        const char* dir = getenv("TJ_GEOMETRY_STAGE_TRACE");
        for (unsigned r = 14; r < 32; ++r) {
            const uint32_t ea = (uint32_t)ctx->gpr[r];
            fprintf(stderr, " r%u=%08X", r, ea);
            if (city_hit != 1 || ea < 0x10000 || ea > 0x0FFFFE00) continue;
            char path[1024];
            const int n = snprintf(path, sizeof(path), "%s\\city-producer-r%u-%08X.bin", dir, r, ea);
            FILE* f = n > 0 && n < sizeof(path) ? fopen(path, "wb") : nullptr;
            bool ok = f && fwrite(vm_base + ea, 512, 1, f) == 1;
            if (f && fclose(f) != 0) ok = false;
            if (!ok) fprintf(stderr, " dump_r%u=failed", r);
        }
        fputc('\n', stderr);
        return;
    }
    const uint32_t pass = site == 0 ? (uint32_t)ctx->gpr[5] :
                          site == 1 ? (uint32_t)ctx->gpr[6] : 7;
    const uint32_t pass_slot = pass < 7 ? pass : 7;
    static volatile LONG counts[3][3][8];
    LONG hit = InterlockedIncrement(&counts[phase][site][pass_slot]);
    /* The first two post-image18 calls can still contain only the transition
     * model. Sample later producer calls without logging every frame/group. */
    const bool later_sample = phase == 2 && site != 1 &&
        (hit == 32 || hit == 128 || hit == 512);
    if (hit > 2 && !later_sample) return;
    const uint32_t root = (uint32_t)ctx->gpr[3];
    fprintf(stderr, "[TJ-GEOMETRY-PRODUCER] phase=%ld site=%u pass=%u hit=%ld "
        "r3=%08X r4=%08X r5=%08X r6=%08X lr=%08X",
        phase, site, pass, hit, root, (uint32_t)ctx->gpr[4],
        (uint32_t)ctx->gpr[5], (uint32_t)ctx->gpr[6], (uint32_t)ctx->lr);
    if (site == 0 && root >= 0x10000 && root < 0x0FFE0000) {
        unsigned populated = 0, visible = 0, models = 0;
        unsigned active_models = 0, plain_flags = 0, bit0_flags = 0, bad_indices = 0;
        uint32_t example[2] = {0, 0};
        for (unsigned i = 0; i < 350; ++i) {
            const uint32_t group = root + 0x5F0 + i * 0xD0;
            const uint32_t count = vm_read32(group + 0x40);
            if (count) {
                ++populated;
                models += count;
                if (!vm_read8(group + 0xF)) {
                    ++visible;
                    if (count > 32) ++bad_indices;
                    for (unsigned j = 0; j < count && j < 32; ++j) {
                        const uint32_t index = vm_read32(group + 0x44 + j * 4);
                        if (index >= 128) { ++bad_indices; continue; }
                        const uint32_t model = root + 0x12250 + index * 0x200;
                        if (model > 0x0FFFFE00) { ++bad_indices; continue; }
                        const uint32_t mode = vm_read32(model + 0x18C);
                        if (!mode || (mode == 1 && pass > 3)) continue;
                        const unsigned bit0 = vm_read32(model + 0x190) & 1;
                        ++active_models;
                        if (bit0) ++bit0_flags; else ++plain_flags;
                        if (!example[bit0]) example[bit0] = model;
                    }
                }
            }
        }
        fprintf(stderr, " blocked194=%u groups=%u enabled_groups=%u models=%u",
            vm_read32(root + 0x194), populated, visible, models);
        fprintf(stderr, " active_models=%u plain_flags=%u bit0_flags=%u bad_indices=%u",
            active_models, plain_flags, bit0_flags, bad_indices);
        for (unsigned i = 0; i < 2; ++i) if (example[i])
            fprintf(stderr, " example%u=%08X/mode%u/flags%08X/param%08X", i, example[i],
                vm_read32(example[i] + 0x18C), vm_read32(example[i] + 0x190),
                vm_read32(example[i] + 0x1A0));
    } else if (site == 1) {
        const uint32_t group = (uint32_t)ctx->gpr[5];
        if (group >= 0x10000 && group < 0x10000000)
            fprintf(stderr, " group_count=%u list=%08X", vm_read32(group + 0x40),
                vm_read32(group + 0x14));
    } else if (site == 2) {
        const uint32_t mesh = (uint32_t)ctx->gpr[6];
        const uint32_t model = (uint32_t)ctx->gpr[27];
        if (mesh >= 0x10000 && mesh < 0x10000000)
            fprintf(stderr, " mesh_counts=%u/%u input_size=%u", vm_read16(mesh + 0x38),
                vm_read16(mesh + 0x3A), vm_read32(mesh + 0x44));
        if (model >= 0x10000 && model < 0x0FFFFE00) {
            const uint32_t palette = vm_read32(model + 0x130);
            const uint32_t source = vm_read32(model + 0x184);
            const uint32_t bones = vm_read32(model + 0x188);
            fprintf(stderr, " model=%08X palette=%08X source=%08X bones=%u flag=%u",
                model, palette, source, bones, vm_read8(model + 0x180));
            fprintf(stderr, " mode18c=%u flags190=%08X guest_pass=%d",
                vm_read32(model + 0x18C), vm_read32(model + 0x190),
                ctx->lr == 0x00089704 ? (int)ctx->gpr[21] : -1);
            if (phase == 2 && bones > 0 && bones <= 256) {
                const char* dir = getenv("TJ_GEOMETRY_STAGE_TRACE");
                const uint32_t size = bones * 48;
                const uint32_t addresses[] = {palette, source};
                for (unsigned i = 0; i < 2; ++i) {
                    const uint32_t ea = addresses[i];
                    if (ea < 0x10000 || ea > 0x10000000 - size) continue;
                    char path[1024];
                    int n = snprintf(path, sizeof(path), "%s\\palette-%ld-%s-%08X.bin",
                        dir, hit, i ? "source" : "draw", ea);
                    FILE* f = n > 0 && n < sizeof(path) ? fopen(path, "wb") : nullptr;
                    bool ok = f && fwrite(vm_base + ea, size, 1, f) == 1;
                    if (f && fclose(f) != 0) ok = false;
                    fprintf(stderr, " palette_%s_dump=%s", i ? "source" : "draw", ok ? "saved" : "failed");
                }
            }
        }
    }
    fputc('\n', stderr);
}

/* The aggregate error flag is written after its component registers have been
 * restored. Observe each original result before that information is lost. */
void tj_survival_loader_result_trace(ppu_context* ctx, uint32_t return_ea,
                                     uint32_t callee_ea)
{
    if (!ctx || !getenv("TJ_SCENE_STATE")) return;
    static volatile LONG logged = 0;
    if (InterlockedIncrement(&logged) > 128) return;
    fprintf(stderr,
        "[TJ-LOADER-RESULT] return=%08X callee=%08X value=%llX failed=%u tid=%llu sp=%08X\n",
        return_ea, callee_ea, (unsigned long long)ctx->gpr[3],
        (unsigned)(((ctx->gpr[3] ^ 1u) & 0xFFu) != 0),
        (unsigned long long)ctx->thread_id, (uint32_t)ctx->gpr[1]);
}

/* TJ_SCENE_STATE=1: trace the Survival scene's post-loading state machine.
 * TJ_SCENE_READY=1: emit one compact marker after that state machine returns
 * to its idle state, allowing the bounded harness to inject movement only
 * after loading and the guest's 30-frame transition have both completed.
 * This is called from an idempotent post-lift injection in func_001F0BC0.
 * It only reports field changes, plus the exact return from the loading-thread
 * completion probe, so diagnostic runs stay bounded and readable. */
void tj_survival_scene_state_trace(ppu_context* ctx, uint32_t scene_ea,
                                   uint32_t site)
{
    const bool trace_state = getenv("TJ_SCENE_STATE") != nullptr;
    const bool report_ready = getenv("TJ_SCENE_READY") != nullptr;
    const bool title_gate = getenv("TJ_TITLE_GATE") != nullptr;
    const bool trace_jobchain_after_scene =
        getenv("TJ_JOBCHAIN_TRACE_AFTER_SCENE") != nullptr;
    const bool trace_signal_after_scene =
        getenv("TJ_SPU_SIGNAL_TRACE") != nullptr;
    if (!trace_state && !report_ready && !title_gate &&
        !trace_jobchain_after_scene && !trace_signal_after_scene) return;
    if (!scene_ea) {
        InterlockedExchange(&g_tj_survival_scene_ready, 0);
        if (title_gate && ctx && ctx->thread_id == 1) {
            tj_title_gate_pending_scene_ea = 0;
            tj_title_gate_evidence_scene_ea = 0;
            tj_title_gate_current_scene_ea = 0;
            tj_title_gate_title_ready_menu_ea = 0;
        }
        return;
    }

    struct snapshot {
        uint32_t mode;
        uint32_t prior_mode;
        uint32_t state;
        uint32_t countdown;
        uint32_t thread_slot;
        uint8_t loading;
        uint8_t thread_active;
        uint8_t gate_1308;
        uint8_t gate_1324;
        uint8_t gate_1328_2c;
        uint8_t gate_1328_2d;
        uint8_t gate_1378_1;
    } now = {};

    const uint32_t toc = (uint32_t)ctx->gpr[2];
    const uint32_t p1308 = vm_read32(toc + 0x1308);
    const uint32_t p1324 = vm_read32(toc + 0x1324);
    const uint32_t p1328 = vm_read32(toc + 0x1328);
    const uint32_t p1378 = vm_read32(toc + 0x1378);

    now.mode = vm_read32(scene_ea + 0x490);
    now.prior_mode = vm_read32(scene_ea + 0x494);
    now.state = vm_read32(scene_ea + 0x498);
    now.countdown = vm_read32(scene_ea + 0x500);
    now.loading = vm_read8(scene_ea + 0x504);
    now.thread_slot = vm_read32(scene_ea + 0x4E8);
    now.thread_active = vm_read8(scene_ea + 0x4F8);
    now.gate_1308 = p1308 ? vm_read8(p1308 + 0x0E) : 0xFF;
    now.gate_1324 = p1324 ? vm_read8(p1324) : 0xFF;
    now.gate_1328_2c = p1328 ? vm_read8(p1328 + 0x2C) : 0xFF;
    now.gate_1328_2d = p1328 ? vm_read8(p1328 + 0x2D) : 0xFF;
    now.gate_1378_1 = p1378 ? vm_read8(p1378 + 1) : 0xFF;

    if (title_gate && ctx->thread_id == 1) {
        const bool exact_title_scene = now.mode == 0 &&
            now.prior_mode == 3 && now.state == 0;
        if (site == 0) {
            if (tj_title_gate_current_scene_ea != scene_ea) {
                tj_title_gate_current_scene_ea = scene_ea;
                tj_title_gate_pending_scene_ea = 0;
                tj_title_gate_evidence_scene_ea = 0;
                tj_title_gate_title_ready_menu_ea = 0;
            } else if (tj_title_gate_pending_scene_ea != scene_ea &&
                       !exact_title_scene) {
                tj_title_gate_pending_scene_ea = 0;
                tj_title_gate_evidence_scene_ea = 0;
                tj_title_gate_title_ready_menu_ea = 0;
            }
        } else if (site == 3) {
            if (tj_title_gate_current_scene_ea == scene_ea &&
                (uint32_t)ctx->gpr[3] != 0) {
                /* Site 3 captures success; Site 2 promotes after the zero write. */
                tj_title_gate_pending_scene_ea = scene_ea;
            } else {
                tj_title_gate_pending_scene_ea = 0;
                if (tj_title_gate_evidence_scene_ea == scene_ea) {
                    tj_title_gate_evidence_scene_ea = 0;
                    tj_title_gate_title_ready_menu_ea = 0;
                }
            }
        } else if (site == 2 &&
                   tj_title_gate_current_scene_ea == scene_ea &&
                   tj_title_gate_pending_scene_ea == scene_ea) {
            if (exact_title_scene) {
                tj_title_gate_evidence_scene_ea = scene_ea;
            } else if (tj_title_gate_evidence_scene_ea == scene_ea) {
                tj_title_gate_evidence_scene_ea = 0;
                tj_title_gate_title_ready_menu_ea = 0;
            }
            tj_title_gate_pending_scene_ea = 0;
        }
    }

    static uint32_t ready_scene = 0;
    static bool saw_loading = false;
    static bool ready_reported = false;
    if (ready_scene != scene_ea || now.mode != 2) {
        InterlockedExchange(&g_tj_survival_scene_ready, 0);
        ready_scene = scene_ea;
        saw_loading = false;
        ready_reported = false;
    }
    if (now.mode == 2 && (now.loading || now.thread_active))
        saw_loading = true;
    if ((report_ready || trace_jobchain_after_scene || trace_signal_after_scene) && saw_loading &&
        !ready_reported && now.mode == 2 &&
        now.state == 0 && now.countdown == 0 && !now.loading &&
        !now.thread_active) {
        if (report_ready) {
            fprintf(stderr,
                    "[TJ-SCENE-READY] scene=%08X mode=%u state=%u\n",
                    scene_ea, now.mode, now.state);
            fflush(stderr);
        }
        tj_jobchain_trace_arm_after_scene();
        spu_taskset_signal_trace_arm_after_scene();
        ready_reported = true;
        if (report_ready)
            InterlockedExchange(&g_tj_survival_scene_ready, 1);
    }

    static snapshot previous = {};
    static uint32_t previous_scene = 0;
    static unsigned samples = 0;
    static uint32_t previous_probe_result = UINT32_MAX;
    const bool probe_changed = site == 1 &&
        previous_probe_result != (uint32_t)ctx->gpr[3];
    if (site == 1) previous_probe_result = (uint32_t)ctx->gpr[3];
    const bool changed = previous_scene != scene_ea ||
                         memcmp(&now, &previous, sizeof(now)) != 0;
    if (trace_state && (changed || probe_changed || samples < 4)) {
        fprintf(stderr,
                "[TJ-SCENE-STATE] site=%u scene=%08X probe_r3=%u "
                "mode=%u prior=%u state=%u countdown=%u loading=%u "
                "thread_slot=%u active=%u gates=%u/%u/%u/%u/%u tid=%llu\n",
                site, scene_ea, (uint32_t)ctx->gpr[3], now.mode,
                now.prior_mode, now.state, now.countdown, now.loading,
                now.thread_slot, now.thread_active, now.gate_1308,
                now.gate_1324, now.gate_1328_2c, now.gate_1328_2d,
                now.gate_1378_1, (unsigned long long)ctx->thread_id);
        fflush(stderr);
    }
    previous = now;
    previous_scene = scene_ea;
    samples++;
}

/* TJ_FADE_STATE=1: trace the exact early-return gates in func_00183524, the
 * full-screen fade renderer used while Survival transitions out of loading.
 * scripts/post_lift.py calls this after each gate without changing guest state. */
void tj_survival_fade_state_trace(ppu_context* ctx, uint32_t fade_ea,
                                  uint32_t site)
{
    if (!getenv("TJ_FADE_STATE") || !fade_ea || site > 4) return;

    struct snapshot {
        uint32_t result;
        uint32_t gfx_flags;
        uint32_t queue_head;
        uint32_t queue_aux;
        uint32_t progress;
        uint32_t work_count;
        uint8_t done;
        uint8_t alternate_done;
    } now = {};

    const uint32_t toc = (uint32_t)ctx->gpr[2];
    const uint32_t gfx_ea = vm_read32(toc - 0xC80u);
    now.result = (uint32_t)ctx->gpr[3];
    now.gfx_flags = gfx_ea ? vm_read32(gfx_ea + 0x08) : 0xFFFFFFFFu;
    now.queue_head = gfx_ea ? vm_read32(gfx_ea + 0x6C8) : 0xFFFFFFFFu;
    now.queue_aux = gfx_ea ? vm_read32(gfx_ea + 0x6CC) : 0xFFFFFFFFu;
    now.progress = vm_read32(fade_ea + 0x28);
    now.work_count = site >= 2 ? vm_read32((uint32_t)ctx->gpr[1] + 0xC4)
                               : 0xFFFFFFFFu;
    now.done = vm_read8(fade_ea + 0x2C);
    now.alternate_done = vm_read8(fade_ea + 0x2D);

    static snapshot previous[5] = {};
    static bool have_previous[5] = {};
    static unsigned hits[5] = {};
    const unsigned hit = ++hits[site];
    const bool changed = !have_previous[site] ||
                         memcmp(&now, &previous[site], sizeof(now)) != 0;
    if (changed || hit <= 4 || (hit % 600) == 0) {
        fprintf(stderr,
                "[TJ-FADE-STATE] site=%u n=%u fade=%08X r3=%08X "
                "gfx=%08X flags=%08X queue=%u/%u progress=%u work=%u "
                "done=%u/%u\n",
                site, hit, fade_ea, now.result, gfx_ea, now.gfx_flags,
                now.queue_head, now.queue_aux, now.progress, now.work_count,
                now.done, now.alternate_done);
        fflush(stderr);
    }
    previous[site] = now;
    have_previous[site] = true;
}

void tj_crt_skip(ppu_context* ctx, const char* what)
{
    static int hits;
    if (hits < 64) { printf("[CRT] skip %s\n", what); hits++; }
    ctx->gpr[3] = 0;
}

/* Guest formatter bridge used by func_00245AC0.
 *
 * Its lifted callers normalize the ABI to:
 *   r4 = destination, r5 = format, r6 = pointer to 8-byte GPR vararg slots.
 * The original formatter depends on CRT stdio lock objects that are still
 * deliberately skipped during bring-up. Returning zero left every formatted
 * path empty; the title then fopen("") and parsed an invalid stream buffer.
 *
 * This implements the ordinary integer/string/pointer/float conversions used
 * by title paths and diagnostics. It is intentionally bounded: the guest
 * wrapper appends the final NUL using the returned length. */
extern "C" void tj_guest_vformat(ppu_context* ctx)
{
    const uint32_t dst_ea = (uint32_t)ctx->gpr[4];
    const uint32_t fmt_ea = (uint32_t)ctx->gpr[5];
    uint32_t ap = (uint32_t)ctx->gpr[6];
    char out[8192];
    size_t used = 0;

    if (!dst_ea || !fmt_ea || !ap) {
        ctx->gpr[3] = 0;
        return;
    }

    const char* fmt = (const char*)(vm_base + fmt_ea);
    auto next_arg = [&ap]() -> uint64_t {
        uint64_t value = vm_read64(ap);
        ap += 8;
        return value;
    };
    auto append = [&out, &used](const char* s, size_t n) {
        size_t room = sizeof(out) - 1 - used;
        if (n > room) n = room;
        if (n) memcpy(out + used, s, n);
        used += n;
    };

    for (size_t i = 0; fmt[i] && used < sizeof(out) - 1; ) {
        if (fmt[i] != '%') {
            out[used++] = fmt[i++];
            continue;
        }
        i++;
        if (fmt[i] == '%') {
            out[used++] = '%';
            i++;
            continue;
        }

        char flags[8] = {};
        size_t nf = 0;
        while (fmt[i] && strchr("-+ #0", fmt[i]) && nf + 1 < sizeof(flags))
            flags[nf++] = fmt[i++];

        int width = -1;
        if (fmt[i] == '*') {
            width = (int)(int32_t)next_arg();
            i++;
        } else if (fmt[i] >= '0' && fmt[i] <= '9') {
            width = 0;
            while (fmt[i] >= '0' && fmt[i] <= '9')
                width = width * 10 + (fmt[i++] - '0');
        }

        int precision = -1;
        if (fmt[i] == '.') {
            i++;
            precision = 0;
            if (fmt[i] == '*') {
                precision = (int)(int32_t)next_arg();
                i++;
            } else {
                while (fmt[i] >= '0' && fmt[i] <= '9')
                    precision = precision * 10 + (fmt[i++] - '0');
            }
        }

        if (fmt[i] == 'h' || fmt[i] == 'l') {
            char first = fmt[i++];
            if (fmt[i] == first) i++;
        } else if (strchr("jztL", fmt[i])) {
            i++;
        }

        const char conv = fmt[i] ? fmt[i++] : '\0';
        char spec[48];
        int sp = snprintf(spec, sizeof(spec), "%%%s", flags);
        if (width >= 0) sp += snprintf(spec + sp, sizeof(spec) - (size_t)sp, "%d", width);
        if (precision >= 0) sp += snprintf(spec + sp, sizeof(spec) - (size_t)sp, ".%d", precision);

        char piece[4096];
        int count = 0;
        if (conv == 's') {
            uint32_t src_ea = (uint32_t)next_arg();
            const char* src = src_ea ? (const char*)(vm_base + src_ea) : "(null)";
            snprintf(spec + sp, sizeof(spec) - (size_t)sp, "s");
            count = snprintf(piece, sizeof(piece), spec, src);
        } else if (conv == 'c') {
            snprintf(spec + sp, sizeof(spec) - (size_t)sp, "c");
            count = snprintf(piece, sizeof(piece), spec, (int)(uint32_t)next_arg());
        } else if (strchr("di", conv)) {
            snprintf(spec + sp, sizeof(spec) - (size_t)sp, "lld");
            count = snprintf(piece, sizeof(piece), spec, (long long)(int64_t)next_arg());
        } else if (strchr("uoxX", conv)) {
            snprintf(spec + sp, sizeof(spec) - (size_t)sp, "ll%c", conv);
            count = snprintf(piece, sizeof(piece), spec, (unsigned long long)next_arg());
        } else if (conv == 'p') {
            snprintf(spec + sp, sizeof(spec) - (size_t)sp, "llX");
            count = snprintf(piece, sizeof(piece), spec, (unsigned long long)next_arg());
        } else if (strchr("aAeEfFgG", conv)) {
            uint64_t bits = next_arg();
            double value;
            memcpy(&value, &bits, sizeof(value));
            snprintf(spec + sp, sizeof(spec) - (size_t)sp, "%c", conv);
            count = snprintf(piece, sizeof(piece), spec, value);
        } else {
            piece[0] = '%';
            piece[1] = conv;
            piece[2] = 0;
            count = conv ? 2 : 1;
        }

        if (count > 0) {
            size_t n = (size_t)count;
            if (n >= sizeof(piece)) n = sizeof(piece) - 1;
            append(piece, n);
        }
    }

    out[used] = 0;
    memcpy(vm_base + dst_ea, out, used);
    ctx->gpr[3] = (uint32_t)used;

    if (getenv("TJ_FMTDBG")) {
        static int reports = 0;
        if (reports++ < 32) {
            fprintf(stderr, "[TJFMT] fmt='%s' -> '%s' (%zu)\n", fmt, out, used);
            fflush(stderr);
        }
    }
}

/* ---------------------------------------------------------------------------
 * libc heap redirect
 *
 * The game's allocator is dlmalloc over a single mspace whose pointer is baked
 * into .data at TOC+0x2A98 = 0x0035BCB8 -> 0x00C0E000, the last object in BSS.
 * That malloc_state is never initialised: it reads as all zeroes for the whole
 * boot, so ok_magic() fails and every allocation returns 0. operator new
 * (func_0023D1FC) then throws and the game aborts with "exception: bad\n * allocation" before it ever reaches graphics init.
 *
 * Until the mspace is genuinely created, point the eight public heap wrappers
 * at the HLE bump allocator (import_resolver.h). Every one of them currently
 * returns 0, so this cannot regress anything.
 *
 * The wrappers were identified from the binary, not from the (wrong) comment
 * tables -- all eight load the mspace handle from TOC+0x2A98 as their first
 * argument:
 *   0x24DDC0 malloc(size)          25 call sites
 *   0x24DD94 free(ptr)            100 call sites; its inner func_0024A7E8 is
 *                                 the one that carries the "mspace_free" string
 *   0x24DCEC memalign(align,size)  50 call sites, both observed passing a
 *                                 constant r3=0x80 -- a 128-byte alignment
 *   0x24DD24 realloc(ptr,size)     its inner calls a free wrapper
 *   0x24DCBC free (internal)       only called from realloc's inner
 *
 * ponytail: bump allocator, no reuse. Fine for bring-up; give the guest mspace
 * real memory when the boot gets far enough to care about fragmentation.
 */
/* Defined in src/import_resolver.h, compiled into main.cpp as C++ -- so these
 * are C++-mangled, not extern "C". */
uint32_t hle_malloc(uint32_t size);
uint32_t hle_memalign(uint32_t align, uint32_t size);
uint32_t hle_calloc(uint32_t count, uint32_t size);
void     hle_free(uint32_t addr);
uint32_t hle_allocation_size(uint32_t addr);

static void tj_heap_log(const char* what, uint32_t a, uint32_t b, uint32_t r)
{
    static int hits;
    if (hits < 24) { printf("[heap] %s(0x%X, 0x%X) -> 0x%08X\n", what, a, b, r); hits++; }
}

void tj_heap_malloc(ppu_context* ctx)
{
    uint32_t n = (uint32_t)ctx->gpr[3];
    uint32_t p = hle_malloc(n);
    tj_heap_log("malloc", n, 0, p);
    ctx->gpr[3] = p;
}

void tj_heap_free(ppu_context* ctx)
{
    hle_free((uint32_t)ctx->gpr[3]);
    ctx->gpr[3] = 0;
}

/* func_0024DCEC is memalign(alignment, size): both observed call sites pass a
 * constant r3=0x80. Treating a large alignment as a calloc element count sent
 * the 1 MB-aligned RSX IO region down the calloc path and returned an
 * unaligned pointer -- the FIFO then overlapped live objects and the game
 * walked its own command buffer as an array of object pointers.
 *
 * Zero the block anyway: memalign does not promise it, but the guest heap this
 * replaces handed back memory the game expected to be clean, and a stale
 * pointer read out of a fresh allocation is far more expensive to debug than
 * the memset. */
void tj_heap_memalign(ppu_context* ctx)
{
    uint32_t align = (uint32_t)ctx->gpr[3];
    uint32_t size  = (uint32_t)ctx->gpr[4];
    uint32_t p = hle_memalign(align, size);
    if (p) memset(vm_base + p, 0, size);
    tj_heap_log("memalign", align, size, p);
    ctx->gpr[3] = p;
}

/* Preserve realloc semantics with allocator-tracked block sizes. Copying the
 * requested new size from a smaller old block read into the next allocation;
 * never freeing the old block also caused the title-to-menu heap exhaustion. */
void tj_heap_realloc(ppu_context* ctx)
{
    uint32_t old = (uint32_t)ctx->gpr[3];
    uint32_t n   = (uint32_t)ctx->gpr[4];
    if (!n) {
        hle_free(old);
        tj_heap_log("realloc", old, n, 0);
        ctx->gpr[3] = 0;
        return;
    }
    uint32_t old_size = hle_allocation_size(old);
    uint32_t p   = hle_malloc(n);
    if (p && old && old_size) {
        uint32_t copy = old_size < n ? old_size : n;
        memcpy(vm_base + p, vm_base + old, copy);
        hle_free(old);
    }
    tj_heap_log("realloc", old, n, p);
    ctx->gpr[3] = p;
}

/* ---------------------------------------------------------------------------
 * abort() with a usable backtrace
 *
 * The guest's own abort prints "abort() is called from 0x..." by walking its
 * stack -- and when the reason for aborting is a corrupted stack, that walker
 * runs off into garbage and takes an access violation, so the crash we see is
 * the reporter, not the fault. Redirect abort to the runtime's own guest-stack
 * scanner (it resolves saved return addresses through function_table) and then
 * leave, so the log names the actual guest chain.
 */
extern "C" void ppu_dump_guest_stack(ppu_context* ctx, const char* tag);

/* The HOST stack is the guest call chain: lifted functions call each other
 * directly, so a host return address inside func_XXXXXXXX's body identifies
 * that guest function. Resolve each frame to the entry whose body address is
 * the largest <= the return address. This works where the guest-stack scan
 * does not, because the lifted ABI does not maintain guest lr. */
static uint32_t tj_guest_of_host_ra(uintptr_t ra, uintptr_t* off)
{
    uintptr_t best = 0;
    uint32_t best_guest = 0;
    for (uint64_t i = 0; i < function_table_count; i++) {
        uintptr_t hf = (uintptr_t)function_table[i].func;
        if (hf && hf <= ra && hf > best) {
            best = hf;
            best_guest = (uint32_t)function_table[i].addr;
        }
    }
    if (off) *off = ra - best;
    return best_guest;
}

void tj_abort(ppu_context* ctx)
{
    fprintf(stderr, "\n[TJ] guest abort() -- r3=0x%08X r4=0x%08X r31=0x%08X\n",
            (uint32_t)ctx->gpr[3], (uint32_t)ctx->gpr[4], (uint32_t)ctx->gpr[31]);

    /* The pure-virtual placeholder reaches abort with `this` still in r3.
     * Dump the object head and its vtable: a vtable slot pointing back at
     * func_0025279C is __cxa_pure_virtual, i.e. this object still carries an
     * abstract base's vptr because the concrete subclass was never
     * constructed (or its constructor did not complete). */
    {
        uint32_t obj = (uint32_t)ctx->gpr[3];
        uint32_t vt  = obj ? vm_read32(obj) : 0;
        fprintf(stderr, "[TJ] this=0x%08X vptr=0x%08X\n", obj, vt);
        if (obj) {
            fprintf(stderr, "      obj[0..7]:");
            for (int i = 0; i < 8; i++) fprintf(stderr, " %08X", vm_read32(obj + i*4));
            fprintf(stderr, "\n");
        }
        if (vt) {
            fprintf(stderr, "      vtable[0..9]:");
            for (int i = 0; i < 10; i++) fprintf(stderr, " %08X", vm_read32(vt + i*4));
            fprintf(stderr, "\n");
        }
    }

    void* bt[64];
    unsigned short n = RtlCaptureStackBackTrace(0, 64, bt, 0);
    fprintf(stderr, "[TJ] guest chain (resolved from the host stack):\n");
    for (unsigned short i = 0; i < n; i++) {
        uintptr_t off = 0;
        uint32_t g = tj_guest_of_host_ra((uintptr_t)bt[i], &off);
        if (g && off < 0x4000)
            fprintf(stderr, "      func_%08X+0x%llX\n", g,
                    (unsigned long long)off);
    }
    fflush(stderr);
    exit(4);
}

/* __cxa_pure_virtual (func_0025279C) -- called when a virtual dispatch lands
 * on a slot the class leaves pure. The display object at 0x00BCB2A0 carries
 * vtable 0x00342130, whose slots 5 and 6 are pure and for which this binary
 * contains no derived class, so reaching one means the boot took a path it
 * should not have. The real fix is upstream of here; treating it as fatal
 * just hides everything that follows, so log it (with `this`) and return 0
 * the way the runtime's garbage-vcall guard does.
 *
 * ponytail: diagnostic band-aid. Skipped methods leave state incomplete --
 * remove this once the path that reaches a pure slot is understood.
 */
void tj_pure_virtual(ppu_context* ctx)
{
    static int hits;
    if (hits < 16) {
        fprintf(stderr, "[TJ] pure virtual on this=0x%08X (vptr=0x%08X) lr=0x%08X -- returning 0\n",
                (uint32_t)ctx->gpr[3],
                ctx->gpr[3] ? vm_read32((uint32_t)ctx->gpr[3]) : 0, (uint32_t)ctx->lr);
        hits++;
    }
    ctx->gpr[3] = 0;
}

/* Guest chain for a host access violation.
 *
 * Must be called from the __except FILTER, not the handler body: the filter
 * runs on the faulting stack before unwinding, so the lifted guest frames are
 * still there to walk. By the time the handler body runs they are gone.
 */
/* TJ_PEEK=<hex>[,<words>]: dump guest memory at the fault. Most "wild pointer"
 * faults trace back to a table the guest built, and the fastest way to see
 * which entry is wrong is to look at it. */
static void tj_peek(void)
{
    const char* e = getenv("TJ_PEEK");
    if (!e || !*e) return;
    char* end = 0;
    unsigned long addr = strtoul(e, &end, 16);
    unsigned n = 16;
    if (end && *end == ',') n = (unsigned)strtoul(end + 1, 0, 0);
    if (n > 256) n = 256;
    fprintf(stderr, "[TJ] peek 0x%08lX:", addr);
    for (unsigned i = 0; i < n; i++) {
        if (i % 8 == 0) fprintf(stderr, "\n   +0x%03X:", i * 4);
        fprintf(stderr, " %08X", vm_read32((uint32_t)addr + i * 4));
    }
    fprintf(stderr, "\n");
}

extern "C" int tj_crash_filter(unsigned long code, void* exception_info)
{
    tj_peek();
    EXCEPTION_POINTERS* ep = (EXCEPTION_POINTERS*)exception_info;
    if (ep && ep->ExceptionRecord && ep->ContextRecord) {
        const EXCEPTION_RECORD* er = ep->ExceptionRecord;
        const uintptr_t rip = (uintptr_t)ep->ContextRecord->Rip;
        const uintptr_t image = (uintptr_t)GetModuleHandleA(NULL);
        uintptr_t host_off = 0;
        uint32_t guest = tj_guest_of_host_ra(rip, &host_off);
        const char* access = "execute";
        uintptr_t target = 0;
        if (er->NumberParameters >= 2) {
            access = er->ExceptionInformation[0] == 0 ? "read" :
                     er->ExceptionInformation[0] == 1 ? "write" : "execute";
            target = (uintptr_t)er->ExceptionInformation[1];
        }
        fprintf(stderr,
                "\n[TJ] AV %s target=0x%llX rip_rva=0x%llX func_%08X+0x%llX\n",
                access, (unsigned long long)target,
                (unsigned long long)(rip - image), guest,
                (unsigned long long)host_off);
    }
    void* bt[64];
    unsigned short n = RtlCaptureStackBackTrace(0, 64, bt, 0);
    fprintf(stderr, "\n[TJ] fault 0x%08lX -- guest chain:\n", code);
    for (unsigned short i = 0; i < n; i++) {
        if (i < 24)
            fprintf(stderr, "      host_rva=0x%llX\n",
                    (unsigned long long)((uintptr_t)bt[i] -
                                         (uintptr_t)GetModuleHandleA(NULL)));
        uintptr_t off = 0;
        uint32_t g = tj_guest_of_host_ra((uintptr_t)bt[i], &off);
        if (g && off < 0x4000)
            fprintf(stderr, "      func_%08X+0x%llX\n", g,
                    (unsigned long long)off);
    }
    fflush(stderr);
    return 1;   /* EXCEPTION_EXECUTE_HANDLER */
}

/* main.cpp's SEH wrapper only covers the main PPU thread.  A fault in an
 * emulated sys_ppu_thread otherwise terminates the process before its guest
 * context is reported.  Register this VEH only for focused diagnostic runs;
 * it records the first-chance AV, then lets Windows continue normal exception
 * dispatch so this logger never hides or changes the crash. */
extern "C" LONG CALLBACK tj_vectored_crash_logger(EXCEPTION_POINTERS* ep)
{
    if (!ep || !ep->ExceptionRecord ||
        ep->ExceptionRecord->ExceptionCode != EXCEPTION_ACCESS_VIOLATION) {
        return EXCEPTION_CONTINUE_SEARCH;
    }

    static volatile LONG reporting = 0;
    if (InterlockedCompareExchange(&reporting, 1, 0) != 0) {
        return EXCEPTION_CONTINUE_SEARCH;
    }

    fprintf(stderr, "\n[TJ] vectored worker AV tid=%lu\n",
            (unsigned long)GetCurrentThreadId());
    tj_crash_filter(EXCEPTION_ACCESS_VIOLATION, ep);
    InterlockedExchange(&reporting, 0);
    return EXCEPTION_CONTINUE_SEARCH;
}
