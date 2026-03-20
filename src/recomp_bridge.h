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

static inline uint8_t vm_read8(uint64_t addr) {
    return *(vm_base + (uint32_t)addr);
}

static inline uint16_t vm_read16(uint64_t addr) {
    uint16_t raw;
    memcpy(&raw, vm_base + (uint32_t)addr, 2);
    return ps3_bswap16(raw);
}

static inline uint32_t vm_read32(uint64_t addr) {
    uint32_t raw;
    memcpy(&raw, vm_base + (uint32_t)addr, 4);
    return ps3_bswap32(raw);
}

static inline uint64_t vm_read64(uint64_t addr) {
    uint64_t raw;
    memcpy(&raw, vm_base + (uint32_t)addr, 8);
    return ps3_bswap64(raw);
}

static inline void vm_write8(uint64_t addr, uint8_t val) {
    *(vm_base + (uint32_t)addr) = val;
}

static inline void vm_write16(uint64_t addr, uint16_t val) {
    uint16_t raw = ps3_bswap16(val);
    memcpy(vm_base + (uint32_t)addr, &raw, 2);
}

static inline void vm_write32(uint64_t addr, uint32_t val) {
    uint32_t raw = ps3_bswap32(val);
    memcpy(vm_base + (uint32_t)addr, &raw, 4);
}

static inline void vm_write64(uint64_t addr, uint64_t val) {
    uint64_t raw = ps3_bswap64(val);
    memcpy(vm_base + (uint32_t)addr, &raw, 8);
}

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
 */
#ifdef _MSC_VER
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

/* LV2 syscall dispatch — uses the runtime's full dispatch table */
#include "lv2_syscall_table.h"

/*
 * Indirect call dispatch.
 * The lifter emits ((void(*)(ppu_context*))ctx->ctr)(ctx) for bctr/bctrl
 * instructions, but ctx->ctr holds a PS3 guest address, not a host pointer.
 * We dispatch through the function table instead.
 */
typedef void (*recomp_func_t)(ppu_context* ctx);
typedef struct dispatch_entry_t {
    uint32_t guest_addr;
    recomp_func_t host_func;
} dispatch_entry_t;
extern const dispatch_entry_t g_dispatch_table[];
extern const int g_dispatch_table_size;

/*
 * Runtime HLE dispatch table — for dynamically registered HLE import handlers.
 * Checked before the compile-time dispatch table so HLE imports take priority.
 */
#define HLE_DISPATCH_MAX 512
typedef struct {
    uint32_t guest_addr;
    recomp_func_t handler;
} hle_dispatch_entry_t;

extern hle_dispatch_entry_t g_hle_dispatch[];
extern int g_hle_dispatch_count;

static inline void hle_register(uint32_t guest_addr, recomp_func_t handler) {
    if (g_hle_dispatch_count < HLE_DISPATCH_MAX) {
        g_hle_dispatch[g_hle_dispatch_count].guest_addr = guest_addr;
        g_hle_dispatch[g_hle_dispatch_count].handler = handler;
        g_hle_dispatch_count++;
    }
}

static inline void ppc_indirect_call(ppu_context* ctx) {
    uint32_t target = (uint32_t)ctx->ctr;

    /* Check runtime HLE dispatch first (small table, linear scan) */
    for (int i = 0; i < g_hle_dispatch_count; i++) {
        if (g_hle_dispatch[i].guest_addr == target) {
            g_hle_dispatch[i].handler(ctx);
            return;
        }
    }

    /* Binary search the compile-time dispatch table */
    int lo = 0, hi = g_dispatch_table_size - 1;
    while (lo <= hi) {
        int mid = (lo + hi) / 2;
        if (g_dispatch_table[mid].guest_addr == target) {
            g_dispatch_table[mid].host_func(ctx);
            return;
        } else if (g_dispatch_table[mid].guest_addr < target) {
            lo = mid + 1;
        } else {
            hi = mid - 1;
        }
    }

    printf("[TJ] WARNING: indirect call to unmapped address 0x%08X (r3=0x%08X r4=0x%08X LR=0x%08X)\n",
           target, (uint32_t)ctx->gpr[3], (uint32_t)ctx->gpr[4], (uint32_t)ctx->lr);
    ctx->gpr[3] = 0; /* return NULL/OK */
}
