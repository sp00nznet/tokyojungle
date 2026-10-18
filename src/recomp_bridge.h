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
#include <stdlib.h>
#ifdef _MSC_VER
#include <intrin.h>
#endif

/*
 * vm_base — the host pointer to the start of the PS3 4GB address space.
 * Defined in the runtime's vm.c, set during vm_init().
 */
#ifdef __cplusplus
extern "C" {
#endif

extern uint8_t* vm_base;

/* Native replacement for the guest's shared vsprintf/vsnprintf dispatcher.
 * The lifted formatter currently cannot run without the skipped CRT stdio
 * lock state, so generated code redirects that one body here. */
void tj_guest_vformat(ppu_context* ctx);

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
/* Initialized before a PPU context runs. UINT64_MAX disables the opt-in watch. */
extern uint64_t ppu_wwatch_word;
#ifdef __cplusplus
}
#endif

/*
 * The generated PPU chunks issue millions of plain scalar guest-memory
 * accesses per frame. Keep this path local to those translation units:
 * runtime/HLE users retain the external accessors above. Configure
 * TJ_ENABLE_GENERATED_VM_FAST_ACCESS=OFF for other runtime VM diagnostic hooks.
 * YDKJ_WWATCH stores and GCM ref-poll reads retain the external accessors.
 */
#if defined(TJ_GENERATED_VM_FAST_ACCESS)
static inline bool tj_generated_vm_store_is_watched(uint32_t ea, uint32_t width)
{
    /* The first comparison also excludes the disabled UINT64_MAX sentinel.
     * Use wide range arithmetic so a high guest address cannot wrap to zero. */
    const uint64_t watched_word = ppu_wwatch_word;
    return watched_word < (uint64_t)ea + width &&
           (uint64_t)ea < watched_word + 4u;
}

static inline uint8_t tj_generated_vm_volatile_load8(const uint8_t* address)
{
#if defined(_MSC_VER)
    return (uint8_t)__iso_volatile_load8(
        reinterpret_cast<const volatile __int8*>(address));
#else
    return *(volatile const uint8_t*)address;
#endif
}

static inline uint16_t tj_generated_vm_volatile_load16(const uint8_t* address)
{
#if defined(_MSC_VER)
    return (uint16_t)__iso_volatile_load16(
        reinterpret_cast<const volatile __int16*>(address));
#else
    return *(volatile const uint16_t*)address;
#endif
}

static inline uint32_t tj_generated_vm_volatile_load32(const uint8_t* address)
{
#if defined(_MSC_VER)
    return (uint32_t)__iso_volatile_load32(
        reinterpret_cast<const volatile __int32*>(address));
#else
    return *(volatile const uint32_t*)address;
#endif
}

static inline uint64_t tj_generated_vm_volatile_load64(const uint8_t* address)
{
#if defined(_MSC_VER)
    return (uint64_t)__iso_volatile_load64(
        reinterpret_cast<const volatile __int64*>(address));
#else
    return *(volatile const uint64_t*)address;
#endif
}

static inline void tj_generated_vm_volatile_store8(uint8_t* address, uint8_t value)
{
#if defined(_MSC_VER)
    __iso_volatile_store8(reinterpret_cast<volatile __int8*>(address), (__int8)value);
#else
    *(volatile uint8_t*)address = value;
#endif
}

static inline void tj_generated_vm_volatile_store16(uint8_t* address, uint16_t value)
{
#if defined(_MSC_VER)
    __iso_volatile_store16(reinterpret_cast<volatile __int16*>(address), (__int16)value);
#else
    *(volatile uint16_t*)address = value;
#endif
}

static inline void tj_generated_vm_volatile_store32(uint8_t* address, uint32_t value)
{
#if defined(_MSC_VER)
    __iso_volatile_store32(reinterpret_cast<volatile __int32*>(address), (__int32)value);
#else
    *(volatile uint32_t*)address = value;
#endif
}

static inline void tj_generated_vm_volatile_store64(uint8_t* address, uint64_t value)
{
#if defined(_MSC_VER)
    __iso_volatile_store64(reinterpret_cast<volatile __int64*>(address), (__int64)value);
#else
    *(volatile uint64_t*)address = value;
#endif
}

static inline uint8_t tj_generated_vm_read8(uint64_t addr)
{
    const uint32_t ea = (uint32_t)addr;
    return tj_generated_vm_volatile_load8(vm_base + ea);
}

static inline uint16_t tj_generated_vm_read16(uint64_t addr)
{
    const uint32_t ea = (uint32_t)addr;
    if ((ea & 1u) == 0)
        return ps3_bswap16(tj_generated_vm_volatile_load16(vm_base + ea));
    volatile const uint8_t* bytes = vm_base + ea;
    return ((uint16_t)bytes[0] << 8) | bytes[1];
}

static inline uint32_t tj_generated_vm_read32(uint64_t addr)
{
    const uint32_t ea = (uint32_t)addr;
    /* cellGcmFinish relies on the runtime's paced ref publication on poll. */
    if (ea == 0x03002008u) [[unlikely]]
        return vm_read32(addr);
    if ((ea & 3u) == 0)
        return ps3_bswap32(tj_generated_vm_volatile_load32(vm_base + ea));
    volatile const uint8_t* bytes = vm_base + ea;
    return ((uint32_t)bytes[0] << 24) |
           ((uint32_t)bytes[1] << 16) |
           ((uint32_t)bytes[2] << 8) |
           bytes[3];
}

static inline uint64_t tj_generated_vm_read64(uint64_t addr)
{
    const uint32_t ea = (uint32_t)addr;
    if ((ea & 7u) == 0)
        return ps3_bswap64(tj_generated_vm_volatile_load64(vm_base + ea));
    volatile const uint8_t* bytes = vm_base + ea;
    return ((uint64_t)bytes[0] << 56) |
           ((uint64_t)bytes[1] << 48) |
           ((uint64_t)bytes[2] << 40) |
           ((uint64_t)bytes[3] << 32) |
           ((uint64_t)bytes[4] << 24) |
           ((uint64_t)bytes[5] << 16) |
           ((uint64_t)bytes[6] << 8) |
           bytes[7];
}

static inline void tj_generated_vm_write8(uint64_t addr, uint8_t value)
{
    const uint32_t ea = (uint32_t)addr;
    if (tj_generated_vm_store_is_watched(ea, 1)) [[unlikely]] {
        vm_write8(ea, value);
        return;
    }
    tj_generated_vm_volatile_store8(vm_base + ea, value);
}

static inline void tj_generated_vm_write16(uint64_t addr, uint16_t value)
{
    const uint32_t ea = (uint32_t)addr;
    if (tj_generated_vm_store_is_watched(ea, 2)) [[unlikely]] {
        vm_write16(ea, value);
        return;
    }
    if ((ea & 1u) == 0) {
        tj_generated_vm_volatile_store16(vm_base + ea, ps3_bswap16(value));
        return;
    }
    volatile uint8_t* bytes = vm_base + ea;
    bytes[0] = (uint8_t)(value >> 8);
    bytes[1] = (uint8_t)value;
}

static inline void tj_generated_vm_write32(uint64_t addr, uint32_t value)
{
    const uint32_t ea = (uint32_t)addr;
    if (tj_generated_vm_store_is_watched(ea, 4)) [[unlikely]] {
        vm_write32(ea, value);
        return;
    }
    if ((ea & 3u) == 0) {
        tj_generated_vm_volatile_store32(vm_base + ea, ps3_bswap32(value));
        return;
    }
    volatile uint8_t* bytes = vm_base + ea;
    bytes[0] = (uint8_t)(value >> 24);
    bytes[1] = (uint8_t)(value >> 16);
    bytes[2] = (uint8_t)(value >> 8);
    bytes[3] = (uint8_t)value;
}

static inline void tj_generated_vm_write64(uint64_t addr, uint64_t value)
{
    const uint32_t ea = (uint32_t)addr;
    if (tj_generated_vm_store_is_watched(ea, 8)) [[unlikely]] {
        vm_write64(ea, value);
        return;
    }
    if ((ea & 7u) == 0) {
        tj_generated_vm_volatile_store64(vm_base + ea, ps3_bswap64(value));
        return;
    }
    volatile uint8_t* bytes = vm_base + ea;
    bytes[0] = (uint8_t)(value >> 56);
    bytes[1] = (uint8_t)(value >> 48);
    bytes[2] = (uint8_t)(value >> 40);
    bytes[3] = (uint8_t)(value >> 32);
    bytes[4] = (uint8_t)(value >> 24);
    bytes[5] = (uint8_t)(value >> 16);
    bytes[6] = (uint8_t)(value >> 8);
    bytes[7] = (uint8_t)value;
}

#define vm_read8(addr)       tj_generated_vm_read8((uint64_t)(addr))
#define vm_read16(addr)      tj_generated_vm_read16((uint64_t)(addr))
#define vm_read32(addr)      tj_generated_vm_read32((uint64_t)(addr))
#define vm_read64(addr)      tj_generated_vm_read64((uint64_t)(addr))
#define vm_write8(addr, v)   tj_generated_vm_write8((uint64_t)(addr), (v))
#define vm_write16(addr, v)  tj_generated_vm_write16((uint64_t)(addr), (v))
#define vm_write32(addr, v)  tj_generated_vm_write32((uint64_t)(addr), (v))
#define vm_write64(addr, v)  tj_generated_vm_write64((uint64_t)(addr), (v))
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

