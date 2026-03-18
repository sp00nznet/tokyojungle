/**
 * Tokyo Jungle Recompiled — Entry Point
 *
 * Initializes the ps3recomp runtime and launches the recompiled game code.
 */

#include <cstdio>
#include <cstdlib>
#include <cstring>

// ps3recomp public headers
#include "ps3emu/ps3types.h"
#include "ps3emu/error_codes.h"
#include "ps3emu/module.h"
#include "ps3emu/nid.h"

// ps3recomp runtime internals (C linkage)
extern "C" {
#include "ppu_context.h"
#include "vm.h"
}

// vm.h is header-only; vm_base needs a definition
// Don't include ppu_memory.h in C++ (has C11 _Atomic issues with MSVC)

// Define vm_base (declared extern in vm.h, used by all memory access)
uint8_t* vm_base = nullptr;

// Game metadata
static constexpr const char* GAME_TITLE = "Tokyo Jungle";
static constexpr const char* GAME_ID    = "NPUA80523";

// Virtual filesystem paths
static constexpr const char* GAME_DATA_PATH  = "input/USRDIR/";

/**
 * Function lookup table for indirect calls.
 * Maps PS3 guest addresses to recompiled host functions.
 */
struct func_entry {
    uint32_t guest_addr;
    void (*host_func)(ppu_context*);
};

// Build the function table from the generated declarations
// For now, we'll set up a basic dispatcher

// Placeholder: in Phase 3 we'll build the full dispatch table
static ppu_context g_main_ctx;

int main(int argc, char* argv[])
{
    printf("=== Tokyo Jungle Recompiled ===\n");
    printf("Built with ps3recomp v0.3.0\n\n");

    // 1. Initialize virtual memory
    printf("[TJ] Initializing virtual memory...\n");
    int32_t vm_rc = vm_init();
    if (vm_rc != 0) {
        fprintf(stderr, "[TJ] ERROR: VM init failed (0x%08X)\n", (unsigned)vm_rc);
        return EXIT_FAILURE;
    }
    printf("[TJ] VM initialized (base=%p)\n", (void*)vm_base);

    // 2. Initialize PPU context
    ppu_context_init(&g_main_ctx);

    // 3. Set up stack (1 MB at the stack region)
    uint32_t stack_size = 1024 * 1024;
    uint32_t stack_addr = VM_STACK_BASE;
    vm_commit(stack_addr, stack_size);
    ppu_set_stack(&g_main_ctx, stack_addr, stack_size);
    printf("[TJ] Stack at 0x%08X (1 MB), SP=0x%08X\n",
           stack_addr, (uint32_t)ppu_get_sp(&g_main_ctx));

    // 4. Load the ELF segments into VM
    printf("[TJ] Loading ELF segments into virtual memory...\n");
    // TODO: Load EBOOT.ELF code/data segments into vm_base + vaddr
    // For now, we'll just test that the infrastructure works

    // 5. Register HLE modules
    printf("[TJ] Registering HLE modules...\n");
    // The ps3recomp runtime auto-registers builtin modules
    // We just need to ensure they're initialized

    // 6. Set up TOC (Table of Contents) from the ELF
    // Tokyo Jungle TOC is at 0x359220
    g_main_ctx.gpr[2] = 0x359220;

    printf("[TJ] Runtime ready.\n");
    printf("[TJ] Entry point: 0x3428F0 (function descriptor)\n");
    printf("[TJ] TOC: 0x%08X\n", (uint32_t)g_main_ctx.gpr[2]);
    printf("\n");

    // 7. Call the entry function
    // The entry point 0x3428F0 is a function descriptor.
    // First 8 bytes = function address, next 8 bytes = TOC.
    // We need to load the ELF first to read these.
    // For now, call the first recompiled function as a test.
    printf("[TJ] Phase 3: Testing recompiled code infrastructure...\n");

    // Test: call a simple function if the code is loaded
    // func_00010204 is the first function in the binary
    // (requires ELF segments to be loaded into VM first)

    printf("[TJ] Build successful! Infrastructure is working.\n");
    printf("[TJ] Next: Load ELF segments and wire up the function dispatch table.\n");

    // Cleanup
    vm_shutdown();

    return EXIT_SUCCESS;
}
