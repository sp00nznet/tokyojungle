/**
 * Tokyo Jungle Recompiled — Game-Specific Stubs & Overrides
 *
 * HLE function stubs for PS3 modules that Tokyo Jungle imports.
 * These intercept NID-resolved function calls and provide
 * host-side implementations or return-success stubs.
 */

#include <cstdio>
#include <cstring>

#include "ps3emu/ps3types.h"
#include "ps3emu/error_codes.h"
#include "ps3emu/module.h"
#include "ps3emu/nid.h"

#include "ppu_context.h"

// Inline memory access for stubs (avoid C11 _Atomic issues)
extern "C" uint8_t* vm_base;
static inline void stubs_vm_write32(uint32_t addr, uint32_t val) {
    uint32_t raw = ps3_bswap32(val);
    memcpy(vm_base + addr, &raw, 4);
}
#define vm_write32(a,v) stubs_vm_write32((uint32_t)(a), (v))

namespace tj_stubs {

// ============================================================================
// Trophy System Stubs — prevent PSN dependency
// ============================================================================

static s32 stub_NpTrophyInit() {
    printf("[TJ:stub] sceNpTrophyInit() -> CELL_OK\n");
    return CELL_OK;
}

static s32 stub_NpTrophyCreateContext(u32 ctx_ptr, u32 comm_id, u32 comm_sign, u64 options) {
    printf("[TJ:stub] sceNpTrophyCreateContext() -> CELL_OK\n");
    if (ctx_ptr) {
        vm_write32(ctx_ptr, 1); // dummy handle
    }
    return CELL_OK;
}

static s32 stub_NpTrophyRegisterContext(u32 context, u32 handle, u32 status_cb, u32 arg, u64 options) {
    printf("[TJ:stub] sceNpTrophyRegisterContext() -> CELL_OK\n");
    return CELL_OK;
}

static s32 stub_NpTrophyUnlockTrophy(u32 context, u32 handle, s32 trophy_id, u32 plat_ptr) {
    printf("[TJ:stub] sceNpTrophyUnlockTrophy(id=%d) -> CELL_OK\n", trophy_id);
    return CELL_OK;
}

// ============================================================================
// Network Stubs — single-player game, no network needed
// ============================================================================

static s32 stub_NetCtlInit() {
    printf("[TJ:stub] cellNetCtlInit() -> CELL_OK\n");
    return CELL_OK;
}

static s32 stub_NetCtlTerm() {
    printf("[TJ:stub] cellNetCtlTerm() -> CELL_OK\n");
    return CELL_OK;
}

// ============================================================================
// NP Commerce Stubs — DLC store, not needed
// ============================================================================

static s32 stub_NpCommerce2Init() {
    printf("[TJ:stub] sceNpCommerce2Init() -> CELL_OK\n");
    return CELL_OK;
}

// ============================================================================
// EULA Stub
// ============================================================================

static s32 stub_SysutilNpEulaShow(u32 type, u32 container, u32 callback, u32 userdata) {
    printf("[TJ:stub] cellSysutilNpEulaShow() -> accepted\n");
    return CELL_OK;
}

// ============================================================================
// Module loading hook — log which modules the game loads
// ============================================================================

static s32 hook_SysmoduleLoadModule(u16 module_id) {
    printf("[TJ:hook] cellSysmoduleLoadModule(0x%04X)\n", module_id);
    // Let the runtime handle it
    return CELL_OK;
}

// ============================================================================
// Registration
// ============================================================================

void register_overrides() {
    printf("[TJ] Game-specific stubs registered.\n");
    // TODO: Register NID overrides with the module system once we
    // wire up the function dispatch table.
    // For now, the stubs are defined and ready to be connected.
}

} // namespace tj_stubs
