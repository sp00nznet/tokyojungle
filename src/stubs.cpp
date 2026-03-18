/**
 * Tokyo Jungle Recompiled — Game-Specific Stubs & Overrides
 *
 * This file contains function overrides, NID patches, and game-specific
 * workarounds needed to get Tokyo Jungle running under ps3recomp.
 *
 * As the recompilation progresses, stubs here will be replaced with
 * proper HLE implementations or fixes in the runtime.
 */

#include <cstdio>
#include <cstring>

#include <ps3emu/ps3types.h>
#include <ps3emu/memory.h>
#include <ps3emu/ppu_context.h>
#include <ps3emu/module.h>
#include <ps3emu/nid.h>

#include "stubs.h"

namespace tj_stubs {

// ============================================================================
// Trophy System Stubs
// ============================================================================
// Tokyo Jungle has trophies but we don't need PSN integration.
// Stub these to return success so the game doesn't hang on init.

static s32 stub_NpTrophyInit(ps3emu::ppu::Context& ctx) {
    printf("[TJ:stub] sceNpTrophyInit() -> success\n");
    return 0; // CELL_OK
}

static s32 stub_NpTrophyCreateContext(ps3emu::ppu::Context& ctx) {
    printf("[TJ:stub] sceNpTrophyCreateContext() -> success\n");
    // Write a dummy context handle
    uint32_t ctx_ptr = ctx.gpr[3];
    if (ctx_ptr) {
        ps3emu::memory::write32(ctx_ptr, 1); // dummy handle
    }
    return 0;
}

static s32 stub_NpTrophyRegisterContext(ps3emu::ppu::Context& ctx) {
    printf("[TJ:stub] sceNpTrophyRegisterContext() -> success\n");
    return 0;
}

static s32 stub_NpTrophyUnlockTrophy(ps3emu::ppu::Context& ctx) {
    uint32_t trophy_id = ctx.gpr[4];
    printf("[TJ:stub] sceNpTrophyUnlockTrophy(id=%u) -> success\n", trophy_id);
    return 0;
}

// ============================================================================
// Network Stubs
// ============================================================================
// Tokyo Jungle is single-player. Stub network calls to fail gracefully.

static s32 stub_NetInit(ps3emu::ppu::Context& ctx) {
    printf("[TJ:stub] cellNetCtlInit() -> no network\n");
    return 0;
}

// ============================================================================
// Module Loading Hooks
// ============================================================================
// Intercept cellSysmoduleLoadModule to log which modules the game loads
// and potentially provide custom handling.

static s32 hook_SysmoduleLoad(ps3emu::ppu::Context& ctx) {
    uint16_t module_id = static_cast<uint16_t>(ctx.gpr[3]);
    printf("[TJ:hook] cellSysmoduleLoadModule(0x%04X)\n", module_id);

    // Let the default handler process it
    return -1; // -1 = fall through to default
}

// ============================================================================
// Save Data Overrides
// ============================================================================
// Redirect save data to local filesystem instead of PS3 HDD

static s32 stub_SaveDataAutoSave(ps3emu::ppu::Context& ctx) {
    printf("[TJ:stub] cellSaveDataAutoSave2() -> redirecting to local\n");
    // TODO: Implement local save data handling
    return 0;
}

static s32 stub_SaveDataAutoLoad(ps3emu::ppu::Context& ctx) {
    printf("[TJ:stub] cellSaveDataAutoLoad2() -> redirecting to local\n");
    // TODO: Implement local save data handling
    return 0;
}

// ============================================================================
// Registration
// ============================================================================

void register_overrides() {
    printf("[TJ] Registering game-specific overrides...\n");

    // Trophy stubs — prevent PSN dependency
    ps3emu::module::register_nid_override(NID("sceNpTrophyInit"),            stub_NpTrophyInit);
    ps3emu::module::register_nid_override(NID("sceNpTrophyCreateContext"),    stub_NpTrophyCreateContext);
    ps3emu::module::register_nid_override(NID("sceNpTrophyRegisterContext"),  stub_NpTrophyRegisterContext);
    ps3emu::module::register_nid_override(NID("sceNpTrophyUnlockTrophy"),    stub_NpTrophyUnlockTrophy);

    // Network stubs
    ps3emu::module::register_nid_override(NID("cellNetCtlInit"),             stub_NetInit);

    // Module loading hook
    ps3emu::module::register_nid_override(NID("cellSysmoduleLoadModule"),    hook_SysmoduleLoad);

    // Save data redirection
    ps3emu::module::register_nid_override(NID("cellSaveDataAutoSave2"),      stub_SaveDataAutoSave);
    ps3emu::module::register_nid_override(NID("cellSaveDataAutoLoad2"),      stub_SaveDataAutoLoad);

    printf("[TJ] %d overrides registered\n", 8);
}

} // namespace tj_stubs
