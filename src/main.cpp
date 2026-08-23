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

// ps3recomp runtime internals — these headers are template-aware and
// guard their own C linkage where needed; do not wrap them in extern "C".
#include "ppu_context.h"
#include "vm.h"
#include "lv2_syscall_table.h"
#include "recomp_bridge.h"

// vm_base definition (declared extern in vm.h)
uint8_t* vm_base = nullptr;

// Global module registry and syscall table (declared extern in headers)
ps3_module_registry g_ps3_module_registry = {};
/* The syscall table lives in the runtime (lv2_register.c); we register into
 * it rather than defining our own copy. */
extern lv2_syscall_table g_lv2_syscalls;

// Runtime HLE dispatch table (declared extern in recomp_bridge.h)
hle_dispatch_entry_t g_hle_dispatch[HLE_DISPATCH_MAX] = {};
int g_hle_dispatch_count = 0;

// ELF loader
#include "elf_loader.h"

// Import table resolver and HLE heap
#include "import_resolver.h"

// HLE import handlers (cellGcm, malloc, thread, etc.)
#include "hle_imports.h"

// The lifter's function_table[] (generated/ppu_recomp.h) is the single source
// of truth for guest addr -> host body. It is emitted in ascending address
// order and sentinel-terminated; function_table_count excludes the sentinel.

static recomp_func_t dispatch_lookup(uint32_t guest_addr) {
    uint64_t lo = 0, hi = function_table_count;
    while (lo < hi) {
        uint64_t mid = lo + (hi - lo) / 2;
        uint32_t a = (uint32_t)function_table[mid].addr;
        if (a == guest_addr) return function_table[mid].func;
        if (a < guest_addr) lo = mid + 1; else hi = mid;
    }
    return nullptr;
}

// Game stubs
namespace tj_stubs {
    void register_overrides();
}

// Guest callback dispatch hook installer (defined in dispatch_glue.cpp)
extern "C" void tj_install_guest_caller(void);
extern "C" void tj_install_watchdog(ppu_context* ctx);

// Main PPU context
/* RSX local memory, as cellGcmGetConfiguration reports it. */
#define TJ_LOCAL_MEM_BASE   0xC0000000u
#define TJ_LOCAL_MEM_SIZE   0x10000000u   /* 256 MB -> ends at 0xD0000000 */

/* Guest-callback scratch stacks. These used to sit at 0xCFFE0000, which is
 * INSIDE the RSX local-memory window above -- the game bump-allocates VRAM up
 * through there, so the two would have quietly overwritten each other. Put
 * them above the main guest stack (VM_STACK_BASE 0xD0000000) instead. */
#define TJ_CB_STACK_BASE    0xD0400000u
#define TJ_CB_STACK_SPAN    0x00100000u

static ppu_context g_main_ctx;

extern "C" void ppu_register_function(uint64_t, void (*)(ppu_context*));
extern "C" void ppu_hle_register_all(void);
extern "C" void ppu_sysprx_register(void);
extern "C" int  tj_crash_filter(unsigned long code);
extern "C" void tj_start_present_thread(void);
extern "C" unsigned int ps3_hle_count(void);
extern "C" void ppu_recomp_register(void);
extern "C" void ppu_install_thread_trampoline(void);
extern "C" void cellfs_set_root_path(const char* root);
extern "C" void cellfs_add_path_mapping(const char* ps3_prefix, const char* host_path);

int main(int argc, char* argv[])
{
    /* The HLE libraries log with printf (stdout) while the guest tty and the
     * runtime diagnostics go to stderr. Redirected to a file, stdout is BLOCK
     * buffered, so a crash discards its tail -- the last few hundred lines
     * before the fault, which are exactly the interesting ones. That made
     * implemented calls look as though they had never happened. Unbuffer both
     * so the log ends where the process did. */
    setvbuf(stdout, NULL, _IONBF, 0);
    setvbuf(stderr, NULL, _IONBF, 0);
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

    // 1b. Commit page 0 as null-pointer guard (reads return 0 instead of crashing)
    vm_commit(0, 0x10000);
    printf("[TJ] Page 0 guard committed (64KB)\n");

    // 1b2. ps3recomp's cellGcmSys keeps its RSX label slots, the CellGcmControl
    // register and the FIFO-callback sentinel in guest memory at 0x03000000,
    // and never commits that itself -- it assumes the harness did. TJ commits
    // lazily, so cellGcmGetControlRegister handed the game a guest EA nothing
    // backed, and the flip handler faulted on its first read of `put`.
    //   0x03000000  256 RSX label slots
    //   0x03002000  CellGcmControl {put, get, ref}
    //   0x03002F00  FIFO command-buffer-full callback sentinel
    vm_commit(0x03000000u, 0x10000u);
    memset(vm_base + 0x03000000u, 0, 0x10000u);
    printf("[TJ] GCM label/control page committed (0x03000000, 64KB)\n");

    // 1b3. Guest-callback scratch stacks. tj_guest_caller (dispatch_glue.cpp)
    // hands each callback a stack walking down from 0xCFFE0000, but nothing
    // committed that range -- it is below VM_STACK_BASE, not part of it. Every
    // guest callback therefore faulted on its own first instruction
    // (`stdu r1,-N(r1)`). It only showed up now because this is the first
    // build where a guest callback -- the GCM flip handler -- actually fires.
    vm_commit(TJ_CB_STACK_BASE, TJ_CB_STACK_SPAN);
    printf("[TJ] Guest-callback stacks committed (0x%08X, %uKB)\n",
           TJ_CB_STACK_BASE, TJ_CB_STACK_SPAN / 1024);

    // 1b4. RSX local memory (VRAM).
    //
    // cellGcmGetConfiguration reports localAddress = 0xC0000000 -- where a
    // real PS3 maps RSX local memory -- and Tokyo Jungle takes it at its
    // word: it carves VRAM into six arenas (0xC0000000, 0xC1000000,
    // 0xC7A00000, 0xC8200000, 0xCD900000, 0xCE300000) and bump-allocates out
    // of them. None of that was ever committed, so the first memcpy into an
    // arena faulted on a perfectly valid VRAM address.
    //
    // The guest never asks for this memory -- no sys_memory_allocate, no
    // mmapper -- because on hardware cellGcmInit maps it.
    vm_commit(TJ_LOCAL_MEM_BASE, TJ_LOCAL_MEM_SIZE);
    printf("[TJ] RSX local memory committed (0x%08X, %u MB)\n",
           TJ_LOCAL_MEM_BASE, TJ_LOCAL_MEM_SIZE / (1024 * 1024));

    // 1b4. cellFs path mappings.
    //
    // There are two filesystems in play: the sys_fs SYSCALL layer (ppu_fs.cpp,
    // driven by PS3_VFS_ROOT) and the cellFs HLE layer (libs/filesystem, with
    // its own prefix table). Only the first was configured, so when the
    // installer called cellFsOpendir it asked for
    //
    //   /dev_bdvd/PS3_GAME/USRDIR/data
    //
    // which mapped to <root>/gamedata/dev_bdvd/PS3_GAME/USRDIR/data -- not
    // where this title's data actually lives. Map the disc and hdd game roots
    // onto USRDIR/ under the same VFS root the syscall layer uses.
    {
        const char* vfs = getenv("PS3_VFS_ROOT");
        if (!vfs || !*vfs) vfs = ".";
        cellfs_set_root_path(vfs);
        /* Map the whole PS3_GAME dir, not just USRDIR: the installer also
         * stats /dev_bdvd/PS3_GAME/ICON0.PNG and friends, and a USRDIR-only
         * mapping sent those to the default gamedata/dev_bdvd/ prefix, where
         * nothing exists. */
        cellfs_add_path_mapping("/dev_bdvd/PS3_GAME/", "");
        cellfs_add_path_mapping("/dev_hdd0/game/NPUA80523/", "");
        cellfs_add_path_mapping("/app_home/", "");
        printf("[TJ] cellFs root=\"%s\" (disc + hdd game dirs -> USRDIR/)\n", vfs);
    }

    // 1c. Initialize LV2 syscall dispatch table
    lv2_register_all_syscalls(&g_lv2_syscalls);

    // Register game-specific syscall stubs (return CELL_OK silently)
    // Syscall 988 (0x3DC) — sys_config/debug, called during CRT init
    static auto syscall_stub_ok = [](ppu_context*) -> int64_t { return 0; };
    lv2_syscall_register(&g_lv2_syscalls, 988, syscall_stub_ok);

    printf("[TJ] LV2 syscall table initialized\n");

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

    // 2b. Resolve import table (populate PLT function descriptors)
    // Populate ps3recomp's NID -> handler registry (src/gen/ppu_hle_nids.cpp,
    // generated by ps3recomp/tools/gen_hle_nids.py --all). Without this the
    // registry is empty, ps3_hle_has() answers no for every import, and they
    // all fall back to a return-0 stub -- including all of cellResc, which is
    // how this game reaches the display.
    ppu_hle_register_all();
    /* Boot-critical context-aware handlers that live outside the generated
     * table: _cellGcmInitBody (NID 0x15BAE46B), sys_initialize_tls, ... .
     * Without this the GCM init stays on TJ's stub, ps3recomp's GCM never
     * gets a FIFO or control register, and the game spins in its flip loop. */
    ppu_sysprx_register();
    printf("[HLE] ps3recomp NID registry: %u handlers\n", ps3_hle_count());

    resolve_all_imports(elf.toc);

    // 2c. Register HLE import handlers for critical functions
    register_hle_imports(elf.toc);
    register_generic_imports();
    prefer_ps3recomp_graphics(elf.toc);

    // 2d. Mirror them into the runtime's dispatch table. Indirect calls now go
    // through ps3recomp's ps3_indirect_call (ppu_loader.cpp), which resolves via
    // ppu_lookup(); without this the whole 0x011xxxxx HLE range is unreachable
    // and every import call reports "unresolved indirect call".
    {
        ppu_recomp_register();        /* the lifted function table */
        /* Without this every sys_ppu_thread_create'd thread spawns and
         * exits immediately without running a single guest instruction:
         * ppu_run() normally installs the trampoline, and TJ dispatches
         * the guest entry itself. The threads report FINISHED, so it
         * looks fine -- the game just never gets what they were for. */
        ppu_install_thread_trampoline();
        for (int i = 0; i < g_hle_dispatch_count; i++)
            ppu_register_function(g_hle_dispatch[i].guest_addr,
                                  g_hle_dispatch[i].handler);
    }

    // 3. Initialize PPU context
    ppu_context_init(&g_main_ctx);
    uint32_t stack_size = 1024 * 1024;
    vm_commit(VM_STACK_BASE, stack_size);
    ppu_set_stack(&g_main_ctx, VM_STACK_BASE, stack_size);
    g_main_ctx.gpr[2] = elf.toc;  // TOC register

    // --- Thread-local storage -------------------------------------------
    // r13 is the PPC64 thread pointer. Nothing was setting it, so it stayed
    // 0 and every TLS access dereferenced a small negative address: the
    // guest's own printf starts `addis r7,r13,0 / addi r7,r7,-28492`, which
    // with r13=0 gives r7 = 0xFFFFFFFFFFFF90B4 and faults as soon as main
    // logs anything.
    //
    // PPC64 ELF places the static TLS block at TP - 0x7000 and the linker
    // folds that bias into every @tprel offset, so -28492 (-0x6F4C) means
    // tls_base + 0xB4 -- inside the 0x134-byte block this ELF declares.
    // __sys_init_tls would normally do this; its import is unimplemented.
    if (elf.tls_memsz) {
        const uint32_t TLS_BASE = 0x01300000;   // free: HLE ends at 0x011FFFFF,
        const uint32_t TLS_BIAS = 0x7000;       // bump heap starts at 0x02000000
        uint32_t span = TLS_BIAS + elf.tls_memsz + 0x1000;
        vm_commit(TLS_BASE, (span + 0xFFFF) & ~0xFFFFu);
        memset(vm_base + TLS_BASE, 0, span);
        if (elf.tls_filesz)
            memcpy(vm_base + TLS_BASE, vm_base + elf.tls_vaddr, elf.tls_filesz);
        g_main_ctx.gpr[13] = TLS_BASE + TLS_BIAS;
        printf("[TJ] TLS block at 0x%08X (image 0x%08X, %u bytes), r13=0x%08X\n",
               TLS_BASE, elf.tls_vaddr, elf.tls_memsz,
               (uint32_t)g_main_ctx.gpr[13]);
    }

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

    // 4b. Install guest_caller hook so HLE bridges (cellSysutilCheckCallback,
    // cellGcm vblank/flip handlers, save-data completion) can dispatch back
    // into recompiled guest code. Without this the main loop hangs on
    // vsync/flip and sysutil never fires events.
    tj_install_guest_caller();
    tj_install_watchdog(&g_main_ctx);

    // 5. Dispatch table info
    printf("[TJ] Dispatch table: %llu functions\n", (unsigned long long)function_table_count);

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
    tj_start_present_thread();

        printf("[TJ] Executing 0x%08X...\n\n", entry_addr);
        /* PS3 _start ABI: r3 = argc, r4 = argv, r5 = envp. This was argc=0
         * with no argv at all, and the game noticed -- its own startup log
         * prints "main %s" and it came out as "main (null)". A PS3 title
         * derives its data directory from argv[0] (the EBOOT path), so with
         * none it never built a content path, never opened a file, and ran an
         * empty frame loop forever: flipping at 60 Hz with nothing to draw.
         *
         * /app_home is the prefix ppu_fs.cpp strips before appending the rest
         * to PS3_VFS_ROOT, so /app_home/USRDIR/... lands on input/USRDIR/...,
         * which is where this title's data/ actually is. */
        {
            const uint32_t ARGV_BASE = 0x00F00000u;
            const char* argv0 = getenv("PS3_ARGV0");
            if (!argv0 || !*argv0) argv0 = "/app_home/USRDIR/EBOOT.BIN";

            vm_commit(ARGV_BASE, 0x10000u);
            memset(vm_base + ARGV_BASE, 0, 0x10000u);

            uint32_t str_ea = ARGV_BASE + 0x100;
            strcpy((char*)(vm_base + str_ea), argv0);
            vm_write32(ARGV_BASE + 0, str_ea);   /* argv[0]            */
            vm_write32(ARGV_BASE + 4, 0);        /* argv[1] = NULL     */

            uint32_t envp_ea = ARGV_BASE + 0x200;
            vm_write32(envp_ea, 0);              /* envp[0] = NULL     */

            g_main_ctx.gpr[3] = 1;               /* argc               */
            g_main_ctx.gpr[4] = ARGV_BASE;       /* argv               */
            g_main_ctx.gpr[5] = envp_ea;         /* envp               */
            printf("[TJ] argv[0] = \"%s\"\n", argv0);
        }

#ifdef _WIN32
        // Structured exception handling for crash debugging
        __try {
            entry_func(&g_main_ctx);
            printf("\n[TJ] Function returned. r3=0x%llX\n",
                   (unsigned long long)g_main_ctx.gpr[3]);
        }
        __except(GetExceptionCode() == EXCEPTION_ACCESS_VIOLATION ?
                 tj_crash_filter(GetExceptionCode()) : EXCEPTION_CONTINUE_SEARCH) {
            DWORD code = GetExceptionCode();
            printf("\n[TJ] CRASH! Exception code: 0x%08lX\n", code);
            printf("[TJ] CTR (last indirect target): 0x%08X\n",
                   (uint32_t)g_main_ctx.ctr);
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
