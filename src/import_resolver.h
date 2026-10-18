/**
 * Tokyo Jungle Recompiled — Import Table Resolver
 *
 * Populates the PS3 binary's import table (PLT) with function descriptors
 * pointing to HLE stub functions. This allows the PLT code stubs at
 * 0x0025Fxxx to dispatch correctly through ppc_indirect_call().
 *
 * Architecture:
 *   1. Reserve a VM region (0x2F000000) for HLE function descriptors (OPDs)
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
#include <mutex>
#include "ps3emu/endian.h"

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
static uint32_t g_heap_end   = 0x0C000000;  // 160 MB; audio starts at 0x0C000000
static constexpr uint32_t g_heap_gcm_hole_begin = 0x03000000;
static constexpr uint32_t g_heap_gcm_hole_end   = 0x03010000;
static int      g_heap_inited = 0;

/* The original bring-up allocator only moved a pointer forward. Tokyo Jungle
 * actively frees transient title/loading assets, so entering the main menu
 * exhausted all 160 MB even though most old blocks were dead. Keep allocator
 * metadata on the host and reuse/coalesce guest blocks. A fixed table avoids
 * recursively depending on the guest heap while servicing malloc itself. */
struct HleHeapBlock {
    uint32_t addr;
    uint32_t size;
    bool free;
};
static constexpr uint32_t HLE_HEAP_MAX_BLOCKS = 65536;
static HleHeapBlock g_heap_blocks[HLE_HEAP_MAX_BLOCKS];
static uint32_t g_heap_block_count = 0;
static std::mutex g_heap_mutex;

static bool hle_heap_insert_locked(uint32_t index, const HleHeapBlock& block) {
    if (g_heap_block_count >= HLE_HEAP_MAX_BLOCKS || index > g_heap_block_count)
        return false;
    memmove(&g_heap_blocks[index + 1], &g_heap_blocks[index],
            (g_heap_block_count - index) * sizeof(HleHeapBlock));
    g_heap_blocks[index] = block;
    g_heap_block_count++;
    return true;
}

void hle_heap_init() {
    std::lock_guard<std::mutex> lock(g_heap_mutex);
    if (g_heap_inited) return;
    // 0x03000000..0x0300FFFF belongs to cellGcmSys labels/control state.
    // Keep the title heap from initializing or allocating over that page.
    vm_commit(g_heap_base, g_heap_gcm_hole_begin - g_heap_base);
    memset(vm_base + g_heap_base, 0, g_heap_gcm_hole_begin - g_heap_base);
    vm_commit(g_heap_gcm_hole_end, g_heap_end - g_heap_gcm_hole_end);
    memset(vm_base + g_heap_gcm_hole_end, 0, g_heap_end - g_heap_gcm_hole_end);
    g_heap_blocks[0] = {g_heap_base, g_heap_gcm_hole_begin - g_heap_base, true};
    g_heap_blocks[1] = {g_heap_gcm_hole_end, g_heap_end - g_heap_gcm_hole_end, true};
    g_heap_block_count = 2;
    g_heap_inited = 1;
    printf("[HLE] Free-list heap initialized: 0x%08X - 0x%08X (GCM hole 0x%08X - 0x%08X)\n",
           g_heap_base, g_heap_end, g_heap_gcm_hole_begin, g_heap_gcm_hole_end);
}

static uint32_t hle_alloc_aligned(uint32_t size, uint32_t align) {
    if (size > UINT32_MAX - 15u) return 0;
    if (!g_heap_inited) hle_heap_init();

    size = (size + 15) & ~15u;
    if (size == 0) size = 16;
    if (align < 16) align = 16;
    if (align & (align - 1)) {
        uint32_t power = 16;
        while (power < align && power < 0x80000000u) power <<= 1;
        align = power;
    }

    uint32_t result = 0;
    {
        std::lock_guard<std::mutex> lock(g_heap_mutex);
        for (uint32_t i = 0; i < g_heap_block_count; i++) {
            HleHeapBlock old = g_heap_blocks[i];
            if (!old.free) continue;
            uint64_t aligned = ((uint64_t)old.addr + align - 1) & ~((uint64_t)align - 1);
            uint64_t old_end = (uint64_t)old.addr + old.size;
            uint64_t end = aligned + size;
            if (end > old_end) continue;

            uint32_t prefix = (uint32_t)(aligned - old.addr);
            uint32_t suffix = (uint32_t)(old_end - end);
            uint32_t needed = (prefix ? 1u : 0u) + (suffix ? 1u : 0u);
            if (g_heap_block_count + needed > HLE_HEAP_MAX_BLOCKS) break;

            if (prefix) {
                g_heap_blocks[i] = {old.addr, prefix, true};
                hle_heap_insert_locked(++i, {(uint32_t)aligned, size, false});
            } else {
                g_heap_blocks[i] = {(uint32_t)aligned, size, false};
            }
            if (suffix)
                hle_heap_insert_locked(i + 1, {(uint32_t)end, suffix, true});
            result = (uint32_t)aligned;
            break;
        }

        if (!result) {
            uint64_t total_free = 0;
            uint32_t largest = 0;
            for (uint32_t i = 0; i < g_heap_block_count; i++)
                if (g_heap_blocks[i].free) {
                    total_free += g_heap_blocks[i].size;
                    if (g_heap_blocks[i].size > largest) largest = g_heap_blocks[i].size;
                }
            printf("[HLE] ERROR: heap allocation failed (requested=%u align=%u free=%llu largest=%u blocks=%u)\n",
                   size, align, (unsigned long long)total_free, largest, g_heap_block_count);
        }
    }
    if (result) memset(vm_base + result, 0, size);
    return result;
}

uint32_t hle_malloc(uint32_t size) {
    return hle_alloc_aligned(size, 16);
}

uint32_t hle_calloc(uint32_t count, uint32_t size) {
    if ((uint64_t)count * size > UINT32_MAX) return 0;
    uint32_t total = count * size;
    uint32_t addr = hle_malloc(total);
    if (addr) {
        memset(vm_base + addr, 0, total);
    }
    return addr;
}

uint32_t hle_memalign(uint32_t align, uint32_t size) {
    return hle_alloc_aligned(size, align);
}

void hle_free(uint32_t addr) {
    if (!addr || !g_heap_inited) return;
    std::lock_guard<std::mutex> lock(g_heap_mutex);
    for (uint32_t i = 0; i < g_heap_block_count; i++) {
        if (g_heap_blocks[i].addr != addr || g_heap_blocks[i].free) continue;
        g_heap_blocks[i].free = true;
        if (i > 0 && g_heap_blocks[i - 1].free &&
            g_heap_blocks[i - 1].addr + g_heap_blocks[i - 1].size == g_heap_blocks[i].addr) {
            g_heap_blocks[i - 1].size += g_heap_blocks[i].size;
            memmove(&g_heap_blocks[i], &g_heap_blocks[i + 1],
                    (g_heap_block_count - i - 1) * sizeof(HleHeapBlock));
            g_heap_block_count--;
            i--;
        }
        if (i + 1 < g_heap_block_count && g_heap_blocks[i + 1].free &&
            g_heap_blocks[i].addr + g_heap_blocks[i].size == g_heap_blocks[i + 1].addr) {
            g_heap_blocks[i].size += g_heap_blocks[i + 1].size;
            memmove(&g_heap_blocks[i + 1], &g_heap_blocks[i + 2],
                    (g_heap_block_count - i - 2) * sizeof(HleHeapBlock));
            g_heap_block_count--;
        }
        return;
    }
}

uint32_t hle_allocation_size(uint32_t addr) {
    if (!addr || !g_heap_inited) return 0;
    std::lock_guard<std::mutex> lock(g_heap_mutex);
    for (uint32_t i = 0; i < g_heap_block_count; i++)
        if (g_heap_blocks[i].addr == addr && !g_heap_blocks[i].free)
            return g_heap_blocks[i].size;
    return 0;
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
#define HLE_OPD_BASE 0x2F000000u
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
/* Per-slot HLE addresses for imports the ELF left unresolved. Each gets a
 * distinct guest address so the log names the import instead of showing an
 * anonymous shared stub. TJ_GENERIC_BASE sits inside the HLE range
 * (0x01100000-0x011FFFFF) well clear of the hand-assigned HLE_ADDR(n). */
#define TJ_GENERIC_MAX   512
#define TJ_GENERIC_BASE  0x01180000u
#define TJ_GENERIC_ADDR(n) (TJ_GENERIC_BASE + (n) * 4u)

static uint32_t g_generic_slot[TJ_GENERIC_MAX];
static int      g_generic_count;

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

// Well-known stub addresses
// For import table default: use the explicit unresolved-OPD handler.
// (registered at runtime in register_hle_imports)
#define HLE_STUB_RETURN_OK   0x01100324  // HLE_ADDR_SILENT_OK = HLE_ADDR(201)
// For OPD patching: same logged CELL_ENOSYS fallback
#define HLE_STUB_SILENT_OK   0x01100324

static void resolve_all_imports(uint32_t toc) {
    // Commit the OPD area
    vm_commit(HLE_OPD_BASE, HLE_OPD_SIZE);
    memset(vm_base + HLE_OPD_BASE, 0, HLE_OPD_SIZE);

    printf("[HLE] Resolving import table...\n");

    // Default: point imports to a logged CELL_ENOSYS fallback
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
    int generic = 0;
    // Stride 4, not 8: this is a table of 32-bit pointers. The stubs read it
    // with lwz at consecutive word offsets (0x2818, 0x281C, 0x2820, ...), and
    // resolve_import writes 32 bits. Stepping by 8 skipped every odd word --
    // half the imports, including __sys_init_tls at 0x342834.
    for (uint32_t slot = 0x3423E0; slot <= 0x342910; slot += 4) {
        uint32_t current = 0;
        memcpy(&current, vm_base + slot, 4);
        current = ps3_bswap32(current);

        // A slot is UNRESOLVED if it is zero, or if it still points into the
        // code segment. The PS3 ELF pre-fills every import pointer with the
        // address of the very stub that reads it -- lazy-binding placeholder,
        // patched by the dynamic linker at load. Following one makes the stub
        // read its own first instruction, li r12,0 == 0x39800000, as a code
        // pointer and branch there.
        //
        // A resolved slot points at an HLE OPD at HLE_OPD_BASE, well above
        // the code segment, so the range test cannot undo real resolutions.
        bool unresolved = (current == 0) ||
                          (current >= 0x00010000u && current < 0x00340000u);
        if (unresolved) {
            // Give each generically-resolved slot its OWN HLE address rather
            // than sharing HLE_STUB_RETURN_OK. The address identifies WHICH
            // import the guest just called --
            // otherwise every unimplemented library call looks alike in the
            // log and there is no way to tell which one mattered.
            if (generic < TJ_GENERIC_MAX) {
                g_generic_slot[generic] = slot;
                resolve_import(slot, TJ_GENERIC_ADDR(generic), toc);
            } else {
                resolve_import(slot, HLE_STUB_RETURN_OK, toc);
            }
            generic++;
        }
    }
    g_generic_count = generic < TJ_GENERIC_MAX ? generic : TJ_GENERIC_MAX;
    printf("[HLE] %d import slots left unresolved by the ELF -> generic stub\n",
           generic);

    printf("[HLE] Import table populated (%u OPDs created)\n",
           (g_next_opd - HLE_OPD_BASE) / 8);

    // Resolve only identified import slots above. Never scan data for opcode
    // patterns: 0x39800000 is also float 1/4096. This ELF has six real numeric
    // constants with those bits (five TOC entries and the value at 0x3B56A8).
    // Replacing them with HLE addresses corrupts original floating-point math.
}
