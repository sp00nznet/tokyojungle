/**
 * Tokyo Jungle Recompiled — Import Table Resolver
 *
 * Populates the PS3 binary's import table (PLT) with function descriptors
 * pointing to HLE stub functions. This allows the PLT code stubs at
 * 0x0025Fxxx to dispatch correctly through ppc_indirect_call().
 *
 * Architecture:
 *   1. Reserve a VM region (0x01000000) for HLE function descriptors (OPDs)
 *   2. For each import, create an OPD {hle_guest_addr, TOC}
 *   3. Write the OPD address into the import table slot (0x342xxx)
 *   4. The HLE function at hle_guest_addr is registered in the dispatch table
 *
 * Since we can't add new entries to the compile-time dispatch table,
 * we instead override the PLT code stubs directly in ppu_stubs.c.
 * This header provides the import table population for any stubs that
 * still use the indirect call path.
 */

#pragma once

#include <cstdio>
#include <cstring>

extern "C" {
#include "ppu_context.h"
#include "vm.h"
}

// Forward declaration
extern uint8_t* vm_base;

/* ========================================================================
 * HLE Heap Allocator
 *
 * Simple bump allocator for game heap allocations. The PS3 CRT's
 * __sys_init_heap sets up the real allocator, but since we stub it,
 * we provide our own.
 * ====================================================================== */

static uint32_t g_heap_base  = 0x02000000;  // Start of heap region
static uint32_t g_heap_ptr   = 0x02000000;  // Current allocation pointer
static uint32_t g_heap_end   = 0x08000000;  // 96 MB heap space
static int      g_heap_inited = 0;

void hle_heap_init() {
    if (g_heap_inited) return;
    // Commit the heap region
    vm_commit(g_heap_base, g_heap_end - g_heap_base);
    memset(vm_base + g_heap_base, 0, g_heap_end - g_heap_base);
    g_heap_inited = 1;
    printf("[HLE] Heap initialized: 0x%08X - 0x%08X (%u MB)\n",
           g_heap_base, g_heap_end, (g_heap_end - g_heap_base) / (1024*1024));
}

uint32_t hle_malloc(uint32_t size) {
    if (!g_heap_inited) hle_heap_init();
    // Align to 16 bytes
    size = (size + 15) & ~15u;
    if (size == 0) size = 16;

    uint32_t addr = g_heap_ptr;
    g_heap_ptr += size;

    if (g_heap_ptr > g_heap_end) {
        printf("[HLE] ERROR: heap exhausted! (requested %u bytes)\n", size);
        return 0;
    }
    return addr;
}

uint32_t hle_calloc(uint32_t count, uint32_t size) {
    uint32_t total = count * size;
    uint32_t addr = hle_malloc(total);
    if (addr) {
        memset(vm_base + addr, 0, total);
    }
    return addr;
}

uint32_t hle_memalign(uint32_t align, uint32_t size) {
    if (!g_heap_inited) hle_heap_init();
    if (align < 16) align = 16;
    // Align the current pointer
    g_heap_ptr = (g_heap_ptr + align - 1) & ~(align - 1);
    return hle_malloc(size);
}

void hle_free(uint32_t addr) {
    // Bump allocator doesn't free — this is fine for initial bring-up
    (void)addr;
}

/* ========================================================================
 * Import Table Population
 *
 * Write function descriptors into the import table slots so that
 * PLT stubs can resolve to our HLE implementations.
 * ====================================================================== */

// Helper: write a 32-bit big-endian value to VM
static inline void import_write32(uint32_t addr, uint32_t val) {
    uint32_t be = ((val >> 24) & 0xFF) | ((val >> 8) & 0xFF00) |
                  ((val << 8) & 0xFF0000) | ((val << 24) & 0xFF000000u);
    memcpy(vm_base + addr, &be, 4);
}

// OPD area: function descriptors for HLE imports
#define HLE_OPD_BASE 0x01000000u
#define HLE_OPD_SIZE 0x00010000u  // 64KB, enough for 8192 OPDs

static uint32_t g_next_opd = HLE_OPD_BASE;

/**
 * Create a function descriptor (OPD) that points to a given guest address.
 * Returns the address of the OPD.
 */
static uint32_t create_opd(uint32_t code_addr, uint32_t toc) {
    uint32_t opd_addr = g_next_opd;
    g_next_opd += 8;
    import_write32(opd_addr, code_addr);
    import_write32(opd_addr + 4, toc);
    return opd_addr;
}

/**
 * Populate a single import table slot.
 * import_slot: address in the data segment (0x342xxx) where the OPD pointer goes
 * code_addr: guest address of the HLE function (must be in dispatch table)
 * toc: TOC value (0x00359220 for this binary)
 */
static void resolve_import(uint32_t import_slot, uint32_t code_addr, uint32_t toc) {
    uint32_t opd_addr = create_opd(code_addr, toc);
    import_write32(import_slot, opd_addr);
}

/* ========================================================================
 * Resolve all imports
 *
 * For each import, we point its OPD at a known function in the dispatch
 * table. Since most HLE functions are simple return-OK stubs, we create
 * a few shared stubs and point many imports to the same stub.
 * ====================================================================== */

// Well-known stub addresses (these functions exist in ppu_stubs.c or ppu_recomp.c)
// We reuse existing CRT stub addresses for generic return-OK behavior
#define HLE_STUB_RETURN_OK 0x0025FC00  // __sys_initialize — returns r3=0

static void resolve_all_imports(uint32_t toc) {
    // Commit the OPD area
    vm_commit(HLE_OPD_BASE, HLE_OPD_SIZE);
    memset(vm_base + HLE_OPD_BASE, 0, HLE_OPD_SIZE);

    printf("[HLE] Resolving import table...\n");

    // Default: point all imports to a generic return-OK stub
    // The import table data addresses and their modules/NIDs are from imports.json
    // For now, we resolve ALL import slots to the generic stub,
    // then override specific ones with real implementations.

    // =====================================================================
    // sysPrxForUser — critical system functions
    // =====================================================================
    // These are the most important for getting game code running.
    // The PLT code stubs read from these data addresses:
    //   0x342810 = sys_lwmutex_lock      (code stub 0x0025FB00)
    //   0x342818 = sys_lwmutex_unlock    (code stub 0x0025FB40)
    //   0x342820 = sys_ppu_thread_create (code stub 0x0025FB80)
    //   0x342828 = _sys_process_atexitspawn
    //   0x342830 = sys_lwmutex_create
    //   0x342838 = sys_ppu_thread_get_id
    //   0x342840 = sys_prx_register_library
    //   0x342848 = _sys_spu_printf_initialize
    //   0x342850 = unknown_744680A2
    //   0x342858 = sys_time_get_system_time
    //   0x342860 = _sys_process_at_Exitspawn
    //   0x342868 = sys_prx_exitspawn_with_level
    //   0x342870 = sys_ppu_thread_once
    //   0x342878 = sys_ppu_thread_exit
    //   0x342880 = sys_lwmutex_destroy
    //   0x342888 = _sys_spu_printf_finalize
    //   0x342890 = sys_process_exit

    // Point all sysPrxForUser imports to return-OK stub initially
    for (uint32_t slot = 0x342810; slot <= 0x342890; slot += 8) {
        resolve_import(slot, HLE_STUB_RETURN_OK, toc);
    }

    // =====================================================================
    // cellGcmSys — Graphics (33 functions)
    // =====================================================================
    for (uint32_t slot = 0x3424AC; slot <= 0x3425AC; slot += 8) {
        resolve_import(slot, HLE_STUB_RETURN_OK, toc);
    }

    // =====================================================================
    // cellSysutil — System utilities (21 functions)
    // =====================================================================
    for (uint32_t slot = 0x3426D4; slot <= 0x342774; slot += 8) {
        resolve_import(slot, HLE_STUB_RETURN_OK, toc);
    }

    // =====================================================================
    // cellSysmodule — Module loading (4 functions)
    // =====================================================================
    for (uint32_t slot = 0x3426C4; slot <= 0x3426DC; slot += 8) {
        resolve_import(slot, HLE_STUB_RETURN_OK, toc);
    }

    // =====================================================================
    // cellFont / cellFontFT — Font system (31 functions)
    // =====================================================================
    for (uint32_t slot = 0x342414; slot <= 0x3424FC; slot += 8) {
        resolve_import(slot, HLE_STUB_RETURN_OK, toc);
    }

    // =====================================================================
    // cellAudio — Audio (11 functions)
    // =====================================================================
    for (uint32_t slot = 0x3423E8; slot <= 0x342438; slot += 8) {
        resolve_import(slot, HLE_STUB_RETURN_OK, toc);
    }

    // =====================================================================
    // sys_io / cellPad — Input (16 functions)
    // =====================================================================
    for (uint32_t slot = 0x342898; slot <= 0x342910; slot += 8) {
        resolve_import(slot, HLE_STUB_RETURN_OK, toc);
    }

    // =====================================================================
    // sys_fs — Filesystem (17 functions)
    // =====================================================================
    for (uint32_t slot = 0x342854; slot <= 0x3428D4; slot += 8) {
        resolve_import(slot, HLE_STUB_RETURN_OK, toc);
    }

    // =====================================================================
    // cellGame — Game data (7 functions)
    // =====================================================================
    for (uint32_t slot = 0x342490; slot <= 0x3424C0; slot += 8) {
        resolve_import(slot, HLE_STUB_RETURN_OK, toc);
    }

    // =====================================================================
    // cellResc, cellSpurs, cellSail, cellPamf, cellRtc,
    // sceNp, cellNetCtl, cellL10n, sceNpTrophy,
    // sceNpCommerce2, cellHttp, cellSsl, cellSysutilNpEula,
    // sys_net — remaining modules
    // =====================================================================
    // These share overlapping address ranges in the import table.
    // Cover the entire import table region to catch any we missed.
    for (uint32_t slot = 0x3423E0; slot <= 0x342910; slot += 8) {
        // Only write if the slot is still 0 (not already resolved)
        uint32_t current = 0;
        memcpy(&current, vm_base + slot, 4);
        if (current == 0) {
            resolve_import(slot, HLE_STUB_RETURN_OK, toc);
        }
    }

    printf("[HLE] Import table populated (%u OPDs created)\n",
           (g_next_opd - HLE_OPD_BASE) / 8);

    // =====================================================================
    // Patch unresolved function pointers (0x39800000) in the data segment.
    // The PS3 dynamic linker normally patches these, but since we load the
    // raw ELF without relocation processing, they remain as placeholders.
    // Replace with the address of our generic HLE stub's OPD.
    // =====================================================================
    // Scan both code and data segments for unresolved function pointers.
    // The value 0x39800000 appears in function descriptors (OPD entries)
    // as the code address field. Replace with our generic HLE stub's
    // guest address so ppc_indirect_call can dispatch it.
    uint32_t scan_start = 0x010000;
    uint32_t scan_end   = 0x340000 + 0x8CE208;
    int patched = 0;
    uint8_t target_be[4] = {0x39, 0x80, 0x00, 0x00}; // 0x39800000 big-endian
    for (uint32_t addr = scan_start; addr < scan_end - 3; addr += 4) {
        if (memcmp(vm_base + addr, target_be, 4) == 0) {
            // Replace code address with our HLE stub address (in dispatch table)
            import_write32(addr, HLE_STUB_RETURN_OK);
            patched++;
        }
    }
    printf("[HLE] Patched %d unresolved function pointers (0x39800000 -> 0x%08X)\n",
           patched, HLE_STUB_RETURN_OK);
}
