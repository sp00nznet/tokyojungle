/**
 * Tokyo Jungle Recompiled — Entry Point
 *
 * Initializes the ps3recomp runtime and launches the game's main PPU thread.
 * This is the host-side bootstrap that sets up the emulated PS3 environment
 * before handing control to the recompiled game code.
 */

#include <cstdio>
#include <cstdlib>
#include <cstring>

// ps3recomp runtime headers
#include <ps3emu/ps3types.h>
#include <ps3emu/memory.h>
#include <ps3emu/ppu_context.h>
#include <ps3emu/module.h>
#include <ps3emu/syscall.h>
#include <ps3emu/thread.h>

// Generated function table from the lifter
#include "function_table.h"

// Game-specific stubs and overrides
#include "stubs.h"

// Game metadata
static constexpr const char* GAME_TITLE = "Tokyo Jungle";
static constexpr const char* GAME_ID    = "NPUA80523";
static constexpr const char* GAME_VER   = "01.00";

// Virtual filesystem paths (mirrors PS3 directory structure)
static constexpr const char* GAME_DATA_PATH  = "hdd0/game/NPUA80523/USRDIR/";
static constexpr const char* SAVE_DATA_PATH  = "hdd0/home/00000001/savedata/";

/**
 * Initialize the ps3recomp runtime subsystems.
 * Order matters — memory must be up before threads, etc.
 */
static bool init_runtime() {
    printf("[TJ] Initializing ps3recomp runtime for %s (%s)\n", GAME_TITLE, GAME_ID);

    // 1. Initialize memory subsystem (256MB main + 256MB VRAM)
    if (!ps3emu::memory::init(256 * 1024 * 1024, 256 * 1024 * 1024)) {
        fprintf(stderr, "[TJ] ERROR: Failed to initialize memory subsystem\n");
        return false;
    }
    printf("[TJ] Memory initialized (256MB main + 256MB VRAM)\n");

    // 2. Initialize syscall handlers
    if (!ps3emu::syscall::init()) {
        fprintf(stderr, "[TJ] ERROR: Failed to initialize syscall handlers\n");
        return false;
    }

    // 3. Register HLE modules
    if (!ps3emu::module::init()) {
        fprintf(stderr, "[TJ] ERROR: Failed to initialize HLE modules\n");
        return false;
    }

    // 4. Register the recompiled function table
    ps3emu::ppu::register_function_table(tj_function_table, tj_function_count);
    printf("[TJ] Registered %u recompiled functions\n", tj_function_count);

    // 5. Apply game-specific patches and stubs
    tj_stubs::register_overrides();
    printf("[TJ] Game-specific overrides applied\n");

    // 6. Set up virtual filesystem mapping
    ps3emu::fs::mount(GAME_DATA_PATH, "input/USRDIR/");
    ps3emu::fs::mount(SAVE_DATA_PATH, "savedata/");
    printf("[TJ] Virtual filesystem mounted\n");

    // 7. Initialize threading subsystem
    if (!ps3emu::thread::init(8, 6)) {  // 8 PPU threads, 6 SPU threads
        fprintf(stderr, "[TJ] ERROR: Failed to initialize threading\n");
        return false;
    }

    return true;
}

/**
 * Create and launch the game's main PPU thread.
 * This mirrors how the PS3 OS starts a game process.
 */
static bool launch_game() {
    printf("[TJ] Launching %s...\n", GAME_TITLE);

    // Find the game's entry point from the function table
    uint32_t entry_point = tj_get_entry_point();
    if (entry_point == 0) {
        fprintf(stderr, "[TJ] ERROR: Could not determine game entry point\n");
        return false;
    }
    printf("[TJ] Entry point: 0x%08X\n", entry_point);

    // Create the main PPU thread with default stack size
    ps3emu::ppu::thread_id main_thread = ps3emu::thread::create_ppu(
        entry_point,
        0,              // arg (r3)
        1024 * 1024,    // 1MB stack
        "main_thread"
    );

    if (main_thread == ps3emu::ppu::INVALID_THREAD) {
        fprintf(stderr, "[TJ] ERROR: Failed to create main PPU thread\n");
        return false;
    }

    printf("[TJ] Main thread created (id=%u), starting execution\n", main_thread);

    // Start execution — this blocks until the game exits
    return ps3emu::thread::run(main_thread);
}

int main(int argc, char* argv[]) {
    printf("=== Tokyo Jungle Recompiled ===\n");
    printf("Built with ps3recomp | Preservation in progress\n\n");

    if (!init_runtime()) {
        fprintf(stderr, "[TJ] Runtime initialization failed. Aborting.\n");
        return EXIT_FAILURE;
    }

    printf("[TJ] Runtime ready.\n\n");

    if (!launch_game()) {
        fprintf(stderr, "[TJ] Game execution failed.\n");
        return EXIT_FAILURE;
    }

    printf("[TJ] Game exited normally.\n");

    // Cleanup
    ps3emu::thread::shutdown();
    ps3emu::module::shutdown();
    ps3emu::memory::shutdown();

    return EXIT_SUCCESS;
}
