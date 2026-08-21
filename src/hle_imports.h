/**
 * Tokyo Jungle Recompiled — HLE Import Handlers
 *
 * Concrete implementations of PS3 library functions called through
 * the PLT import stubs. These are registered in the runtime HLE
 * dispatch table and called via ppc_indirect_call().
 *
 * Each handler reads arguments from ctx->gpr[3..10] (PPC64 calling
 * convention) and writes the return value to ctx->gpr[3].
 */

#pragma once

#include <cstdio>
#include <cstring>

extern "C" {
#include "ppu_context.h"
#include "vm.h"
#include "recomp_bridge.h"
}

extern uint8_t* vm_base;

// Forward declarations from import_resolver.h
extern uint32_t hle_malloc(uint32_t size);
extern uint32_t hle_calloc(uint32_t count, uint32_t size);
extern uint32_t hle_memalign(uint32_t align, uint32_t size);
extern void     hle_free(uint32_t addr);
extern void     hle_heap_init();

/* ========================================================================
 * Guest address assignment for HLE imports
 *
 * We assign unique guest addresses in the 0x01100000-0x011FFFFF range
 * for each HLE function. These addresses are written into import table
 * OPDs and registered in the HLE dispatch table.
 * ====================================================================== */

#define HLE_ADDR_BASE       0x01100000u
#define HLE_ADDR(n)         (HLE_ADDR_BASE + (n) * 4)

// sysPrxForUser
#define HLE_ADDR_MALLOC         HLE_ADDR(0)
#define HLE_ADDR_FREE           HLE_ADDR(1)
#define HLE_ADDR_CALLOC         HLE_ADDR(2)
#define HLE_ADDR_MEMALIGN       HLE_ADDR(3)
#define HLE_ADDR_MEMCPY         HLE_ADDR(4)
#define HLE_ADDR_MEMSET         HLE_ADDR(5)
#define HLE_ADDR_MEMMOVE        HLE_ADDR(6)
#define HLE_ADDR_STRLEN         HLE_ADDR(7)
#define HLE_ADDR_STRCMP          HLE_ADDR(8)
#define HLE_ADDR_STRCPY         HLE_ADDR(9)
#define HLE_ADDR_STRNCPY        HLE_ADDR(10)
#define HLE_ADDR_PRINTF         HLE_ADDR(11)
#define HLE_ADDR_SPRINTF        HLE_ADDR(12)
#define HLE_ADDR_SNPRINTF       HLE_ADDR(13)
// Thread/sync
#define HLE_ADDR_LWMUTEX_CREATE HLE_ADDR(20)
#define HLE_ADDR_LWMUTEX_LOCK   HLE_ADDR(21)
#define HLE_ADDR_LWMUTEX_UNLOCK HLE_ADDR(22)
#define HLE_ADDR_LWMUTEX_DESTROY HLE_ADDR(23)
#define HLE_ADDR_THREAD_CREATE  HLE_ADDR(24)
#define HLE_ADDR_THREAD_EXIT    HLE_ADDR(25)
#define HLE_ADDR_THREAD_ONCE    HLE_ADDR(26)
#define HLE_ADDR_THREAD_GET_ID  HLE_ADDR(27)
#define HLE_ADDR_PROCESS_EXIT   HLE_ADDR(28)
#define HLE_ADDR_PROCESS_ATEXIT HLE_ADDR(29)
#define HLE_ADDR_PROCESS_AT_EXIT HLE_ADDR(30)
#define HLE_ADDR_SPU_PRINTF_INIT HLE_ADDR(31)
#define HLE_ADDR_SPU_PRINTF_FIN  HLE_ADDR(32)
#define HLE_ADDR_TIME_GET       HLE_ADDR(33)
#define HLE_ADDR_PRX_EXITSPAWN  HLE_ADDR(34)
#define HLE_ADDR_PRX_REG_LIB    HLE_ADDR(35)
#define HLE_ADDR_UNKNOWN_744680 HLE_ADDR(36)
// cellGcmSys
#define HLE_ADDR_GCM_INIT           HLE_ADDR(50)
#define HLE_ADDR_GCM_GET_CONFIG     HLE_ADDR(51)
#define HLE_ADDR_GCM_SET_FLIP_MODE  HLE_ADDR(52)
#define HLE_ADDR_GCM_GET_FLIP_STATUS HLE_ADDR(53)
#define HLE_ADDR_GCM_RESET_FLIP     HLE_ADDR(54)
#define HLE_ADDR_GCM_SET_DISPLAY_BUF HLE_ADDR(55)
#define HLE_ADDR_GCM_GET_LABEL_ADDR HLE_ADDR(56)
#define HLE_ADDR_GCM_SET_TILE_INFO  HLE_ADDR(57)
#define HLE_ADDR_GCM_BIND_TILE      HLE_ADDR(58)
#define HLE_ADDR_GCM_BIND_ZCULL     HLE_ADDR(59)
#define HLE_ADDR_GCM_MAP_MAIN_MEM   HLE_ADDR(60)
#define HLE_ADDR_GCM_MAP_EA_IO      HLE_ADDR(61)
#define HLE_ADDR_GCM_MAP_EA_IO_FLAGS HLE_ADDR(62)
#define HLE_ADDR_GCM_UNMAP_EA_IO    HLE_ADDR(63)
#define HLE_ADDR_GCM_UNMAP_IO       HLE_ADDR(64)
#define HLE_ADDR_GCM_ADDR_TO_OFFSET HLE_ADDR(65)
#define HLE_ADDR_GCM_GET_CTRL_REG   HLE_ADDR(66)
#define HLE_ADDR_GCM_SET_FLIP_HANDLER HLE_ADDR(67)
#define HLE_ADDR_GCM_SET_VBLANK_HANDLER HLE_ADDR(68)
#define HLE_ADDR_GCM_SET_USER_HANDLER HLE_ADDR(69)
#define HLE_ADDR_GCM_GET_TILED_PITCH HLE_ADDR(70)
// cellSysutil
#define HLE_ADDR_SYSUTIL_REG_CB     HLE_ADDR(80)
#define HLE_ADDR_SYSUTIL_CHECK_CB   HLE_ADDR(81)
#define HLE_ADDR_SYSUTIL_GET_PARAM_INT HLE_ADDR(82)
#define HLE_ADDR_VIDEOOUT_GET_STATE  HLE_ADDR(83)
#define HLE_ADDR_VIDEOOUT_GET_RES    HLE_ADDR(84)
#define HLE_ADDR_VIDEOOUT_CONFIGURE  HLE_ADDR(85)
// cellSysmodule
#define HLE_ADDR_SYSMOD_LOAD        HLE_ADDR(90)
#define HLE_ADDR_SYSMOD_UNLOAD      HLE_ADDR(91)
// cellPad
#define HLE_ADDR_PAD_INIT           HLE_ADDR(100)
#define HLE_ADDR_PAD_END            HLE_ADDR(101)
#define HLE_ADDR_PAD_GET_DATA       HLE_ADDR(102)
#define HLE_ADDR_PAD_GET_INFO2      HLE_ADDR(103)
#define HLE_ADDR_PAD_SET_PORT       HLE_ADDR(104)
// cellAudio
#define HLE_ADDR_AUDIO_INIT         HLE_ADDR(110)
#define HLE_ADDR_AUDIO_PORT_OPEN    HLE_ADDR(111)
#define HLE_ADDR_AUDIO_PORT_START   HLE_ADDR(112)
#define HLE_ADDR_AUDIO_PORT_STOP    HLE_ADDR(113)
#define HLE_ADDR_AUDIO_PORT_CLOSE   HLE_ADDR(114)
#define HLE_ADDR_AUDIO_QUIT         HLE_ADDR(115)
#define HLE_ADDR_AUDIO_GET_PORT_CFG HLE_ADDR(116)
// cellGame
#define HLE_ADDR_GAME_BOOT_CHECK    HLE_ADDR(120)
#define HLE_ADDR_GAME_CONTENT_PERMIT HLE_ADDR(121)
#define HLE_ADDR_GAME_GET_PARAM_INT HLE_ADDR(122)
// Generic return-OK
#define HLE_ADDR_GENERIC_OK         HLE_ADDR(200)
#define HLE_ADDR_SILENT_OK          HLE_ADDR(201)

/* ========================================================================
 * HLE Handler Implementations
 * ====================================================================== */

// --- Generic return-OK stub ---
static void hle_generic_ok(ppu_context* ctx) {
    ctx->gpr[3] = 0;
}

// --- Silent return-OK stub (for mass-patched OPDs) ---
//
// Diagnoses retry-loops on unresolved imports: prints unique caller LRs and
// arg patterns the first ~30 hits, then logs every 10000th hit thereafter
// so a tight loop becomes visible without flooding stdout.
static void hle_silent_ok(ppu_context* ctx) {
    static int s_call_count = 0;
    static uint32_t s_last_lr = 0;
    s_call_count++;
    uint32_t lr = (uint32_t)ctx->lr;
    if (s_call_count <= 30 || (lr != s_last_lr) || (s_call_count % 10000) == 0) {
        fprintf(stderr,
            "[silent_ok #%d] LR=0x%08X r3=0x%llX r4=0x%llX r5=0x%llX\n",
            s_call_count, lr,
            (unsigned long long)ctx->gpr[3],
            (unsigned long long)ctx->gpr[4],
            (unsigned long long)ctx->gpr[5]);
        fflush(stderr);
        s_last_lr = lr;
    }
    ctx->gpr[3] = 0;
}

// --- Memory allocation (sysPrxForUser _sys_malloc etc.) ---
// The PS3 CRT wraps these around the heap allocated by __sys_init_heap.
// We use our own bump allocator.

static void hle_sys_malloc(ppu_context* ctx) {
    uint32_t size = (uint32_t)ctx->gpr[3];
    uint32_t addr = hle_malloc(size);
    ctx->gpr[3] = addr;
}

static void hle_sys_free(ppu_context* ctx) {
    uint32_t addr = (uint32_t)ctx->gpr[3];
    hle_free(addr);
    ctx->gpr[3] = 0;
}

static void hle_sys_calloc(ppu_context* ctx) {
    uint32_t count = (uint32_t)ctx->gpr[3];
    uint32_t size = (uint32_t)ctx->gpr[4];
    uint32_t addr = hle_calloc(count, size);
    ctx->gpr[3] = addr;
}

static void hle_sys_memalign(ppu_context* ctx) {
    uint32_t align = (uint32_t)ctx->gpr[3];
    uint32_t size = (uint32_t)ctx->gpr[4];
    uint32_t addr = hle_memalign(align, size);
    ctx->gpr[3] = addr;
}

// --- String/memory operations ---
static void hle_sys_memcpy(ppu_context* ctx) {
    uint32_t dst = (uint32_t)ctx->gpr[3];
    uint32_t src = (uint32_t)ctx->gpr[4];
    uint32_t len = (uint32_t)ctx->gpr[5];
    if (dst && src && len > 0 && len < 0x10000000) {
        memmove(vm_base + dst, vm_base + src, len);
    }
    // r3 = dst (already set)
}

static void hle_sys_memset(ppu_context* ctx) {
    uint32_t dst = (uint32_t)ctx->gpr[3];
    uint32_t val = (uint32_t)ctx->gpr[4];
    uint32_t len = (uint32_t)ctx->gpr[5];
    if (dst && len > 0 && len < 0x10000000) {
        memset(vm_base + dst, (int)val, len);
    }
    // r3 = dst (already set)
}

static void hle_sys_memmove(ppu_context* ctx) {
    uint32_t dst = (uint32_t)ctx->gpr[3];
    uint32_t src = (uint32_t)ctx->gpr[4];
    uint32_t len = (uint32_t)ctx->gpr[5];
    if (dst && src && len > 0 && len < 0x10000000) {
        memmove(vm_base + dst, vm_base + src, len);
    }
}

static void hle_sys_strlen(ppu_context* ctx) {
    uint32_t str = (uint32_t)ctx->gpr[3];
    if (str) {
        ctx->gpr[3] = (uint64_t)strlen((const char*)(vm_base + str));
    } else {
        ctx->gpr[3] = 0;
    }
}

static void hle_sys_strcmp(ppu_context* ctx) {
    uint32_t s1 = (uint32_t)ctx->gpr[3];
    uint32_t s2 = (uint32_t)ctx->gpr[4];
    if (s1 && s2) {
        ctx->gpr[3] = (uint64_t)(int64_t)strcmp(
            (const char*)(vm_base + s1), (const char*)(vm_base + s2));
    } else {
        ctx->gpr[3] = 0;
    }
}

static void hle_sys_strcpy(ppu_context* ctx) {
    uint32_t dst = (uint32_t)ctx->gpr[3];
    uint32_t src = (uint32_t)ctx->gpr[4];
    if (dst && src) {
        strcpy((char*)(vm_base + dst), (const char*)(vm_base + src));
    }
}

static void hle_sys_strncpy(ppu_context* ctx) {
    uint32_t dst = (uint32_t)ctx->gpr[3];
    uint32_t src = (uint32_t)ctx->gpr[4];
    uint32_t n   = (uint32_t)ctx->gpr[5];
    if (dst && src && n > 0) {
        strncpy((char*)(vm_base + dst), (const char*)(vm_base + src), n);
    }
}

static void hle_sys_printf(ppu_context* ctx) {
    uint32_t fmt = (uint32_t)ctx->gpr[3];
    if (fmt) {
        printf("[GAME] %s", (const char*)(vm_base + fmt));
    }
    ctx->gpr[3] = 0;
}

// --- Thread/sync stubs ---
static void hle_lwmutex_create(ppu_context* ctx) {
    // r3 = mutex_ea, r4 = attr_ea
    // Write a dummy mutex handle
    uint32_t mutex_ea = (uint32_t)ctx->gpr[3];
    if (mutex_ea) {
        // Zero out the mutex structure (safe no-op)
        memset(vm_base + mutex_ea, 0, 32);
    }
    ctx->gpr[3] = 0; // CELL_OK
}

static void hle_lwmutex_lock(ppu_context* ctx) {
    ctx->gpr[3] = 0; // CELL_OK — single-threaded, no contention
}

static void hle_lwmutex_unlock(ppu_context* ctx) {
    ctx->gpr[3] = 0;
}

static void hle_lwmutex_destroy(ppu_context* ctx) {
    ctx->gpr[3] = 0;
}

static void hle_thread_create(ppu_context* ctx) {
    // r3 = thread_id_ea, r4 = entry, r5 = arg, r6 = prio, r7 = stack_size, r8 = flags
    printf("[HLE] sys_ppu_thread_create(entry=0x%08X) — stubbed (single-threaded)\n",
           (uint32_t)ctx->gpr[4]);
    // Write a dummy thread ID
    uint32_t tid_ea = (uint32_t)ctx->gpr[3];
    if (tid_ea) {
        uint32_t fake_tid = 0x100;
        uint32_t be = ((fake_tid >> 24) & 0xFF) | ((fake_tid >> 8) & 0xFF00) |
                      ((fake_tid << 8) & 0xFF0000) | ((fake_tid << 24) & 0xFF000000u);
        memcpy(vm_base + tid_ea, &be, 4);
    }
    ctx->gpr[3] = 0;
}

static void hle_thread_exit(ppu_context* ctx) {
    printf("[HLE] sys_ppu_thread_exit(0x%08X)\n", (uint32_t)ctx->gpr[3]);
    ctx->gpr[3] = 0;
}

static void hle_thread_once(ppu_context* ctx) {
    // r3 = once_ea, r4 = init_func
    // Check if already initialized
    uint32_t once_ea = (uint32_t)ctx->gpr[3];
    if (once_ea) {
        uint32_t val = vm_read32(once_ea);
        if (val == 0) {
            vm_write32(once_ea, 1);
            // Call the init function — but we'd need to dispatch it
            // For now, just mark as done
        }
    }
    ctx->gpr[3] = 0;
}

static void hle_thread_get_id(ppu_context* ctx) {
    ctx->gpr[3] = 1; // Return thread ID 1
}

static void hle_process_exit(ppu_context* ctx) {
    printf("[HLE] sys_process_exit(%d)\n", (int32_t)ctx->gpr[3]);
    ctx->gpr[3] = 0;
}

static void hle_time_get(ppu_context* ctx) {
    // sys_time_get_system_time: r3 = pointer to store time
    uint32_t time_ea = (uint32_t)ctx->gpr[3];
    if (time_ea) {
        // Write a fake timestamp (microseconds since boot)
        vm_write64(time_ea, 1000000ULL); // 1 second
    }
    ctx->gpr[3] = 0;
}

// --- cellGcmSys ---
// RSX local memory area in VM
#define GCM_LOCAL_MEM_BASE  0x10000000u
#define GCM_LOCAL_MEM_SIZE  0x10000000u  // 256 MB
// Label area
#define GCM_LABEL_BASE      0x0F000000u
#define GCM_LABEL_SIZE      0x00001000u  // 4KB for labels
// Control register area
#define GCM_CTRL_BASE       0x0F001000u
#define GCM_CTRL_SIZE       0x00001000u

static int g_gcm_inited = 0;

static void hle_gcm_init(ppu_context* ctx) {
    // _cellGcmInitBody(cmdSize, ioSize, ioAddress)
    uint32_t cmd_size = (uint32_t)ctx->gpr[3];
    uint32_t io_size  = (uint32_t)ctx->gpr[4];
    uint32_t io_addr  = (uint32_t)ctx->gpr[5];
    printf("[HLE] _cellGcmInitBody(cmdSize=0x%X, ioSize=0x%X, ioAddr=0x%08X)\n",
           cmd_size, io_size, io_addr);

    // Commit RSX local memory
    vm_commit(GCM_LOCAL_MEM_BASE, GCM_LOCAL_MEM_SIZE);
    // Commit label + control areas
    vm_commit(GCM_LABEL_BASE, GCM_LABEL_SIZE + GCM_CTRL_SIZE);
    memset(vm_base + GCM_LABEL_BASE, 0, GCM_LABEL_SIZE + GCM_CTRL_SIZE);

    g_gcm_inited = 1;
    ctx->gpr[3] = 0; // CELL_OK
}

static void hle_gcm_get_config(ppu_context* ctx) {
    // cellGcmGetConfiguration(CellGcmConfig* config)
    uint32_t config_ea = (uint32_t)ctx->gpr[3];
    if (config_ea) {
        // CellGcmConfig: {localAddress, ioAddress, localSize, ioSize, memFreq, coreFreq}
        vm_write32(config_ea + 0,  GCM_LOCAL_MEM_BASE);  // localAddress
        vm_write32(config_ea + 4,  0x00000000);           // ioAddress
        vm_write32(config_ea + 8,  GCM_LOCAL_MEM_SIZE);   // localSize
        vm_write32(config_ea + 12, 0x01000000);           // ioSize (16MB)
        vm_write32(config_ea + 16, 650000000);            // memoryFrequency
        vm_write32(config_ea + 20, 500000000);            // coreFrequency
    }
    ctx->gpr[3] = 0;
}

static void hle_gcm_get_ctrl_reg(ppu_context* ctx) {
    // Returns pointer to CellGcmControl in main memory
    ctx->gpr[3] = GCM_CTRL_BASE;
}

static void hle_gcm_get_label_addr(ppu_context* ctx) {
    // cellGcmGetLabelAddress(index) — returns pointer to label[index]
    uint32_t index = (uint32_t)ctx->gpr[3];
    ctx->gpr[3] = GCM_LABEL_BASE + index * 16;
}

static void hle_gcm_addr_to_offset(ppu_context* ctx) {
    // cellGcmAddressToOffset(address, *offset)
    uint32_t addr = (uint32_t)ctx->gpr[3];
    uint32_t offset_ea = (uint32_t)ctx->gpr[4];
    uint32_t offset = 0;
    if (addr >= GCM_LOCAL_MEM_BASE && addr < GCM_LOCAL_MEM_BASE + GCM_LOCAL_MEM_SIZE) {
        offset = addr - GCM_LOCAL_MEM_BASE;
    }
    if (offset_ea) {
        vm_write32(offset_ea, offset);
    }
    ctx->gpr[3] = 0;
}

static void hle_gcm_map_main_mem(ppu_context* ctx) {
    // cellGcmMapMainMemory(ea, size, *offset)
    uint32_t ea = (uint32_t)ctx->gpr[3];
    uint32_t size = (uint32_t)ctx->gpr[4];
    uint32_t offset_ea = (uint32_t)ctx->gpr[5];
    printf("[HLE] cellGcmMapMainMemory(ea=0x%08X, size=0x%X)\n", ea, size);
    if (offset_ea) {
        vm_write32(offset_ea, ea); // Simple identity mapping
    }
    ctx->gpr[3] = 0;
}

static void hle_gcm_map_ea_io(ppu_context* ctx) {
    ctx->gpr[3] = 0; // CELL_OK
}

// --- cellSysutil ---
static void hle_sysutil_reg_cb(ppu_context* ctx) {
    printf("[HLE] cellSysutilRegisterCallback(slot=%u, func=0x%08X)\n",
           (uint32_t)ctx->gpr[3], (uint32_t)ctx->gpr[4]);
    ctx->gpr[3] = 0;
}

static void hle_sysutil_check_cb(ppu_context* ctx) {
    ctx->gpr[3] = 0; // No pending callbacks
}

static void hle_sysutil_get_param_int(ppu_context* ctx) {
    uint32_t id = (uint32_t)ctx->gpr[3];
    uint32_t val_ea = (uint32_t)ctx->gpr[4];
    int32_t val = 0;
    switch (id) {
        case 0x101: val = 1; break;  // CELL_SYSUTIL_SYSTEMPARAM_ID_LANG (English)
        case 0x102: val = 0; break;  // CELL_SYSUTIL_SYSTEMPARAM_ID_ENTER_BUTTON_ASSIGN
        default: break;
    }
    if (val_ea) vm_write32(val_ea, (uint32_t)val);
    ctx->gpr[3] = 0;
}

static void hle_videoout_get_state(ppu_context* ctx) {
    // cellVideoOutGetState(videoOut, deviceIndex, *state)
    uint32_t state_ea = (uint32_t)ctx->gpr[5];
    if (state_ea) {
        memset(vm_base + state_ea, 0, 16);
        vm_write32(state_ea + 0, 2);  // state = CELL_VIDEO_OUT_OUTPUT_STATE_ENABLED
        vm_write32(state_ea + 4, 1);  // colorSpace (RGB)
        // displayMode — set to 1080p
        vm_write16(state_ea + 8, 0x80);   // resolutionId (1080)
        vm_write16(state_ea + 10, 0);     // scanMode
    }
    ctx->gpr[3] = 0;
}

static void hle_videoout_get_res(ppu_context* ctx) {
    // cellVideoOutGetResolution(resId, *resolution)
    uint32_t res_ea = (uint32_t)ctx->gpr[4];
    if (res_ea) {
        vm_write16(res_ea + 0, 1280); // width
        vm_write16(res_ea + 2, 720);  // height
    }
    ctx->gpr[3] = 0;
}

static void hle_videoout_configure(ppu_context* ctx) {
    ctx->gpr[3] = 0;
}

// --- cellSysmodule ---
static void hle_sysmod_load(ppu_context* ctx) {
    printf("[HLE] cellSysmoduleLoadModule(0x%04X)\n", (uint32_t)ctx->gpr[3]);
    ctx->gpr[3] = 0;
}

static void hle_sysmod_unload(ppu_context* ctx) {
    ctx->gpr[3] = 0;
}

// --- cellPad ---
static void hle_pad_init(ppu_context* ctx) {
    printf("[HLE] cellPadInit(%u)\n", (uint32_t)ctx->gpr[3]);
    ctx->gpr[3] = 0;
}

static void hle_pad_get_data(ppu_context* ctx) {
    // cellPadGetData(port, *data)
    uint32_t data_ea = (uint32_t)ctx->gpr[4];
    if (data_ea) {
        memset(vm_base + data_ea, 0, 128); // Zero pad data
    }
    ctx->gpr[3] = 0;
}

static void hle_pad_get_info2(ppu_context* ctx) {
    uint32_t info_ea = (uint32_t)ctx->gpr[3];
    if (info_ea) {
        memset(vm_base + info_ea, 0, 64);
        vm_write32(info_ea + 0, 0);  // max_connect = 0 (no pads)
    }
    ctx->gpr[3] = 0;
}

// --- cellAudio ---
static void hle_audio_init(ppu_context* ctx) {
    printf("[HLE] cellAudioInit()\n");
    ctx->gpr[3] = 0;
}

static void hle_audio_port_open(ppu_context* ctx) {
    // Write port number to output
    uint32_t port_ea = (uint32_t)ctx->gpr[4];
    if (port_ea) vm_write32(port_ea, 0); // port 0
    ctx->gpr[3] = 0;
}

// --- cellGame ---
static void hle_game_boot_check(ppu_context* ctx) {
    // cellGameBootCheck(*type, *attr, *contentInfo, *dirName)
    uint32_t type_ea = (uint32_t)ctx->gpr[3];
    if (type_ea) vm_write32(type_ea, 1); // CELL_GAME_GAMETYPE_HDD
    ctx->gpr[3] = 0;
}

static void hle_game_content_permit(ppu_context* ctx) {
    // cellGameContentPermit(*contentInfoPath, *usrdirPath)
    uint32_t ci_ea = (uint32_t)ctx->gpr[3];
    uint32_t ud_ea = (uint32_t)ctx->gpr[4];
    if (ci_ea) {
        const char* path = "/dev_hdd0/game/NPUA80523";
        memcpy(vm_base + ci_ea, path, strlen(path) + 1);
    }
    if (ud_ea) {
        const char* path = "/dev_hdd0/game/NPUA80523/USRDIR";
        memcpy(vm_base + ud_ea, path, strlen(path) + 1);
    }
    ctx->gpr[3] = 0;
}

/* ========================================================================
 * Registration — call this after import table population
 * ====================================================================== */

#include "tj_import_names.h"

/* One handler shared by every import the ELF left unresolved. Each such import
 * has its OWN guest address (TJ_GENERIC_ADDR), so ctx->ctr identifies which
 * slot the guest called even though the body is the same.
 *
 * ps3recomp implements a large part of the firmware surface already (its
 * libs/ tree: cellGcmSys, cellResc, cellAudio, cellFs, ...), keyed by NID.
 * generated/tj_import_names.h carries the NID for every import in this ELF, so
 * ask ps3recomp first and only fall back to return-0 when it has nothing. That
 * is what lights up cellResc -- Tokyo Jungle drives its display through the
 * resolution converter, so an unimplemented cellRescInit/SetSrc/SetDsts means
 * nothing is ever scanned out no matter how well the rest of the boot goes.
 *
 * Imports ps3recomp does not implement are reported once, with arguments --
 * that list is the roadmap of what still needs writing.
 */
extern "C" int  ps3_hle_has(unsigned int nid);
extern "C" void ps3_hle_call(unsigned int nid, ppu_context* ctx);

static void hle_generic_named(ppu_context* ctx) {
    static unsigned char seen[TJ_GENERIC_MAX];
    uint32_t idx = ((uint32_t)ctx->ctr - TJ_GENERIC_BASE) / 4u;
    if (idx >= TJ_GENERIC_MAX) { ctx->gpr[3] = 0; return; }

    uint32_t slot = g_generic_slot[idx];
    uint32_t nid  = tj_import_nid(slot);

    if (nid && ps3_hle_has(nid)) {
        if (!seen[idx]) {
            seen[idx] = 1;
            printf("[import] -> ps3recomp %s (NID 0x%08X)\n",
                   tj_import_name(slot), nid);
        }
        ps3_hle_call(nid, ctx);
        return;
    }

    if (!seen[idx]) {
        seen[idx] = 1;
        printf("[import] UNIMPLEMENTED %s (slot 0x%08X)  r3=0x%08X r4=0x%08X "
               "r5=0x%08X r6=0x%08X\n",
               tj_import_name(slot), slot, (uint32_t)ctx->gpr[3],
               (uint32_t)ctx->gpr[4], (uint32_t)ctx->gpr[5],
               (uint32_t)ctx->gpr[6]);
    }
    ctx->gpr[3] = 0;
}

/* Hand the graphics libraries to ps3recomp.
 *
 * TJ's own cellGcm* handlers are bring-up stubs: they record a little state
 * and return 0, and nothing downstream of them draws. ps3recomp's
 * libs/video/cellGcmSys.c is the implementation wired to the RSX command
 * translator and the D3D12 backend that renders Rubber Ducky, and its
 * cellResc sits on top of that same state -- which is why cellRescInit
 * faults when it runs against TJ's stub GCM instead.
 *
 * So for these modules, prefer ps3recomp wherever it has the NID: rebind the
 * slot to a per-slot generic address, which routes through ps3_hle_call.
 * Runs after register_hle_imports so it overrides TJ's bindings.
 *
 * TJ_LEGACY_GCM=1 keeps TJ's stubs, for bisecting a regression against them.
 */
static void prefer_ps3recomp_graphics(uint32_t toc) {
    if (getenv("TJ_LEGACY_GCM")) {
        printf("[HLE] TJ_LEGACY_GCM: keeping TJ's own graphics stubs\n");
        return;
    }
    static const char* const kPrefixes[] = { "cellGcm", "cellResc", "cellVideoOut" };
    int moved = 0;
    for (int i = 0; i < g_tj_import_name_count; i++) {
        const tj_import_entry* e = &g_tj_import_names[i];
        int want = 0;
        /* Substring, not prefix: the module's own init is exported as
         * _cellGcmInitBody with a leading underscore, and matching on a
         * prefix left it on TJ's stub -- so ps3recomp's GCM was never
         * initialised, there was no FIFO or control register, and the game
         * spun forever in its flip loop waiting for a present. */
        for (unsigned p = 0; p < sizeof(kPrefixes) / sizeof(kPrefixes[0]); p++) {
            if (strstr(e->name, kPrefixes[p]) != 0) { want = 1; break; }
        }
        if (!want || !ps3_hle_has(e->nid)) continue;
        if (g_generic_count >= TJ_GENERIC_MAX) break;
        int idx = g_generic_count++;
        g_generic_slot[idx] = e->slot;
        resolve_import(e->slot, TJ_GENERIC_ADDR(idx), toc);
        hle_register(TJ_GENERIC_ADDR(idx), hle_generic_named);
        moved++;
    }
    printf("[HLE] %d graphics imports routed to ps3recomp\n", moved);
}

/* Register the per-slot generic addresses. Called after resolve_all_imports,
 * which is what fills g_generic_slot/g_generic_count. */
static void register_generic_imports(void) {
    for (int i = 0; i < g_generic_count; i++)
        hle_register(TJ_GENERIC_ADDR(i), hle_generic_named);
    printf("[HLE] Registered %d per-slot generic import stubs\n",
           g_generic_count);
}

static void register_hle_imports(uint32_t toc) {
    printf("[HLE] Registering HLE import handlers...\n");

    // --- sysPrxForUser ---
    // NID 0x1573DC3F = sys_lwmutex_lock, stub=0x342810
    resolve_import(0x342810, HLE_ADDR_LWMUTEX_LOCK, toc);
    hle_register(HLE_ADDR_LWMUTEX_LOCK, hle_lwmutex_lock);

    // NID 0x1BC200F4 = sys_lwmutex_unlock, stub=0x342818
    resolve_import(0x342814, HLE_ADDR_LWMUTEX_UNLOCK, toc);
    hle_register(HLE_ADDR_LWMUTEX_UNLOCK, hle_lwmutex_unlock);

    // Slot 0x342820 is sys_lwmutex_create, NOT sys_ppu_thread_create. The
    // caller is the libc heap init (func_0024DB0C), and its r4 points at
    // 0x0028E630 = { protocol=2, recursive=0x10, name="_lc_mtx" } -- a
    // sys_lwmutex_attribute_t, immediately followed by the string
    // "mspace_free". It is called once per dlmalloc mspace.
    //
    // The old slot->name comments in this file and in import_resolver.h were
    // written assuming an 8-byte import stride; the table is 4-byte (see
    // import_resolver.h), so every name in those lists may be off. Trust the
    // stub disassembly and the call-site arguments, not the comments.
    resolve_import(0x342820, HLE_ADDR_LWMUTEX_CREATE, toc);
    hle_register(HLE_ADDR_THREAD_CREATE, hle_thread_create);

    // NID 0x2C847572 = _sys_process_atexitspawn, stub=0x342828
    resolve_import(0x34281C, HLE_ADDR_PROCESS_ATEXIT, toc);
    hle_register(HLE_ADDR_PROCESS_ATEXIT, hle_generic_ok);

    // NID 0x2F85C0EF = sys_lwmutex_create, stub=0x342830
    resolve_import(0x342820, HLE_ADDR_LWMUTEX_CREATE, toc);
    hle_register(HLE_ADDR_LWMUTEX_CREATE, hle_lwmutex_create);

    // NID 0x350D454E = sys_ppu_thread_get_id, stub=0x342838
    resolve_import(0x342824, HLE_ADDR_THREAD_GET_ID, toc);
    hle_register(HLE_ADDR_THREAD_GET_ID, hle_thread_get_id);

    // NID 0x42B23552 = sys_prx_register_library, stub=0x342840
    resolve_import(0x342828, HLE_ADDR_PRX_REG_LIB, toc);
    hle_register(HLE_ADDR_PRX_REG_LIB, hle_generic_ok);

    // NID 0x45FE2FCE = _sys_spu_printf_initialize, stub=0x342848
    resolve_import(0x34282C, HLE_ADDR_SPU_PRINTF_INIT, toc);
    hle_register(HLE_ADDR_SPU_PRINTF_INIT, hle_generic_ok);

    // NID 0x744680A2 = unknown, stub=0x342850
    resolve_import(0x342850, HLE_ADDR_UNKNOWN_744680, toc);
    hle_register(HLE_ADDR_UNKNOWN_744680, hle_generic_ok);

    // NID 0x8461E528 = sys_time_get_system_time, stub=0x342858
    resolve_import(0x342834, HLE_ADDR_TIME_GET, toc);
    hle_register(HLE_ADDR_TIME_GET, hle_time_get);

    // NID 0x96328741 = _sys_process_at_Exitspawn, stub=0x342860
    resolve_import(0x342838, HLE_ADDR_PROCESS_AT_EXIT, toc);
    hle_register(HLE_ADDR_PROCESS_AT_EXIT, hle_generic_ok);

    // NID 0xA2C7BA64 = sys_prx_exitspawn_with_level, stub=0x342868
    resolve_import(0x34283C, HLE_ADDR_PRX_EXITSPAWN, toc);
    hle_register(HLE_ADDR_PRX_EXITSPAWN, hle_generic_ok);

    // NID 0xA3E3BE68 = sys_ppu_thread_once, stub=0x342870
    resolve_import(0x342840, HLE_ADDR_THREAD_ONCE, toc);
    hle_register(HLE_ADDR_THREAD_ONCE, hle_thread_once);

    // NID 0xAFF080A4 = sys_ppu_thread_exit, stub=0x342878
    resolve_import(0x342844, HLE_ADDR_THREAD_EXIT, toc);
    hle_register(HLE_ADDR_THREAD_EXIT, hle_thread_exit);

    // NID 0xC3476D0C = sys_lwmutex_destroy, stub=0x342880
    resolve_import(0x342848, HLE_ADDR_LWMUTEX_DESTROY, toc);
    hle_register(HLE_ADDR_LWMUTEX_DESTROY, hle_lwmutex_destroy);

    // NID 0xDD3B27AC = _sys_spu_printf_finalize, stub=0x342888
    resolve_import(0x34284C, HLE_ADDR_SPU_PRINTF_FIN, toc);
    hle_register(HLE_ADDR_SPU_PRINTF_FIN, hle_generic_ok);

    // NID 0xE6F2C1E7 = sys_process_exit, stub=0x342890
    resolve_import(0x342850, HLE_ADDR_PROCESS_EXIT, toc);
    hle_register(HLE_ADDR_PROCESS_EXIT, hle_process_exit);

    // --- cellGcmSys (key functions) ---
    // NID 0x15BAE46B = _cellGcmInitBody, stub=0x3424BC
    resolve_import(0x3424B4, HLE_ADDR_GCM_INIT, toc);
    hle_register(HLE_ADDR_GCM_INIT, hle_gcm_init);
    printf("[HLE]   _cellGcmInitBody -> stub=0x3424BC, HLE=0x%08X\n", HLE_ADDR_GCM_INIT);

    resolve_import(0x3424BC, HLE_ADDR_GCM_ADDR_TO_OFFSET, toc);
    hle_register(HLE_ADDR_GCM_ADDR_TO_OFFSET, hle_gcm_addr_to_offset);

    resolve_import(0x3424D0, HLE_ADDR_GCM_SET_FLIP_MODE, toc);
    hle_register(HLE_ADDR_GCM_SET_FLIP_MODE, hle_generic_ok);

    resolve_import(0x3424E4, HLE_ADDR_GCM_GET_FLIP_STATUS, toc);
    hle_register(HLE_ADDR_GCM_GET_FLIP_STATUS, hle_generic_ok);

    resolve_import(0x3424CC, HLE_ADDR_GCM_BIND_TILE, toc);
    hle_register(HLE_ADDR_GCM_BIND_TILE, hle_generic_ok);

    resolve_import(0x3424F0, HLE_ADDR_GCM_BIND_ZCULL, toc);
    hle_register(HLE_ADDR_GCM_BIND_ZCULL, hle_generic_ok);

    resolve_import(0x3424F4, HLE_ADDR_GCM_MAP_MAIN_MEM, toc);
    hle_register(HLE_ADDR_GCM_MAP_MAIN_MEM, hle_gcm_map_main_mem);

    resolve_import(0x3424E0, HLE_ADDR_GCM_MAP_EA_IO, toc);
    hle_register(HLE_ADDR_GCM_MAP_EA_IO, hle_gcm_map_ea_io);

    resolve_import(0x3424D8, HLE_ADDR_GCM_MAP_EA_IO_FLAGS, toc);
    hle_register(HLE_ADDR_GCM_MAP_EA_IO_FLAGS, hle_gcm_map_ea_io);

    resolve_import(0x342524, HLE_ADDR_GCM_GET_CONFIG, toc);
    hle_register(HLE_ADDR_GCM_GET_CONFIG, hle_gcm_get_config);

    resolve_import(0x342500, HLE_ADDR_GCM_GET_CTRL_REG, toc);
    hle_register(HLE_ADDR_GCM_GET_CTRL_REG, hle_gcm_get_ctrl_reg);

    resolve_import(0x34252C, HLE_ADDR_GCM_GET_LABEL_ADDR, toc);
    hle_register(HLE_ADDR_GCM_GET_LABEL_ADDR, hle_gcm_get_label_addr);

    resolve_import(0x342510, HLE_ADDR_GCM_SET_TILE_INFO, toc);
    hle_register(HLE_ADDR_GCM_SET_TILE_INFO, hle_generic_ok);

    resolve_import(0x34250C, HLE_ADDR_GCM_RESET_FLIP, toc);
    hle_register(HLE_ADDR_GCM_RESET_FLIP, hle_generic_ok);

    resolve_import(0x3424F8, HLE_ADDR_GCM_SET_FLIP_HANDLER, toc);
    hle_register(HLE_ADDR_GCM_SET_FLIP_HANDLER, hle_generic_ok);

    resolve_import(0x342508, HLE_ADDR_GCM_SET_VBLANK_HANDLER, toc);
    hle_register(HLE_ADDR_GCM_SET_VBLANK_HANDLER, hle_generic_ok);

    resolve_import(0x3424B0, HLE_ADDR_GCM_SET_USER_HANDLER, toc);
    hle_register(HLE_ADDR_GCM_SET_USER_HANDLER, hle_generic_ok);

    resolve_import(0x3424AC, HLE_ADDR_GCM_GET_TILED_PITCH, toc);
    hle_register(HLE_ADDR_GCM_GET_TILED_PITCH, hle_generic_ok);

    // Remaining resolved GCM NIDs:
    // 0x21397818 = _cellGcmSetFlipCommand, stub=0x3424C4
    resolve_import(0x3424C4, HLE_ADDR_GENERIC_OK, toc);
    // 0xD8F88E1A = _cellGcmSetFlipCommandWithWaitLabel, stub=0x342584
    resolve_import(0x342584, HLE_ADDR_GENERIC_OK, toc);
    // 0x63387071 = cellGcmGetLastFlipTime, stub=0x34250C
    resolve_import(0x34250C, HLE_ADDR_GENERIC_OK, toc);
    // 0xD0B1D189 = cellGcmSetTile, stub=0x34257C
    resolve_import(0x34257C, HLE_ADDR_GENERIC_OK, toc);
    // 0xD9B7653E = cellGcmUnbindTile, stub=0x34258C
    resolve_import(0x34258C, HLE_ADDR_GENERIC_OK, toc);
    // 0xA75640E8 = cellGcmUnbindZcull, stub=0x34255C
    resolve_import(0x34255C, HLE_ADDR_GENERIC_OK, toc);
    // 0xA53D12AE = cellGcmSetDisplayBuffer, stub=0x34254C
    resolve_import(0x3424FC, HLE_ADDR_GCM_SET_DISPLAY_BUF, toc);
    hle_register(HLE_ADDR_GCM_SET_DISPLAY_BUF, hle_generic_ok);

    // --- cellSysutil ---
    resolve_import(0x342708, HLE_ADDR_SYSUTIL_REG_CB, toc);
    hle_register(HLE_ADDR_SYSUTIL_REG_CB, hle_sysutil_reg_cb);

    resolve_import(0x3426DC, HLE_ADDR_SYSUTIL_CHECK_CB, toc);
    hle_register(HLE_ADDR_SYSUTIL_CHECK_CB, hle_sysutil_check_cb);

    resolve_import(0x3426E8, HLE_ADDR_SYSUTIL_GET_PARAM_INT, toc);
    hle_register(HLE_ADDR_SYSUTIL_GET_PARAM_INT, hle_sysutil_get_param_int);

    resolve_import(0x3426FC, HLE_ADDR_VIDEOOUT_GET_STATE, toc);
    hle_register(HLE_ADDR_VIDEOOUT_GET_STATE, hle_videoout_get_state);

    resolve_import(0x34271C, HLE_ADDR_VIDEOOUT_GET_RES, toc);
    hle_register(HLE_ADDR_VIDEOOUT_GET_RES, hle_videoout_get_res);

    resolve_import(0x3426D8, HLE_ADDR_VIDEOOUT_CONFIGURE, toc);
    hle_register(HLE_ADDR_VIDEOOUT_CONFIGURE, hle_videoout_configure);

    // --- cellSysmodule ---
    resolve_import(0x3426C8, HLE_ADDR_SYSMOD_LOAD, toc);
    hle_register(HLE_ADDR_SYSMOD_LOAD, hle_sysmod_load);

    resolve_import(0x3426C4, HLE_ADDR_SYSMOD_UNLOAD, toc);
    hle_register(HLE_ADDR_SYSMOD_UNLOAD, hle_sysmod_unload);

    // --- cellPad ---
    resolve_import(0x342898, HLE_ADDR_PAD_INIT, toc);
    hle_register(HLE_ADDR_PAD_INIT, hle_pad_init);

    resolve_import(0x3428AC, HLE_ADDR_PAD_END, toc);
    hle_register(HLE_ADDR_PAD_END, hle_generic_ok);

    resolve_import(0x3428B8, HLE_ADDR_PAD_GET_DATA, toc);
    hle_register(HLE_ADDR_PAD_GET_DATA, hle_pad_get_data);

    resolve_import(0x3428C0, HLE_ADDR_PAD_GET_INFO2, toc);
    hle_register(HLE_ADDR_PAD_GET_INFO2, hle_pad_get_info2);

    resolve_import(0x3428B0, HLE_ADDR_PAD_SET_PORT, toc);
    hle_register(HLE_ADDR_PAD_SET_PORT, hle_generic_ok);

    // --- cellAudio ---
    resolve_import(0x3423E8, HLE_ADDR_AUDIO_INIT, toc);
    hle_register(HLE_ADDR_AUDIO_INIT, hle_audio_init);

    resolve_import(0x342408, HLE_ADDR_AUDIO_PORT_OPEN, toc);
    hle_register(HLE_ADDR_AUDIO_PORT_OPEN, hle_audio_port_open);

    resolve_import(0x342400, HLE_ADDR_AUDIO_PORT_START, toc);
    hle_register(HLE_ADDR_AUDIO_PORT_START, hle_generic_ok);

    resolve_import(0x3423F8, HLE_ADDR_AUDIO_PORT_STOP, toc);
    hle_register(HLE_ADDR_AUDIO_PORT_STOP, hle_generic_ok);

    resolve_import(0x3423F4, HLE_ADDR_AUDIO_PORT_CLOSE, toc);
    hle_register(HLE_ADDR_AUDIO_PORT_CLOSE, hle_generic_ok);

    resolve_import(0x342404, HLE_ADDR_AUDIO_QUIT, toc);
    hle_register(HLE_ADDR_AUDIO_QUIT, hle_generic_ok);

    resolve_import(0x3423FC, HLE_ADDR_AUDIO_GET_PORT_CFG, toc);
    hle_register(HLE_ADDR_AUDIO_GET_PORT_CFG, hle_generic_ok);

    // --- cellGame ---
    resolve_import(0x3424A8, HLE_ADDR_GAME_BOOT_CHECK, toc);
    hle_register(HLE_ADDR_GAME_BOOT_CHECK, hle_game_boot_check);

    resolve_import(0x342494, HLE_ADDR_GAME_CONTENT_PERMIT, toc);
    hle_register(HLE_ADDR_GAME_CONTENT_PERMIT, hle_game_content_permit);

    resolve_import(0x34249C, HLE_ADDR_GAME_GET_PARAM_INT, toc);
    hle_register(HLE_ADDR_GAME_GET_PARAM_INT, hle_generic_ok);

    // Register generic and silent OK handlers
    hle_register(HLE_ADDR_GENERIC_OK, hle_generic_ok);
    hle_register(HLE_ADDR_SILENT_OK, hle_silent_ok);

    printf("[HLE] Registered %d HLE handlers\n", g_hle_dispatch_count);
}
