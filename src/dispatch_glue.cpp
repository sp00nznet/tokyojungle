/*
 * Tokyo Jungle Recompiled — dispatch glue
 *
 * The ps3recomp v0.5.1+ lifter emits unconditional `extern "C"` references
 * to a small set of host-provided symbols in the source preamble:
 *
 *   - extern "C" void ps3_indirect_call(ppu_context*);
 *   - extern "C" __declspec(thread) void (*g_trampoline_fn)(void*);
 *
 * It also expects the host project to install a `g_ps3_guest_caller`
 * hook so that HLE bridges (cellSysutilCheckCallback, cellGcm vblank/flip
 * handlers, save-data completion, etc.) can dispatch back into recompiled
 * guest code.
 *
 * TJ already provides indirect call dispatch as `ppc_indirect_call` (a
 * static inline in recomp_bridge.h). This file exposes the externs the
 * lifter expects and wires them through to the existing dispatcher.
 */

#include <cstdio>
#include <cstring>
#include <cstdlib>

#include "ppu_context.h"
#include "recomp_bridge.h"
#include <ps3emu/guest_call.h>

/* ---------------------------------------------------------------------------
 * Trampoline TLS state
 *
 * The new lifter folds cross-fragment fallthrough chains into trampoline
 * stores instead of direct calls — each fragment sets g_trampoline_fn and
 * returns instead of recursing, and DRAIN_TRAMPOLINE() at every call site
 * drains the chain iteratively. This avoids exhausting the native call
 * stack on long fragment chains.
 * -----------------------------------------------------------------------*/

/* Defined by the runtime (ppu_loader.cpp) now that this target compiles it;
 * we only reference it here. */
extern "C" __declspec(thread) void (*g_trampoline_fn)(void*);

/* ---------------------------------------------------------------------------
 * Indirect call dispatch
 *
 * The lifter emits `ps3_indirect_call(ctx);` for bctrl/bctr; we forward
 * to TJ's existing inline `ppc_indirect_call` which has the binary-search
 * dispatch table + OPD fallback.
 * -----------------------------------------------------------------------*/

extern "C" volatile uint64_t g_indirect_call_count = 0;

/* ps3_indirect_call is the runtime's (ppu_loader.cpp) now -- it does the same
 * dispatch-table + OPD resolution this wrapper delegated to, so defining our
 * own only duplicated the symbol. The counter it kept was diagnostic; the
 * runtime has its own instrumentation. */
extern "C" void ps3_indirect_call(ppu_context* ctx);

/* ---------------------------------------------------------------------------
 * Guest callback dispatch hook
 *
 * Installed into ps3recomp's HLE runtime at startup. The runtime calls us
 * whenever it needs to fire a guest-registered callback (sysutil events,
 * vblank/flip handlers, save-data completion). We read the OPD at
 * `opd_addr`, build a minimal ppu_context on the host stack with a
 * dedicated guest-side scratch stack, place args in r3..r6, and invoke
 * the target via the dispatch table.
 *
 * Without this hook, HLE bridges that need to call guest code silently
 * skip the dispatch — which is why TJ's main loop hangs on
 * cellSysutilCheckCallback (no events ever fire) and cellGcmSetWaitFlip
 * (no vblank handler ever runs).
 * -----------------------------------------------------------------------*/

extern "C" {
/* Defined by libs/system/cellSysutil.c now that the ps3recomp module set is
 * linked in; we only install our hook into it. */
extern ps3_guest_caller_fn g_ps3_guest_caller;
}

/* ps3_guest_caller_fn widened from four args to eight (r3..r10) in ps3recomp;
 * this glue predated that. Take all eight and place them, so a callback that
 * reads past r6 gets the real value instead of whatever was left in the
 * register. */
static void tj_guest_caller(uint32_t opd_addr,
                            uint64_t a0, uint64_t a1,
                            uint64_t a2, uint64_t a3,
                            uint64_t a4, uint64_t a5,
                            uint64_t a6, uint64_t a7)
{
    if (!opd_addr) return;

    /* OPD layout: [0]=func entry, [4]=TOC, [8]=env */
    uint32_t func = vm_read32(opd_addr);
    uint32_t toc  = vm_read32(opd_addr + 4);
    if (!func) return;

    /* Bounce a small per-callback scratch stack out of a reserved high
     * region so reentrant Check calls don't trample each other. */
    static uint32_t s_cb_sp = 0xD04F0000;   /* see TJ_CB_STACK_BASE in main.cpp */

    ppu_context cb_ctx;
    ppu_context_init(&cb_ctx);
    ppu_set_stack(&cb_ctx, s_cb_sp, 0x10000);
    cb_ctx.cia    = func;
    cb_ctx.gpr[2] = toc ? toc : 0x00359220; /* TJ TOC */
    cb_ctx.gpr[3] = a0;
    cb_ctx.gpr[4] = a1;
    cb_ctx.gpr[5] = a2;
    cb_ctx.gpr[6] = a3;
    cb_ctx.gpr[7] = a4;
    cb_ctx.gpr[8] = a5;
    cb_ctx.gpr[9] = a6;
    cb_ctx.gpr[10] = a7;
    cb_ctx.lr     = 0; /* return-to-zero sentinel */
    cb_ctx.ctr    = func;

    s_cb_sp -= 0x1000;
    if (s_cb_sp < 0xD0410000) s_cb_sp = 0xD04F0000;

    fprintf(stderr, "[TJ:guest-cb] opd=0x%08X func=0x%08X r3=0x%llX\n",
            opd_addr, func, (unsigned long long)a0);
    fflush(stderr);

    ps3_indirect_call(&cb_ctx);

    /* Drain trampoline chain so the callback completes synchronously */
    while (g_trampoline_fn) {
        void (*tf)(void*) = g_trampoline_fn;
        g_trampoline_fn = nullptr;
        tf(&cb_ctx);
    }
}

extern "C" void tj_install_guest_caller(void)
{
    g_ps3_guest_caller = tj_guest_caller;
    printf("[TJ] guest_caller hook installed\n");
}

/* ---------------------------------------------------------------------------
 * Watchdog
 *
 * Lightweight diagnostic that periodically samples the main thread's guest
 * ppu_context and prints CIA/LR/CTR/SP plus a few caller registers. Helps
 * identify where the guest is spinning when the host process is alive but
 * stdout has gone quiet.
 *
 * Set via tj_install_watchdog(&g_main_ctx) from main(). The watchdog reads
 * the live context — fields can update mid-sample so values may be slightly
 * inconsistent across registers, but for "where are we stuck" diagnosis
 * it's plenty.
 * -----------------------------------------------------------------------*/

#include <windows.h>

extern "C" const char* g_hle_inflight[];   /* ppu_hle.cpp: in-flight HLE per guest thread */
extern "C" uint32_t    g_sc_inflight[];   /* ppu_loader.cpp: in-flight lv2 syscall */

static volatile ppu_context* g_watchdog_ctx = nullptr;
static HANDLE g_watchdog_main_thread = NULL;

extern "C" volatile uint64_t g_indirect_call_count;

/* RIP -> guest function resolver. Walks function_table looking for the entry
 * whose host body is the largest <= rip; that's the recompiled body containing
 * rip. Returns the guest_addr (which doubles as the func name suffix) and host
 * offset within the function. */
static uint32_t tj_resolve_rip(uintptr_t rip, uintptr_t* out_offset)
{
    uintptr_t best = 0;
    uint32_t best_guest = 0;
    for (uint64_t i = 0; i < function_table_count; i++) {
        uintptr_t hf = (uintptr_t)function_table[i].func;
        if (hf && hf <= rip && hf > best) {
            best = hf;
            best_guest = (uint32_t)function_table[i].addr;
        }
    }
    if (out_offset) *out_offset = rip - best;
    return best_guest;
}

static DWORD WINAPI tj_watchdog_thread(LPVOID)
{
    int sample = 0;
    uint32_t last_guest = 0;
    int stuck_count = 0;
    uint64_t last_calls = 0;
    for (;;) {
        Sleep(2000);
        volatile ppu_context* c = g_watchdog_ctx;
        HANDLE main_th = g_watchdog_main_thread;
        if (!c || !main_th) continue;

        SuspendThread(main_th);
        CONTEXT hc = {}; hc.ContextFlags = CONTEXT_CONTROL | CONTEXT_INTEGER;
        GetThreadContext(main_th, &hc);
        uint64_t lr = c->lr, ctr = c->ctr, sp = c->gpr[1];
        uint64_t r3 = c->gpr[3], r4 = c->gpr[4];
        ResumeThread(main_th);

        uintptr_t off = 0;
        uint32_t guest = tj_resolve_rip((uintptr_t)hc.Rip, &off);
        uint64_t calls = g_indirect_call_count;
        uint64_t calls_delta = calls - last_calls;
        last_calls = calls;
        if (guest == last_guest) stuck_count++; else stuck_count = 0;
        last_guest = guest;

        /* The HLE the main thread is INSIDE right now. CTR is the last bctrl
         * target, which goes stale the moment a call returns -- it kept naming
         * sys_lwmutex_unlock for a thread that had long since left it. */
        const char* in_hle = g_hle_inflight[0];

        /* TJ_POLLWATCH=0x198: the main loop parks polling a counter at
         * [r31+OFF] (func_00213A14 waits for outstanding requests to reach
         * zero). Print r31 and that word so a stuck wait says WHAT it is
         * waiting on, instead of just where. */
        uint64_t r31 = c->gpr[31];
        char pollbuf[64] = "";
        { static long off = -1;
          if (off < 0) { const char* e = getenv("TJ_POLLWATCH");
                         off = e ? strtol(e, nullptr, 0) : 0; }
          if (off > 0 && r31 && r31 < 0x100000000ull)
              snprintf(pollbuf, sizeof pollbuf, " r31=0x%08X [r31+0x%lX]=%u",
                       (uint32_t)r31, off, vm_read32((uint32_t)r31 + (uint32_t)off));
        }

        /* TJ_TIMEWATCH: the game's own clock, [[TOC+0x1860]+0x18]. func_00205E6C
         * returns it, and the main loop waits for it to pass a stored value + 3.
         * If it never advances, that wait is forever. */
        char timebuf[128] = "";
        if (getenv("TJ_TIMEWATCH")) {
            uint32_t obj = vm_read32(0x00359220u + 0x1860u);
            /* [TOC+0x154C] is the object the VBLANK handler ticks (+0x8/+0x10/
             * +0x14); [TOC+0x1860]+0x18 is the clock the main loop waits on.
             * Printing both says whether the game has any advancing time at
             * all, or just this one field stuck. */
            uint32_t vb = vm_read32(0x00359220u + 0x154Cu);
            if (obj) snprintf(timebuf, sizeof timebuf,
                              " gameclock[0x%08X]=%llu vbobj=0x%08X vb+0x14=%u",
                              obj + 0x18, (unsigned long long)vm_read64(obj + 0x18),
                              vb, vb ? vm_read32(vb + 0x14) : 0);
            else     snprintf(timebuf, sizeof timebuf, " gameclock=<obj null>");
        }

        fprintf(stderr,
            "[WATCHDOG #%d] guest=func_%08X+0x%llX RIP=0x%llX  "
            "LR=0x%08llX CTR=0x%08llX SP=0x%08llX r3=0x%llX r4=0x%llX "
            "bctrl/2s=%llu in_hle=%s syscall=%u%s%s%s\n",
            sample++, guest, (unsigned long long)off,
            (unsigned long long)hc.Rip,
            (unsigned long long)lr, (unsigned long long)ctr,
            (unsigned long long)sp,
            (unsigned long long)r3, (unsigned long long)r4,
            (unsigned long long)calls_delta,
            in_hle ? in_hle : (g_sc_inflight[0] ? "(in syscall)" : "(none -- in guest code)"),
            g_sc_inflight[0],
            pollbuf,
            timebuf,
            stuck_count > 1 ? "  [STUCK]" : "");
        fflush(stderr);
        if (sample > 30) break;
    }
    return 0;
}

extern "C" void tj_install_watchdog(ppu_context* ctx)
{
    g_watchdog_ctx = ctx;
    HANDLE real = NULL;
    DuplicateHandle(GetCurrentProcess(), GetCurrentThread(),
                    GetCurrentProcess(), &real, 0, FALSE,
                    DUPLICATE_SAME_ACCESS);
    g_watchdog_main_thread = real;
    HANDLE h = CreateThread(NULL, 0, tj_watchdog_thread, NULL, 0, NULL);
    if (h) CloseHandle(h);
    fprintf(stderr, "[TJ] Watchdog installed (main thread=%p)\n", real);
}
