/* Present / vblank ticker.
 *
 * Nothing in Tokyo Jungle's harness was advancing the display. The game
 * requests a flip (_cellGcmSetFlipCommand), then polls cellGcmGetFlipStatus
 * until it clears -- and it never did, because cellGcmTickFlip is what clears
 * it and no thread was calling it. The boot sat in that poll forever.
 *
 * ps3recomp's own harness (runtime/ppu/tests/boot_main.cpp) and the LBP port
 * both run a thread like this; TJ has its own main() and so needs its own.
 *
 * The ticks are driven off real elapsed time rather than off how long
 * present() takes: on a hidden or occluded window DXGI throttles Present
 * hard, and pacing the guest's frame counter behind it drops the whole boot
 * to a few frames a second.
 *
 * VBlank deadlines use a rational nominal-60-Hz cadence. The outer FIFO
 * drain cadence and the per-tick flip/drain order remain load-bearing for
 * titles that fence every render pass. TJ_VBLANK_SOURCE=display (opt-in)
 * fires the same per-tick work from the monitor's real vblanks instead, with
 * a QPC watchdog (tj_vblank_source.h, backend display waiter).
 * TJ_PRESENT_PHASE=1 (opt-in, display source only) presents vsync frames
 * with SyncInterval = display divisor; TJ_PRESENT_PHASE=hold instead keeps an
 * off-tick frame's swap-chain Present until right after the next tick
 * (TjPresentPhase); tickets, completions and drains do not move.
 */
#include <stdio.h>
#include <stdarg.h>
#include <stdlib.h>
#include <string.h>
#include <windows.h>
#include <mmsystem.h>
#include "tj_vblank_cadence.h"
#include "tj_vblank_source.h"

#include "ppu_context.h"
#include "sys_ppu_thread.h"
#include "guest_world_rendezvous.h"

extern "C" uint32_t tj_resolve_host_rip(uintptr_t rip, uintptr_t* out_offset);
extern "C" HANDLE tj_frame_prof_main_thread(void);
extern "C" volatile long g_rsx_survival_trace_armed;
extern "C" void* cellGcm_fifo_kick_event(void);
static HANDLE g_tj_present_thread = NULL;
static int g_tj_fifo_kick_wait_requested = 0;

/* TJ_FIFO_KICK_WAIT=1 opts into the shared process-owned GCM dry-queue event.
 * Keep event creation ahead of the interrupt worker, which can poll the guest
 * reference register as soon as it starts. */
static HANDLE tj_fifo_kick_wait_event(int* requested)
{
    const char* setting = getenv("TJ_FIFO_KICK_WAIT");
    *requested = setting && strcmp(setting, "1") == 0;
    return *requested ? (HANDLE)cellGcm_fifo_kick_event() : NULL;
}

/* The presenter's 1 ms nap. With the display VBlank source it also ends on
 * the waiter's tick event; NULL keeps the plain Sleep(1). */
static void tj_present_nap(HANDLE vblank_event)
{
    if (!vblank_event || WaitForSingleObject(vblank_event, 1) == WAIT_FAILED)
        Sleep(1);
}

static void tj_fifo_kick_wait_once(HANDLE event, int* active,
                                   int* failure_logged, uint64_t* wakes,
                                   uint64_t* timeouts, uint64_t* fallbacks,
                                   DWORD timeout_ms, HANDLE vblank_event)
{
    if (!event) {
        if (timeout_ms) tj_present_nap(vblank_event);
        return;
    }
    if (!*active) {
        if (fallbacks) ++*fallbacks;
        if (timeout_ms) tj_present_nap(vblank_event);
        return;
    }

    HANDLE handles[2] = {event, vblank_event};
    DWORD result = vblank_event
        ? WaitForMultipleObjects(2, handles, FALSE, timeout_ms)
        : WaitForSingleObject(event, timeout_ms);
    DWORD error = result == WAIT_FAILED ? GetLastError() : 0;
    if (vblank_event && result == WAIT_OBJECT_0 + 1) return;   /* display VBlank */
    if (result == WAIT_OBJECT_0) {
        if (wakes) ++*wakes;
        return;
    }
    if (result == WAIT_TIMEOUT) {
        if (timeouts) ++*timeouts;
        return;
    }

    *active = 0;
    if (fallbacks) ++*fallbacks;
    if (!*failure_logged) {
        *failure_logged = 1;
        fprintf(stderr,
                "[TJ-FIFO-KICK-WAIT] event=disabled result=%lu error=%lu fallback_wait_ms=%lu\n",
                (unsigned long)result, (unsigned long)error,
                (unsigned long)timeout_ms);
        fflush(stderr);
    }
    if (timeout_ms) tj_present_nap(vblank_event);
}

static uint64_t tj_vblank_clock_now(TjVBlankCadence* cadence,
                                    bool* qpc_active, uint64_t* frequency)
{
    if (*qpc_active) {
        LARGE_INTEGER sample = {0};
        if (QueryPerformanceCounter(&sample) && sample.QuadPart >= 0)
            return (uint64_t)sample.QuadPart;
        /* Rebase explicitly into one clock domain; never compare a stale QPC
         * deadline with millisecond fallback timestamps. */
        *qpc_active = false;
        *frequency = 1000ULL;
        uint64_t now = (uint64_t)GetTickCount64();
        tj_vblank_cadence_rearm_after_clock_switch(cadence, now, *frequency);
        fprintf(stderr, "[TJ] VBlank QPC read failed; switched to GetTickCount64 fallback\n");
        return now;
    }
    return (uint64_t)GetTickCount64();
}

static bool tj_survival_trace_is_armed()
{
    return InterlockedCompareExchange(&g_rsx_survival_trace_armed, 0, 0) != 0;
}

/* Matches CellGcmFrameProfSnapshot in ps3recomp's cellGcmSys.h. Keep this
 * small C ABI bridge local: title code does not otherwise consume HLE internals. */
struct tj_gcm_frame_prof_snapshot {
    uint32_t generation;
    uint32_t flip_sequence;
    uint32_t source_kind;
    uint32_t source_host_tid;
    uint32_t put;
    uint32_t get;
    uint32_t current;
};

/* Matches CellGcmFlipTicket.  Keep the host-only ABI bridge local like the
 * frame-profiler snapshot above. */
struct tj_gcm_flip_ticket {
    uint64_t generation;
    uint32_t buffer_id;
    uint32_t source;
};
enum {
    TJ_GCM_FLIP_PUBLICATION_QPC_OFF = 0,
    TJ_GCM_FLIP_PUBLICATION_QPC_VALID = 1,
    TJ_GCM_FLIP_PUBLICATION_QPC_FAILED = 2
};
struct tj_gcm_flip_publication_timing {
    uint64_t publication_qpc;
    uint32_t publication_qpc_state;
};

extern "C" void cellGcm_frame_prof_snapshot(tj_gcm_frame_prof_snapshot* out);
extern "C" {
extern unsigned int cellGcm_flip_request_count(void);
int cellGcm_flip_consumed_ticket_snapshot(tj_gcm_flip_ticket* out_ticket);
unsigned int cellGcmGetCurrentDisplayBufferId(void);
void spu_taskset_signal_snapshot(FILE* out);
void spu_completion_atomic_history_snapshot(FILE* out, uint32_t completion_ea);
extern volatile long g_tj_survival_scene_ready;
extern volatile long g_tj_last_task_manager_ea;
extern volatile long g_tj_jobguard_stage[64];
extern volatile unsigned int g_tj_jobguard_guard[64];
extern volatile unsigned int g_tj_jobguard_old[64];
extern volatile unsigned int g_tj_jobguard_zero[64];
extern volatile unsigned int g_tj_jobguard_chain[64];
}

static uint64_t tj_thread_cpu_us(HANDLE thread)
{
    FILETIME created, exited, kernel, user;
    if (!thread || !GetThreadTimes(thread, &created, &exited, &kernel, &user))
        return 0;
    ULARGE_INTEGER k = {}, u = {};
    k.LowPart = kernel.dwLowDateTime;
    k.HighPart = kernel.dwHighDateTime;
    u.LowPart = user.dwLowDateTime;
    u.HighPart = user.dwHighDateTime;
    return (k.QuadPart + u.QuadPart) / 10u;
}

static const char* tj_rip_module(uintptr_t rip, char* name, size_t size,
                                uintptr_t* out_base = nullptr)
{
    if (out_base) *out_base = 0;
    HMODULE module = NULL;
    if (!GetModuleHandleExA(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS |
                            GET_MODULE_HANDLE_EX_FLAG_UNCHANGED_REFCOUNT,
                            reinterpret_cast<LPCSTR>(rip), &module) ||
        !GetModuleFileNameA(module, name, (DWORD)size)) {
        snprintf(name, size, "unknown");
        return name;
    }
    if (out_base) *out_base = reinterpret_cast<uintptr_t>(module);
    const char* slash = strrchr(name, '\\');
    return slash ? slash + 1 : name;
}

/* TJ_THREADS=1: every 2 s, briefly suspend each selected PPU host thread and
 * report its exact host RIP, resolved lifted guest function, live guest
 * registers, last syscall/HLE callsite, and CPU delta. TJ_THREADS_FILTER can
 * restrict this to one thread name (for example, loading_thread). */
static void tj_dump_threads(void)
{
    static int on = -1;
    if (on < 0) on = getenv("TJ_THREADS") ? 1 : 0;
    if (!on) return;

    static ULONGLONG next = 0;
    ULONGLONG now = GetTickCount64();
    if (now < next) return;
    next = now + 2000;

    static const char* kState[] = { "FREE", "RUNNING", "FINISHED", "DETACHED" };
    static uint64_t last_cpu[PPU_THREAD_MAX] = {};
    const char* filter = getenv("TJ_THREADS_FILTER");
    fprintf(stderr, "[threads]\n");
    for (int i = 0; i < PPU_THREAD_MAX; i++) {
        ppu_thread_info* t = &g_ppu_threads[i];
        if (t->state == PPU_THREAD_STATE_FREE) continue;
        if (filter && *filter && !strstr(t->name, filter)) continue;

        HANDLE thread = NULL;
        if (t->host_thread) {
            DuplicateHandle(GetCurrentProcess(), t->host_thread,
                            GetCurrentProcess(), &thread, 0, FALSE,
                            DUPLICATE_SAME_ACCESS);
        }

        CONTEXT hc = {};
        hc.ContextFlags = CONTEXT_CONTROL;
        bool sampled = false;
        ppu_context guest = {};
        if (thread && SuspendThread(thread) != (DWORD)-1) {
            sampled = GetThreadContext(thread, &hc) != 0;
            guest = t->ctx;
            ResumeThread(thread);
        }

        uint64_t cpu = tj_thread_cpu_us(thread);
        uint64_t cpu_delta = last_cpu[i] && cpu >= last_cpu[i]
                           ? cpu - last_cpu[i] : 0;
        last_cpu[i] = cpu;

        uintptr_t host_offset = 0;
        uint32_t guest_func = sampled
                            ? tj_resolve_host_rip((uintptr_t)hc.Rip, &host_offset)
                            : 0;
        char module_path[MAX_PATH] = {};
        const char* module = sampled
                           ? tj_rip_module((uintptr_t)hc.Rip, module_path,
                                           sizeof(module_path))
                           : "unsampled";

        fprintf(stderr,
                "   tid=%d %-9s name=\"%s\" entry=0x%08llX "
                "cpu/2s=%lluus rip=0x%llX module=%s ",
                i + 1, kState[t->state & 3], t->name,
                (unsigned long long)t->entry_addr,
                (unsigned long long)cpu_delta,
                (unsigned long long)(sampled ? hc.Rip : 0), module);
        if (guest_func) {
            fprintf(stderr, "guest=func_%08X+0x%llX ", guest_func,
                    (unsigned long long)host_offset);
        }
        unsigned trace_slot = guest.thread_id < 64 ? (unsigned)guest.thread_id : 0;
        fprintf(stderr,
                "cia=0x%08llX lr=0x%08llX ctr=0x%08llX sp=0x%08llX "
                "r3=0x%08llX r25=0x%08llX r26=0x%08llX prof_pc=0x%08X "
                "jg={stage=%ld guard=0x%08X old=%u zero=%u chain=0x%08X}\n",
                (unsigned long long)guest.cia,
                (unsigned long long)guest.lr,
                (unsigned long long)guest.ctr,
                (unsigned long long)guest.gpr[1],
                (unsigned long long)guest.gpr[3],
                (unsigned long long)guest.gpr[25],
                (unsigned long long)guest.gpr[26], t->prof_pc,
                g_tj_jobguard_stage[trace_slot],
                g_tj_jobguard_guard[trace_slot],
                g_tj_jobguard_old[trace_slot],
                g_tj_jobguard_zero[trace_slot],
                g_tj_jobguard_chain[trace_slot]);
        if (thread) CloseHandle(thread);
    }
}

/* TJ_FRAME_PROF=1 enables an opt-in, low-rate sampling profiler after the
 * Survival trace arms. TJ_FRAME_PROF_EARLY=<nonzero> explicitly permits it
 * before that title-specific arm. It samples PPU host threads plus Tokyo main, aggregates
 * in memory, and prints once every TJ_FRAME_PROF_FRAMES flips (default 30).
 * TJ_FRAME_PROF_MS sets sampling cadence in ms (default 8; clamped 2..100).
 * TJ_FRAME_PROF_MAIN_ONLY=1 skips all worker handle duplication/sampling. */
enum { TJ_FRAME_PROF_TOP = 6, TJ_FRAME_PROF_SITES = 96,
       TJ_FRAME_PROF_RUNTIME_SITES = 48, TJ_FRAME_PROF_MAIN_SLOT = PPU_THREAD_MAX,
       TJ_FRAME_PROF_RENDERER_SLOT = PPU_THREAD_MAX + 1,
       TJ_FRAME_PROF_SLOT_COUNT = PPU_THREAD_MAX + 2 };
static const uint32_t TJ_FRAME_PROF_RENDERER_TID = UINT32_MAX;

struct tj_frame_prof_site {
    uint32_t guest;
    uint32_t ppu_tid;
    DWORD host_tid;
    uint32_t samples;
    uint64_t cpu_us;
};

struct tj_frame_prof_runtime_site {
    uint32_t ppu_tid;       /* 0 means Tokyo main, never worker slot 0 */
    DWORD host_tid;
    uintptr_t rip;
    uintptr_t rva;
    char module[MAX_PATH];
    uint32_t samples;
    uint64_t cpu_us;
};

struct tj_frame_prof_state {
    int enabled;
    int main_only;
    unsigned sample_ms;
    unsigned frame_limit;
    ULONGLONG next_sample_ms;
    uint32_t last_generation;
    uint32_t last_flip;
    unsigned frames;
    unsigned samples;
    unsigned runtime_samples;
    uint64_t runtime_cpu_us;
    unsigned unknown_samples;
    uint64_t unknown_cpu_us;
    uint64_t capacity_us;
    uint64_t cpu_us;
    uint64_t last_cpu_us[TJ_FRAME_PROF_SLOT_COUNT];
    ULONGLONG last_sample_ms[TJ_FRAME_PROF_SLOT_COUNT];
    DWORD last_host_tid[TJ_FRAME_PROF_SLOT_COUNT];
    tj_frame_prof_site sites[TJ_FRAME_PROF_SITES];
    unsigned site_count;
    tj_frame_prof_runtime_site runtime_sites[TJ_FRAME_PROF_RUNTIME_SITES];
    unsigned runtime_site_count;
};

static unsigned tj_frame_prof_setting(const char* name, unsigned fallback,
                                      unsigned minimum, unsigned maximum)
{
    const char* value = getenv(name);
    if (!value || !*value) return fallback;
    char* end = NULL;
    unsigned long parsed = strtoul(value, &end, 10);
    if (!end || *end || parsed < minimum || parsed > maximum) return fallback;
    return (unsigned)parsed;
}

static int tj_frame_prof_early_requested()
{
    static int enabled = -1;
    if (enabled < 0) {
        const char* value = getenv("TJ_FRAME_PROF_EARLY");
        char* end = NULL;
        unsigned long parsed = value && *value ? strtoul(value, &end, 10) : 0;
        enabled = end && *end == '\0' && parsed != 0;
    }
    return enabled;
}

static tj_frame_prof_state& tj_frame_prof(int early_explicit)
{
    static tj_frame_prof_state state = {};
    if (state.enabled == 0) {
        state.enabled = getenv("TJ_FRAME_PROF") ? 1 : -1;
        if (state.enabled > 0) {
            state.sample_ms = tj_frame_prof_setting("TJ_FRAME_PROF_MS", 8, 2, 100);
            state.frame_limit = tj_frame_prof_setting("TJ_FRAME_PROF_FRAMES", 30, 1, 600);
            state.main_only = getenv("TJ_FRAME_PROF_MAIN_ONLY") ? 1 : 0;
            fprintf(stderr, "[TJ-FRAME-PROF] enabled sampler=independent arm=%s sample=%ums frames=%u mode=%s\n",
                    early_explicit ? "early-explicit" : "survival",
                    state.sample_ms, state.frame_limit,
                    state.main_only ? "main-only" : "main+workers");
        }
    }
    return state;
}

static void tj_frame_prof_record_runtime(tj_frame_prof_state& state,
                                         uint32_t ppu_tid, DWORD host_tid,
                                         uintptr_t rip, uint64_t cpu_delta)
{
    state.runtime_samples++;
    state.runtime_cpu_us += cpu_delta;
    char module_path[MAX_PATH] = {};
    uintptr_t module_base = 0;
    const char* module = tj_rip_module(rip, module_path, sizeof(module_path), &module_base);
    for (unsigned i = 0; i < state.runtime_site_count; ++i) {
        tj_frame_prof_runtime_site& site = state.runtime_sites[i];
        if (site.ppu_tid == ppu_tid && site.host_tid == host_tid && site.rip == rip &&
            strcmp(site.module, module) == 0) {
            site.samples++;
            site.cpu_us += cpu_delta;
            return;
        }
    }
    if (state.runtime_site_count == TJ_FRAME_PROF_RUNTIME_SITES) return;
    tj_frame_prof_runtime_site& site = state.runtime_sites[state.runtime_site_count++];
    site.ppu_tid = ppu_tid;
    site.host_tid = host_tid;
    site.rip = rip;
    site.rva = module_base ? rip - module_base : 0;
    snprintf(site.module, sizeof(site.module), "%s", module);
    site.samples = 1;
    site.cpu_us = cpu_delta;
}

static void tj_frame_prof_record(tj_frame_prof_state& state, bool context_ok,
                                 uint32_t guest, uint32_t ppu_tid,
                                 DWORD host_tid, uintptr_t rip,
                                 uint64_t cpu_delta)
{
    state.samples++;
    state.cpu_us += cpu_delta;
    if (!context_ok) {
        state.unknown_samples++;
        state.unknown_cpu_us += cpu_delta;
        return;
    }
    if (!guest) {
        tj_frame_prof_record_runtime(state, ppu_tid, host_tid, rip, cpu_delta);
        return;
    }
    for (unsigned i = 0; i < state.site_count; ++i) {
        tj_frame_prof_site& site = state.sites[i];
        if (site.guest == guest && site.ppu_tid == ppu_tid &&
            site.host_tid == host_tid) {
            site.samples++;
            site.cpu_us += cpu_delta;
            return;
        }
    }
    if (state.site_count == TJ_FRAME_PROF_SITES) return;
    tj_frame_prof_site& site = state.sites[state.site_count++];
    site.guest = guest;
    site.ppu_tid = ppu_tid;
    site.host_tid = host_tid;
    site.samples = 1;
    site.cpu_us = cpu_delta;
}

static void tj_frame_prof_sample_thread(tj_frame_prof_state& state, HANDLE thread,
                                        uint32_t ppu_tid, unsigned cpu_slot,
                                        ULONGLONG now)
{
    if (!thread) return;
    DWORD host_tid = GetThreadId(thread);
    CONTEXT context = {};
    context.ContextFlags = CONTEXT_CONTROL;
    bool sampled = false;
    if (SuspendThread(thread) != (DWORD)-1) {
        sampled = GetThreadContext(thread, &context) != 0;
        ResumeThread(thread);
    }
    uint64_t cpu = tj_thread_cpu_us(thread);
    if (state.last_host_tid[cpu_slot] != host_tid) {
        state.last_host_tid[cpu_slot] = host_tid;
        state.last_cpu_us[cpu_slot] = cpu;
        state.last_sample_ms[cpu_slot] = now;
        uintptr_t offset = 0;
        uint32_t guest = sampled ? tj_resolve_host_rip((uintptr_t)context.Rip, &offset) : 0;
        tj_frame_prof_record(state, sampled, guest, ppu_tid, host_tid,
                             sampled ? (uintptr_t)context.Rip : 0, 0);
        return;
    }
    uint64_t cpu_delta = state.last_cpu_us[cpu_slot] && cpu >= state.last_cpu_us[cpu_slot]
                       ? cpu - state.last_cpu_us[cpu_slot] : 0;
    if (state.last_sample_ms[cpu_slot] && now >= state.last_sample_ms[cpu_slot])
        state.capacity_us += (now - state.last_sample_ms[cpu_slot]) * 1000u;
    state.last_cpu_us[cpu_slot] = cpu;
    state.last_sample_ms[cpu_slot] = now;
    uintptr_t offset = 0;
    uint32_t guest = sampled ? tj_resolve_host_rip((uintptr_t)context.Rip, &offset) : 0;
    tj_frame_prof_record(state, sampled, guest, ppu_tid, host_tid,
                         sampled ? (uintptr_t)context.Rip : 0, cpu_delta);
}

static const char* tj_frame_prof_role(uint32_t ppu_tid)
{
    if (ppu_tid == TJ_FRAME_PROF_RENDERER_TID) return "renderer";
    return ppu_tid == 0 ? "main" : "worker";
}

static const char* tj_frame_prof_source(uint32_t source_kind)
{
    return source_kind == 1 ? "direct-hle" :
           source_kind == 2 ? "fifo-dispatch" : "unknown";
}

static void tj_frame_prof_reset_window(tj_frame_prof_state& state)
{
    state.frames = 0;
    state.samples = 0;
    state.runtime_samples = 0;
    state.runtime_cpu_us = 0;
    state.unknown_samples = 0;
    state.unknown_cpu_us = 0;
    state.capacity_us = 0;
    state.cpu_us = 0;
    state.site_count = 0;
    state.runtime_site_count = 0;
}

/* Arm transitions must establish fresh CPU/time baselines so an interval from
 * before Survival tracing cannot appear as profiler CPU. */
static void tj_frame_prof_reset_sampling_baseline(tj_frame_prof_state& state)
{
    memset(state.last_cpu_us, 0, sizeof(state.last_cpu_us));
    memset(state.last_sample_ms, 0, sizeof(state.last_sample_ms));
    memset(state.last_host_tid, 0, sizeof(state.last_host_tid));
    state.next_sample_ms = 0;
}

static void tj_frame_prof_emit(tj_frame_prof_state& state,
                               const tj_gcm_frame_prof_snapshot& gcm)
{
    uint64_t wait_est = state.capacity_us > state.cpu_us
                      ? state.capacity_us - state.cpu_us : 0;
    fprintf(stderr,
            "[TJ-FRAME-PROF] generation=%u frames=%u mode=%s "
            "flip={sequence=%u source=%s source-host-tid=%u} "
            "fifo-best-effort={put=0x%08X get=0x%08X current=0x%08X} "
            "samples=%u cpu=%lluus capacity=%lluus wait-est=%lluus "
            "runtime={samples=%u cpu=%lluus} unknown={samples=%u cpu=%lluus}\n",
            gcm.generation, state.frames,
            state.main_only ? "main-only" : "main+workers", gcm.flip_sequence,
            tj_frame_prof_source(gcm.source_kind), gcm.source_host_tid,
            gcm.put, gcm.get, gcm.current, state.samples,
            (unsigned long long)state.cpu_us,
            (unsigned long long)state.capacity_us,
            (unsigned long long)wait_est, state.runtime_samples,
            (unsigned long long)state.runtime_cpu_us, state.unknown_samples,
            (unsigned long long)state.unknown_cpu_us);
    for (unsigned rank = 0; rank < TJ_FRAME_PROF_TOP; ++rank) {
        unsigned best = TJ_FRAME_PROF_SITES;
        for (unsigned i = 0; i < state.site_count; ++i) {
            const tj_frame_prof_site& candidate = state.sites[i];
            bool used = false;
            for (unsigned prior = 0; prior < rank; ++prior)
                if (state.sites[prior].guest == candidate.guest &&
                    state.sites[prior].ppu_tid == candidate.ppu_tid &&
                    state.sites[prior].host_tid == candidate.host_tid) used = true;
            if (!used && (best == TJ_FRAME_PROF_SITES ||
                          candidate.cpu_us > state.sites[best].cpu_us ||
                          (candidate.cpu_us == state.sites[best].cpu_us &&
                           candidate.samples > state.sites[best].samples))) {
                best = i;
            }
        }
        if (best == TJ_FRAME_PROF_SITES) break;
        /* Move selected site into rank prefix so subsequent passes exclude it. */
        tj_frame_prof_site selected = state.sites[best];
        state.sites[best] = state.sites[rank];
        state.sites[rank] = selected;
        fprintf(stderr,
                "[TJ-FRAME-PROF] top=%u guest=func_%08X thread=%s ppu-tid=%u host-tid=%lu "
                "samples=%u cpu=%lluus\n",
                rank + 1, selected.guest, tj_frame_prof_role(selected.ppu_tid),
                selected.ppu_tid,
                (unsigned long)selected.host_tid, selected.samples,
                (unsigned long long)selected.cpu_us);
    }
    for (unsigned rank = 0; rank < TJ_FRAME_PROF_TOP; ++rank) {
        unsigned best = TJ_FRAME_PROF_RUNTIME_SITES;
        for (unsigned i = rank; i < state.runtime_site_count; ++i) {
            const tj_frame_prof_runtime_site& candidate = state.runtime_sites[i];
            if (best == TJ_FRAME_PROF_RUNTIME_SITES ||
                candidate.cpu_us > state.runtime_sites[best].cpu_us ||
                (candidate.cpu_us == state.runtime_sites[best].cpu_us &&
                 candidate.samples > state.runtime_sites[best].samples)) best = i;
        }
        if (best == TJ_FRAME_PROF_RUNTIME_SITES) break;
        tj_frame_prof_runtime_site selected = state.runtime_sites[best];
        state.runtime_sites[best] = state.runtime_sites[rank];
        state.runtime_sites[rank] = selected;
        fprintf(stderr,
                "[TJ-FRAME-PROF] runtime-top=%u thread=%s ppu-tid=%u host-tid=%lu "
                "rip=0x%llX module=%s rva=0x%llX samples=%u cpu=%lluus\n",
                rank + 1, tj_frame_prof_role(selected.ppu_tid), selected.ppu_tid,
                (unsigned long)selected.host_tid, (unsigned long long)selected.rip,
                selected.module, (unsigned long long)selected.rva,
                selected.samples, (unsigned long long)selected.cpu_us);
    }
    tj_frame_prof_reset_window(state);
}

static void tj_frame_prof_tick(void)
{
    static int trace_was_armed;
    /* Keep the profiler entirely dormant until the title-specific Survival
     * trace starts, unless early profiling was explicitly requested. In
     * particular this avoids querying profiler config,
     * duplicating worker handles, or carrying boot CPU into the first window. */
    const int survival_armed = tj_survival_trace_is_armed();
    const int early_explicit = !survival_armed && tj_frame_prof_early_requested();
    if (!survival_armed && !early_explicit) {
        trace_was_armed = 0;
        return;
    }
    tj_frame_prof_state& state = tj_frame_prof(early_explicit);
    if (state.enabled < 0) return;
    ULONGLONG now = GetTickCount64();
    tj_gcm_frame_prof_snapshot gcm = {};
    cellGcm_frame_prof_snapshot(&gcm);
    if (!trace_was_armed) {
        trace_was_armed = 1;
        tj_frame_prof_reset_window(state);
        tj_frame_prof_reset_sampling_baseline(state);
        state.last_generation = gcm.generation;
        state.last_flip = gcm.flip_sequence;
    }
    if (now >= state.next_sample_ms) {
        state.next_sample_ms = now + state.sample_ms;
        HANDLE main_thread = tj_frame_prof_main_thread();
        tj_frame_prof_sample_thread(state, main_thread, 0,
                                    TJ_FRAME_PROF_MAIN_SLOT, now);
        if (main_thread) CloseHandle(main_thread);
        if (!state.main_only) {
            /* Sampler joins before renderer teardown, so this handle stays
             * owned and open throughout the read. No extra handle per sample. */
            tj_frame_prof_sample_thread(state, g_tj_present_thread,
                                        TJ_FRAME_PROF_RENDERER_TID,
                                        TJ_FRAME_PROF_RENDERER_SLOT, now);
            for (unsigned tid = 2; tid <= PPU_THREAD_MAX; ++tid) {
                HANDLE copy = ppu_thread_duplicate_live_worker(tid);
                tj_frame_prof_sample_thread(state, copy, tid,
                                            tid - 1, now);
                if (copy) CloseHandle(copy);
            }
        }
    }
    if (gcm.generation != state.last_generation) {
        state.last_generation = gcm.generation;
        state.last_flip = gcm.flip_sequence;
        tj_frame_prof_reset_window(state);
        return;
    }
    if (!gcm.flip_sequence) return;
    if (!state.last_flip) {
        state.last_flip = gcm.flip_sequence;
        return;
    }
    if (gcm.flip_sequence == state.last_flip) return;
    state.frames += gcm.flip_sequence - state.last_flip;
    state.last_flip = gcm.flip_sequence;
    if (state.frames >= state.frame_limit) tj_frame_prof_emit(state, gcm);
}

/* Sampling after the present thread's FIFO/catch-up loop biases every sample
 * toward the guest's following wait. At low FPS that loop can also take seconds,
 * defeating the configured sampling cadence. Only the opt-in profiler gets an
 * independent host thread; it never dispatches guest work or advances a tick. */
static HANDLE s_tj_frame_prof_thread, s_tj_frame_prof_stop_event;

static DWORD WINAPI tj_frame_prof_worker(LPVOID)
{
    if (!guest_world_register_current("tj-frame-prof")) {
        fprintf(stderr, "[TJ-FRAME-PROF] sampler registration failed\n");
        return 1;
    }
    DWORD result = 0;
    for (;;) {
        DWORD wait = WaitForSingleObject(s_tj_frame_prof_stop_event, 4);
        if (wait == WAIT_OBJECT_0) break;
        if (wait != WAIT_TIMEOUT) {
            fprintf(stderr, "[TJ-FRAME-PROF] sampler wait failed (%lu)\n", GetLastError());
            result = 1;
            break;
        }
        tj_frame_prof_tick();
    }
    guest_world_unregister_current();
    return result;
}

static void tj_start_frame_prof_thread(void)
{
    if (!getenv("TJ_FRAME_PROF") || s_tj_frame_prof_thread) return;
    s_tj_frame_prof_stop_event = CreateEventW(NULL, TRUE, FALSE, NULL);
    if (!s_tj_frame_prof_stop_event) {
        fprintf(stderr, "[TJ-FRAME-PROF] sampler event creation failed (%lu)\n", GetLastError());
        return;
    }
    s_tj_frame_prof_thread = CreateThread(NULL, 0, tj_frame_prof_worker, NULL, 0, NULL);
    if (!s_tj_frame_prof_thread) {
        fprintf(stderr, "[TJ-FRAME-PROF] sampler thread creation failed (%lu)\n", GetLastError());
        CloseHandle(s_tj_frame_prof_stop_event);
        s_tj_frame_prof_stop_event = NULL;
    }
}

static void tj_stop_frame_prof_thread(void)
{
    if (s_tj_frame_prof_thread) {
        SetEvent(s_tj_frame_prof_stop_event);
        WaitForSingleObject(s_tj_frame_prof_thread, INFINITE);
        CloseHandle(s_tj_frame_prof_thread);
        s_tj_frame_prof_thread = NULL;
    }
    if (s_tj_frame_prof_stop_event) {
        CloseHandle(s_tj_frame_prof_stop_event);
        s_tj_frame_prof_stop_event = NULL;
    }
}

/* TJ_FIFO_PERF also times present-thread segments. TJ_PRESENT_LOOP_TRACE=1
 * enables only this host collector, without per-command FIFO instrumentation.
 * Aggregated once/second; this runs on only the present thread. */
struct tj_fifo_perf_loop {
    int enabled;
    LARGE_INTEGER frequency;
    uint64_t next_report_ns;
    uint64_t report_start_ms;
    uint64_t loops, ticks, sleep_ns, tick_present_ns;
    uint64_t drain1_ns, drain2_ns, dump_profiler_ns, pump_ns;
    uint64_t loop_max_ns, present_gap_max_ns, last_present_ns;
    uint64_t sleep_max_ns, tick_present_max_ns, drain1_max_ns, drain2_max_ns;
    uint64_t dump_profiler_max_ns, pump_max_ns, report_ns, report_max_ns;
    uint64_t last_clock_ns;
    uint64_t fifo_kick_wakes, fifo_kick_timeouts, fifo_kick_fallbacks;
};

/* One stderr write per report line: stderr is unbuffered, so fprintf writes
 * one character per system call (~2.6 us each to the TJ_STDERR_FILE log).
 * fwrite holds the stream lock, so the line stays whole. */
#if defined(__clang__) || defined(__GNUC__)
__attribute__((format(printf, 1, 2)))
#endif
static void tj_present_log_line(const char* format, ...)
{
    char line[2048];
    va_list args;
    va_start(args, format);
    const int length = vsnprintf(line, sizeof(line), format, args);
    va_end(args);
    if (length >= 0 && (size_t)length < sizeof(line)) {
        fwrite(line, 1, (size_t)length, stderr);
        return;
    }
    va_start(args, format);
    vfprintf(stderr, format, args);
    va_end(args);
}

static void tj_fifo_perf_add_segment(uint64_t& total, uint64_t& maximum,
                                     uint64_t elapsed)
{
    total += elapsed;
    if (elapsed > maximum) maximum = elapsed;
}

static uint64_t tj_fifo_perf_now_ns(tj_fifo_perf_loop& state)
{
    if (state.enabled <= 0) return 0;
    LARGE_INTEGER now = {};
    if (state.frequency.QuadPart <= 0 ||
        (uint64_t)state.frequency.QuadPart > UINT64_MAX / 1000000000ULL ||
        !QueryPerformanceCounter(&now) || now.QuadPart < 0) {
        state.enabled = -1;
        fprintf(stderr, "[TJ-PRESENT-PERF] event=disabled reason=invalid-clock\n");
        fflush(stderr);
        return 0;
    }
    const uint64_t ticks = (uint64_t)now.QuadPart;
    const uint64_t frequency = (uint64_t)state.frequency.QuadPart;
    if (ticks / frequency > UINT64_MAX / 1000000000ULL) {
        state.enabled = -1;
        fprintf(stderr, "[TJ-PRESENT-PERF] event=disabled reason=clock-overflow\n");
        fflush(stderr);
        return 0;
    }
    uint64_t whole = (ticks / frequency) * 1000000000ULL;
    uint64_t fraction = ((ticks % frequency) * 1000000000ULL) / frequency;
    if (fraction > UINT64_MAX - whole || whole + fraction < state.last_clock_ns) {
        state.enabled = -1;
        fprintf(stderr, "[TJ-PRESENT-PERF] event=disabled reason=invalid-clock\n");
        fflush(stderr);
        return 0;
    }
    state.last_clock_ns = whole + fraction;
    return state.last_clock_ns;
}

static uint64_t tj_fifo_perf_elapsed_ns(tj_fifo_perf_loop& state, uint64_t begin)
{
    uint64_t end = tj_fifo_perf_now_ns(state);
    if (state.enabled <= 0 || end < begin) return 0;
    return end - begin;
}

static tj_fifo_perf_loop& tj_fifo_perf_loop_state()
{
    static tj_fifo_perf_loop state = {};
    if (!state.enabled) {
        const char* loop_trace = getenv("TJ_PRESENT_LOOP_TRACE");
        state.enabled = (getenv("TJ_FIFO_PERF") ||
            (loop_trace && strcmp(loop_trace, "1") == 0)) ? 1 : -1;
        if (state.enabled > 0) {
            if (!QueryPerformanceFrequency(&state.frequency) ||
                state.frequency.QuadPart <= 0) {
                state.enabled = -1;
                fprintf(stderr, "[TJ-PRESENT-PERF] event=disabled reason=invalid-frequency\n");
                fflush(stderr);
                return state;
            }
            state.report_start_ms = GetTickCount64();
        }
    }
    return state;
}

static uint64_t tj_fifo_perf_loop_report(tj_fifo_perf_loop& state)
{
    if (state.enabled <= 0) return 0;
    uint64_t now = tj_fifo_perf_now_ns(state);
    if (state.enabled <= 0) return 0;
    if (now > UINT64_MAX - 1000000000ULL) {
        state.enabled = -1;
        fprintf(stderr, "[TJ-PRESENT-PERF] event=disabled reason=clock-overflow\n");
        fflush(stderr);
        return 0;
    }
    if (!state.next_report_ns) state.next_report_ns = now + 1000000000ULL;
    if (now < state.next_report_ns) return 0;
    uint64_t report_begin = now;
    uint64_t report_end_ms = GetTickCount64();
    tj_present_log_line(
            "[TJ-PRESENT-PERF] start_ms=%llu end_ms=%llu loops=%llu ticks=%llu loop-max=%lluus present-gap-max=%lluus "
            "sleep=%lluus/%lluus tick-present=%lluus/%lluus "
            "fifo-drain-1=%lluus/%lluus fifo-drain-2=%lluus/%lluus "
            "dump-profiler=%lluus/%lluus pump=%lluus/%lluus "
            "fifo-kick-wakes=%llu fifo-kick-timeouts=%llu fifo-kick-fallbacks=%llu "
            "report-prev=%lluus/%lluus\n",
            (unsigned long long)state.report_start_ms,
            (unsigned long long)report_end_ms,
            (unsigned long long)state.loops, (unsigned long long)state.ticks,
            (unsigned long long)(state.loop_max_ns / 1000ULL),
            (unsigned long long)(state.present_gap_max_ns / 1000ULL),
            (unsigned long long)(state.sleep_ns / 1000ULL),
            (unsigned long long)(state.sleep_max_ns / 1000ULL),
            (unsigned long long)(state.tick_present_ns / 1000ULL),
            (unsigned long long)(state.tick_present_max_ns / 1000ULL),
            (unsigned long long)(state.drain1_ns / 1000ULL),
            (unsigned long long)(state.drain1_max_ns / 1000ULL),
            (unsigned long long)(state.drain2_ns / 1000ULL),
            (unsigned long long)(state.drain2_max_ns / 1000ULL),
            (unsigned long long)(state.dump_profiler_ns / 1000ULL),
            (unsigned long long)(state.dump_profiler_max_ns / 1000ULL),
            (unsigned long long)(state.pump_ns / 1000ULL),
            (unsigned long long)(state.pump_max_ns / 1000ULL),
            (unsigned long long)state.fifo_kick_wakes,
            (unsigned long long)state.fifo_kick_timeouts,
            (unsigned long long)state.fifo_kick_fallbacks,
            (unsigned long long)(state.report_ns / 1000ULL),
            (unsigned long long)(state.report_max_ns / 1000ULL));
    fflush(stderr);
    uint64_t next = now + 1000000000ULL;
    state.loops = state.ticks = state.sleep_ns = state.tick_present_ns = 0;
    state.drain1_ns = state.drain2_ns = state.dump_profiler_ns = state.pump_ns = 0;
    state.loop_max_ns = state.present_gap_max_ns = 0;
    state.sleep_max_ns = state.tick_present_max_ns = 0;
    state.drain1_max_ns = state.drain2_max_ns = 0;
    state.dump_profiler_max_ns = state.pump_max_ns = 0;
    state.report_ns = state.report_max_ns = 0;
    state.fifo_kick_wakes = state.fifo_kick_timeouts = 0;
    state.fifo_kick_fallbacks = 0;
    state.next_report_ns = next;
    state.report_start_ms = report_end_ms;
    return tj_fifo_perf_elapsed_ns(state, report_begin);
}

static void tj_fifo_perf_mark_present(tj_fifo_perf_loop& state)
{
    uint64_t now = tj_fifo_perf_now_ns(state);
    if (state.enabled <= 0) return;
    if (state.last_present_ns) {
        uint64_t gap = now - state.last_present_ns;
        if (gap > state.present_gap_max_ns) state.present_gap_max_ns = gap;
    }
    state.last_present_ns = now;
}

/* TJ_PRESENT_HEARTBEAT=1: bounded, read-only liveness/provenance diagnostic.
 * Keep this on the present thread so it observes the same ticker/FIFO order;
 * emit at most once per second and never alter scheduling or guest state. */
static int tj_present_heartbeat_enabled()
{
    static int enabled = -1;
    if (enabled < 0)
        enabled = getenv("TJ_PRESENT_HEARTBEAT") ? 1 : 0;
    return enabled;
}

static int tj_present_heartbeat(uint64_t loop_iteration, int rsx_ok,
                                int fired, uint64_t consumed_this_window)
{
    if (!tj_present_heartbeat_enabled()) return 0;
    static ULONGLONG next_report = 0;
    static uint64_t last_taken_generation = 0;
    static uint32_t last_taken_buffer = 0;
    ULONGLONG now = GetTickCount64();
    if (now < next_report) return 0;
    next_report = now + 1000;

    tj_gcm_frame_prof_snapshot fifo = {};
    cellGcm_frame_prof_snapshot(&fifo);
    tj_gcm_flip_ticket consumed = {};
    int have_consumed = cellGcm_flip_consumed_ticket_snapshot(&consumed);
    if (have_consumed) {
        last_taken_generation = consumed.generation;
        last_taken_buffer = consumed.buffer_id;
    }
    fprintf(stderr,
            "[TJ-PRESENT-HB] alive=1 ms=%llu loop=%llu rsx_ok=%d "
            "fired=%d consumed_window=%llu consumed_gen=%llu "
            "consumed_buf=%u display_buf=%u snapshot_gen=%u flip_seq=%u source=%u "
            "source_tid=%u flips=%u fifo={put=%08X get=%08X current=%08X} "
            "backend_frame=unavailable backend_draws=unavailable\n",
            (unsigned long long)now,
            (unsigned long long)loop_iteration, rsx_ok, fired,
            (unsigned long long)consumed_this_window,
            (unsigned long long)last_taken_generation, last_taken_buffer,
            cellGcmGetCurrentDisplayBufferId(), fifo.generation,
            fifo.flip_sequence, fifo.source_kind,
            fifo.source_host_tid, cellGcm_flip_request_count(),
            fifo.put, fifo.get, fifo.current);
    fflush(stderr);
    return 1;
}

/* TJ_SPU_COMPLETION_WAIT_TRACE=1: capture the SPURS producer state even when
 * the guest stops before entering func_0025CA50.  Sample only after the exact
 * Survival scene-ready gate, emit once after two full seconds with no new
 * flip request, and never modify guest or scheduler state. */
extern "C" void cellGcm_user_interrupt_snapshot(FILE* out);
extern "C" void cellSpurs_jobchain_snapshot(FILE* out, uint32_t flag_ea);
static void tj_survival_stall_snapshot_tick()
{
    static const int enabled =
        getenv("TJ_SPU_COMPLETION_WAIT_TRACE") ? 1 : 0;
    static ULONGLONG last_change_ms = 0;
    static unsigned int last_flips = 0;
    static bool initialized = false;
    static bool reported = false;
    if (!enabled || reported) return;

    if (InterlockedCompareExchange(&g_tj_survival_scene_ready, 0, 0) == 0) {
        last_change_ms = 0;
        initialized = false;
        return;
    }

    ULONGLONG now_ms = GetTickCount64();
    unsigned int flips = cellGcm_flip_request_count();
    if (!initialized) {
        last_flips = flips;
        last_change_ms = now_ms;
        initialized = true;
        return;
    }
    if (flips != last_flips) {
        last_flips = flips;
        last_change_ms = now_ms;
        return;
    }
    ULONGLONG unchanged_ms = now_ms - last_change_ms;
    if (unchanged_ms < 2000) return;

    uint32_t manager_ea = (uint32_t)InterlockedCompareExchange(
        &g_tj_last_task_manager_ea, 0, 0);
    uint32_t completion_ea = manager_ea ? manager_ea + 0x2Cu : 0;
    fprintf(stderr,
            "[TJ-STALL-SNAPSHOT] state=BEGIN flips=%u unchanged_ms=%llu "
            "manager=0x%08X completion_ea=0x%08X\n",
            flips, (unsigned long long)unchanged_ms, manager_ea, completion_ea);
    cellGcm_user_interrupt_snapshot(stderr);
    cellSpurs_jobchain_snapshot(stderr, 0x005F7600u);
    spu_taskset_signal_snapshot(stderr);
    if (completion_ea) {
        spu_completion_atomic_history_snapshot(stderr, completion_ea);
    } else {
        fprintf(stderr,
                "[TJ-SPU-COMPLETION-ATOM] target=unavailable matched=0\n");
    }
    fprintf(stderr, "[TJ-STALL-SNAPSHOT] state=END\n");
    fflush(stderr);
    reported = true;
}

extern "C" {
int  rsx_d3d12_backend_init(int width, int height, const char* title);
void rsx_d3d12_backend_shutdown(void);
void rsx_d3d12_backend_present(void);
void rsx_d3d12_backend_present_buffer(uint64_t generation, uint32_t buffer_id);
int  rsx_d3d12_backend_pump_messages(void);
void rsx_d3d12_backend_dump_message_pump_trace(void);
void rsx_d3d12_backend_set_close_observer(void (*observer)(void));
int  cellSaveData_busy(void);
void rsx_d3d12_backend_set_fence_wait_service(
    uint32_t (*service)(void* context, uint32_t remaining_ms), void* context);
void rsx_d3d12_backend_set_fence_wait_fifo_pulse(int enabled);
int  rsx_d3d12_backend_display_vblank_start(char* note, size_t note_size);
void rsx_d3d12_backend_display_vblank_stop(void);
uint64_t rsx_d3d12_backend_display_vblank_ticks(uint64_t* last_tick_qpc);
void* rsx_d3d12_backend_display_vblank_event(void);
void rsx_d3d12_backend_display_vblank_describe(char* out, size_t size, int window);
uint32_t rsx_d3d12_backend_display_vblank_divisor(void);
void rsx_d3d12_backend_present_hold_arm(int arm);
int  rsx_d3d12_backend_present_held(void);
int  rsx_d3d12_backend_present_release(void);
uint64_t rsx_d3d12_backend_present_count(void);
uint64_t rsx_d3d12_backend_present_forced_count(void);
void rsx_d3d12_backend_present_interval_arm(uint32_t interval);
void rsx_d3d12_backend_present_phase_tick(uint64_t tick_qpc);
uint64_t rsx_d3d12_backend_present_paced_count(void);
uint64_t rsx_d3d12_backend_present_forced_take(uint64_t* stamped, uint64_t* sum,
                                               uint64_t* min, uint64_t* max);
uint32_t rsx_d3d12_backend_swap_buffer_count(void);
void cellGcmTickVBlank(void);
int cellGcm_complete_flip_ticket(const tj_gcm_flip_ticket* ticket);
int cellGcm_flip_uses_vsync(void);
void cellGcm_rsx_process_fifo(void);
int  cellGcm_take_flip_ticket(tj_gcm_flip_ticket* out_ticket);
int  cellGcm_take_flip_ticket_timed(tj_gcm_flip_ticket* out_ticket,
                                     tj_gcm_flip_publication_timing* out_timing);
uint32_t cellGcm_flip_request_count(void);
}

/* Timestamp-only flip ring. Sole writer: presenter thread. Default OFF; no
 * additional clock/counter reads or output until explicitly enabled. */
struct TjFlipTimelineRecord {
    uint64_t seq, generation, publication_qpc, publish_to_take_ticks;
    uint64_t take_qpc, present_return_qpc, complete_qpc;
    uint32_t source, buffer;
    uint32_t publication_qpc_state;
    int publish_to_take_valid, take_qpc_valid, present_return_qpc_valid, complete_qpc_valid;
    int present_tick_context, present_tick_valid, complete_tick_context, complete_tick_valid;
    uint64_t present_tick_due_qpc, present_tick_actual_qpc;
    uint64_t complete_tick_due_qpc, complete_tick_actual_qpc;
    uint64_t drain_ns_since_prev_complete, drain_calls_since_prev_complete;
    uint64_t request_delta_during_drain;
    int drain_time_valid, request_delta_valid, drain_interval_split;
};
struct TjFlipTimeline {
    int enabled, qpc_init_ok, qpc_available, qpc_failures, dumped;
    const char* qpc_failure_stage;
    uint64_t frequency, anchor, anchor_after, host_ms_anchor, last_qpc;
    uint64_t takes, completions, association_misses;
    uint64_t drain_ticks, drain_calls, request_delta;
    uint64_t active_drain_checkpoint_qpc;
    uint32_t active_drain_request_count;
    int drain_time_valid, drain_active, drain_interval_split;
    int tick_context, tick_valid;
    uint64_t tick_due, tick_actual;
    TjFlipTimelineRecord active, ring[1024];
};
static TjFlipTimeline s_tj_flip_timeline = {};

static uint64_t tj_flip_timeline_now(const char* stage)
{
    TjFlipTimeline& t = s_tj_flip_timeline;
    if (!t.enabled || !t.qpc_available) return 0;
    LARGE_INTEGER now = {0};
    if (!QueryPerformanceCounter(&now) || now.QuadPart <= 0 ||
        (uint64_t)now.QuadPart < t.last_qpc) {
        t.qpc_available = 0;
        t.drain_time_valid = 0;
        ++t.qpc_failures;
        t.qpc_failure_stage = stage;
        return 0;
    }
    t.last_qpc = (uint64_t)now.QuadPart;
    return t.last_qpc;
}

static void tj_flip_timeline_init(int qpc_active, uint64_t frequency)
{
    const char* setting = getenv("TJ_FLIP_TIMELINE");
    TjFlipTimeline& t = s_tj_flip_timeline;
    if (!setting || strcmp(setting, "1") != 0) {
        t.enabled = 0;
        return;
    }
    memset(&t, 0, sizeof(t));
    t.enabled = 1;
    t.qpc_init_ok = t.qpc_available = qpc_active && frequency >= 60;
    t.frequency = t.qpc_init_ok ? frequency : 0;
    t.qpc_failure_stage = "none";
    t.drain_time_valid = t.qpc_available;
    t.anchor = tj_flip_timeline_now("anchor");
    t.host_ms_anchor = GetTickCount64();
    t.anchor_after = tj_flip_timeline_now("anchor_after");
}

static void tj_flip_timeline_tick(int qpc_active, uint64_t due)
{
    TjFlipTimeline& t = s_tj_flip_timeline;
    if (!t.enabled) return;
    t.tick_context = 1;
    t.tick_due = qpc_active ? due : 0;
    t.tick_actual = qpc_active ? tj_flip_timeline_now("tick") : 0;
    t.tick_valid = t.tick_actual != 0;
}

static void tj_flip_timeline_offtick(void)
{
    TjFlipTimeline& t = s_tj_flip_timeline;
    if (!t.enabled) return;
    t.tick_context = t.tick_valid = 0;
    t.tick_due = t.tick_actual = 0;
}

static void tj_flip_timeline_take(const tj_gcm_flip_ticket& ticket,
                                  const tj_gcm_flip_publication_timing* publication = nullptr)
{
    TjFlipTimeline& t = s_tj_flip_timeline;
    if (!t.enabled) return;
    if (t.active.generation) ++t.association_misses;
    t.active = {};
    ++t.takes;
    t.active.generation = ticket.generation;
    t.active.source = ticket.source;
    t.active.buffer = ticket.buffer_id;
    t.active.publication_qpc_state = publication
        ? publication->publication_qpc_state : TJ_GCM_FLIP_PUBLICATION_QPC_OFF;
    t.active.publication_qpc = t.active.publication_qpc_state ==
        TJ_GCM_FLIP_PUBLICATION_QPC_VALID ? publication->publication_qpc : 0;
    t.active.take_qpc = tj_flip_timeline_now("take");
    t.active.take_qpc_valid = t.active.take_qpc != 0;
    if (t.active.publication_qpc_state == TJ_GCM_FLIP_PUBLICATION_QPC_VALID &&
        t.active.publication_qpc > 0 && t.active.take_qpc_valid &&
        t.active.publication_qpc <= t.active.take_qpc) {
        t.active.publish_to_take_ticks =
            t.active.take_qpc - t.active.publication_qpc;
        t.active.publish_to_take_valid = 1;
    }
    t.active.present_tick_context = t.tick_context;
    t.active.present_tick_valid = t.tick_valid;
    t.active.present_tick_due_qpc = t.tick_due;
    t.active.present_tick_actual_qpc = t.tick_actual;
}

static void tj_flip_timeline_present_return(void)
{
    TjFlipTimeline& t = s_tj_flip_timeline;
    if (!t.enabled) return;
    t.active.present_return_qpc = tj_flip_timeline_now("present_return");
    t.active.present_return_qpc_valid = t.active.present_return_qpc != 0;
}

static void tj_flip_timeline_drain_checkpoint(uint64_t finish,
                                               uint32_t request_count)
{
    TjFlipTimeline& t = s_tj_flip_timeline;
    if (!t.enabled || !t.drain_active) return;
    if (t.active_drain_checkpoint_qpc && finish >= t.active_drain_checkpoint_qpc)
        t.drain_ticks += finish - t.active_drain_checkpoint_qpc;
    else
        t.drain_time_valid = 0;
    t.request_delta += (uint32_t)(request_count - t.active_drain_request_count);
    t.active_drain_checkpoint_qpc = finish;
    t.active_drain_request_count = request_count;
}

static void tj_flip_timeline_complete(const tj_gcm_flip_ticket& ticket)
{
    TjFlipTimeline& t = s_tj_flip_timeline;
    if (!t.enabled) return;
    if (!t.active.generation || t.active.generation != ticket.generation) {
        ++t.association_misses;
        return;
    }
    TjFlipTimelineRecord& r = t.active;
    r.complete_qpc = tj_flip_timeline_now("complete");
    r.complete_qpc_valid = r.complete_qpc != 0;
    if (t.drain_active) {
        tj_flip_timeline_drain_checkpoint(
            r.complete_qpc, cellGcm_flip_request_count());
        t.drain_interval_split = 1;
    }
    r.complete_tick_context = t.tick_context;
    r.complete_tick_valid = t.tick_valid;
    r.complete_tick_due_qpc = t.tick_due;
    r.complete_tick_actual_qpc = t.tick_actual;
    r.drain_time_valid = t.drain_time_valid && !t.association_misses;
    r.drain_ns_since_prev_complete = t.frequency
        ? (uint64_t)((long double)t.drain_ticks * 1000000000.0L / t.frequency) : 0;
    r.drain_calls_since_prev_complete = t.drain_calls;
    r.request_delta_during_drain = t.request_delta;
    r.request_delta_valid = t.qpc_available && !t.association_misses;
    r.drain_interval_split = t.drain_interval_split;
    r.seq = ++t.completions;
    t.ring[(r.seq - 1) % 1024] = r;
    t.active = {};
    t.drain_ticks = t.drain_calls = t.request_delta = 0;
    t.drain_time_valid = t.qpc_available;
    t.drain_interval_split = t.drain_active;
}

static void tj_flip_timeline_reject_completion(const tj_gcm_flip_ticket& ticket)
{
    TjFlipTimeline& t = s_tj_flip_timeline;
    if (!t.enabled) return;
    ++t.association_misses;
    if (t.active.generation == ticket.generation)
        t.active = {};
}

static void tj_flip_timeline_drain(void)
{
    TjFlipTimeline& t = s_tj_flip_timeline;
    if (!t.enabled) {
        cellGcm_rsx_process_fifo();
        return;
    }
    t.drain_active = 1;
    ++t.drain_calls;
    t.active_drain_checkpoint_qpc = tj_flip_timeline_now("drain_begin");
    t.active_drain_request_count = cellGcm_flip_request_count();
    cellGcm_rsx_process_fifo();
    uint64_t finish = tj_flip_timeline_now("drain_end");
    tj_flip_timeline_drain_checkpoint(finish, cellGcm_flip_request_count());
    t.drain_active = 0;
    t.active_drain_checkpoint_qpc = 0;
}

static void tj_flip_timeline_dump(void)
{
    TjFlipTimeline& t = s_tj_flip_timeline;
    if (!t.enabled || t.dumped) return;
    uint64_t records = t.completions < 1024 ? t.completions : 1024;
    fprintf(stderr, "[TJ-FLIP-TIMELINE] schema=2 event=header enabled=1 qpc_init_ok=%d qpc_available=%d qpc_frequency=%llu qpc_anchor=%llu qpc_anchor_after=%llu host_ms_anchor=%llu host_ms_uncertainty_ms=32 capacity=1024 drain_calls_semantics=starts_in_interval takes=%llu completions=%llu records=%llu ring_overwrites=%llu association_misses=%llu qpc_failures=%d qpc_failure_stage=%s\n",
        t.qpc_init_ok, t.qpc_available, (unsigned long long)t.frequency,
        (unsigned long long)t.anchor, (unsigned long long)t.anchor_after,
        (unsigned long long)t.host_ms_anchor,
        (unsigned long long)t.takes, (unsigned long long)t.completions,
        (unsigned long long)records, (unsigned long long)(t.completions-records),
        (unsigned long long)t.association_misses, t.qpc_failures, t.qpc_failure_stage);
    rsx_d3d12_backend_dump_message_pump_trace();
    for (uint64_t i = 0; i < records; ++i) {
        uint64_t seq = t.completions-records+1+i;
        const TjFlipTimelineRecord& r = t.ring[(seq-1)%1024];
        fprintf(stderr, "[TJ-FLIP-TIMELINE] event=flip seq=%llu generation=%llu source=%u buffer=%u publication_qpc=%llu publication_qpc_state=%u publish_to_take_ticks=%llu publish_to_take_valid=%d take_qpc=%llu take_qpc_valid=%d present_return_qpc=%llu present_return_qpc_valid=%d complete_qpc=%llu complete_qpc_valid=%d present_tick_context=%d present_tick_valid=%d present_tick_due_qpc=%llu present_tick_actual_qpc=%llu complete_tick_context=%d complete_tick_valid=%d complete_tick_due_qpc=%llu complete_tick_actual_qpc=%llu drain_ns_since_prev_complete=%llu drain_time_valid=%d drain_calls_since_prev_complete=%llu request_delta_during_drain=%llu request_delta_valid=%d drain_interval_split=%d interval_kind=%s\n",
            (unsigned long long)r.seq, (unsigned long long)r.generation, r.source, r.buffer,
            (unsigned long long)r.publication_qpc, r.publication_qpc_state,
            (unsigned long long)r.publish_to_take_ticks, r.publish_to_take_valid,
            (unsigned long long)r.take_qpc, r.take_qpc_valid,
            (unsigned long long)r.present_return_qpc, r.present_return_qpc_valid,
            (unsigned long long)r.complete_qpc, r.complete_qpc_valid,
            r.present_tick_context, r.present_tick_valid,
            (unsigned long long)r.present_tick_due_qpc, (unsigned long long)r.present_tick_actual_qpc,
            r.complete_tick_context, r.complete_tick_valid,
            (unsigned long long)r.complete_tick_due_qpc, (unsigned long long)r.complete_tick_actual_qpc,
            (unsigned long long)r.drain_ns_since_prev_complete, r.drain_time_valid,
            (unsigned long long)r.drain_calls_since_prev_complete,
            (unsigned long long)r.request_delta_during_drain, r.request_delta_valid,
            r.drain_interval_split,
            r.seq == 1 ? "first" : "steady");
    }
    fflush(stderr);
    t.dumped = 1;
}

static int tj_present_complete_ticket(const tj_gcm_flip_ticket* ticket)
{
    int completed = cellGcm_complete_flip_ticket(ticket);
    if (completed) tj_flip_timeline_complete(*ticket);
    else tj_flip_timeline_reject_completion(*ticket);
    return completed;
}

/* TJ_PRESENT_PHASE (opt-in). With the display VBlank source driving the
 * ticks on an output at a multiple of 60 Hz (divisor >= 2, e.g. 120 Hz), an
 * off-tick frame's Present(1,0) lands anywhere between two ticks and is shown
 * for 1 or 3 refreshes: judder even at 58-60 FPS.
 *
 * TJ_PRESENT_PHASE=1 (interval): every vsync frame's Present uses
 * SyncInterval = divisor, so DXGI shows each frame for at least `divisor`
 * refreshes and, with one frame per tick, settles on exactly `divisor`.
 * Nothing is held or moved: take, composition, completion and drains stay.
 *
 * TJ_PRESENT_PHASE=hold (A/B only): such a vsync frame is still taken,
 * replayed, composited and submitted at once, and still completes at the next
 * VBlank, but the backend keeps its swap-chain Present until right after that
 * VBlank. A frame composed within TJ_PRESENT_PHASE_EARLY_US (default 2000,
 * max 8000) of the newest tick, with no Present since that tick, is already
 * in phase and goes out at once. At most one frame is held: its ticket is
 * awaiting_vblank, so no newer ticket is taken before that VBlank. The next
 * frame's prefix replays need the back buffer and force a held Present out
 * early (phase_forced).
 *
 * Unset, on the QPC source or at divisor 1 both modes do nothing. Presenter
 * thread only. */
enum { TJ_PRESENT_PHASE_OFF = 0, TJ_PRESENT_PHASE_INTERVAL = 1, TJ_PRESENT_PHASE_HOLD = 2 };
struct TjPresentPhase {
    int enabled;                    /* mode != OFF */
    int mode;
    const TjVBlankSource* source;   /* presenter stack, valid in its loop */
    uint64_t frequency;             /* QPC */
    uint64_t early_ticks;
    uint64_t tick_stamp;            /* due stamp of the newest fired VBlank */
    uint64_t presents_at_tick;      /* backend Present count when it fired */
    uint64_t held_since;            /* QPC when the held frame was composed */
    uint64_t presents_seen, paced_seen;
    uint32_t last_interval;
    /* [TJ-VBLANK-SOURCE] trace window */
    uint64_t held, immediate, forced, hold_sum, hold_max;
};
static TjPresentPhase s_tj_present_phase = {};

static void tj_vblank_trace_present(void);

static uint64_t tj_present_phase_now(void)
{
    LARGE_INTEGER now = {};
    return QueryPerformanceCounter(&now) && now.QuadPart > 0
        ? (uint64_t)now.QuadPart : 0;
}

static void tj_present_phase_init(const TjVBlankSource* source)
{
    TjPresentPhase& p = s_tj_present_phase;
    const char* value = getenv("TJ_PRESENT_PHASE");
    if (!value) return;
    const int mode = strcmp(value, "1") == 0 ? TJ_PRESENT_PHASE_INTERVAL
        : strcmp(value, "hold") == 0 ? TJ_PRESENT_PHASE_HOLD : TJ_PRESENT_PHASE_OFF;
    if (mode == TJ_PRESENT_PHASE_OFF) return;
    LARGE_INTEGER frequency = {};
    if (!QueryPerformanceFrequency(&frequency) || frequency.QuadPart <= 0) {
        fprintf(stderr, "[TJ-PRESENT-PHASE] enabled=0 reason=no_qpc\n");
        return;
    }
    unsigned early_us = 2000;
    const char* early = getenv("TJ_PRESENT_PHASE_EARLY_US");
    if (early && *early) {
        char* end = NULL;
        const unsigned long parsed = strtoul(early, &end, 10);
        if (end && !*end && parsed <= 8000) early_us = (unsigned)parsed;
    }
    p.enabled = 1;
    p.mode = mode;
    p.source = source;
    p.frequency = (uint64_t)frequency.QuadPart;
    p.early_ticks = p.frequency * early_us / 1000000u;
    p.presents_seen = rsx_d3d12_backend_present_count();
    p.paced_seen = rsx_d3d12_backend_present_paced_count();
    if (mode == TJ_PRESENT_PHASE_HOLD)
        fprintf(stderr, "[TJ-PRESENT-PHASE] enabled=1 mode=hold early_us=%u swap_buffers=%u source=%s%s\n",
                early_us, (unsigned)rsx_d3d12_backend_swap_buffer_count(),
                source->display ? "display" : "qpc",
                source->display ? "" : " hold=never (needs TJ_VBLANK_SOURCE=display)");
    else
        fprintf(stderr, "[TJ-PRESENT-PHASE] enabled=1 mode=interval swap_buffers=%u source=%s%s\n",
                (unsigned)rsx_d3d12_backend_swap_buffer_count(),
                source->display ? "display" : "qpc",
                source->display ? "" : " interval=1 (needs TJ_VBLANK_SOURCE=display)");
}

/* Interval mode: SyncInterval for this vsync frame's Present (0 = the
 * ordinary Present(1,0)). Display source following and divisor >= 2 only. */
static uint32_t tj_present_phase_interval(void)
{
    TjPresentPhase& p = s_tj_present_phase;
    if (p.mode != TJ_PRESENT_PHASE_INTERVAL || !p.source->following ||
        !cellGcm_flip_uses_vsync())
        return 0;
    const uint32_t divisor = rsx_d3d12_backend_display_vblank_divisor();
    if (divisor < 2) return 0;
    p.last_interval = divisor > 4 ? 4u : divisor;
    return p.last_interval;
}

/* Hold mode: an off-tick vsync frame; keep its Present for the next tick? */
static int tj_present_phase_hold_next(int vblank)
{
    const TjPresentPhase& p = s_tj_present_phase;
    return p.mode == TJ_PRESENT_PHASE_HOLD && !vblank && p.source->following &&
           cellGcm_flip_uses_vsync() &&
           rsx_d3d12_backend_display_vblank_divisor() >= 2;
}

/* Every fired VBlank, before cellGcmTickVBlank and its completions. */
static void tj_present_phase_tick(uint64_t due, bool qpc_active)
{
    TjPresentPhase& p = s_tj_present_phase;
    if (p.mode != TJ_PRESENT_PHASE_HOLD) return;
    p.tick_stamp = qpc_active ? due : 0;
    p.presents_at_tick = rsx_d3d12_backend_present_count();
    rsx_d3d12_backend_present_phase_tick(p.tick_stamp);   /* forced-offset stamp */
}

/* After the held frame's ticket completed: right after its VBlank
 * (at_tick), or early when vsync was turned off before that VBlank. */
static void tj_present_phase_release(int at_tick)
{
    TjPresentPhase& p = s_tj_present_phase;
    if (!p.enabled || !rsx_d3d12_backend_present_held()) return;
    const int presented = rsx_d3d12_backend_present_release();
    if (presented && at_tick) {
        const uint64_t now = tj_present_phase_now();
        const uint64_t hold = now && p.held_since && now >= p.held_since
            ? now - p.held_since : 0;
        ++p.held;
        p.hold_sum += hold;
        if (hold > p.hold_max) p.hold_max = hold;
    } else if (presented) {
        ++p.forced;
    }
    p.held_since = 0;
    if (presented) tj_vblank_trace_present();
}

/* The backend just held this frame: present it now when it is already in
 * phase, or when it completed at once and waits for no VBlank. The caller
 * then records it in the trace as an ordinary present. */
static void tj_present_phase_composed(int awaiting_vblank)
{
    TjPresentPhase& p = s_tj_present_phase;
    if (!rsx_d3d12_backend_present_held()) return;
    const uint64_t now = tj_present_phase_now();
    const int in_phase = now && p.tick_stamp && now >= p.tick_stamp &&
        now - p.tick_stamp <= p.early_ticks &&
        rsx_d3d12_backend_present_count() == p.presents_at_tick;
    if (!awaiting_vblank || in_phase) {
        if (rsx_d3d12_backend_present_release()) ++p.immediate;
        return;
    }
    p.held_since = now;
}

/* Trace fields; empty (unchanged line) when the phase is off. phase_swaps is
 * every successful swap-chain Present in the window (the presenter's own
 * presents= leaves out held frames the backend force-presented). */
static void tj_present_phase_trace(char* out, size_t size)
{
    TjPresentPhase& p = s_tj_present_phase;
    out[0] = '\0';
    if (!p.enabled) return;
    const uint64_t presents = rsx_d3d12_backend_present_count();
    const uint64_t paced = rsx_d3d12_backend_present_paced_count();
    const uint64_t swaps = presents - p.presents_seen;
    const uint64_t paced_window = paced - p.paced_seen;
    p.presents_seen = presents;
    p.paced_seen = paced;
    const double us = 1000000.0 / (double)p.frequency;
    if (p.mode == TJ_PRESENT_PHASE_INTERVAL) {
        snprintf(out, size, " phase_mode=interval phase_interval=%u phase_paced=%llu phase_swaps=%llu",
                 (unsigned)p.last_interval, (unsigned long long)paced_window,
                 (unsigned long long)swaps);
        return;
    }
    uint64_t stamped = 0, after_sum = 0, after_min = 0, after_max = 0;
    const uint64_t backend_forced = rsx_d3d12_backend_present_forced_take(
        &stamped, &after_sum, &after_min, &after_max);
    snprintf(out, size,
             " phase_mode=hold phase_held=%llu phase_immediate=%llu phase_forced=%llu"
             " phase_hold_us=%.0f/%.0f phase_forced_after_vblank_us=%.0f/%.0f/%.0f"
             " phase_swaps=%llu",
             (unsigned long long)p.held, (unsigned long long)p.immediate,
             (unsigned long long)(p.forced + backend_forced),
             p.held ? (double)p.hold_sum / (double)p.held * us : 0.0,
             (double)p.hold_max * us,
             stamped ? (double)after_min * us : 0.0,
             stamped ? (double)after_sum / (double)stamped * us : 0.0,
             (double)after_max * us, (unsigned long long)swaps);
    p.held = p.immediate = p.forced = p.hold_sum = p.hold_max = 0;
}

static int tj_present_flip(int vblank, tj_gcm_flip_ticket* awaiting_vblank)
{
    if (awaiting_vblank->generation) {
        if (!vblank && cellGcm_flip_uses_vsync()) return 0;
        tj_present_complete_ticket(awaiting_vblank);
        awaiting_vblank->generation = 0;
        /* TJ_PRESENT_PHASE: a held Present goes out after that completion,
         * right after this VBlank (or now, when vsync was turned off). */
        tj_present_phase_release(vblank);
        /* Do not also complete a second frame on the same display tick. */
        return 0;
    }
    tj_gcm_flip_ticket ticket = {};
    tj_gcm_flip_publication_timing publication = {};
    int took = s_tj_flip_timeline.enabled
        ? cellGcm_take_flip_ticket_timed(&ticket, &publication)
        : cellGcm_take_flip_ticket(&ticket);
    if (!took) return 0;
    tj_flip_timeline_take(ticket,
        s_tj_flip_timeline.enabled ? &publication : nullptr);
    const int phase_hold = tj_present_phase_hold_next(vblank);
    const uint32_t phase_interval = tj_present_phase_interval();
    if (phase_hold) rsx_d3d12_backend_present_hold_arm(1);
    if (phase_interval) rsx_d3d12_backend_present_interval_arm(phase_interval);
    rsx_d3d12_backend_present_buffer(ticket.generation, ticket.buffer_id);
    if (phase_interval) rsx_d3d12_backend_present_interval_arm(0);
    if (phase_hold) rsx_d3d12_backend_present_hold_arm(0);
    tj_flip_timeline_present_return();
    if (!vblank && cellGcm_flip_uses_vsync())
        *awaiting_vblank = ticket;
    else tj_present_complete_ticket(&ticket);
    if (phase_hold) tj_present_phase_composed(awaiting_vblank->generation != 0);
    return 1;
}

/* A frame already handed to DXGI only awaits the next emulated VBlank. Avoid
 * putting another potentially blocking GPU drain in front of that completion.
 * Opt-in experiment: the tick drain still runs after completing the ticket. */
static int tj_present_outer_drain_allowed(int pacing_enabled,
                                         const tj_gcm_flip_ticket* awaiting_vblank)
{
    return !pacing_enabled || !awaiting_vblank->generation ||
        !cellGcm_flip_uses_vsync();
}

static volatile LONG g_tj_present_stop;

/* TJ_VBLANK_SOURCE_TRACE=1: one line per second with the source, per-second
 * fired counts, the Present offset after the VBlank that released it, and the
 * backend's waiter / DXGI present-cadence counters. Presenter thread only. */
struct TjVBlankTrace {
    int enabled;                    /* -1 unread */
    ULONGLONG next_ms;
    uint64_t fired_display, fired_qpc;
    uint64_t vblank_qpc;            /* due stamp of the newest fired VBlank */
    uint64_t present_min, present_max, present_sum, presents;
};
static TjVBlankTrace s_tj_vblank_trace = { -1 };

static bool tj_vblank_trace_enabled()
{
    if (s_tj_vblank_trace.enabled < 0) {
        const char* value = getenv("TJ_VBLANK_SOURCE_TRACE");
        s_tj_vblank_trace.enabled = value && strcmp(value, "1") == 0;
    }
    return s_tj_vblank_trace.enabled > 0;
}

static void tj_vblank_trace_fire(uint64_t due, bool qpc_active)
{
    if (tj_vblank_trace_enabled())
        s_tj_vblank_trace.vblank_qpc = qpc_active ? due : 0;
}

static void tj_vblank_trace_present(void)
{
    TjVBlankTrace& t = s_tj_vblank_trace;
    if (t.enabled <= 0 || !t.vblank_qpc) return;
    /* TJ_PRESENT_PHASE: a held frame is not on its way yet; its release
     * records it. */
    if (s_tj_present_phase.enabled && rsx_d3d12_backend_present_held()) return;
    LARGE_INTEGER now = {};
    if (!QueryPerformanceCounter(&now) || (uint64_t)now.QuadPart < t.vblank_qpc)
        return;
    const uint64_t offset = (uint64_t)now.QuadPart - t.vblank_qpc;
    if (!t.presents || offset < t.present_min) t.present_min = offset;
    if (offset > t.present_max) t.present_max = offset;
    t.present_sum += offset;
    ++t.presents;
}

static void tj_vblank_trace_tick(const TjVBlankSource& source,
                                 uint64_t published, uint64_t frequency)
{
    if (!tj_vblank_trace_enabled()) return;
    TjVBlankTrace& t = s_tj_vblank_trace;
    const ULONGLONG now_ms = GetTickCount64();
    if (!t.next_ms) t.next_ms = now_ms + 1000;
    if (now_ms < t.next_ms) return;
    t.next_ms = now_ms + 1000;
    char waiter[384];
    rsx_d3d12_backend_display_vblank_describe(waiter, sizeof(waiter), 1);
    char phase[256];
    tj_present_phase_trace(phase, sizeof(phase));
    const double us = frequency ? 1000000.0 / (double)frequency : 0.0;
    tj_present_log_line(
            "[TJ-VBLANK-SOURCE] event=trace t_ms=%llu source=%s fired_display=%llu "
            "fired_qpc=%llu published=%llu consumed=%llu fallbacks=%llu resumes=%llu "
            "dropped=%llu presents=%llu present_after_vblank_us=%.0f/%.0f/%.0f%s %s\n",
            (unsigned long long)now_ms, source.following ? "display" : "qpc",
            (unsigned long long)(source.fired_display - t.fired_display),
            (unsigned long long)(source.fired_qpc - t.fired_qpc),
            (unsigned long long)published, (unsigned long long)source.consumed,
            (unsigned long long)source.fallbacks, (unsigned long long)source.resumes,
            (unsigned long long)source.dropped, (unsigned long long)t.presents,
            t.presents ? (double)t.present_min * us : 0.0,
            t.presents ? (double)t.present_sum / (double)t.presents * us : 0.0,
            (double)t.present_max * us, phase, waiter);
    t.fired_display = source.fired_display;
    t.fired_qpc = source.fired_qpc;
    t.present_min = t.present_max = t.present_sum = t.presents = 0;
}

static void tj_vblank_source_log_switch(const TjVBlankSource* source,
                                        const char* event)
{
    static unsigned logged;
    if (logged >= 32) return;       /* later switches: trace counters only */
    ++logged;
    char waiter[384];
    rsx_d3d12_backend_display_vblank_describe(waiter, sizeof(waiter), 0);
    fprintf(stderr, "[TJ-VBLANK-SOURCE] event=%s source=%s fallbacks=%llu resumes=%llu %s\n",
            event, source->following ? "display" : "qpc",
            (unsigned long long)source->fallbacks,
            (unsigned long long)source->resumes, waiter);
}

/* Display source only: refresh the watchdog from the backend waiter and
 * return its published tick count (0 when the source is off). Leaving QPC
 * removes the watchdog's time base, so the source is abandoned for good. */
static uint64_t tj_vblank_source_poll(TjVBlankSource* source,
                                      TjVBlankCadence* cadence, uint64_t now,
                                      bool qpc_active)
{
    if (!source->display) return 0;
    uint64_t last_tick = 0;
    const uint64_t published = rsx_d3d12_backend_display_vblank_ticks(&last_tick);
    if (!qpc_active) {
        tj_vblank_source_abandon(source, published);
        tj_vblank_source_log_switch(source, "abandon_qpc_failed");
        return 0;
    }
    if (tj_vblank_source_watch(source, cadence, now, published, last_tick))
        tj_vblank_source_log_switch(source, source->following
            ? "resume" : "fallback_no_tick_50ms");
    return published;
}

/* This context lives on the presenter stack and is registered with the
 * backend only around an outer FIFO drain. Checked GPU fence waits invoke it
 * on this same presenter thread while the drain is blocked. */
struct TjFenceVBlankServiceContext {
    TjVBlankCadence* cadence;
    bool* qpc_active;
    uint64_t* frequency;
    tj_gcm_flip_ticket* awaiting_vblank;
    int* rsx_ok;
    TjVBlankSource* source;
    int ticket_serviced;
};

static uint32_t tj_present_fence_wait_service(void* opaque,
                                               uint32_t remaining_ms)
{
    TjFenceVBlankServiceContext* context =
        (TjFenceVBlankServiceContext*)opaque;
    if (!remaining_ms) return 1;
    if (!context || !context->cadence || !context->qpc_active ||
        !context->frequency || !context->awaiting_vblank || !context->rsx_ok ||
        !context->source || context->ticket_serviced ||
        InterlockedCompareExchange(&g_tj_present_stop, 0, 0) ||
        !*context->rsx_ok || !context->awaiting_vblank->generation)
        return remaining_ms;
    /* TJ_PRESENT_PHASE=hold: a held Present goes out only from the outer
     * tick (never from inside a nested render_frame wait). Once the backend
     * has force-presented it, ticks are serviced here as before. */
    if (s_tj_present_phase.enabled && rsx_d3d12_backend_present_held())
        return remaining_ms;

    /* A mode switch retains the ordinary presenter's immediate-completion
     * behavior. It does not invent a VBlank or move the 60 Hz phase. */
    if (!cellGcm_flip_uses_vsync()) {
        context->ticket_serviced = 1;
        tj_present_complete_ticket(context->awaiting_vblank);
        context->awaiting_vblank->generation = 0;
        return remaining_ms;
    }

    const uint64_t now = tj_vblank_clock_now(
        context->cadence, context->qpc_active, context->frequency);
    const uint64_t published = tj_vblank_source_poll(
        context->source, context->cadence, now, *context->qpc_active);
    if (tj_vblank_source_due(context->source, context->cadence, now, published)) {
        const uint64_t due = tj_vblank_source_stamp(context->source, context->cadence);
        tj_flip_timeline_tick(*context->qpc_active, due);
        tj_vblank_trace_fire(due, *context->qpc_active);
        tj_present_phase_tick(due, *context->qpc_active);
        cellGcmTickVBlank();
        context->ticket_serviced = 1;
        tj_present_complete_ticket(context->awaiting_vblank);
        context->awaiting_vblank->generation = 0;
        /* Exactly one deadline is consumed here. Any additional catch-up ticks
         * remain for the presenter's normal bounded outer-loop path. */
        tj_vblank_source_advance(context->source, context->cadence, now);
        return remaining_ms;
    }
    /* Display ticks have no deadline to sleep toward: re-check each 1 ms. */
    if (context->source->following) return 1;

    /* Convert the remaining time to a positive millisecond slice, rounding up
     * so the wait reaches the deadline without polling early. Cap that slice
     * at the backend's original remaining timeout. If scaling could overflow,
     * use a conservative 1 ms slice. */
    const uint64_t until_deadline = context->cadence->deadline - now;
    uint64_t wait_ms = 1;
    if (*context->frequency && until_deadline <= UINT64_MAX / 1000ULL) {
        const uint64_t scaled = until_deadline * 1000ULL;
        wait_ms = scaled / *context->frequency;
        if (scaled % *context->frequency) ++wait_ms;
        if (!wait_ms) wait_ms = 1;
    }
    if (wait_ms > remaining_ms) wait_ms = remaining_ms;
    return wait_ms ? (uint32_t)wait_ms : 1;
}

/* The hidden runner ends its owned process with Kill(). Explicit process-local
 * events let it request and acknowledge the ring export after the sample. */
static HANDLE s_tj_flip_dump_request, s_tj_flip_dump_complete;
static void tj_flip_timeline_export_init(void)
{
    if (!s_tj_flip_timeline.enabled) return;
    char name[96];
    snprintf(name, sizeof(name), "Local\\TJFlipTimelineDump-%lu",
             (unsigned long)GetCurrentProcessId());
    s_tj_flip_dump_request = CreateEventA(NULL, TRUE, FALSE, name);
    snprintf(name, sizeof(name), "Local\\TJFlipTimelineDone-%lu",
             (unsigned long)GetCurrentProcessId());
    s_tj_flip_dump_complete = CreateEventA(NULL, TRUE, FALSE, name);
}
static void tj_flip_timeline_export_poll(void)
{
    if (!s_tj_flip_timeline.enabled || s_tj_flip_timeline.dumped ||
        !s_tj_flip_dump_request) return;
    if (WaitForSingleObject(s_tj_flip_dump_request, 0) == WAIT_OBJECT_0) {
        tj_flip_timeline_dump();
        if (s_tj_flip_dump_complete) SetEvent(s_tj_flip_dump_complete);
    }
}
/* WM_CLOSE runs on this window/presenter owner. Preserve the completed-only
 * ring before DestroyWindow can let the guest exit the process. A pending
 * explicit request keeps its normal dump-before-acknowledgement contract. */
static void tj_flip_timeline_close_dump(void)
{
    tj_flip_timeline_export_poll();
    tj_flip_timeline_dump();
}
static void tj_flip_timeline_export_close(void)
{
    if (s_tj_flip_dump_request) CloseHandle(s_tj_flip_dump_request);
    if (s_tj_flip_dump_complete) CloseHandle(s_tj_flip_dump_complete);
    s_tj_flip_dump_request = s_tj_flip_dump_complete = NULL;
}
extern "C" int tj_start_gcm_interrupt_thread(void);
extern "C" int tj_stop_gcm_interrupt_thread(void);

static DWORD WINAPI tj_present_thread(LPVOID kick_event_parameter)
{
    HANDLE fifo_kick_event = (HANDLE)kick_event_parameter;
    const int fifo_kick_wait_requested = g_tj_fifo_kick_wait_requested;
    int fifo_kick_wait_active = fifo_kick_event != NULL;
    int fifo_kick_failure_logged = 0;
    if (!guest_world_register_current("tj-present")) return 0;
    /* Windows' default ~15.6-ms timer granularity inflates the present
     * thread's Sleep(1), GCM Sleep(1), and SPURS 2-ms timed waits. Request
     * millisecond resolution for this thread's lifetime, then balance it on
     * every normal exit so the process does not retain the global request. */
    const bool timer_period_active = timeBeginPeriod(1) == TIMERR_NOERROR;
    const char* title = getenv("PS3_TITLE");
    if (!title || !*title) title = "Tokyo Jungle (ps3recomp)";

    int rsx_ok = (rsx_d3d12_backend_init(1280, 720, title) == 0);
    const int backend_initialized = rsx_ok;
    fprintf(stderr, "[rsx] backend init %s\n",
            rsx_ok ? "OK -- window open" : "FAILED");

    LARGE_INTEGER vblank_qpc_frequency = {0};
    LARGE_INTEGER vblank_qpc_now = {0};
    bool vblank_qpc_active = QueryPerformanceFrequency(&vblank_qpc_frequency) &&
        vblank_qpc_frequency.QuadPart >= 60 &&
        QueryPerformanceCounter(&vblank_qpc_now) && vblank_qpc_now.QuadPart >= 0;
    uint64_t vblank_clock_frequency = vblank_qpc_active
        ? (uint64_t)vblank_qpc_frequency.QuadPart : 1000ULL;
    uint64_t vblank_clock_now = vblank_qpc_active
        ? (uint64_t)vblank_qpc_now.QuadPart : (uint64_t)GetTickCount64();
    TjVBlankCadence vblank_cadence;
    tj_vblank_cadence_init(&vblank_cadence, vblank_clock_now,
                           vblank_clock_frequency);
    if (!vblank_qpc_active)
        fprintf(stderr, "[TJ] VBlank clock using GetTickCount64 fallback (60 Hz)\n");
    tj_flip_timeline_init(vblank_qpc_active, vblank_clock_frequency);
    tj_flip_timeline_export_init();
    rsx_d3d12_backend_set_close_observer(
        s_tj_flip_timeline.enabled ? tj_flip_timeline_close_dump : nullptr);

    /* TJ_VBLANK_SOURCE=display (opt-in): the emulated VBlank follows the
     * monitor's real vblanks (one per round(refresh/60)) under a 50 ms QPC
     * watchdog. Unset or any other value keeps the QPC cadence exactly. */
    const char* vblank_source_request = getenv("TJ_VBLANK_SOURCE");
    char vblank_source_note[192] = "";
    int display_vblank = 0;
    if (vblank_source_request && _stricmp(vblank_source_request, "display") == 0) {
        if (!rsx_ok)
            snprintf(vblank_source_note, sizeof(vblank_source_note), "reason=no_window");
        else if (!vblank_qpc_active)
            snprintf(vblank_source_note, sizeof(vblank_source_note), "reason=no_qpc");
        else
            display_vblank = rsx_d3d12_backend_display_vblank_start(
                vblank_source_note, sizeof(vblank_source_note));
    }
    const uint64_t vblank_source_now = tj_vblank_clock_now(
        &vblank_cadence, &vblank_qpc_active, &vblank_clock_frequency);
    TjVBlankSource vblank_source;
    tj_vblank_source_init(&vblank_source, display_vblank && vblank_qpc_active,
                          vblank_source_now, vblank_clock_frequency);
    const HANDLE display_vblank_event = vblank_source.display
        ? (HANDLE)rsx_d3d12_backend_display_vblank_event() : NULL;
    fprintf(stderr, "[TJ-VBLANK-SOURCE] source=%s requested=%s%s%s\n",
            vblank_source.display ? "display" : "qpc",
            vblank_source_request && *vblank_source_request
                ? vblank_source_request : "unset",
            vblank_source_note[0] ? " " : "", vblank_source_note);
    tj_present_phase_init(&vblank_source);   /* TJ_PRESENT_PHASE=1 only */

    const char* pacing_request = getenv("TJ_PENDING_FLIP_PACING");
    const int pending_flip_pacing = pacing_request && strcmp(pacing_request, "1") == 0;
    if (pending_flip_pacing)
        fprintf(stderr, "[TJ-PENDING-FLIP-PACING] enabled=1 defer=outer_drain_until_vblank\n");
    const char* fence_vblank_request = getenv("TJ_FENCE_VBLANK_SERVICE");
    const int fence_vblank_service_enabled =
        fence_vblank_request && strcmp(fence_vblank_request, "1") == 0;
    if (fence_vblank_service_enabled)
        fprintf(stderr,
                "[TJ-FENCE-VBLANK-SERVICE] enabled=1 scope=outer_pending_vsync_drain\n");
    const char* fifo_pulse_request = getenv("TJ_FENCE_VBLANK_FIFO_PULSE");
    const int fence_vblank_fifo_pulse = fence_vblank_service_enabled &&
        fifo_pulse_request && strcmp(fifo_pulse_request, "1") == 0;
    if (fence_vblank_fifo_pulse)
        fprintf(stderr,
                "[TJ-FENCE-VBLANK-FIFO-PULSE] enabled=1 scope=outer_pending_vsync_complete_packet\n");
    const int heartbeat_enabled = tj_present_heartbeat_enabled();
    uint64_t heartbeat_loop = 0;
    uint64_t heartbeat_consumed = 0;
    tj_gcm_flip_ticket awaiting_vblank = {};
    /* Closing the game window ends the game (TJ_KEEP_RUNNING_ON_CLOSE=1 keeps
     * the old headless behaviour). Guest keeps ticking until any in-flight
     * save/load finishes (max 15 s), then the process exits. */
    const int exit_on_close = backend_initialized && !getenv("TJ_KEEP_RUNNING_ON_CLOSE");
    ULONGLONG close_seen_ms = 0;
    int exit_after_close = 0;
    for (;;) {
        if (InterlockedCompareExchange(&g_tj_present_stop, 0, 0)) break;
        if (exit_on_close && !rsx_ok) {
            const ULONGLONG now_ms = GetTickCount64();
            if (!close_seen_ms) {
                close_seen_ms = now_ms;
                fprintf(stderr, "[TJ] window closed: ending game once saves finish\n");
            }
            if (!cellSaveData_busy() || now_ms - close_seen_ms >= 15000) {
                exit_after_close = 1;
                break;
            }
        }
        if (heartbeat_enabled) ++heartbeat_loop;
        tj_fifo_perf_loop& perf = tj_fifo_perf_loop_state();
        const bool perf_enabled = perf.enabled > 0;
        uint64_t loop_begin = perf_enabled ? tj_fifo_perf_now_ns(perf) : 0;
        uint64_t before_wait = tj_vblank_clock_now(
            &vblank_cadence, &vblank_qpc_active, &vblank_clock_frequency);
        uint64_t vblank_published = tj_vblank_source_poll(
            &vblank_source, &vblank_cadence, before_wait, vblank_qpc_active);
        const DWORD wait_timeout_ms = tj_vblank_source_due(
            &vblank_source, &vblank_cadence, before_wait, vblank_published) ? 0 : 1;
        const HANDLE vblank_wake = vblank_source.following ? display_vblank_event : NULL;
        uint64_t segment_begin = loop_begin;
        /* Service render-pass label waits promptly; the separate VBlank
         * deadline below controls ticks and vsynced flip completion. */
        if (fifo_kick_event) {
            tj_fifo_kick_wait_once(
                fifo_kick_event, &fifo_kick_wait_active,
                &fifo_kick_failure_logged,
                perf_enabled ? &perf.fifo_kick_wakes : NULL,
                perf_enabled ? &perf.fifo_kick_timeouts : NULL,
                perf_enabled ? &perf.fifo_kick_fallbacks : NULL,
                wait_timeout_ms, vblank_wake);
        } else {
            if (fifo_kick_wait_requested && perf_enabled)
                ++perf.fifo_kick_fallbacks;
            if (wait_timeout_ms) tj_present_nap(vblank_wake);
        }
        if (perf_enabled) {
            uint64_t elapsed = tj_fifo_perf_elapsed_ns(perf, segment_begin);
            tj_fifo_perf_add_segment(perf.sleep_ns, perf.sleep_max_ns, elapsed);
        }
        uint64_t now = tj_vblank_clock_now(
            &vblank_cadence, &vblank_qpc_active, &vblank_clock_frequency);
        vblank_published = tj_vblank_source_poll(
            &vblank_source, &vblank_cadence, now, vblank_qpc_active);

        int fired = 0;
        while (tj_vblank_source_due(&vblank_source, &vblank_cadence, now,
                                    vblank_published) && fired < 240) {
            segment_begin = perf_enabled ? tj_fifo_perf_now_ns(perf) : 0;
            const uint64_t vblank_due =
                tj_vblank_source_stamp(&vblank_source, &vblank_cadence);
            tj_flip_timeline_tick(vblank_qpc_active, vblank_due);
            tj_vblank_trace_fire(vblank_due, vblank_qpc_active);
            tj_present_phase_tick(vblank_due, vblank_qpc_active);
            cellGcmTickVBlank();
            /* Consume the completed frame before releasing the guest into its
             * next SetFlip, then advance FIFO/user interrupts after that flip
             * boundary. This preserves the one-deep ticket. */
            if (rsx_ok && tj_present_flip(1, &awaiting_vblank)) {
                if (perf_enabled) tj_fifo_perf_mark_present(perf);
                if (heartbeat_enabled) ++heartbeat_consumed;
                tj_vblank_trace_present();
            }
            if (perf_enabled) {
                uint64_t elapsed = tj_fifo_perf_elapsed_ns(perf, segment_begin);
                tj_fifo_perf_add_segment(perf.tick_present_ns, perf.tick_present_max_ns,
                                         elapsed);
                perf.ticks++;
            }
            segment_begin = perf_enabled ? tj_fifo_perf_now_ns(perf) : 0;
            if (rsx_ok)
                tj_flip_timeline_drain();
            if (perf_enabled) {
                uint64_t elapsed = tj_fifo_perf_elapsed_ns(perf, segment_begin);
                tj_fifo_perf_add_segment(perf.drain1_ns, perf.drain1_max_ns, elapsed);
            }
            tj_vblank_source_advance(&vblank_source, &vblank_cadence, now);
            fired++;
            /* A catch-up burst can drain up to 240 costly GPU batches before
             * the outer message pump. Service this window's thread between
             * ticks too, so capture, close and private controller messages
             * do not wait behind the entire burst. No guest ticks are skipped.
             * Timed into the same pump segment as the outer pump, so a modal
             * window loop here shows as pump time, not only as loop-max. */
            segment_begin = perf_enabled ? tj_fifo_perf_now_ns(perf) : 0;
            if (rsx_ok && rsx_d3d12_backend_pump_messages() != 0)
                rsx_ok = 0;
            if (perf_enabled) {
                uint64_t elapsed = tj_fifo_perf_elapsed_ns(perf, segment_begin);
                tj_fifo_perf_add_segment(perf.pump_ns, perf.pump_max_ns, elapsed);
            }
            /* Keep the existing one-shot trace control responsive while a
             * sustained backlog uses the bounded inner catch-up loop. */
            tj_flip_timeline_export_poll();
            /* Rendering/draining can cross another display deadline. Check the
             * current clock before the next due predicate, after this tick's
             * work and advance; retain the bounded catch-up/resync below. */
            now = tj_vblank_clock_now(
                &vblank_cadence, &vblank_qpc_active, &vblank_clock_frequency);
            vblank_published = tj_vblank_source_poll(
                &vblank_source, &vblank_cadence, now, vblank_qpc_active);
        }
        if (fired >= 240 && tj_vblank_source_due(&vblank_source, &vblank_cadence,
                                                 now, vblank_published))
            tj_vblank_source_resync(&vblank_source, &vblank_cadence, now,
                                    vblank_published); /* fell far behind -- resync */

        /* Also drain at the outer cadence: titles fence every render pass on
         * an RSX label the drain writes, and waiting a full 16 ms per fence
         * paces the guest to single-digit fps. */
        segment_begin = perf_enabled ? tj_fifo_perf_now_ns(perf) : 0;
        tj_dump_threads();
        if (perf_enabled) {
            uint64_t elapsed = tj_fifo_perf_elapsed_ns(perf, segment_begin);
            tj_fifo_perf_add_segment(perf.dump_profiler_ns, perf.dump_profiler_max_ns,
                                     elapsed);
        }

        if (rsx_ok) {
            segment_begin = perf_enabled ? tj_fifo_perf_now_ns(perf) : 0;
            tj_flip_timeline_offtick();
            if (tj_present_flip(0, &awaiting_vblank)) {
                if (perf_enabled) tj_fifo_perf_mark_present(perf);
                if (heartbeat_enabled) ++heartbeat_consumed;
                tj_vblank_trace_present();
            }
            if (perf_enabled) {
                uint64_t elapsed = tj_fifo_perf_elapsed_ns(perf, segment_begin);
                tj_fifo_perf_add_segment(perf.tick_present_ns, perf.tick_present_max_ns,
                                         elapsed);
            }
            if (tj_present_outer_drain_allowed(pending_flip_pacing, &awaiting_vblank)) {
                segment_begin = perf_enabled ? tj_fifo_perf_now_ns(perf) : 0;
                const int service_this_drain = fence_vblank_service_enabled && rsx_ok &&
                    !InterlockedCompareExchange(&g_tj_present_stop, 0, 0) &&
                    awaiting_vblank.generation && cellGcm_flip_uses_vsync();
                if (service_this_drain) {
                    TjFenceVBlankServiceContext service_context = {
                        &vblank_cadence, &vblank_qpc_active,
                        &vblank_clock_frequency, &awaiting_vblank, &rsx_ok,
                        &vblank_source, 0
                    };
                    rsx_d3d12_backend_set_fence_wait_service(
                        tj_present_fence_wait_service, &service_context);
                    rsx_d3d12_backend_set_fence_wait_fifo_pulse(fence_vblank_fifo_pulse);
                    tj_flip_timeline_drain();
                    rsx_d3d12_backend_set_fence_wait_service(nullptr, nullptr);
                } else {
                    tj_flip_timeline_drain();
                }
                if (perf_enabled) {
                    uint64_t elapsed = tj_fifo_perf_elapsed_ns(perf, segment_begin);
                    tj_fifo_perf_add_segment(perf.drain2_ns, perf.drain2_max_ns, elapsed);
                }
            }
            segment_begin = perf_enabled ? tj_fifo_perf_now_ns(perf) : 0;
            if (rsx_d3d12_backend_pump_messages() != 0)
                rsx_ok = 0;           /* window closed */
            if (perf_enabled) {
                uint64_t elapsed = tj_fifo_perf_elapsed_ns(perf, segment_begin);
                tj_fifo_perf_add_segment(perf.pump_ns, perf.pump_max_ns, elapsed);
            }
        }
        if (perf_enabled) {
            perf.loops++;
            uint64_t loop_elapsed = tj_fifo_perf_elapsed_ns(perf, loop_begin);
            if (loop_elapsed > perf.loop_max_ns) perf.loop_max_ns = loop_elapsed;
            uint64_t report_elapsed = tj_fifo_perf_loop_report(perf);
            if (report_elapsed) {
                /* report() reset prior window. Carry only its own cost into
                 * next window; never re-add already reported loop segments. */
                perf.loops = 1;
                perf.loop_max_ns = report_elapsed;
                tj_fifo_perf_add_segment(perf.report_ns, perf.report_max_ns,
                                         report_elapsed);
            }
        }
        if (heartbeat_enabled &&
            tj_present_heartbeat(heartbeat_loop, rsx_ok, fired,
                                 heartbeat_consumed))
            heartbeat_consumed = 0;
        tj_vblank_trace_tick(vblank_source, vblank_published, vblank_clock_frequency);
        tj_flip_timeline_export_poll();
        tj_survival_stall_snapshot_tick();
    }
    /* Clear the presenter-thread TLS hook before backend teardown even if a
     * future control-flow change adds an exit from the normal drain branch. */
    if (fence_vblank_service_enabled)
        rsx_d3d12_backend_set_fence_wait_service(nullptr, nullptr);
    rsx_d3d12_backend_set_close_observer(nullptr);
    tj_flip_timeline_dump();
    tj_flip_timeline_export_close();
    if (exit_after_close) {
        /* Guest threads are still running; skip backend teardown and end the
         * process directly after flushing logs (no CRT/DLL shutdown races). */
        fprintf(stderr, "[TJ] process exit after window close (save_busy=%d)\n",
                cellSaveData_busy());
        fflush(NULL);
        TerminateProcess(GetCurrentProcess(), 0);
    }
    if (display_vblank)
        rsx_d3d12_backend_display_vblank_stop();
    if (backend_initialized)
        rsx_d3d12_backend_shutdown();
    if (timer_period_active)
        timeEndPeriod(1);
    guest_world_unregister_current();
    return 0;
}

static int tj_gcm_interrupt_worker_enabled(void)
{
    const char* setting = getenv("TJ_GCM_INTERRUPT_WORKER");
    return !setting || strcmp(setting, "0") != 0;
}

extern "C" void tj_start_present_thread(void)
{
    if (getenv("TJ_NO_PRESENT")) {
        printf("[TJ] TJ_NO_PRESENT: no present thread (headless)\n");
        return;
    }
    if (g_tj_present_thread) return;
    int fifo_kick_requested = 0;
    HANDLE fifo_kick_event = tj_fifo_kick_wait_event(&fifo_kick_requested);
    g_tj_fifo_kick_wait_requested = fifo_kick_requested;
    fprintf(stderr, "[TJ-FIFO-KICK-WAIT] enabled=%d requested=%d\n",
            fifo_kick_event != NULL, fifo_kick_requested);
    fflush(stderr);
    /* The guest VBlank handler releases the FIFO's label64 wait. It must run
     * independently when main blocks inside the command-buffer recycler.
     * Retain an explicit process-local opt-out for cooperative diagnostics. */
    if (tj_gcm_interrupt_worker_enabled() && !tj_start_gcm_interrupt_thread()) {
        fprintf(stderr, "[TJ] GPU interrupt worker creation failed\n");
        return;
    }
    InterlockedExchange(&g_tj_present_stop, 0);
    g_tj_present_thread = CreateThread(NULL, 4u * 1024 * 1024,
                                       tj_present_thread, fifo_kick_event, 0, NULL);
    if (!g_tj_present_thread) {
        fprintf(stderr, "[TJ] present thread creation failed (%lu)\n", GetLastError());
        tj_stop_gcm_interrupt_thread();
        return;
    }
    tj_start_frame_prof_thread();
    printf("[TJ] present thread started (60 Hz vblank/flip ticker)\n");
}

extern "C" void tj_stop_present_thread(void)
{
    tj_stop_frame_prof_thread();
    InterlockedExchange(&g_tj_present_stop, 1);
    if (g_tj_present_thread) {
        WaitForSingleObject(g_tj_present_thread, INFINITE);
        CloseHandle(g_tj_present_thread);
        g_tj_present_thread = NULL;
    }
    tj_stop_gcm_interrupt_thread();
}
