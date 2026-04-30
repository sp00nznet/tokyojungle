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

extern "C" __declspec(thread) void (*g_trampoline_fn)(void*) = nullptr;

/* ---------------------------------------------------------------------------
 * Indirect call dispatch
 *
 * The lifter emits `ps3_indirect_call(ctx);` for bctrl/bctr; we forward
 * to TJ's existing inline `ppc_indirect_call` which has the binary-search
 * dispatch table + OPD fallback.
 * -----------------------------------------------------------------------*/

extern "C" volatile uint64_t g_indirect_call_count = 0;

extern "C" void ps3_indirect_call(ppu_context* ctx)
{
    g_indirect_call_count++;
    ppc_indirect_call(ctx);
}

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
ps3_guest_caller_fn g_ps3_guest_caller;  /* declared in ps3emu/guest_call.h */
}

static void tj_guest_caller(uint32_t opd_addr,
                            uint64_t a0, uint64_t a1,
                            uint64_t a2, uint64_t a3)
{
    if (!opd_addr) return;

    /* OPD layout: [0]=func entry, [4]=TOC, [8]=env */
    uint32_t func = vm_read32(opd_addr);
    uint32_t toc  = vm_read32(opd_addr + 4);
    if (!func) return;

    /* Bounce a small per-callback scratch stack out of a reserved high
     * region so reentrant Check calls don't trample each other. */
    static uint32_t s_cb_sp = 0xCFFE0000;

    ppu_context cb_ctx;
    ppu_context_init(&cb_ctx);
    ppu_set_stack(&cb_ctx, s_cb_sp, 0x10000);
    cb_ctx.cia    = func;
    cb_ctx.gpr[2] = toc ? toc : 0x00359220; /* TJ TOC */
    cb_ctx.gpr[3] = a0;
    cb_ctx.gpr[4] = a1;
    cb_ctx.gpr[5] = a2;
    cb_ctx.gpr[6] = a3;
    cb_ctx.lr     = 0; /* return-to-zero sentinel */
    cb_ctx.ctr    = func;

    s_cb_sp -= 0x1000;
    if (s_cb_sp < 0xCFF80000) s_cb_sp = 0xCFFE0000;

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

static volatile ppu_context* g_watchdog_ctx = nullptr;
static HANDLE g_watchdog_main_thread = NULL;

extern "C" volatile uint64_t g_indirect_call_count;

/* RIP -> guest function resolver. Walks g_dispatch_table looking for the
 * entry whose host_func is the largest <= rip; that's the recompiled body
 * containing rip. Returns the guest_addr (which doubles as the func name
 * suffix) and host offset within the function. */
static uint32_t tj_resolve_rip(uintptr_t rip, uintptr_t* out_offset)
{
    uintptr_t best = 0;
    uint32_t best_guest = 0;
    for (int i = 0; i < g_dispatch_table_size; i++) {
        uintptr_t hf = (uintptr_t)g_dispatch_table[i].host_func;
        if (hf && hf <= rip && hf > best) {
            best = hf;
            best_guest = g_dispatch_table[i].guest_addr;
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

        fprintf(stderr,
            "[WATCHDOG #%d] guest=func_%08X+0x%llX RIP=0x%llX  "
            "LR=0x%08llX CTR=0x%08llX SP=0x%08llX r3=0x%llX r4=0x%llX "
            "bctrl/2s=%llu%s\n",
            sample++, guest, (unsigned long long)off,
            (unsigned long long)hc.Rip,
            (unsigned long long)lr, (unsigned long long)ctr,
            (unsigned long long)sp,
            (unsigned long long)r3, (unsigned long long)r4,
            (unsigned long long)calls_delta,
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
