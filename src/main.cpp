/**
 * Tokyo Jungle Recompiled — Entry Point
 *
 * Loads the decrypted EBOOT.ELF into virtual memory, sets up the
 * function dispatch table, and begins executing recompiled game code.
 */

#include <cstdio>
#include <cstdlib>
#include <cstring>

#ifdef _WIN32
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#endif

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

// vm_base definition (declared extern in vm.h)
uint8_t* vm_base = nullptr;

// ELF loader
#include "elf_loader.h"

// Dispatch table (defined in generated/dispatch_table.c)
typedef void (*recomp_func_t)(ppu_context* ctx);
struct dispatch_entry_t {
    uint32_t guest_addr;
    recomp_func_t host_func;
};
extern "C" const dispatch_entry_t g_dispatch_table[];
extern "C" const int g_dispatch_table_size;

static recomp_func_t dispatch_lookup(uint32_t guest_addr) {
    int lo = 0, hi = g_dispatch_table_size - 1;
    while (lo <= hi) {
        int mid = (lo + hi) / 2;
        if (g_dispatch_table[mid].guest_addr == guest_addr)
            return g_dispatch_table[mid].host_func;
        else if (g_dispatch_table[mid].guest_addr < guest_addr)
            lo = mid + 1;
        else
            hi = mid - 1;
    }
    return nullptr;
}

// Game stubs
namespace tj_stubs {
    void register_overrides();
}

// Main PPU context
static ppu_context g_main_ctx;

int main(int argc, char* argv[])
{
    // Force unbuffered stdout for crash debugging
    setvbuf(stdout, NULL, _IONBF, 0);
    setvbuf(stderr, NULL, _IONBF, 0);

    printf("=== Tokyo Jungle Recompiled ===\n");
    printf("Built with ps3recomp v0.3.0\n");
    printf("Preservation in progress\n\n");

    // 1. Initialize virtual memory
    printf("[TJ] Initializing virtual memory...\n");
    int32_t vm_rc = vm_init();
    if (vm_rc != 0) {
        fprintf(stderr, "[TJ] ERROR: VM init failed (0x%08X)\n", (unsigned)vm_rc);
        return EXIT_FAILURE;
    }
    printf("[TJ] VM initialized (base=%p)\n", (void*)vm_base);

    // 2. Load ELF
    const char* elf_path = "input/EBOOT.ELF";
    if (argc > 1) elf_path = argv[1];

    printf("[TJ] Loading ELF: %s\n", elf_path);
    ElfLoadResult elf = load_elf_into_vm(elf_path);
    if (!elf.success) {
        fprintf(stderr, "[TJ] ERROR: Failed to load ELF.\n");
        vm_shutdown();
        return EXIT_FAILURE;
    }

    printf("[TJ] Code: 0x%08X (%.1f MB), Data: 0x%08X (%.1f KB + %.1f MB BSS)\n",
           elf.code_base, elf.code_size / (1024.0 * 1024.0),
           elf.data_base, elf.data_size / 1024.0, elf.bss_size / (1024.0 * 1024.0));

    // 3. Initialize PPU context
    ppu_context_init(&g_main_ctx);
    uint32_t stack_size = 1024 * 1024;
    vm_commit(VM_STACK_BASE, stack_size);
    ppu_set_stack(&g_main_ctx, VM_STACK_BASE, stack_size);
    g_main_ctx.gpr[2] = elf.toc;  // TOC register

    // Pre-fill the stack with TOC value at offset 0x28 of every 16-byte
    // aligned position. PPC64 ABI saves TOC at SP+0x28, and the lifter
    // generates ld r2,0x28(r1) after inter-module calls. Since the binary
    // doesn't always explicitly save TOC before calls, we pre-initialize.
    {
        uint64_t toc_be = ps3_bswap64(elf.toc);
        for (uint32_t off = 0x28; off < stack_size; off += 0x10) {
            uint32_t addr = VM_STACK_BASE + off;
            memcpy(vm_base + addr, &toc_be, 8);
        }
        printf("[TJ] Stack TOC slots initialized\n");
    }

    printf("[TJ] SP=0x%08X TOC=0x%08X Entry=0x%08X\n",
           (uint32_t)g_main_ctx.gpr[1], (uint32_t)g_main_ctx.gpr[2],
           (uint32_t)elf.func_addr);

    // 4. Register stubs
    tj_stubs::register_overrides();

    // 5. Dispatch table info
    printf("[TJ] Dispatch table: %d functions\n", g_dispatch_table_size);

    // 6. Look up and call entry point
    printf("\n[TJ] ============================================\n");
    printf("[TJ]  Starting Tokyo Jungle\n");
    printf("[TJ] ============================================\n\n");

    uint32_t entry_addr = (uint32_t)elf.func_addr;
    recomp_func_t entry_func = dispatch_lookup(entry_addr);

    if (!entry_func) {
        printf("[TJ] Entry 0x%08X not in dispatch table, trying nearby...\n", entry_addr);
        // Try nearby addresses (alignment issues)
        for (int delta = -8; delta <= 8; delta += 4) {
            entry_func = dispatch_lookup(entry_addr + delta);
            if (entry_func) {
                printf("[TJ] Found at 0x%08X (offset %+d)\n", entry_addr + delta, delta);
                entry_addr += delta;
                break;
            }
        }
    }

    if (!entry_func) {
        // Entry is likely _start which calls through a function descriptor chain.
        // Let's try the first function in the code segment.
        printf("[TJ] Trying code base 0x%08X...\n", elf.code_base);
        entry_func = dispatch_lookup(elf.code_base);
        if (!entry_func) {
            // Try 0x10200 (typical _start after ELF header)
            entry_func = dispatch_lookup(0x10204);
            if (entry_func) entry_addr = 0x10204;
        } else {
            entry_addr = elf.code_base;
        }
    }

    if (entry_func) {
        printf("[TJ] Executing 0x%08X...\n\n", entry_addr);
        g_main_ctx.gpr[3] = 0; // argc

#ifdef _WIN32
        // Structured exception handling for crash debugging
        __try {
            entry_func(&g_main_ctx);
            printf("\n[TJ] Function returned. r3=0x%llX\n",
                   (unsigned long long)g_main_ctx.gpr[3]);
        }
        __except(EXCEPTION_EXECUTE_HANDLER) {
            DWORD code = GetExceptionCode();
            printf("\n[TJ] CRASH! Exception code: 0x%08lX\n", code);
            printf("[TJ] PPU state at crash:\n");
            printf("[TJ]   r1 (SP):  0x%08X\n", (uint32_t)g_main_ctx.gpr[1]);
            printf("[TJ]   r2 (TOC): 0x%08X\n", (uint32_t)g_main_ctx.gpr[2]);
            printf("[TJ]   r3:       0x%08X\n", (uint32_t)g_main_ctx.gpr[3]);
            printf("[TJ]   r4:       0x%08X\n", (uint32_t)g_main_ctx.gpr[4]);
            printf("[TJ]   r5:       0x%08X\n", (uint32_t)g_main_ctx.gpr[5]);
            printf("[TJ]   LR:       0x%08X\n", (uint32_t)g_main_ctx.lr);
            printf("[TJ]   CR:       0x%08X\n", g_main_ctx.cr);
            for (int i = 0; i < 32; i++) {
                if (g_main_ctx.gpr[i] != 0)
                    printf("[TJ]   r%d: 0x%016llX\n", i,
                           (unsigned long long)g_main_ctx.gpr[i]);
            }
        }
#else
        entry_func(&g_main_ctx);
        printf("\n[TJ] Function returned. r3=0x%llX\n",
               (unsigned long long)g_main_ctx.gpr[3]);
#endif
    } else {
        fprintf(stderr, "[TJ] ERROR: No valid entry point found.\n");
    }

    vm_shutdown();
    return EXIT_SUCCESS;
}
