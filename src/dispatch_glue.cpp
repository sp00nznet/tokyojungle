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
#include <atomic>
#include <mutex>
#include <cstdlib>

#include "ppu_context.h"
#include "recomp_bridge.h"
#include "ppu_thread_exit_signal.h"
#include "guest_world_rendezvous.h"
#include "vm.h"
#include "tj_callback_stack_pool.h"
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
extern "C" __declspec(thread) ppu_context* g_active_ctx;
extern "C" void ppu_guest_callback_enter(void);
extern "C" void ppu_guest_callback_leave(void);

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
extern "C" uint32_t ppu_thread_allocate_guest_stack(uint32_t stack_size);

static uint32_t tj_allocate_callback_stack(uint32_t stack_size)
{
    /* Called under the callback pool lock. Use the fixed reservation first;
     * additional live/nested callbacks use the locked worker allocator. Each
     * allocation includes a real guard page and 64 KB of usable stack. */
    static vm_stack_alloc fixed = {0xD0400000u, 0xD0500000u};
    uint32_t base = vm_stack_allocate(&fixed, stack_size);
    return base ? base : ppu_thread_allocate_guest_stack(stack_size);
}

static void tj_guest_caller(uint32_t opd_addr,
                            uint64_t a0, uint64_t a1,
                            uint64_t a2, uint64_t a3,
                            uint64_t a4, uint64_t a5,
                            uint64_t a6, uint64_t a7)
{
    if (!opd_addr) return;

    /* This adapter is also used for non-GCM callbacks. Do not let their HLE
     * calls dispatch a pending GPU callback onto this same per-thread stack. */
    struct CallbackScope {
        CallbackScope() { ppu_guest_callback_enter(); }
        ~CallbackScope() { ppu_guest_callback_leave(); }
    } callback_scope;

    /* Capture each callback descriptor on first use. Tokyo Jungle later reuses
     * parts of its data segment while streaming title assets, including some
     * registered callback OPDs. A callback registration is immutable, so keep
     * its original {code, TOC} instead of dispatching overwritten pack bytes. */
    struct CallbackOpd { uint32_t opd, func, toc; };
    static CallbackOpd s_callbacks[64] = {};
    static uint32_t s_callback_count = 0;
    static std::mutex s_callback_lock;
    uint32_t func = 0, toc = 0;
    {
        std::lock_guard<std::mutex> lock(s_callback_lock);
        for (uint32_t i = 0; i < s_callback_count; ++i) {
            if (s_callbacks[i].opd == opd_addr) {
                func = s_callbacks[i].func;
                toc = s_callbacks[i].toc;
                break;
            }
        }
        if (!func) {
            func = vm_read32(opd_addr);
            toc = vm_read32(opd_addr + 4);
            if (func && s_callback_count < 64)
                s_callbacks[s_callback_count++] = { opd_addr, func, toc };
        }
    }
    if (!func) return;

    /* Process lifetime: callbacks can still unwind after the guest root exits.
     * Lease per invocation, not per host thread: both >16 concurrent callers
     * and nested callbacks used to overwrite another live callback's stack. */
    static auto* s_stack_pool = new TjCallbackStackPool(tj_allocate_callback_stack);
    auto stack_lease = s_stack_pool->acquire();
    if (!stack_lease.base) {
        fprintf(stderr, "[TJ] ERROR: Guest callback stack capacity exhausted (opd=%08X).\n", opd_addr);
        return;
    }
    static std::atomic<uint32_t> s_next_callback_thread { 0 };
    static thread_local uint64_t s_cb_thread_id =
        0x7FFF0000ull + s_next_callback_thread.fetch_add(1);

    ppu_context cb_ctx;
    ppu_context_init(&cb_ctx);
    ppu_set_stack(&cb_ctx, stack_lease.base, 0x10000);
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
    cb_ctx.gpr[13] = 0x01307000u;
    cb_ctx.lr     = 0; /* return-to-zero sentinel */
    cb_ctx.ctr    = func;
    ppu_context* saved_active = g_active_ctx;
    struct ActiveContextScope {
        ppu_context* saved;
        ~ActiveContextScope() { g_active_ctx = saved; }
    } active_scope{saved_active};
    cb_ctx.thread_id = saved_active ? saved_active->thread_id : s_cb_thread_id;

    /* Bounded title-start evidence; does not alter callback or input policy. */
    static const bool trace_title_callbacks = std::getenv("TJ_TITLE_STATE") != nullptr;
    static thread_local unsigned title_callback_hits = 0;
    if (trace_title_callbacks && func == 0x001F2D3Cu && title_callback_hits++ < 2) {
        const uint32_t holder = vm_read32((uint32_t)cb_ctx.gpr[2] + 0x1550);
        const uint32_t object = holder ? vm_read32(holder) : 0;
        const uint32_t vtable = object ? vm_read32(object) : 0;
        const uint32_t target = vtable ? vm_read32(vtable + 0x24) : 0;
        fprintf(stderr, "[TJ-GCM-CB-ACTOR] tid=%llu opd=%08X object=%08X virtual_opd=%08X virtual_pc=%08X\n",
                (unsigned long long)cb_ctx.thread_id, opd_addr, object, target,
                target ? vm_read32(target) : 0);
    }

    static const bool trace_guest_callbacks = std::getenv("TJ_GUEST_CB_TRACE") != nullptr;
    if (trace_guest_callbacks) {
        fprintf(stderr, "[TJ:guest-cb] opd=0x%08X func=0x%08X r3=0x%llX\n",
                opd_addr, func, (unsigned long long)a0);
        fflush(stderr);
    }

    g_active_ctx = &cb_ctx;
    try {
        ps3_indirect_call(&cb_ctx);

        /* Drain trampoline chain so the callback completes synchronously */
        while (g_trampoline_fn) {
            void (*tf)(void*) = g_trampoline_fn;
            g_trampoline_fn = nullptr;
            tf(&cb_ctx);
        }
    } catch (const ppu_thread_exit_signal&) {
        g_active_ctx = saved_active;
        if (saved_active) throw;
        g_trampoline_fn = nullptr;
        return;
    }
    g_active_ctx = saved_active;
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
static HANDLE g_watchdog_thread = NULL;
static HANDLE g_watchdog_stop_event = NULL;

extern "C" volatile uint64_t g_indirect_call_count;
extern "C" uint32_t g_last_hle_nid;
extern "C" const char* g_last_hle_name;
extern "C" volatile LONG g_tj_survival_scene_ready;
extern "C" uint8_t* vm_base;

enum {
    TJ_PHYSICS_STACK_BASE = 0xD0000000u,
    TJ_PHYSICS_STACK_END = 0xD0100000u,
    TJ_PHYSICS_OUTER_RETURN_LR = 0x0011E1B0u,
    TJ_PHYSICS_OUTER_FUNC = 0x0011C56Cu,
    TJ_PHYSICS_OUTER_ALT_FUNC = 0x0014BA68u,
};

struct tj_physics_outer_frame_snapshot {
    uint32_t sp;
    uint64_t saved_lr;
    uint32_t hit_count;
    uint32_t vector_bits[4];
    uint32_t scalar_bits;
    bool payload_valid;
};

static bool tj_physics_stack_range(uint32_t address, uint32_t size)
{
    return address >= TJ_PHYSICS_STACK_BASE &&
           address <= TJ_PHYSICS_STACK_END &&
           size <= TJ_PHYSICS_STACK_END - address;
}

static uint32_t tj_physics_read_be32(const uint8_t* bytes)
{
    return ((uint32_t)bytes[0] << 24) | ((uint32_t)bytes[1] << 16) |
           ((uint32_t)bytes[2] << 8) | bytes[3];
}

static uint64_t tj_physics_read_be64(const uint8_t* bytes)
{
    return ((uint64_t)bytes[0] << 56) | ((uint64_t)bytes[1] << 48) |
           ((uint64_t)bytes[2] << 40) | ((uint64_t)bytes[3] << 32) |
           ((uint64_t)bytes[4] << 24) | ((uint64_t)bytes[5] << 16) |
           ((uint64_t)bytes[6] << 8) | bytes[7];
}

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
    if (out_offset) *out_offset = best ? rip - best : 0;
    return best_guest;
}

/* Worker-thread diagnostics live in tj_present.cpp, but the generated
 * function table is intentionally owned by this dispatch layer. Keep one
 * resolver here and reject RIPs from DLLs/runtime code before assigning a
 * guest function name. */
extern "C" uint32_t tj_resolve_host_rip(uintptr_t rip, uintptr_t* out_offset)
{
    if (out_offset) *out_offset = 0;

    HMODULE containing = NULL;
    HMODULE self = GetModuleHandleA(NULL);
    if (!GetModuleHandleExA(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS |
                            GET_MODULE_HANDLE_EX_FLAG_UNCHANGED_REFCOUNT,
                            reinterpret_cast<LPCSTR>(rip), &containing) ||
        containing != self) {
        return 0;
    }

    static uintptr_t generated_lo = 0;
    static uintptr_t generated_hi = 0;
    if (!generated_lo) {
        uintptr_t lo = UINTPTR_MAX;
        uintptr_t hi = 0;
        for (uint64_t i = 0; i < function_table_count; ++i) {
            uintptr_t host = reinterpret_cast<uintptr_t>(function_table[i].func);
            if (host < lo) lo = host;
            if (host > hi) hi = host;
        }
        generated_lo = lo;
        generated_hi = hi;
    }
    if (rip < generated_lo || rip > generated_hi + 0x10000u)
        return 0;

    return tj_resolve_rip(rip, out_offset);
}

static DWORD WINAPI tj_watchdog_thread(LPVOID)
{
    if (!guest_world_register_current("tj-watchdog")) return 0;
    int max_samples = 30;
    if (const char* configured = getenv("TJ_WATCHDOG_SAMPLES")) {
        int parsed = atoi(configured);
        if (parsed > 0) max_samples = parsed;
    }
    int sample = 0;
    uint32_t last_guest = 0;
    int stuck_count = 0;
    uint64_t last_calls = 0;
    static const bool physics_watchdog = getenv("TJ_PHYSICS_WATCHDOG") != nullptr;
    unsigned physics_records = 0;
    for (;;) {
        if (WaitForSingleObject(g_watchdog_stop_event, 2000) == WAIT_OBJECT_0)
            break;
        volatile ppu_context* c = g_watchdog_ctx;
        HANDLE main_th = g_watchdog_main_thread;
        if (!c || !main_th) continue;

        DWORD prior_suspend = SuspendThread(main_th);
        if (prior_suspend == (DWORD)-1) {
            fprintf(stderr, "[WATCHDOG] SuspendThread failed (%lu); stopping\n",
                    GetLastError());
            fflush(stderr);
            break;
        }
        CONTEXT hc = {}; hc.ContextFlags = CONTEXT_CONTROL | CONTEXT_INTEGER;
        if (!GetThreadContext(main_th, &hc)) {
            DWORD err = GetLastError();
            DWORD prior_resume = ResumeThread(main_th);
            if (prior_resume == (DWORD)-1) {
                fprintf(stderr, "[WATCHDOG] GetThreadContext failed (%lu), ResumeThread failed (%lu); stopping\n",
                        err, GetLastError());
            } else {
                fprintf(stderr, "[WATCHDOG] GetThreadContext failed (%lu); sample skipped\n", err);
            }
            fflush(stderr);
            if (prior_resume == (DWORD)-1) break;
            continue;
        }
        uint64_t cia = c->cia, lr = c->lr, ctr = c->ctr, sp = c->gpr[1];
        uint64_t r3 = c->gpr[3], r4 = c->gpr[4];
        double f28 = 0.0, f29 = 0.0, f30 = 0.0, f31 = 0.0;
        uint64_t r18 = 0, r19 = 0, r20 = 0, r28 = 0;
        tj_physics_outer_frame_snapshot physics_frames[8] = {};
        unsigned physics_frame_count = 0;
        if (physics_watchdog) {
            f28 = c->fpr[28]; f29 = c->fpr[29];
            f30 = c->fpr[30]; f31 = c->fpr[31];
            r18 = c->gpr[18]; r19 = c->gpr[19];
            r20 = c->gpr[20]; r28 = c->gpr[28];

            /* Raw-byte snapshots only while suspended: no VM accessor, output,
             * allocation, or guest-state writes. The saved LR is in the caller
             * frame at +0x10; payload reads remain within the validated stack. */
            uint32_t child_sp = (uint32_t)sp;
            if (vm_base && sp <= 0xFFFFFFFFull && tj_physics_stack_range(child_sp, 8) &&
                (child_sp & 0xFu) == 0) {
                for (; physics_frame_count < 8; ++physics_frame_count) {
                    uint64_t caller_sp64 = tj_physics_read_be64(vm_base + child_sp);
                    if (caller_sp64 > 0xFFFFFFFFull) break;
                    uint32_t caller_sp = (uint32_t)caller_sp64;
                    if (caller_sp <= child_sp || (caller_sp & 0xFu) != 0 ||
                        !tj_physics_stack_range(caller_sp, 0x18))
                        break;
                    tj_physics_outer_frame_snapshot& frame =
                        physics_frames[physics_frame_count];
                    frame.sp = caller_sp;
                    frame.saved_lr = tj_physics_read_be64(vm_base + caller_sp + 0x10);
                    if (tj_physics_stack_range(caller_sp, 0xF04)) {
                        frame.hit_count = tj_physics_read_be32(vm_base + caller_sp + 0x7E0);
                        for (unsigned lane = 0; lane < 4; ++lane)
                            frame.vector_bits[lane] = tj_physics_read_be32(
                                vm_base + caller_sp + 0xD00 + lane * 4);
                        frame.scalar_bits = tj_physics_read_be32(vm_base + caller_sp + 0xF00);
                        frame.payload_valid = true;
                    }
                    child_sp = caller_sp;
                    if (!tj_physics_stack_range(child_sp, 8)) break;
                }
            }
        }
        uint64_t host_rvas[16] = {};
        unsigned host_rva_count = 0;
        HMODULE self = GetModuleHandleA(NULL);
        const auto* dos = reinterpret_cast<const IMAGE_DOS_HEADER*>(self);
        const auto* nt = reinterpret_cast<const IMAGE_NT_HEADERS*>(
            reinterpret_cast<const uint8_t*>(self) + dos->e_lfanew);
        const uint64_t self_base = reinterpret_cast<uint64_t>(self);
        const uint64_t self_end = self_base + nt->OptionalHeader.SizeOfImage;
        MEMORY_BASIC_INFORMATION stack_mbi = {};
        if (hc.Rsp && VirtualQuery(reinterpret_cast<LPCVOID>(hc.Rsp), &stack_mbi,
                                   sizeof(stack_mbi)) == sizeof(stack_mbi) &&
            stack_mbi.State == MEM_COMMIT &&
            !(stack_mbi.Protect & (PAGE_NOACCESS | PAGE_GUARD))) {
            const uintptr_t stack_base = reinterpret_cast<uintptr_t>(stack_mbi.BaseAddress);
            const uintptr_t stack_end = stack_base + stack_mbi.RegionSize;
            if (hc.Rsp >= stack_base && hc.Rsp < stack_end) {
                size_t words = (size_t)((stack_end - hc.Rsp) / sizeof(uint64_t));
                if (words > 256) words = 256;
                const uint64_t* host_stack = reinterpret_cast<const uint64_t*>(hc.Rsp);
                for (size_t i = 0; i < words && host_rva_count < 16; ++i) {
                    uint64_t candidate = host_stack[i];
                    if (candidate >= self_base && candidate < self_end)
                        host_rvas[host_rva_count++] = candidate - self_base;
                }
            }
        }
        if (ResumeThread(main_th) == (DWORD)-1) {
            fprintf(stderr, "[WATCHDOG] ResumeThread failed (%lu); stopping\n",
                    GetLastError());
            fflush(stderr);
            break;
        }

        uintptr_t off = 0;
        uint32_t guest = tj_resolve_rip((uintptr_t)hc.Rip, &off);
        uint64_t calls = g_indirect_call_count;
        uint64_t calls_delta = calls - last_calls;
        last_calls = calls;
        if (guest == last_guest) stuck_count++; else stuck_count = 0;
        last_guest = guest;

        fprintf(stdout,
            "[WATCHDOG #%d] guest=func_%08X+0x%llX RIP=0x%llX  "
            "CIA=0x%08llX LR=0x%08llX CTR=0x%08llX SP=0x%08llX "
            "r3=0x%llX r4=0x%llX lastHLE=0x%08X(%s) bctrl/2s=%llu%s\n",
            sample++, guest, (unsigned long long)off,
            (unsigned long long)hc.Rip,
            (unsigned long long)cia, (unsigned long long)lr,
            (unsigned long long)ctr,
            (unsigned long long)sp,
            (unsigned long long)r3, (unsigned long long)r4,
            g_last_hle_nid, g_last_hle_name ? g_last_hle_name : "",
            (unsigned long long)calls_delta,
            stuck_count > 1 ? "  [STUCK]" : "");
        fflush(stdout);
        if (stuck_count == 2) {
            fprintf(stdout, "[WATCHDOG HOSTSTACK] rsp=0x%llX exe-rvas:",
                    (unsigned long long)hc.Rsp);
            for (unsigned i = 0; i < host_rva_count; ++i)
                fprintf(stdout, " 0x%llX", (unsigned long long)host_rvas[i]);
            fputc('\n', stdout);
            fflush(stdout);
        }
        const tj_physics_outer_frame_snapshot* outer_frame = nullptr;
        if (physics_watchdog) {
            for (unsigned i = 0; i < physics_frame_count; ++i) {
                if (physics_frames[i].saved_lr == TJ_PHYSICS_OUTER_RETURN_LR &&
                    physics_frames[i].payload_valid) {
                    outer_frame = &physics_frames[i];
                    break;
                }
            }
        }
        const bool physics_outer_guest =
            guest == TJ_PHYSICS_OUTER_FUNC || guest == TJ_PHYSICS_OUTER_ALT_FUNC;
        if (physics_watchdog && physics_records < 16 &&
            InterlockedCompareExchange(&g_tj_survival_scene_ready, 0, 0) != 0 &&
            (outer_frame || physics_outer_guest)) {
            float vector[4] = {}, scalar = 0.0f;
            if (outer_frame) {
                for (unsigned lane = 0; lane < 4; ++lane)
                    memcpy(&vector[lane], &outer_frame->vector_bits[lane], sizeof(float));
                memcpy(&scalar, &outer_frame->scalar_bits, sizeof(float));
            }
            if (outer_frame) {
                fprintf(stdout,
                        "[TJ-PHYSICS-WATCHDOG #%u] guest=func_%08X+0x%llX "
                        "f28=%.9g f29=%.9g f30=%.9g f31=%.9g f31-f29=%.9g "
                        "r18=0x%llX r19=0x%llX r20=0x%llX r28=0x%llX "
                        "outer=matched sp=0x%08X return=0x%08llX hits=0x%08X "
                        "vec_raw=%08X,%08X,%08X,%08X vec=%.9g,%.9g,%.9g,%.9g "
                        "scalar_raw=%08X scalar=%.9g\n",
                        physics_records++, guest, (unsigned long long)off,
                        f28, f29, f30, f31, f31 - f29,
                        (unsigned long long)r18, (unsigned long long)r19,
                        (unsigned long long)r20, (unsigned long long)r28,
                        outer_frame->sp, (unsigned long long)outer_frame->saved_lr,
                        outer_frame->hit_count,
                        outer_frame->vector_bits[0], outer_frame->vector_bits[1],
                        outer_frame->vector_bits[2], outer_frame->vector_bits[3],
                        vector[0], vector[1], vector[2], vector[3],
                        outer_frame->scalar_bits, scalar);
            } else {
                fprintf(stdout,
                        "[TJ-PHYSICS-WATCHDOG #%u] guest=func_%08X+0x%llX "
                        "f28=%.9g f29=%.9g f30=%.9g f31=%.9g f31-f29=%.9g "
                        "r18=0x%llX r19=0x%llX r20=0x%llX r28=0x%llX "
                        "outer=unmatched collision=omitted\n",
                        physics_records++, guest, (unsigned long long)off,
                        f28, f29, f30, f31, f31 - f29,
                        (unsigned long long)r18, (unsigned long long)r19,
                        (unsigned long long)r20, (unsigned long long)r28);
            }
            fflush(stdout);
        }
        if (sample > max_samples) break;
    }
    guest_world_unregister_current();
    return 0;
}

extern "C" void tj_install_watchdog(ppu_context* ctx)
{
    if (g_watchdog_thread) return;
    HANDLE real = NULL;
    if (!DuplicateHandle(GetCurrentProcess(), GetCurrentThread(),
                         GetCurrentProcess(), &real, 0, FALSE,
                         DUPLICATE_SAME_ACCESS)) {
        fprintf(stderr, "[TJ] Watchdog main-handle duplication failed (%lu)\n",
                GetLastError());
        return;
    }
    g_watchdog_ctx = ctx;
    g_watchdog_main_thread = real;
    g_watchdog_stop_event = CreateEventA(NULL, TRUE, FALSE, NULL);
    if (!g_watchdog_stop_event) {
        CloseHandle(g_watchdog_main_thread);
        g_watchdog_main_thread = NULL;
        return;
    }
    g_watchdog_thread = CreateThread(NULL, 0, tj_watchdog_thread, NULL, 0, NULL);
    if (!g_watchdog_thread) {
        CloseHandle(g_watchdog_stop_event);
        CloseHandle(g_watchdog_main_thread);
        g_watchdog_stop_event = NULL;
        g_watchdog_main_thread = NULL;
        return;
    }
    fprintf(stdout, "[TJ] Watchdog installed (main thread=%p)\n", real);
    fflush(stdout);
}

extern "C" void tj_stop_watchdog(void)
{
    if (g_watchdog_stop_event) SetEvent(g_watchdog_stop_event);
    if (g_watchdog_thread) {
        WaitForSingleObject(g_watchdog_thread, INFINITE);
        CloseHandle(g_watchdog_thread);
        g_watchdog_thread = NULL;
    }
    if (g_watchdog_main_thread) {
        CloseHandle(g_watchdog_main_thread);
        g_watchdog_main_thread = NULL;
    }
    if (g_watchdog_stop_event) {
        CloseHandle(g_watchdog_stop_event);
        g_watchdog_stop_event = NULL;
    }
    g_watchdog_ctx = nullptr;
}

/* Caller owns returned duplicate. Present-thread diagnostics sample this handle
 * without exposing watchdog lifetime state across translation units. */
extern "C" HANDLE tj_frame_prof_main_thread(void)
{
    HANDLE copy = NULL;
    HANDLE original = g_watchdog_main_thread;
    if (original) {
        DuplicateHandle(GetCurrentProcess(), original, GetCurrentProcess(),
                        &copy, 0, FALSE, DUPLICATE_SAME_ACCESS);
    }
    return copy;
}
