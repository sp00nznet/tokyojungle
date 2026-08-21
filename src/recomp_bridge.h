/**
 * Tokyo Jungle Recompiled — Bridge Header
 *
 * Provides the ppu_context type and vm_read/vm_write functions that
 * the lifter-generated C code expects, backed by the ps3recomp runtime.
 *
 * We intentionally avoid including ppu_memory.h (C11 _Atomic issues
 * with MSVC) and instead provide our own inline memory accessors.
 */

#pragma once

/* Runtime ppu_context (has gpr, fpr, vr, cr, lr, ctr, xer, etc.) */
#include "ppu_context.h"

/* Endian swap functions from the runtime */
#include "ps3emu/endian.h"

#include <stdint.h>
#include <string.h>
#include <stdio.h>

/*
 * vm_base — the host pointer to the start of the PS3 4GB address space.
 * Defined in the runtime's vm.c, set during vm_init().
 */
#ifdef __cplusplus
extern "C" {
#endif

extern uint8_t* vm_base;

#ifdef __cplusplus
}
#endif

/*
 * Memory access functions.
 * The lifter generates calls like vm_read32(ctx->gpr[1] + 0x80)
 * where the address is uint64_t (from GPR). We truncate to uint32_t
 * since PS3 uses a 32-bit address space.
 *
 * All PS3 memory is big-endian; we swap on load/store.
 */

/* Memory accessors are DECLARED here and defined in the runtime's
 * ppu_loader.cpp, which is now part of this target. They used to be static
 * inlines in this header; once the loader translation unit joined the build it
 * saw both its own real definitions and these, and the two collided. The
 * runtime's ppu_memory.h has a separate uint32_t-taking static-inline family
 * for library code -- the lifted code wants this uint64_t one. */
#ifdef __cplusplus
extern "C" {
#endif
uint8_t  vm_read8 (uint64_t addr);
uint16_t vm_read16(uint64_t addr);
uint32_t vm_read32(uint64_t addr);
uint64_t vm_read64(uint64_t addr);
void     vm_write8 (uint64_t addr, uint8_t  val);
void     vm_write16(uint64_t addr, uint16_t val);
void     vm_write32(uint64_t addr, uint32_t val);
void     vm_write64(uint64_t addr, uint64_t val);
#ifdef __cplusplus
}
#endif

/*
 * 128-bit multiply helper for mulhd/mulhdu instructions.
 * MSVC doesn't support __int128, so we use _mul128/_umul128 intrinsics.
 */
#ifdef _MSC_VER
#include <intrin.h>
static inline int64_t ppc_mulhd(int64_t a, int64_t b) {
    int64_t hi;
    _mul128(a, b, &hi);
    return hi;
}
static inline uint64_t ppc_mulhdu(uint64_t a, uint64_t b) {
    uint64_t hi;
    _umul128(a, b, &hi);
    return hi;
}
#else
static inline int64_t ppc_mulhd(int64_t a, int64_t b) {
    return (int64_t)((__int128)(a) * (__int128)(b) >> 64);
}
static inline uint64_t ppc_mulhdu(uint64_t a, uint64_t b) {
    return (uint64_t)((unsigned __int128)(a) * (unsigned __int128)(b) >> 64);
}
#endif

/*
 * GCC built-in shims for MSVC.
 * The lifter generates __builtin_clz for cntlzw instructions.
 *
 * clang-cl also defines _MSC_VER, but there these ARE real builtins and
 * redefining them is an error -- so gate on "MSVC and not clang".
 */
#if defined(_MSC_VER) && !defined(__clang__)
#include <intrin.h>
static inline int __builtin_clz(unsigned int x) {
    unsigned long idx;
    if (_BitScanReverse(&idx, x)) return 31 - (int)idx;
    return 32;
}
static inline int __builtin_clzll(unsigned long long x) {
    unsigned long idx;
    if (_BitScanReverse64(&idx, x)) return 63 - (int)idx;
    return 64;
}
#endif

/* LV2 syscall dispatch. DECLARED, not included: the runtime's
 * lv2_syscall_table.h defines lv2_syscall as a static inline, and
 * ppu_loader.cpp -- now part of this target -- defines the real one. Pulling
 * the header in here put both in that translation unit. The lifted code only
 * needs the declaration; the loader supplies the body. */
#ifdef __cplusplus
extern "C" {
#endif
void lv2_syscall(ppu_context* ctx);
#ifdef __cplusplus
}
#endif

/*
 * Indirect call dispatch.
 * The lifter emits ((void(*)(ppu_context*))ctx->ctr)(ctx) for bctr/bctrl
 * instructions, but ctx->ctr holds a PS3 guest address, not a host pointer.
 * We dispatch through the function table instead.
 */
typedef void (*recomp_func_t)(ppu_context* ctx);

/* The lifter's function table (generated/ppu_recomp.h, defined in the
 * generated source). Declared here rather than by including that header,
 * which defines its own copy of ppu_context and would collide with the
 * runtime's. Keep the type spelling identical to the generated one. */
#ifdef __cplusplus
extern "C" {
#endif
typedef struct { uint64_t addr; void (*func)(ppu_context*); const char* name; } func_entry;
extern const func_entry function_table[];
extern const uint64_t   function_table_count;  /* entries, excluding sentinel */
#ifdef __cplusplus
}
#endif


/*
 * Runtime HLE dispatch table — for dynamically registered HLE import handlers.
 * Checked before the compile-time dispatch table so HLE imports take priority.
 */
#define HLE_DISPATCH_MAX 512
typedef struct {
    uint32_t guest_addr;
    recomp_func_t handler;
} hle_dispatch_entry_t;

#ifdef __cplusplus
extern "C" {
#endif
extern hle_dispatch_entry_t g_hle_dispatch[];
extern int g_hle_dispatch_count;
#ifdef __cplusplus
}
#endif

static inline void hle_register(uint32_t guest_addr, recomp_func_t handler) {
    if (g_hle_dispatch_count < HLE_DISPATCH_MAX) {
        g_hle_dispatch[g_hle_dispatch_count].guest_addr = guest_addr;
        g_hle_dispatch[g_hle_dispatch_count].handler = handler;
        g_hle_dispatch_count++;
    }
}

