/**
 * Tokyo Jungle Recompiled — Entry Point
 *
 * Loads the decrypted EBOOT.ELF into virtual memory, sets up the
 * function dispatch table, and begins executing recompiled game code.
 */

#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <atomic>
#include <array>
#include <mutex>

#ifdef _WIN32
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <io.h>
#include <fcntl.h>
#endif

// ps3recomp public headers
#include "ps3emu/ps3types.h"
#include "ps3emu/error_codes.h"
#include "ps3emu/module.h"
#include "ps3emu/nid.h"
#include "../../ps3recomp/libs/font/cellFont.h"
#include "../../ps3recomp/libs/font/cellFontFT.h"
#include "../../ps3recomp/libs/font/cellL10n.h"
#include "../../ps3recomp/libs/audio/cellAudio.h"
#include "../../ps3recomp/libs/codec/cellPamf.h"
#include "../../ps3recomp/libs/codec/cellSail.h"
#include "../../ps3recomp/libs/misc/cellAvconfExt.h"
#include "../../ps3recomp/libs/video/cellGcmSys.h"
#include "../../ps3recomp/libs/video/cellResc.h"
#include "../../ps3recomp/libs/system/cellGame.h"
#include "../../ps3recomp/libs/system/cellMsgDialog.h"
#include "../../ps3recomp/libs/system/cellSysutil.h"
#include "../../ps3recomp/libs/input/cellKb.h"
#include "../../ps3recomp/libs/network/sceNp.h"
#include "../../ps3recomp/libs/network/cellSsl.h"
#include "../../ps3recomp/libs/spurs/cellSpurs.h"

extern "C" s32 sceNpBasicGetFriendListEntryCount(u32* count);

// ps3recomp runtime internals — these headers are template-aware and
// guard their own C linkage where needed; do not wrap them in extern "C".
#include "ppu_context.h"
#include "ppu_thread_exit_signal.h"
#include "vm.h"
#include "guest_world_rendezvous.h"
#include "lv2_syscall_table.h"
#include "sys_ppu_thread.h"
#include "recomp_bridge.h"
#include "ps3emu/guest_call.h"
#include "tj_pamf_media.h"
#include "../../ps3recomp/runtime/prx/prx_loader.h"

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
extern "C" void cellSaveDataFixedDelete_ctx(ppu_context* ctx);

// Main PPU context
/* RSX local memory, as cellGcmGetConfiguration reports it. */
#define TJ_LOCAL_MEM_BASE   0xC0000000u
#define TJ_LOCAL_MEM_SIZE   0x10000000u   /* 256 MB -> ends at 0xD0000000 */
#define TJ_MAIN_STACK_SIZE  0x00100000u

/* Guest-callback scratch stacks. These used to sit at 0xCFFE0000, which is
 * INSIDE the RSX local-memory window above -- the game bump-allocates VRAM up
 * through there, so the two would have quietly overwritten each other. Put
 * them above the main guest stack (VM_STACK_BASE 0xD0000000) instead. */
#define TJ_CB_STACK_BASE    0xD0400000u
#define TJ_CB_STACK_SPAN    0x00100000u

extern "C" vm_stack_alloc g_vm_stack_alloc;

/* The title owns fixed main/callback stacks outside sys_ppu_thread_create.
 * Exclude their prefix before guest dispatch: otherwise the worker allocator
 * starts at D0000000 and installs guard pages inside the live main stack. */
static bool tj_reserve_fixed_guest_stacks(vm_stack_alloc* stacks)
{
    const uint32_t reserved_end = TJ_CB_STACK_BASE + TJ_CB_STACK_SPAN;
    static_assert(VM_STACK_BASE + TJ_MAIN_STACK_SIZE <= TJ_CB_STACK_BASE,
                  "main and callback stacks must not overlap");
    static_assert((reserved_end % VM_PAGE_SIZE) == 0,
                  "worker stack reservation must be page aligned");
    if (!stacks || stacks->next_addr != VM_STACK_BASE ||
        reserved_end > stacks->region_end)
        return false;
    stacks->next_addr = reserved_end;
    return true;
}

static ppu_context g_main_ctx;

extern "C" __declspec(thread) void (*g_trampoline_fn)(void*);

/* Keep the C++ thread-exit signal out of main's SEH function.  The caller still
 * owns the normal cleanup path after this guest root stops executing. */
static bool tj_run_main_guest_entry(recomp_func_t entry)
{
    try {
        entry(&g_main_ctx);
        return false;
    } catch (const ppu_thread_exit_signal&) {
        g_trampoline_fn = nullptr;
        return true;
    }
}

extern "C" void ppu_register_function(uint64_t, void (*)(ppu_context*));
extern "C" void ppu_set_tls_image(uint32_t image_base);
extern "C" uint64_t ppu_thread_register_main(void);
extern "C" __declspec(thread) ppu_context* g_active_ctx;
extern "C" void ps3_hle_register(uint32_t nid, const char* name, void* handler);
extern "C" void ps3_hle_register_ctx(uint32_t nid, const char* name,
                                      void (*handler)(ppu_context*));
extern "C" void ppu_hle_register_all(void);
extern "C" void ppu_sysprx_register(void);
extern "C" void ppu_fs_register(void);
extern "C" int  tj_crash_filter(unsigned long code, void* exception_info);
#ifdef _WIN32
extern "C" LONG CALLBACK tj_vectored_crash_logger(EXCEPTION_POINTERS*);
#endif
extern "C" void tj_start_present_thread(void);
extern "C" void tj_stop_present_thread(void);
extern "C" void tj_stop_watchdog(void);
extern "C" unsigned int ps3_hle_count(void);
extern "C" unsigned int ps3_hle_ctx_count(void);
extern "C" void ppu_recomp_register(void);
extern "C" void ppu_install_thread_trampoline(void);
extern "C" void ppu_guard_page(uint32_t guest_ea);
extern "C" void tj_spu_register_all(void);
extern "C" unsigned spu_workload_count(void);
extern "C" const char* ppu_vfs_root;
extern "C" int ppu_fs_guest_to_host(const char* guest, char* out, size_t cap);
extern "C" void cellfs_set_root_path(const char* root);
extern "C" void cellfs_add_path_mapping(const char* ps3_prefix, const char* host_path);
extern "C" void cellGame_init_from_paramsfo(const char* sfo_path);
extern "C" int32_t sceNpDrmIsAvailable(const uint8_t* k_licensee, const char* drm_path);
#define TJ_FONT_LIBRARY_EA 0x03F00000u

struct TjFontGlyphCache { uint32_t ea = 0, capacity = 0; };
static std::array<TjFontGlyphCache, CELL_FONT_MAX_OPENED> g_tj_font_glyph_cache;
static std::mutex g_tj_font_glyph_mutex;

static void tj_release_font_glyph(uint32_t handle)
{
    if (handle >= g_tj_font_glyph_cache.size()) return;
    hle_free(g_tj_font_glyph_cache[handle].ea);
    g_tj_font_glyph_cache[handle] = {};
}

static bool tj_read_cell_font(uint32_t font_ea, CellFont* font)
{
    if (!font_ea || !font) return false;
    font->handle = vm_read32(font_ea + 0);
    font->type   = vm_read32(font_ea + 4);
    uint32_t bits = vm_read32(font_ea + 8);
    memcpy(&font->scale_x, &bits, sizeof bits);
    bits = vm_read32(font_ea + 12);
    memcpy(&font->scale_y, &bits, sizeof bits);
    return true;
}

static void tj_write_cell_font(uint32_t font_ea, const CellFont* font)
{
    if (!font_ea || !font) return;
    vm_write32(font_ea + 0, font->handle);
    vm_write32(font_ea + 4, font->type);
    uint32_t bits;
    memcpy(&bits, &font->scale_x, sizeof bits);
    vm_write32(font_ea + 8, bits);
    memcpy(&bits, &font->scale_y, sizeof bits);
    vm_write32(font_ea + 12, bits);
}

static void tj_write_guest_float(uint32_t guest_ea, float value)
{
    uint32_t bits;
    memcpy(&bits, &value, sizeof bits);
    vm_write32(guest_ea, bits);
}

static void tj_write_font_metrics(uint32_t metrics_ea,
                                  const CellFontGlyphMetrics* metrics)
{
    if (!metrics_ea || !metrics) return;
    const float values[8] = {
        metrics->width, metrics->height,
        metrics->h_bearing_x, metrics->h_bearing_y, metrics->h_advance,
        metrics->v_bearing_x, metrics->v_bearing_y, metrics->v_advance
    };
    for (uint32_t i = 0; i < 8; ++i)
        tj_write_guest_float(metrics_ea + i * 4, values[i]);
}

static void tj_cellAudioOutGetDeviceInfo(ppu_context* ctx)
{
    const uint32_t info_ea = (uint32_t)ctx->gpr[5];
    if (!info_ea) {
        ctx->gpr[3] = (uint64_t)(int64_t)(int32_t)CELL_AUDIO_OUT_ERROR_ILLEGAL_PARAMETER;
        return;
    }

    CellAudioOutDeviceInfo info = {};
    const int32_t rc = cellAudioOutGetDeviceInfo((uint32_t)ctx->gpr[3],
                                                  (uint32_t)ctx->gpr[4],
                                                  &info);
    if (rc == CELL_OK) {
        static_assert(sizeof(CellAudioOutSoundMode) == 8,
                      "Unexpected CellAudioOutSoundMode layout");
        static_assert(sizeof(CellAudioOutDeviceInfo) == 0x88,
                      "Unexpected CellAudioOutDeviceInfo layout");
        vm_write8(info_ea + 0, info.portType);
        vm_write8(info_ea + 1, info.availableModeCount);
        vm_write8(info_ea + 2, info.state);
        vm_write8(info_ea + 3, info.reserved[0]);
        vm_write8(info_ea + 4, info.reserved[1]);
        vm_write8(info_ea + 5, info.reserved[2]);
        vm_write16(info_ea + 6, info.latency);
        for (uint32_t i = 0; i < 16; ++i) {
            const uint32_t mode_ea = info_ea + 8 + i * 8;
            const CellAudioOutSoundMode& mode = info.availableModes[i];
            vm_write8(mode_ea + 0, mode.type);
            vm_write8(mode_ea + 1, mode.channel);
            vm_write8(mode_ea + 2, mode.fs);
            vm_write8(mode_ea + 3, mode.reserved);
            vm_write32(mode_ea + 4, mode.layout);
        }
    }
    ctx->gpr[3] = (uint64_t)(int64_t)rc;
}

static void tj_cellAudioOutConfigure(ppu_context* ctx)
{
    const uint32_t config_ea = (uint32_t)ctx->gpr[4];
    if (!config_ea) {
        ctx->gpr[3] = (uint64_t)(int64_t)(int32_t)CELL_AUDIO_OUT_ERROR_ILLEGAL_PARAMETER;
        return;
    }

    CellAudioOutConfiguration config = {};
    config.channel = vm_read8(config_ea + 0);
    config.encoder = vm_read8(config_ea + 1);
    config.downMixer = vm_read32(config_ea + 12);
    const int32_t rc = cellAudioOutConfigure((uint32_t)ctx->gpr[3], &config,
        (void*)(uintptr_t)(uint32_t)ctx->gpr[5], (uint32_t)ctx->gpr[6]);
    ctx->gpr[3] = (uint64_t)(int64_t)rc;
}

static void tj_cellSailMemAllocatorInitialize(ppu_context* ctx)
{
    const uint32_t allocator_ea = (uint32_t)ctx->gpr[3];
    const uint32_t funcs_ea = (uint32_t)ctx->gpr[4];
    if (!allocator_ea) {
        ctx->gpr[3] = (uint64_t)(int64_t)(int32_t)CELL_SAIL_ERROR_INVALID_ARGUMENT;
        return;
    }

    /* CellSailMemAllocator is { CellSailMemAllocatorFuncs*, pArg }, not the
     * callback pair inline. The old bridge copied the two callbacks into this
     * object, corrupting the allocator later copied into CellSailPlayer. */
    vm_write32(allocator_ea + 0, funcs_ea);
    ctx->gpr[3] = CELL_OK;
}

enum : uint32_t {
    TJ_SAIL_EVENT_PLAYER_CALL_COMPLETED = 2,
    TJ_SAIL_EVENT_PLAYER_STATE_CHANGED  = 3,
    TJ_SAIL_EVENT_SOURCE_EOS             = 8,
    TJ_SAIL_CALL_BOOT                    = 1,
    TJ_SAIL_CALL_OPEN_STREAM             = 2,
    TJ_SAIL_CALL_CLOSE_STREAM            = 3,
    TJ_SAIL_CALL_START                   = 10,
    TJ_SAIL_CALL_STOP                    = 11,
    TJ_SAIL_STATE_INITIALIZED            = 0,
    TJ_SAIL_STATE_BOOT_TRANSITION        = 1,
};

/* Tokyo Jungle must not observe STOP until its loading worker has returned.
 * Publishing STOP directly from GraphicsAdapterGetFrame's EOS callback can
 * race that worker and correlates with the missing image-18 handoff.
 * Keep EOS and loader completion as independent latches; whichever arrives
 * second publishes STOP exactly once.  The same ordering also preserves the
 * proven no-media fallback. */
static std::atomic<uint32_t> g_tj_deferred_stop_player_ea { 0 };
static std::atomic<bool> g_tj_media_load_complete { false };

constexpr uint32_t TJ_SAIL_GUEST_FRAME_COUNT = 4;
constexpr uint32_t TJ_SAIL_AUDIO_SAMPLE_COUNT = 256;
constexpr uint32_t TJ_SAIL_AUDIO_CHANNELS = 2;
constexpr uint32_t TJ_SAIL_AUDIO_BYTES_PER_SAMPLE = 4;
constexpr uint32_t TJ_SAIL_AUDIO_FRAME_BYTES =
    TJ_SAIL_AUDIO_SAMPLE_COUNT * TJ_SAIL_AUDIO_CHANNELS *
    TJ_SAIL_AUDIO_BYTES_PER_SAMPLE;
static std::atomic<uint32_t> g_tj_media_descriptor_ea { 0 };
static std::atomic<uint32_t> g_tj_media_player_ea { 0 };
static std::atomic<bool> g_tj_media_eos_sent { false };
static std::mutex g_tj_media_guest_mutex;
static uint32_t g_tj_media_frame_ea[TJ_SAIL_GUEST_FRAME_COUNT] = {};
static uint32_t g_tj_media_audio_frame_ea[TJ_SAIL_GUEST_FRAME_COUNT] = {};
static uint32_t g_tj_media_frame_bytes = 0;
static uint32_t g_tj_media_audio_frame_bytes = 0;
static uint32_t g_tj_media_session_id = 0;
static uint32_t g_tj_media_next_buffer = 0;
static uint32_t g_tj_media_audio_next_buffer = 0;
static uint32_t g_tj_media_last_frame = UINT32_MAX;
static uint64_t g_tj_media_last_pts_90k = 0;

static void tj_cellSailReleaseMediaFramesLocked()
{
    for (uint32_t& frame_ea : g_tj_media_frame_ea) {
        if (frame_ea) hle_free(frame_ea);
        frame_ea = 0;
    }
    for (uint32_t& frame_ea : g_tj_media_audio_frame_ea) {
        if (frame_ea) hle_free(frame_ea);
        frame_ea = 0;
    }
    g_tj_media_frame_bytes = 0;
    g_tj_media_audio_frame_bytes = 0;
    g_tj_media_next_buffer = 0;
    g_tj_media_audio_next_buffer = 0;
    g_tj_media_last_frame = UINT32_MAX;
    g_tj_media_last_pts_90k = 0;
}

static void tj_cellSailStopMediaLocked()
{
    tj_pamf_stop();
    tj_cellSailReleaseMediaFramesLocked();
    g_tj_media_player_ea.store(0, std::memory_order_release);
}

static void tj_cellSailReachMediaEosLocked()
{
    /* GetFrame buffers remain adapter-owned after the last successful return.
     * Stop the decoder at EOS, but retain those guest EAs until an explicit
     * stop/close/finalize or the next session replaces them. */
    tj_pamf_stop();
    g_tj_media_player_ea.store(0, std::memory_order_release);
}

static bool tj_cellSailAllocateMediaFramesLocked(const TjPamfMediaInfo& info)
{
    tj_cellSailReleaseMediaFramesLocked();
    const uint64_t frame_bytes64 = (uint64_t)info.width * info.height * 4;
    if (!frame_bytes64 || frame_bytes64 > UINT32_MAX) return false;
    g_tj_media_frame_bytes = (uint32_t)frame_bytes64;

    for (uint32_t& frame_ea : g_tj_media_frame_ea) {
        frame_ea = hle_memalign(128, g_tj_media_frame_bytes);
        if (!frame_ea) {
            tj_cellSailReleaseMediaFramesLocked();
            return false;
        }
    }
    g_tj_media_audio_frame_bytes = TJ_SAIL_AUDIO_FRAME_BYTES;
    for (uint32_t& frame_ea : g_tj_media_audio_frame_ea) {
        frame_ea = hle_memalign(128, g_tj_media_audio_frame_bytes);
        if (!frame_ea) {
            tj_cellSailReleaseMediaFramesLocked();
            return false;
        }
    }
    return true;
}

static int8_t tj_cellSailFrameRateCode(const TjPamfMediaInfo& info)
{
    if (info.frame_rate_num == 24000 && info.frame_rate_den == 1001) return 0;
    if (info.frame_rate_num == 24 && info.frame_rate_den == 1) return 1;
    if (info.frame_rate_num == 25 && info.frame_rate_den == 1) return 2;
    if (info.frame_rate_num == 30000 && info.frame_rate_den == 1001) return 3;
    if (info.frame_rate_num == 30 && info.frame_rate_den == 1) return 4;
    if (info.frame_rate_num == 50 && info.frame_rate_den == 1) return 5;
    if (info.frame_rate_num == 60000 && info.frame_rate_den == 1001) return 6;
    if (info.frame_rate_num == 60 && info.frame_rate_den == 1) return 7;
    return -1;
}

static void tj_cellSailPublishMediaFormat(uint32_t player_ea,
                                          const TjPamfMediaInfo& info,
                                          uint32_t session_id)
{
    const uint32_t adapter_ea = vm_read32(player_ea + 0x5C);
    if (!adapter_ea || !vm_read8(adapter_ea + 0)) return;

    const uint32_t format_ea = adapter_ea + 0x1C;
    vm_write8(format_ea + 0x00, 0); /* ARGB interleaved */
    vm_write8(format_ea + 0x01, 0); /* progressive */
    vm_write8(format_ea + 0x02, 8); /* bits per color */
    vm_write8(format_ea + 0x03,
              (uint8_t)tj_cellSailFrameRateCode(info));
    vm_write16(format_ea + 0x04, (uint16_t)info.width);
    vm_write16(format_ea + 0x06, (uint16_t)info.height);
    vm_write32(format_ea + 0x08, info.width * 4);
    vm_write32(format_ea + 0x0C, 0xFF);
    vm_write8(format_ea + 0x10, 1); /* BT.709 */
    vm_write8(format_ea + 0x11, 1); /* square pixels */
    vm_write8(format_ea + 0x12, 1); /* limited source range */
    vm_write8(format_ea + 0x13, 0xFF);
    vm_write32(format_ea + 0x14, 0xFFFFFFFFu);
    vm_write64(format_ea + 0x18, UINT64_MAX);

    /* CellSail normally publishes the negotiated format before frames begin.
     * Tokyo Jungle supplied this callback; invoke it only on the guest thread. */
    const uint32_t format_changed_opd = vm_read32(adapter_ea + 12);
    const uint32_t callback_arg = vm_read32(adapter_ea + 24);
    if (format_changed_opd) {
        ps3_invoke_guest(format_changed_opd, callback_arg, format_ea,
                         session_id, 0, 0, 0, 0, 0);
    }
}

static void tj_cellSailPublishSoundFormat(uint32_t player_ea,
                                          uint32_t session_id)
{
    const uint32_t adapter_ea = vm_read32(player_ea + 0x58);
    if (!adapter_ea || !vm_read8(adapter_ea + 0)) return;

    const uint32_t format_ea = adapter_ea + 0x18;
    vm_write8(format_ea + 0x00, 1); /* FLOAT32 */
    vm_write8(format_ea + 0x01, TJ_SAIL_AUDIO_CHANNELS);
    vm_write16(format_ea + 0x02, TJ_SAIL_AUDIO_SAMPLE_COUNT);
    vm_write32(format_ea + 0x04, 48000);
    vm_write32(format_ea + 0x08, 2); /* stereo channel layout */
    vm_write32(format_ea + 0x0C, 0xFFFFFFFFu);
    vm_write64(format_ea + 0x10, UINT64_MAX);

    const uint32_t format_changed_opd = vm_read32(adapter_ea + 12);
    const uint32_t callback_arg = vm_read32(adapter_ea + 16);
    if (format_changed_opd) {
        ps3_invoke_guest(format_changed_opd, callback_arg, format_ea,
                         session_id, 0, 0, 0, 0, 0);
    }
}

static bool tj_cellSailNotify(uint32_t player_ea, uint32_t major,
                              uint32_t minor, uint64_t param0,
                              uint64_t param1)
{
    if (!player_ea) return false;

    const uint32_t callback_opd = vm_read32(player_ea + 8);
    const uint32_t callback_arg = vm_read32(player_ea + 12);
    if (!callback_opd) return false;

    const uint64_t event = ((uint64_t)major << 32) | minor;
    fprintf(stderr,
            "[cellSail] Notify(self=0x%08X event=%u:%u p0=0x%llX p1=0x%llX)\n",
            player_ea, major, minor,
            (unsigned long long)param0, (unsigned long long)param1);
    fflush(stderr);
    ps3_invoke_guest(callback_opd, callback_arg, event,
                     param0, param1, 0, 0, 0, 0);
    return true;
}

static void tj_cellSailPlayerInitialize2(ppu_context* ctx)
{
    const uint32_t player_ea       = (uint32_t)ctx->gpr[3];
    const uint32_t allocator_ea    = (uint32_t)ctx->gpr[4];
    const uint32_t callback_opd    = (uint32_t)ctx->gpr[5];
    const uint32_t callback_arg    = (uint32_t)ctx->gpr[6];
    const uint32_t attribute_ea    = (uint32_t)ctx->gpr[7];
    const uint32_t resource_ea     = (uint32_t)ctx->gpr[8];

    if (!player_ea || !allocator_ea || !callback_opd ||
        !attribute_ea || !resource_ea) {
        ctx->gpr[3] = (uint64_t)(int64_t)(int32_t)CELL_SAIL_ERROR_INVALID_ARGUMENT;
        return;
    }

    /* RPCS3's public CellSail layout: allocator[8], callback, callbackArg,
     * attribute[32], resource[16], playback state. Guest VM helpers preserve
     * the big-endian values while copying between guest structures. */
    for (uint32_t off = 0; off < 8; off += 4)
        vm_write32(player_ea + off, vm_read32(allocator_ea + off));
    vm_write32(player_ea + 8, callback_opd);
    vm_write32(player_ea + 12, callback_arg);
    for (uint32_t off = 0; off < 32; off += 4)
        vm_write32(player_ea + 16 + off, vm_read32(attribute_ea + off));
    for (uint32_t off = 0; off < 16; off += 4)
        vm_write32(player_ea + 48 + off, vm_read32(resource_ea + off));
    vm_write8(player_ea + 84, 1); /* paused */
    vm_write8(player_ea + 85, 0); /* booted */

    fprintf(stderr,
            "[cellSail] PlayerInitialize2(self=0x%08X cb=0x%08X arg=0x%08X)\n",
            player_ea, callback_opd, callback_arg);
    fflush(stderr);

    /* CellSailEvent is passed by value: major occupies the high 32 bits.
     * Initialization synchronously reports PLAYER_STATE_CHANGED (3), state
     * INITIALIZED (0). Tokyo Jungle observes this callback during media
     * startup; later asynchronous work has a separate completion wait. */
    tj_cellSailNotify(player_ea, TJ_SAIL_EVENT_PLAYER_STATE_CHANGED, 0,
                      TJ_SAIL_STATE_INITIALIZED, 0);
    ctx->gpr[3] = CELL_OK;
}

static void tj_cellSailSoundAdapterInitialize(ppu_context* ctx)
{
    const uint32_t self_ea      = (uint32_t)ctx->gpr[3];
    const uint32_t callbacks_ea = (uint32_t)ctx->gpr[4];
    const uint32_t arg_ea       = (uint32_t)ctx->gpr[5];
    if (!self_ea || !callbacks_ea) {
        ctx->gpr[3] = (uint64_t)(int64_t)(int32_t)CELL_SAIL_ERROR_INVALID_ARGUMENT;
        return;
    }

    vm_write8(self_ea + 0, 1);  /* initialized */
    vm_write8(self_ea + 1, 0);  /* registered */
    vm_write32(self_ea + 4,  vm_read32(callbacks_ea + 0));
    vm_write32(self_ea + 8,  vm_read32(callbacks_ea + 4));
    vm_write32(self_ea + 12, vm_read32(callbacks_ea + 8));
    vm_write32(self_ea + 16, arg_ea);
    ctx->gpr[3] = CELL_OK;
}

static void tj_cellSailGraphicsAdapterInitialize(ppu_context* ctx)
{
    const uint32_t self_ea      = (uint32_t)ctx->gpr[3];
    const uint32_t callbacks_ea = (uint32_t)ctx->gpr[4];
    const uint32_t arg_ea       = (uint32_t)ctx->gpr[5];
    if (!self_ea || !callbacks_ea) {
        ctx->gpr[3] = (uint64_t)(int64_t)(int32_t)CELL_SAIL_ERROR_INVALID_ARGUMENT;
        return;
    }

    vm_write8(self_ea + 0, 1);  /* initialized */
    vm_write8(self_ea + 1, 0);  /* registered */
    vm_write32(self_ea + 4,  vm_read32(callbacks_ea + 0));
    vm_write32(self_ea + 8,  vm_read32(callbacks_ea + 4));
    vm_write32(self_ea + 12, vm_read32(callbacks_ea + 8));
    vm_write32(self_ea + 16, vm_read32(callbacks_ea + 12));
    vm_write32(self_ea + 20, vm_read32(callbacks_ea + 16));
    vm_write32(self_ea + 24, arg_ea);
    ctx->gpr[3] = CELL_OK;
}

static void tj_cellSailPlayerSetSoundAdapter(ppu_context* ctx)
{
    const uint32_t player_ea  = (uint32_t)ctx->gpr[3];
    const uint32_t index      = (uint32_t)ctx->gpr[4];
    const uint32_t adapter_ea = (uint32_t)ctx->gpr[5];
    if (!player_ea || !adapter_ea || !vm_read8(adapter_ea + 0)) {
        ctx->gpr[3] = (uint64_t)(int64_t)(int32_t)CELL_SAIL_ERROR_INVALID_ARGUMENT;
        return;
    }

    /* Public CellSail guest layout: player.sAdapter=+0x58 and the sound
     * adapter's index follows its arg pointer at +0x14. */
    vm_write32(player_ea + 0x58, adapter_ea);
    vm_write32(adapter_ea + 0x14, index);
    vm_write8(adapter_ea + 1, 1); /* registered */
    fprintf(stderr,
            "[cellSail] PlayerSetSoundAdapter(self=0x%08X index=%u adapter=0x%08X)\n",
            player_ea, index, adapter_ea);
    fflush(stderr);
    ctx->gpr[3] = CELL_OK;
}

static void tj_cellSailPlayerSetGraphicsAdapter(ppu_context* ctx)
{
    const uint32_t player_ea  = (uint32_t)ctx->gpr[3];
    const uint32_t index      = (uint32_t)ctx->gpr[4];
    const uint32_t adapter_ea = (uint32_t)ctx->gpr[5];
    if (!player_ea || !adapter_ea || !vm_read8(adapter_ea + 0)) {
        ctx->gpr[3] = (uint64_t)(int64_t)(int32_t)CELL_SAIL_ERROR_INVALID_ARGUMENT;
        return;
    }

    /* CellSailVideoFormat is 0x20 bytes; graphics adapter index is +0x3C. */
    vm_write32(player_ea + 0x5C, adapter_ea);
    vm_write32(adapter_ea + 0x3C, index);
    vm_write8(adapter_ea + 1, 1); /* registered */
    fprintf(stderr,
            "[cellSail] PlayerSetGraphicsAdapter(self=0x%08X index=%u adapter=0x%08X)\n",
            player_ea, index, adapter_ea);
    fflush(stderr);
    ctx->gpr[3] = CELL_OK;
}

static void tj_cellSailPlayerBoot(ppu_context* ctx)
{
    const uint32_t player_ea = (uint32_t)ctx->gpr[3];
    if (!player_ea || !vm_read32(player_ea + 8)) {
        ctx->gpr[3] = (uint64_t)(int64_t)(int32_t)CELL_SAIL_ERROR_INVALID_ARGUMENT;
        return;
    }

    fprintf(stderr, "[cellSail] PlayerBoot(self=0x%08X)\n", player_ea);
    fflush(stderr);
    tj_cellSailNotify(player_ea, TJ_SAIL_EVENT_PLAYER_STATE_CHANGED, 0,
                      TJ_SAIL_STATE_BOOT_TRANSITION, 0);
    vm_write8(player_ea + 85, 1); /* booted */
    tj_cellSailNotify(player_ea, TJ_SAIL_EVENT_PLAYER_CALL_COMPLETED,
                      TJ_SAIL_CALL_BOOT, 0, 0);
    ctx->gpr[3] = CELL_OK;
}

static void tj_cellSailPlayerOpenStream(ppu_context* ctx)
{
    const uint32_t player_ea = (uint32_t)ctx->gpr[3];
    if (!player_ea || !vm_read8(player_ea + 85)) {
        ctx->gpr[3] = (uint64_t)(int64_t)(int32_t)CELL_SAIL_ERROR_INVALID_STATE;
        return;
    }

    fprintf(stderr, "[cellSail] PlayerOpenStream(self=0x%08X) - %s\n",
            player_ea, tj_pamf_prepared() ? "host PAMF bridge" : "no-media fallback");
    fflush(stderr);
    tj_cellSailNotify(player_ea, TJ_SAIL_EVENT_PLAYER_CALL_COMPLETED,
                      TJ_SAIL_CALL_OPEN_STREAM, 0, 0);
    ctx->gpr[3] = CELL_OK;
}

static void tj_cellSailPlayerCloseStream(ppu_context* ctx)
{
    const uint32_t player_ea = (uint32_t)ctx->gpr[3];
    if (!player_ea) {
        ctx->gpr[3] = (uint64_t)(int64_t)(int32_t)CELL_SAIL_ERROR_INVALID_ARGUMENT;
        return;
    }

    {
        std::lock_guard<std::mutex> lock(g_tj_media_guest_mutex);
        tj_cellSailStopMediaLocked();
    }
    fprintf(stderr, "[cellSail] PlayerCloseStream(self=0x%08X)\n", player_ea);
    fflush(stderr);
    tj_cellSailNotify(player_ea, TJ_SAIL_EVENT_PLAYER_CALL_COMPLETED,
                      TJ_SAIL_CALL_CLOSE_STREAM, 0, 0);
    ctx->gpr[3] = CELL_OK;
}

static void tj_cellSailPlayerStart(ppu_context* ctx)
{
    const uint32_t player_ea = (uint32_t)ctx->gpr[3];
    if (!player_ea || !vm_read8(player_ea + 85)) {
        ctx->gpr[3] = (uint64_t)(int64_t)(int32_t)CELL_SAIL_ERROR_INVALID_STATE;
        return;
    }

    vm_write8(player_ea + 84, 0); /* not paused */
    g_tj_media_load_complete.store(false, std::memory_order_release);
    g_tj_deferred_stop_player_ea.store(0, std::memory_order_release);

    TjPamfMediaInfo media_info{};
    bool media_started = false;
    uint32_t session_id = 0;
    {
        std::lock_guard<std::mutex> lock(g_tj_media_guest_mutex);
        if (tj_pamf_get_info(&media_info) &&
            tj_cellSailAllocateMediaFramesLocked(media_info) &&
            tj_pamf_start()) {
            ++g_tj_media_session_id;
            if (!g_tj_media_session_id) ++g_tj_media_session_id;
            session_id = g_tj_media_session_id;
            g_tj_media_player_ea.store(player_ea, std::memory_order_release);
            g_tj_media_eos_sent.store(false, std::memory_order_release);
            media_started = true;
        } else {
            tj_cellSailStopMediaLocked();
        }
    }

    if (media_started) {
        tj_cellSailPublishMediaFormat(player_ea, media_info, session_id);
        tj_cellSailPublishSoundFormat(player_ea, session_id);
        fprintf(stderr,
                "[cellSail] PlayerStart(self=0x%08X) - host PAMF session=%u\n",
                player_ea, session_id);
        fflush(stderr);
        tj_cellSailNotify(player_ea, TJ_SAIL_EVENT_PLAYER_CALL_COMPLETED,
                          TJ_SAIL_CALL_START, 0, 0);
        ctx->gpr[3] = CELL_OK;
        return;
    }

    fprintf(stderr,
            "[cellSail] PlayerStart(self=0x%08X) - no-media fallback\n",
            player_ea);
    fflush(stderr);
    tj_cellSailNotify(player_ea, TJ_SAIL_EVENT_PLAYER_CALL_COMPLETED,
                      TJ_SAIL_CALL_START, 0, 0);
    /* Decoder absence retains the proven fallback: report end-of-source but
     * keep the movie-active bit set until the loader-return hook completes. */
    g_tj_deferred_stop_player_ea.store(player_ea,
                                       std::memory_order_release);
    tj_cellSailNotify(player_ea, TJ_SAIL_EVENT_SOURCE_EOS, 0, 0, 0);
    ctx->gpr[3] = CELL_OK;
}

static void tj_cellSailCompleteStoppedPlayback(uint32_t player_ea,
                                               const char* completion_name,
                                               const char* fade_name)
{
    if (!player_ea) return;

    vm_write8(player_ea + 84, 1); /* playback is now stopped */
    fprintf(stderr, "[cellSail] %s(self=0x%08X) -> STOP\n",
            completion_name, player_ea);
    fflush(stderr);
    tj_cellSailNotify(player_ea, TJ_SAIL_EVENT_PLAYER_CALL_COMPLETED,
                      TJ_SAIL_CALL_STOP, 0, 0);

    /* The real graphics-adapter path marks the loading fade complete after
     * it consumes the final decoded frame. This bridge needs the same guest
     * completion state once no further frames can arrive. The USA ELF's fixed
     * TOC owns the fade singleton pointer at +0x1328. */
    constexpr uint32_t tj_toc_ea = 0x00359220u;
    const uint32_t fade_ea = vm_read32(tj_toc_ea + 0x1328u);
    if (fade_ea) {
        const uint8_t old_done = vm_read8(fade_ea + 0x2Cu);
        vm_write8(fade_ea + 0x2Cu, 1);
        fprintf(stderr,
                "[cellSail] %sFadeComplete(fade=0x%08X done=%u->1)\n",
                fade_name, fade_ea, old_done);
        fflush(stderr);
    }
}

static void tj_cellSailTryCompleteStoppedPlaybackAfterLoad(void)
{
    if (!g_tj_media_load_complete.load(std::memory_order_acquire)) return;
    if (tj_pamf_active()) return;
    const uint32_t player_ea =
        g_tj_deferred_stop_player_ea.exchange(0, std::memory_order_acq_rel);
    if (!player_ea) return;

    tj_cellSailCompleteStoppedPlayback(player_ea, "CompleteAfterLoad",
                                       "Media");
}

void tj_cellSailCompleteNoMediaAfterLoad(void)
{
    g_tj_media_load_complete.store(true, std::memory_order_release);
    tj_cellSailTryCompleteStoppedPlaybackAfterLoad();
}

static int64_t tj_sys_ppu_thread_join(ppu_context* ctx)
{
    /* The scene controller joins its loading_thread through JThread at this
     * USA ELF callsite. Title/menu loads do not execute the Survival-only
     * 001EFC5C return hook, so their movies otherwise wait forever after EOS.
     * Observe successful completion of the real join; unrelated joins and
     * failed waits must not release the movie/loader completion latch. */
    const bool scene_loader_join = ctx->lr == 0x001F0EBCu;
    const int64_t result = sys_ppu_thread_join(ctx);
    if (result == CELL_OK && scene_loader_join)
        tj_cellSailCompleteNoMediaAfterLoad();
    return result;
}

static void tj_cellSailPlayerStop(ppu_context* ctx)
{
    const uint32_t player_ea = (uint32_t)ctx->gpr[3];
    if (!player_ea) {
        ctx->gpr[3] = (uint64_t)(int64_t)(int32_t)CELL_SAIL_ERROR_INVALID_ARGUMENT;
        return;
    }

    {
        std::lock_guard<std::mutex> lock(g_tj_media_guest_mutex);
        tj_cellSailStopMediaLocked();
    }
    uint32_t expected = player_ea;
    g_tj_deferred_stop_player_ea.compare_exchange_strong(
        expected, 0, std::memory_order_acq_rel);
    vm_write8(player_ea + 84, 1); /* paused */
    fprintf(stderr, "[cellSail] PlayerStop(self=0x%08X)\n", player_ea);
    fflush(stderr);
    tj_cellSailNotify(player_ea, TJ_SAIL_EVENT_PLAYER_CALL_COMPLETED,
                      TJ_SAIL_CALL_STOP, 0, 0);
    ctx->gpr[3] = CELL_OK;
}

static void tj_cellSailPlayerSetRepeatMode(ppu_context* ctx)
{
    const uint32_t player_ea = (uint32_t)ctx->gpr[3];
    const int32_t repeat_mode = (int32_t)ctx->gpr[4];
    const uint32_t command_ea = (uint32_t)ctx->gpr[5];
    if (!player_ea) {
        ctx->gpr[3] = (uint64_t)(int64_t)(int32_t)CELL_SAIL_ERROR_INVALID_ARGUMENT;
        return;
    }

    vm_write32(player_ea + 64, command_ea);
    vm_write32(player_ea + 68, (uint32_t)repeat_mode);
    ctx->gpr[3] = (uint64_t)(int64_t)repeat_mode;
}

static void tj_cellSailPlayerGetRepeatMode(ppu_context* ctx)
{
    const uint32_t player_ea = (uint32_t)ctx->gpr[3];
    if (!player_ea) {
        ctx->gpr[3] = (uint64_t)(int64_t)(int32_t)CELL_SAIL_ERROR_INVALID_ARGUMENT;
        return;
    }

    /* CellSail keeps the optional start-command pointer in the player.  The
     * SDK getter returns the mode; pCommand is not an output buffer (RPCS3's
     * firmware model likewise only assigns its local pointer parameter). */
    ctx->gpr[3] = (uint64_t)(int64_t)(int32_t)vm_read32(player_ea + 68);
}

static void tj_cellSailPlayerSetPaused(ppu_context* ctx)
{
    const uint32_t player_ea = (uint32_t)ctx->gpr[3];
    if (!player_ea) {
        ctx->gpr[3] = (uint64_t)(int64_t)(int32_t)CELL_SAIL_ERROR_INVALID_ARGUMENT;
        return;
    }

    const bool paused = ctx->gpr[4] != 0;
    vm_write8(player_ea + 84, paused ? 1 : 0);
    tj_pamf_set_paused(paused);
    ctx->gpr[3] = CELL_OK;
}

static void tj_read_guest_string(uint32_t string_ea, char* out, size_t out_size)
{
    if (!out || out_size == 0) return;
    size_t i = 0;
    if (string_ea) {
        for (; i + 1 < out_size; ++i) {
            const uint8_t ch = vm_read8(string_ea + (uint32_t)i);
            out[i] = (char)ch;
            if (!ch) break;
        }
    }
    if (i == 0 || out[i] != '\0') out[i < out_size ? i : out_size - 1] = '\0';
}

static void tj_cellSailPlayerCreateDescriptor(ppu_context* ctx)
{
    const uint32_t player_ea  = (uint32_t)ctx->gpr[3];
    const int32_t stream_type = (int32_t)ctx->gpr[4];
    const uint32_t uri_ea     = (uint32_t)ctx->gpr[6];
    const uint32_t out_ea     = (uint32_t)ctx->gpr[7];
    if (!player_ea || !out_ea) {
        ctx->gpr[3] = (uint64_t)(int64_t)(int32_t)CELL_SAIL_ERROR_INVALID_ARGUMENT;
        return;
    }

    /* CellSailDescriptor is guest-owned and opaque to the title. Reserve the
     * SDK's full 0x100-byte object allowance; the public fields occupy 0x10.
     * Keep a private URI copy in the unused tail for GetUri. */
    const uint32_t descriptor_ea = hle_malloc(0x100);
    if (!descriptor_ea) {
        ctx->gpr[3] = (uint64_t)(int64_t)(int32_t)CELL_SAIL_ERROR_OUT_OF_MEMORY;
        return;
    }

    vm_write8(descriptor_ea + 0, 0); /* autoSelection */
    vm_write8(descriptor_ea + 1, 0); /* registered */
    vm_write32(descriptor_ea + 4, (uint32_t)stream_type);
    vm_write32(descriptor_ea + 8, 0);  /* private: host media prepared */
    vm_write32(descriptor_ea + 12, 0); /* private: database guest EA */

    char uri[0xF0] = {};
    tj_read_guest_string(uri_ea, uri, sizeof(uri));
    for (uint32_t i = 0; i < sizeof(uri); ++i) {
        vm_write8(descriptor_ea + 0x10 + i, (uint8_t)uri[i]);
        if (!uri[i]) break;
    }

    bool media_prepared = false;
    TjPamfMediaInfo media_info{};
    if (stream_type == 0 && std::strncmp(uri, "x-cell-fs://", 12) == 0) {
        char host_path[1100] = {};
        const char* guest_path = uri + 12;
        std::lock_guard<std::mutex> lock(g_tj_media_guest_mutex);
        tj_cellSailStopMediaLocked();
        tj_pamf_close();
        g_tj_media_descriptor_ea.store(0, std::memory_order_release);
        if (!std::getenv("TJ_PAMF_DISABLE") &&
            ppu_fs_guest_to_host(guest_path, host_path, sizeof(host_path)) &&
            tj_pamf_prepare(host_path, &media_info)) {
            vm_write32(descriptor_ea + 8, 1);
            g_tj_media_descriptor_ea.store(descriptor_ea,
                                           std::memory_order_release);
            media_prepared = true;
        }
    }

    vm_write32(out_ea, descriptor_ea);
    vm_write32(player_ea + 68, 0); /* repeatMode */
    fprintf(stderr,
            "[cellSail] PlayerCreateDescriptor(self=0x%08X type=%d uriEA=0x%08X desc=0x%08X media=%s uri='%s')\n",
            player_ea, stream_type, uri_ea, descriptor_ea,
            media_prepared ? "PAMF" : "fallback", uri);
    fflush(stderr);
    ctx->gpr[3] = CELL_OK;
}

static void tj_cellSailPlayerDestroyDescriptor(ppu_context* ctx)
{
    const uint32_t player_ea = (uint32_t)ctx->gpr[3];
    const uint32_t descriptor_ea = (uint32_t)ctx->gpr[4];
    if (!player_ea || !descriptor_ea) {
        ctx->gpr[3] = (uint64_t)(int64_t)(int32_t)
            CELL_SAIL_ERROR_INVALID_ARGUMENT;
        return;
    }

    std::lock_guard<std::mutex> lock(g_tj_media_guest_mutex);
    if (g_tj_media_descriptor_ea.load(std::memory_order_acquire) == descriptor_ea &&
        g_tj_media_player_ea.load(std::memory_order_acquire)) {
        ctx->gpr[3] = (uint64_t)(int64_t)(int32_t)CELL_SAIL_ERROR_INVALID_STATE;
        return;
    }

    if (vm_read8(descriptor_ea + 1)) {
        // The title closes the stream, then destroys this descriptor at
        // 001824F0; it does not import PlayerRemoveDescriptor. Detach the
        // closed descriptor from its owning player before freeing it.
        const uint32_t count = vm_read32(player_ea + 72);
        uint32_t found = count;
        if (count <= 2) {
            for (uint32_t i = 0; i < count; ++i) {
                if (vm_read32(player_ea + 76 + i * 4) == descriptor_ea) {
                    found = i;
                    break;
                }
            }
        }
        if (found == count) {
            ctx->gpr[3] = (uint64_t)(int64_t)(int32_t)CELL_SAIL_ERROR_INVALID_STATE;
            return;
        }
        for (uint32_t i = found + 1; i < count; ++i)
            vm_write32(player_ea + 76 + (i - 1) * 4,
                       vm_read32(player_ea + 76 + i * 4));
        vm_write32(player_ea + 76 + (count - 1) * 4, 0);
        vm_write32(player_ea + 72, count - 1);
        vm_write8(descriptor_ea + 1, 0);
    }

    uint32_t expected = descriptor_ea;
    if (g_tj_media_descriptor_ea.compare_exchange_strong(
            expected, 0, std::memory_order_acq_rel)) {
        tj_cellSailStopMediaLocked();
        tj_pamf_close();
    }
    hle_free(descriptor_ea);
    ctx->gpr[3] = CELL_OK;
}

static void tj_cellSailPlayerAddDescriptor(ppu_context* ctx)
{
    const uint32_t player_ea     = (uint32_t)ctx->gpr[3];
    const uint32_t descriptor_ea = (uint32_t)ctx->gpr[4];
    if (!player_ea || !descriptor_ea) {
        ctx->gpr[3] = (uint64_t)(int64_t)(int32_t)CELL_SAIL_ERROR_INVALID_ARGUMENT;
        return;
    }

    const uint32_t count = vm_read32(player_ea + 72);
    if (count >= 2) {
        ctx->gpr[3] = (uint64_t)(int64_t)(int32_t)CELL_SAIL_ERROR_INVALID_STATE;
        return;
    }
    vm_write32(player_ea + 76 + count * 4, descriptor_ea);
    vm_write32(player_ea + 72, count + 1);
    vm_write8(descriptor_ea + 1, 1);
    fprintf(stderr,
            "[cellSail] PlayerAddDescriptor(self=0x%08X desc=0x%08X count=%u)\n",
            player_ea, descriptor_ea, count + 1);
    fflush(stderr);
    ctx->gpr[3] = CELL_OK;
}

static void tj_cellSailPlayerRemoveDescriptor(ppu_context* ctx)
{
    const uint32_t player_ea     = (uint32_t)ctx->gpr[3];
    const uint32_t descriptor_ea = (uint32_t)ctx->gpr[4];
    if (!player_ea) {
        ctx->gpr[3] = (uint64_t)(int64_t)(int32_t)CELL_SAIL_ERROR_INVALID_ARGUMENT;
        return;
    }

    uint32_t count = vm_read32(player_ea + 72);
    if (count > 2) count = 2;
    uint32_t found = count;
    for (uint32_t i = 0; i < count; ++i) {
        if (vm_read32(player_ea + 76 + i * 4) == descriptor_ea) {
            found = i;
            break;
        }
    }
    if (found < count) {
        for (uint32_t i = found + 1; i < count; ++i)
            vm_write32(player_ea + 76 + (i - 1) * 4,
                       vm_read32(player_ea + 76 + i * 4));
        vm_write32(player_ea + 76 + (count - 1) * 4, 0);
        vm_write32(player_ea + 72, --count);
        if (descriptor_ea) vm_write8(descriptor_ea + 1, 0);
    }
    ctx->gpr[3] = count;
}

static void tj_cellSailDescriptorSetAutoSelection(ppu_context* ctx)
{
    const uint32_t descriptor_ea = (uint32_t)ctx->gpr[3];
    const uint8_t enabled = (uint8_t)ctx->gpr[4];
    if (!descriptor_ea) {
        ctx->gpr[3] = (uint64_t)(int64_t)(int32_t)CELL_SAIL_ERROR_INVALID_ARGUMENT;
        return;
    }
    vm_write8(descriptor_ea + 0, enabled);
    ctx->gpr[3] = enabled;
}

static void tj_cellSailDescriptorCreateDatabase(ppu_context* ctx)
{
    const uint32_t descriptor_ea = (uint32_t)ctx->gpr[3];
    const uint32_t database_ea   = (uint32_t)ctx->gpr[4];
    const uint32_t database_size = (uint32_t)ctx->gpr[5];
    if (!descriptor_ea || !database_ea || !database_size) {
        ctx->gpr[3] = (uint64_t)(int64_t)(int32_t)CELL_SAIL_ERROR_INVALID_ARGUMENT;
        return;
    }

    /* The host bridge owns container parsing/decoding. Keep the guest reader
     * opaque and deterministic; the context-aware PAMF query handlers below
     * publish the parsed timing without native-dereferencing a guest EA. */
    const uint32_t clear_size = database_size < 0x1000 ? database_size : 0x1000;
    for (uint32_t i = 0; i < clear_size; ++i) vm_write8(database_ea + i, 0);
    vm_write32(descriptor_ea + 12, database_ea);
    fprintf(stderr,
            "[cellSail] DescriptorCreateDatabase(desc=0x%08X db=0x%08X size=0x%X) - %s\n",
            descriptor_ea, database_ea, database_size,
            vm_read32(descriptor_ea + 8) ? "host PAMF" : "empty fallback");
    fflush(stderr);
    ctx->gpr[3] = CELL_OK;
}

static void tj_cellSailDescriptorDestroyDatabase(ppu_context* ctx)
{
    ctx->gpr[3] = (uint32_t)ctx->gpr[3] ? CELL_OK
        : (uint64_t)(int64_t)(int32_t)CELL_SAIL_ERROR_INVALID_ARGUMENT;
}

/* Generic cellPamf exports receive native C pointers, but imported PS3 calls
 * pass 32-bit guest addresses. These context handlers keep guest EAs inside
 * VM helpers and expose metadata parsed by the host bridge. */
static void tj_cellPamfReaderGetPresentationStartTime(ppu_context* ctx)
{
    const uint32_t reader_ea = (uint32_t)ctx->gpr[3];
    const uint32_t time_ea   = (uint32_t)ctx->gpr[4];
    if (!reader_ea || !time_ea) {
        ctx->gpr[3] = (uint64_t)(int64_t)(int32_t)CELL_PAMF_ERROR_INVALID_ARG;
        return;
    }
    TjPamfMediaInfo info{};
    const uint64_t pts = tj_pamf_get_info(&info)
        ? info.presentation_start_90k : 0;
    vm_write64(time_ea, pts);
    fprintf(stderr,
            "[cellPamf] GetPresentationStartTime(reader=0x%08X) -> %llu\n",
            reader_ea, (unsigned long long)pts);
    ctx->gpr[3] = CELL_OK;
}

static void tj_cellPamfReaderGetPresentationEndTime(ppu_context* ctx)
{
    const uint32_t reader_ea = (uint32_t)ctx->gpr[3];
    const uint32_t time_ea   = (uint32_t)ctx->gpr[4];
    if (!reader_ea || !time_ea) {
        ctx->gpr[3] = (uint64_t)(int64_t)(int32_t)CELL_PAMF_ERROR_INVALID_ARG;
        return;
    }
    TjPamfMediaInfo info{};
    const uint64_t pts = tj_pamf_get_info(&info)
        ? info.presentation_end_90k : 0;
    vm_write64(time_ea, pts);
    fprintf(stderr,
            "[cellPamf] GetPresentationEndTime(reader=0x%08X) -> %llu\n",
            reader_ea, (unsigned long long)pts);
    ctx->gpr[3] = CELL_OK;
}

static void tj_cellPamfReaderGetMuxRateBound(ppu_context* ctx)
{
    TjPamfMediaInfo info{};
    const uint32_t mux_rate = tj_pamf_get_info(&info)
        ? info.mux_rate_bound : 0;
    fprintf(stderr,
            "[cellPamf] GetMuxRateBound(reader=0x%08X) -> %u\n",
            (uint32_t)ctx->gpr[3], mux_rate);
    ctx->gpr[3] = mux_rate;
}

static void tj_cellPamfReaderGetNumberOfStreams(ppu_context* ctx)
{
    const uint32_t reader_ea = (uint32_t)ctx->gpr[3];
    if (!reader_ea) {
        ctx->gpr[3] = (uint64_t)(int64_t)(int32_t)CELL_PAMF_ERROR_INVALID_ARG;
        return;
    }
    TjPamfMediaInfo info{};
    const int32_t stream_count = tj_pamf_get_info(&info)
        ? (int32_t)info.stream_count : 0;
    fprintf(stderr,
            "[cellPamf] GetNumberOfStreams(reader=0x%08X) -> %d\n",
            reader_ea, stream_count);
    ctx->gpr[3] = (uint64_t)(int64_t)stream_count;
}

static void tj_cellSailDescriptorGetStreamType(ppu_context* ctx)
{
    const uint32_t descriptor_ea = (uint32_t)ctx->gpr[3];
    const uint32_t type_out_ea   = (uint32_t)ctx->gpr[4];
    if (!descriptor_ea || !type_out_ea) {
        ctx->gpr[3] = (uint64_t)(int64_t)(int32_t)CELL_SAIL_ERROR_INVALID_ARGUMENT;
        return;
    }
    vm_write32(type_out_ea, vm_read32(descriptor_ea + 4));
    ctx->gpr[3] = CELL_OK;
}

static void tj_cellSailDescriptorGetUri(ppu_context* ctx)
{
    const uint32_t descriptor_ea = (uint32_t)ctx->gpr[3];
    const uint32_t uri_out_ea    = (uint32_t)ctx->gpr[4];
    const uint32_t max_len       = (uint32_t)ctx->gpr[5];
    if (!descriptor_ea || !uri_out_ea || !max_len) {
        ctx->gpr[3] = (uint64_t)(int64_t)(int32_t)CELL_SAIL_ERROR_INVALID_ARGUMENT;
        return;
    }
    uint32_t i = 0;
    for (; i + 1 < max_len && i < 0xF0; ++i) {
        const uint8_t ch = vm_read8(descriptor_ea + 0x10 + i);
        vm_write8(uri_out_ea + i, ch);
        if (!ch) break;
    }
    if (i + 1 >= max_len || i >= 0xF0) vm_write8(uri_out_ea + max_len - 1, 0);
    ctx->gpr[3] = CELL_OK;
}

static void tj_cellSailGraphicsAdapterGetFrame(ppu_context* ctx)
{
    const uint32_t adapter_ea = (uint32_t)ctx->gpr[3];
    const uint32_t info_ea    = (uint32_t)ctx->gpr[4];
    if (!adapter_ea || !info_ea) {
        ctx->gpr[3] = 0;
        return;
    }

    /* Return the currently presented image, retaining it between decoder
     * deadlines. The guest polls on display ticks and omits its movie draw
     * for NULL, exposing the other display buffer's old white fade. NULL is
     * appropriate before the first image and after EOS, not between frames. */
    for (uint32_t i = 0; i < 0x18; ++i) vm_write8(info_ea + i, 0);

    uint32_t frame_ea = 0;
    uint32_t frame_index = 0;
    uint32_t session_id = 0;
    uint32_t media_player_ea = 0;
    uint32_t last_frame = UINT32_MAX;
    uint64_t pts_90k = 0;
    bool source_eof = false;
    bool complete_eos = false;
    bool new_frame = false;
    {
        std::lock_guard<std::mutex> lock(g_tj_media_guest_mutex);
        media_player_ea =
            g_tj_media_player_ea.load(std::memory_order_acquire);
        if (media_player_ea && g_tj_media_frame_bytes) {
            const uint32_t buffer_index =
                g_tj_media_next_buffer % TJ_SAIL_GUEST_FRAME_COUNT;
            const uint32_t candidate_ea = g_tj_media_frame_ea[buffer_index];
            if (candidate_ea && tj_pamf_pop_argb(
                    vm_base + candidate_ea, g_tj_media_frame_bytes,
                    &pts_90k, &frame_index)) {
                frame_ea = candidate_ea;
                session_id = g_tj_media_session_id;
                g_tj_media_last_frame = frame_index;
                g_tj_media_last_pts_90k = pts_90k;
                ++g_tj_media_next_buffer;
                new_frame = true;
            }
            source_eof = tj_pamf_eof();
            last_frame = g_tj_media_last_frame;
            if (!frame_ea && source_eof && media_player_ea) {
                bool expected = false;
                if (g_tj_media_eos_sent.compare_exchange_strong(
                        expected, true, std::memory_order_acq_rel)) {
                    /* Claim and tear down this exact session while its player
                     * remains current. Callbacks stay outside this mutex. */
                    tj_cellSailReachMediaEosLocked();
                    complete_eos = true;
                }
            } else if (!frame_ea && last_frame != UINT32_MAX) {
                frame_ea = g_tj_media_frame_ea[
                    (g_tj_media_next_buffer - 1u) % TJ_SAIL_GUEST_FRAME_COUNT];
                session_id = g_tj_media_session_id;
                frame_index = last_frame;
                pts_90k = g_tj_media_last_pts_90k;
            }
        }
    }

    if (frame_ea) {
        vm_write32(info_ea + 0x00, frame_ea);
        vm_write32(info_ea + 0x04, session_id);
        vm_write32(info_ea + 0x08, frame_index);
        vm_write32(info_ea + 0x0C, 0); /* normal frame */
        vm_write64(info_ea + 0x10, pts_90k);
        if (new_frame && frame_index == 0) {
            fprintf(stderr,
                    "[cellSail] GraphicsAdapterGetFrame first ARGB frame=0x%08X session=%u pts=%llu\n",
                    frame_ea, session_id, (unsigned long long)pts_90k);
            fflush(stderr);
        }
        ctx->gpr[3] = frame_ea;
        return;
    }

    if (complete_eos) {
        fprintf(stderr,
                "[cellSail] PAMF source EOS after decoded frame %u\n",
                last_frame);
        fflush(stderr);
        tj_cellSailNotify(media_player_ea, TJ_SAIL_EVENT_SOURCE_EOS,
                          0, 0, 0);
        g_tj_deferred_stop_player_ea.store(media_player_ea,
                                           std::memory_order_release);
        tj_cellSailTryCompleteStoppedPlaybackAfterLoad();
    }
    ctx->gpr[3] = 0;
}

/* Complete the five Tokyo Jungle imports that were still reaching
 * ps3_hle_call's unresolved-NID fallback in existing runtime artifacts.
 * Keep them context-aware: every pointer below is a guest EA, not a native
 * Windows pointer. */
static void tj_cellSailSoundAdapterGetFrame(ppu_context* ctx)
{
    const uint32_t adapter_ea = (uint32_t)ctx->gpr[3];
    const uint32_t samples    = (uint32_t)ctx->gpr[4];
    const uint32_t info_ea    = (uint32_t)ctx->gpr[5];
    if (!adapter_ea || !info_ea || !samples ||
        samples > TJ_SAIL_AUDIO_SAMPLE_COUNT) {
        ctx->gpr[3] = (uint64_t)(int64_t)(int32_t)CELL_SAIL_ERROR_INVALID_ARGUMENT;
        return;
    }
    if (!vm_read8(adapter_ea + 0) || !vm_read8(adapter_ea + 1)) {
        ctx->gpr[3] = (uint64_t)(int64_t)(int32_t)CELL_SAIL_ERROR_INVALID_STATE;
        return;
    }

    /* CellSailSoundFrameInfo: pBuffer, sessionId, tag, status, pts. */
    for (uint32_t i = 0; i < 0x18; ++i) vm_write8(info_ea + i, 0);

    uint32_t buffer_ea = 0;
    uint32_t session_id = 0;
    uint32_t tag = 0;
    uint64_t pts_90k = 0;
    {
        std::lock_guard<std::mutex> lock(g_tj_media_guest_mutex);
        const uint32_t player_ea =
            g_tj_media_player_ea.load(std::memory_order_acquire);
        if (player_ea && vm_read32(player_ea + 0x58) == adapter_ea &&
            g_tj_media_audio_frame_bytes) {
            const uint32_t buffer_index =
                g_tj_media_audio_next_buffer % TJ_SAIL_GUEST_FRAME_COUNT;
            const uint32_t candidate_ea =
                g_tj_media_audio_frame_ea[buffer_index];
            if (candidate_ea && tj_pamf_pop_audio_f32be(
                    vm_base + candidate_ea, g_tj_media_audio_frame_bytes,
                    samples, &pts_90k, &tag)) {
                buffer_ea = candidate_ea;
                session_id = g_tj_media_session_id;
                ++g_tj_media_audio_next_buffer;
            }
        }
    }

    if (buffer_ea) {
        vm_write32(info_ea + 0x00, buffer_ea);
        vm_write32(info_ea + 0x04, session_id);
        vm_write32(info_ea + 0x08, tag);
        vm_write32(info_ea + 0x0C, 0); /* normal frame */
        vm_write64(info_ea + 0x10, pts_90k);
        if (tag == 0) {
            fprintf(stderr, "[cellSail] SoundAdapterGetFrame first audio buffer=0x%08X session=%u pts=%llu\n",
                    buffer_ea, session_id, (unsigned long long)pts_90k);
            fflush(stderr);
        }
        ctx->gpr[3] = TJ_SAIL_AUDIO_SAMPLE_COUNT;
        return;
    }

    /* No audio stream, decoder underrun, or not-yet-due block. The title
     * explicitly retries until a complete 256-sample block is returned. */
    ctx->gpr[3] = 0;
}

static void tj_cellSailSoundAdapterUpdateAvSync(ppu_context* ctx)
{
    const uint32_t adapter_ea = (uint32_t)ctx->gpr[3];
    ctx->gpr[3] = (adapter_ea && vm_read8(adapter_ea + 0))
        ? CELL_OK
        : (uint64_t)(int64_t)(int32_t)CELL_SAIL_ERROR_INVALID_STATE;
}

static void tj_cellSailGraphicsAdapterSetPreferredFormat(ppu_context* ctx)
{
    const uint32_t adapter_ea = (uint32_t)ctx->gpr[3];
    const uint32_t format_ea  = (uint32_t)ctx->gpr[4];
    if (!adapter_ea || !format_ea) {
        ctx->gpr[3] = (uint64_t)(int64_t)(int32_t)CELL_SAIL_ERROR_INVALID_ARGUMENT;
        return;
    }
    if (!vm_read8(adapter_ea + 0)) {
        ctx->gpr[3] = (uint64_t)(int64_t)(int32_t)CELL_SAIL_ERROR_INVALID_STATE;
        return;
    }

    /* Public CellSailGraphicsAdapter layout: callbacks + arg end at 0x1C,
     * followed by the exact 0x20-byte CellSailVideoFormat and index at 0x3C. */
    for (uint32_t off = 0; off < 0x20; off += 4)
        vm_write32(adapter_ea + 0x1C + off, vm_read32(format_ea + off));
    ctx->gpr[3] = CELL_OK;
}

static void tj_cellSpursTasksetAttributeSetTasksetSize(ppu_context* ctx)
{
    constexpr uint32_t TJ_SPURS_TASK_ERROR_INVAL        = 0x80410902u;
    constexpr uint32_t TJ_SPURS_TASK_ERROR_ALIGN        = 0x80410910u;
    constexpr uint32_t TJ_SPURS_TASK_ERROR_NULL_POINTER = 0x80410911u;
    constexpr uint32_t CELL_SPURS_TASKSET_SIZE             = 0x1900u;
    constexpr uint32_t CELL_SPURS_TASKSET2_SIZE            = 0x2900u;

    const uint32_t attr_ea = (uint32_t)ctx->gpr[3];
    const uint32_t size    = (uint32_t)ctx->gpr[4];
    if (!attr_ea) {
        ctx->gpr[3] = (uint64_t)(int64_t)(int32_t)TJ_SPURS_TASK_ERROR_NULL_POINTER;
        return;
    }
    if (attr_ea & 7u) {
        ctx->gpr[3] = (uint64_t)(int64_t)(int32_t)TJ_SPURS_TASK_ERROR_ALIGN;
        return;
    }
    if (size != CELL_SPURS_TASKSET_SIZE && size != CELL_SPURS_TASKSET2_SIZE) {
        ctx->gpr[3] = (uint64_t)(int64_t)(int32_t)TJ_SPURS_TASK_ERROR_INVAL;
        return;
    }

    /* CellSpursTasksetAttribute::taskset_size is the BE u32 at +0x20. */
    vm_write32(attr_ea + 0x20, size);
    ctx->gpr[3] = CELL_OK;
}

static void tj_cellSpursTaskGetContextSaveAreaSize(ppu_context* ctx)
{
    const uint32_t task_pattern_ea   = (uint32_t)ctx->gpr[3];
    const uint32_t common_pattern_ea = (uint32_t)ctx->gpr[4];
    if (!task_pattern_ea || !common_pattern_ea) {
        constexpr uint32_t TJ_SPURS_TASK_ERROR_NULL_POINTER = 0x80410911u;
        ctx->gpr[3] = (uint64_t)(int64_t)(int32_t)TJ_SPURS_TASK_ERROR_NULL_POINTER;
        return;
    }

    /* Tokyo Jungle checks this import as a status-returning setup step.  It
     * does not consume a computed context-byte count here; returning one
     * aborts its Survival worker creation path after BarrierInitialize. */
    ctx->gpr[3] = CELL_OK;
}

static void tj_cellSpursJobChainAttributeInitialize(ppu_context* ctx)
{
    /* PPC64 ELFv1 reserves eight-byte parameter slots. Arguments 1-8 are in
     * r3-r10; arguments 9-14 begin at caller SP+0x70. Tokyo Jungle writes all
     * six stack arguments at every call site, including nonzero auto-request,
     * tag, descriptor-size, and initial-SPU values. */
    const uint32_t sp = (uint32_t)ctx->gpr[1];
    const uint32_t auto_request = (uint32_t)vm_read64(sp + 0x70u);
    const uint32_t tag1         = (uint32_t)vm_read64(sp + 0x78u);
    const uint32_t tag2         = (uint32_t)vm_read64(sp + 0x80u);
    const uint32_t fixed_mem    = (uint32_t)vm_read64(sp + 0x88u);
    const uint32_t max_size     = (uint32_t)vm_read64(sp + 0x90u);
    const uint32_t initial_spus = (uint32_t)vm_read64(sp + 0x98u);

    const int32_t rc = cellSpursJobChainAttributeInitializeFull(
        (uint32_t)ctx->gpr[3], (uint32_t)ctx->gpr[4], ctx->gpr[5],
        ctx->gpr[6], (uint32_t)ctx->gpr[7], (uint32_t)ctx->gpr[8],
        ctx->gpr[9], (uint32_t)ctx->gpr[10], auto_request, tag1, tag2,
        fixed_mem, max_size, initial_spus);
    ctx->gpr[3] = (uint64_t)(int64_t)rc;
}

static void tj_cellFontInitializeWithRevision(ppu_context* ctx)
{
    ctx->gpr[3] = (uint64_t)(int64_t)cellFontInit(nullptr);
}

static void tj_cellFontInitLibraryFreeTypeWithRevision(ppu_context* ctx)
{
    uint32_t library_out = (uint32_t)ctx->gpr[5];
    int32_t rc = cellFontFTInit(nullptr, nullptr);
    if (library_out) vm_write32(library_out, TJ_FONT_LIBRARY_EA);
    ctx->gpr[3] = (uint64_t)(int64_t)rc;
}

static void tj_cellFontOpenFontset(ppu_context* ctx)
{
    uint32_t type_ea = (uint32_t)ctx->gpr[4];
    uint32_t font_ea = (uint32_t)ctx->gpr[5];
    CellFontType type = {};
    CellFont font = {};
    if (type_ea) {
        type.type = vm_read32(type_ea + 0);
        type.map  = vm_read32(type_ea + 4);
    }
    int32_t rc = cellFontOpenFontset((CellFontLibrary)ctx->gpr[3],
                                     type_ea ? &type : nullptr,
                                     font_ea ? &font : nullptr);
    if (rc == 0 && font_ea) {
        vm_write32(font_ea + 0, font.handle);
        vm_write32(font_ea + 4, font.type);
        uint32_t bits;
        memcpy(&bits, &font.scale_x, sizeof bits); vm_write32(font_ea + 8, bits);
        memcpy(&bits, &font.scale_y, sizeof bits); vm_write32(font_ea + 12, bits);
    }
    ctx->gpr[3] = (uint64_t)(int64_t)rc;
}

static void tj_cellFontCreateRenderer(ppu_context* ctx)
{
    /* Firmware ABI: (library, rendererConfig, rendererOut). */
    uint32_t renderer_ea = (uint32_t)ctx->gpr[5];
    const uint64_t limit = ppu_vm_size ? ppu_vm_size : (UINT64_C(1) << 32);
    if (!renderer_ea || (uint64_t)renderer_ea + 4 > limit) {
        ctx->gpr[3] = (uint64_t)(int64_t)(int32_t)CELL_FONT_ERROR_INVALID_PARAMETER;
        return;
    }
    CellFontRenderer renderer = 0;
    int32_t rc = cellFontCreateRenderer((CellFontLibrary)ctx->gpr[3],
                                        renderer_ea ? &renderer : nullptr);
    if (rc == 0 && renderer_ea) vm_write32(renderer_ea, renderer);
    ctx->gpr[3] = (uint64_t)(int64_t)rc;
}

static void tj_cellFontDestroyRenderer(ppu_context* ctx)
{
    /* Firmware passes the same guest object populated by CreateRenderer,
     * while the native font library expects the handle stored inside it. */
    const uint32_t renderer_ea = (uint32_t)ctx->gpr[3];
    const uint64_t limit = ppu_vm_size ? ppu_vm_size : (UINT64_C(1) << 32);
    if (!renderer_ea || (uint64_t)renderer_ea + 4 > limit) {
        ctx->gpr[3] = (uint64_t)(int64_t)(int32_t)CELL_FONT_ERROR_INVALID_PARAMETER;
        return;
    }
    const int32_t rc = cellFontDestroyRenderer(vm_read32(renderer_ea));
    if (rc == CELL_OK) vm_write32(renderer_ea, UINT32_MAX);
    ctx->gpr[3] = (uint64_t)(int64_t)rc;
}

static void tj_cellFontOpenFontInstance(ppu_context* ctx)
{
    std::lock_guard<std::mutex> lock(g_tj_font_glyph_mutex);
    uint32_t source_ea = (uint32_t)ctx->gpr[3];
    uint32_t output_ea = (uint32_t)ctx->gpr[4];
    CellFont source = {}, instance = {};
    if (!tj_read_cell_font(source_ea, &source) || !output_ea) {
        ctx->gpr[3] = CELL_FONT_ERROR_INVALID_PARAMETER;
        return;
    }
    const int32_t rc = cellFontOpenFontInstance(&source, &instance);
    if (rc == CELL_OK) tj_write_cell_font(output_ea, &instance);
    ctx->gpr[3] = (uint64_t)(int64_t)rc;
}

static void tj_cellFontCloseFont(ppu_context* ctx)
{
    std::lock_guard<std::mutex> lock(g_tj_font_glyph_mutex);
    CellFont font = {};
    if (!tj_read_cell_font((uint32_t)ctx->gpr[3], &font)) {
        ctx->gpr[3] = 0x80540002u;
        return;
    }
    const int32_t rc = cellFontCloseFont(&font);
    if (rc == CELL_OK) tj_release_font_glyph(font.handle);
    ctx->gpr[3] = (uint64_t)(int64_t)rc;
}

static void tj_cellFontEnd(ppu_context* ctx)
{
    std::lock_guard<std::mutex> lock(g_tj_font_glyph_mutex);
    const int32_t rc = cellFontEnd();
    if (rc == CELL_OK)
        for (uint32_t i = 0; i < g_tj_font_glyph_cache.size(); ++i)
            tj_release_font_glyph(i);
    ctx->gpr[3] = (uint64_t)(int64_t)rc;
}

static void tj_cellFontBindRenderer(ppu_context* ctx)
{
    uint32_t font_ea = (uint32_t)ctx->gpr[3];
    uint32_t renderer_ea = (uint32_t)ctx->gpr[4];
    const uint64_t limit = ppu_vm_size ? ppu_vm_size : (UINT64_C(1) << 32);
    if (!font_ea || !renderer_ea || (uint64_t)font_ea + 16 > limit ||
        (uint64_t)renderer_ea + 4 > limit) {
        ctx->gpr[3] = (uint64_t)(int64_t)(int32_t)CELL_FONT_ERROR_INVALID_PARAMETER;
        return;
    }
    CellFont font = {};
    tj_read_cell_font(font_ea, &font);
    int32_t rc = cellFontBindRenderer(&font, vm_read32(renderer_ea));
    ctx->gpr[3] = (uint64_t)(int64_t)rc;
}

static void tj_cellFontSetScalePixel(ppu_context* ctx)
{
    uint32_t font_ea = (uint32_t)ctx->gpr[3];
    CellFont font = {};
    if (!tj_read_cell_font(font_ea, &font)) {
        ctx->gpr[3] = 0x80540002u;
        return;
    }
    int32_t rc = cellFontSetScalePixel(&font,
                                       (float)ctx->fpr[1],
                                       (float)ctx->fpr[2]);
    if (rc == 0) tj_write_cell_font(font_ea, &font);
    ctx->gpr[3] = (uint64_t)(int64_t)rc;
}

static void tj_cellFontSetResolutionDpi(ppu_context* ctx)
{
    CellFont font = {};
    if (!tj_read_cell_font((uint32_t)ctx->gpr[3], &font)) {
        ctx->gpr[3] = 0x80540002u;
        return;
    }
    ctx->gpr[3] = (uint64_t)(int64_t)cellFontSetResolutionDpi(
        &font, (uint32_t)ctx->gpr[4], (uint32_t)ctx->gpr[5]);
}

static void tj_cellFontSetScalePoint(ppu_context* ctx)
{
    uint32_t font_ea = (uint32_t)ctx->gpr[3];
    CellFont font = {};
    if (!tj_read_cell_font(font_ea, &font)) {
        ctx->gpr[3] = 0x80540002u;
        return;
    }
    int32_t rc = cellFontSetScalePoint(&font,
                                       (float)ctx->fpr[1],
                                       (float)ctx->fpr[2]);
    if (rc == CELL_OK) tj_write_cell_font(font_ea, &font);
    ctx->gpr[3] = (uint64_t)(int64_t)rc;
}

static void tj_cellFontSetEffectWeight(ppu_context* ctx)
{
    CellFont font = {};
    if (!tj_read_cell_font((uint32_t)ctx->gpr[3], &font)) {
        ctx->gpr[3] = 0x80540002u;
        return;
    }
    ctx->gpr[3] = (uint64_t)(int64_t)cellFontSetEffectWeight(&font,
                                                             (float)ctx->fpr[1]);
}

static void tj_cellFontSetEffectSlant(ppu_context* ctx)
{
    CellFont font = {};
    if (!tj_read_cell_font((uint32_t)ctx->gpr[3], &font)) {
        ctx->gpr[3] = 0x80540002u;
        return;
    }
    ctx->gpr[3] = (uint64_t)(int64_t)cellFontSetEffectSlant(&font,
                                                            (float)ctx->fpr[1]);
}

static void tj_cellFontGetHorizontalLayout(ppu_context* ctx)
{
    uint32_t layout_ea = (uint32_t)ctx->gpr[4];
    CellFont font = {};
    CellFontHorizontalLayout layout = {};
    if (!tj_read_cell_font((uint32_t)ctx->gpr[3], &font) || !layout_ea) {
        ctx->gpr[3] = 0x80540002u;
        return;
    }
    int32_t rc = cellFontGetHorizontalLayout(&font, &layout);
    if (rc == 0) {
        tj_write_guest_float(layout_ea + 0, layout.baseLineY);
        tj_write_guest_float(layout_ea + 4, layout.lineHeight);
        tj_write_guest_float(layout_ea + 8, layout.effectHeight);
    }
    ctx->gpr[3] = (uint64_t)(int64_t)rc;
}

static void tj_cellFontUnbindRenderer(ppu_context* ctx)
{
    CellFont font = {};
    if (!tj_read_cell_font((uint32_t)ctx->gpr[3], &font)) {
        ctx->gpr[3] = 0x80540002u;
        return;
    }
    ctx->gpr[3] = (uint64_t)(int64_t)cellFontUnbindRenderer(&font);
}

static void tj_cellFontSetupRenderScalePixel(ppu_context* ctx)
{
    uint32_t font_ea = (uint32_t)ctx->gpr[3];
    CellFont font = {};
    if (!tj_read_cell_font(font_ea, &font)) {
        ctx->gpr[3] = 0x80540002u;
        return;
    }
    int32_t rc = cellFontSetupRenderScalePixel(&font,
                                                (float)ctx->fpr[1],
                                                (float)ctx->fpr[2]);
    if (rc == 0) tj_write_cell_font(font_ea, &font);
    ctx->gpr[3] = (uint64_t)(int64_t)rc;
}

static void tj_cellFontGetVerticalLayout(ppu_context* ctx)
{
    /* CellFontVerticalLayout is the vertical counterpart of the same
     * three-float layout record. The current font backend has no separate
     * vertical API, so preserve the ABI and provide matching scale metrics. */
    uint32_t layout_ea = (uint32_t)ctx->gpr[4];
    CellFont font = {};
    CellFontHorizontalLayout layout = {};
    if (!tj_read_cell_font((uint32_t)ctx->gpr[3], &font) || !layout_ea) {
        ctx->gpr[3] = 0x80540002u;
        return;
    }
    int32_t rc = cellFontGetHorizontalLayout(&font, &layout);
    if (rc == 0) {
        tj_write_guest_float(layout_ea + 0, layout.baseLineY);
        tj_write_guest_float(layout_ea + 4, layout.lineHeight);
        tj_write_guest_float(layout_ea + 8, layout.effectHeight);
    }
    ctx->gpr[3] = (uint64_t)(int64_t)rc;
}

static void tj_cellFontSetupRenderEffectWeight(ppu_context* ctx)
{
    tj_cellFontSetEffectWeight(ctx);
}

static void tj_cellFontSetupRenderEffectSlant(ppu_context* ctx)
{
    tj_cellFontSetEffectSlant(ctx);
}

static void tj_cellFontRenderSurfaceInit(ppu_context* ctx)
{
    uint32_t surface_ea = (uint32_t)ctx->gpr[3];
    if (!surface_ea) return;
    uint32_t width = (int32_t)ctx->gpr[7] > 0 ? (uint32_t)ctx->gpr[7] : 0;
    uint32_t height = (int32_t)ctx->gpr[8] > 0 ? (uint32_t)ctx->gpr[8] : 0;
    vm_write32(surface_ea + 0, (uint32_t)ctx->gpr[4]);
    vm_write32(surface_ea + 4, (uint32_t)ctx->gpr[5]);
    vm_write32(surface_ea + 8, (uint32_t)ctx->gpr[6]);
    vm_write32(surface_ea + 12, width);
    vm_write32(surface_ea + 16, height);
    vm_write32(surface_ea + 20, 0);
    vm_write32(surface_ea + 24, 0);
    vm_write32(surface_ea + 28, width);
    vm_write32(surface_ea + 32, height);
}

static void tj_cellFontRenderSurfaceSetScissor(ppu_context* ctx)
{
    uint32_t surface_ea = (uint32_t)ctx->gpr[3];
    if (!surface_ea) return;
    int32_t x0 = (int32_t)ctx->gpr[4];
    int32_t y0 = (int32_t)ctx->gpr[5];
    uint32_t width = (uint32_t)ctx->gpr[6];
    uint32_t height = (uint32_t)ctx->gpr[7];
    uint32_t surface_width = vm_read32(surface_ea + 12);
    uint32_t surface_height = vm_read32(surface_ea + 16);
    uint32_t sx0 = x0 > 0 ? (uint32_t)x0 : 0;
    uint32_t sy0 = y0 > 0 ? (uint32_t)y0 : 0;
    uint64_t requested_x1 = (uint64_t)sx0 + width;
    uint64_t requested_y1 = (uint64_t)sy0 + height;
    uint32_t sx1 = (uint32_t)(requested_x1 < surface_width ? requested_x1 : surface_width);
    uint32_t sy1 = (uint32_t)(requested_y1 < surface_height ? requested_y1 : surface_height);
    if (sx0 > surface_width) sx0 = surface_width;
    if (sy0 > surface_height) sy0 = surface_height;
    vm_write32(surface_ea + 20, sx0);
    vm_write32(surface_ea + 24, sy0);
    vm_write32(surface_ea + 28, sx1);
    vm_write32(surface_ea + 32, sy1);
}

static void tj_cellFontGetRenderCharGlyphMetrics(ppu_context* ctx)
{
    CellFont font = {};
    uint32_t metrics_ea = (uint32_t)ctx->gpr[5];
    CellFontGlyphMetrics metrics = {};
    if (!tj_read_cell_font((uint32_t)ctx->gpr[3], &font) || !metrics_ea) {
        ctx->gpr[3] = 0x80540002u;
        return;
    }
    int32_t rc = cellFontGetRenderCharGlyphMetrics(&font,
                                                    (uint32_t)ctx->gpr[4],
                                                    &metrics);
    if (rc == 0) tj_write_font_metrics(metrics_ea, &metrics);
    ctx->gpr[3] = (uint64_t)(int64_t)rc;
}

static void tj_cellFontGetCharGlyphMetrics(ppu_context* ctx)
{
    CellFont font = {};
    const uint32_t metrics_ea = (uint32_t)ctx->gpr[5];
    CellFontGlyphMetrics metrics = {};
    if (!tj_read_cell_font((uint32_t)ctx->gpr[3], &font) || !metrics_ea) {
        ctx->gpr[3] = 0x80540002u;
        return;
    }
    const int32_t rc = cellFontGetCharGlyphMetrics(
        &font, (uint32_t)ctx->gpr[4], &metrics);
    if (rc == CELL_OK) tj_write_font_metrics(metrics_ea, &metrics);
    ctx->gpr[3] = (uint64_t)(int64_t)rc;
}

static void tj_cellFontGetFontIdCode(ppu_context* ctx)
{
    CellFont font = {};
    const uint32_t font_ea = (uint32_t)ctx->gpr[3];
    const uint32_t font_id_ea = (uint32_t)ctx->gpr[5];
    const uint32_t font_code_ea = (uint32_t)ctx->gpr[6];
    const uint64_t limit = ppu_vm_size ? ppu_vm_size : (UINT64_C(1) << 32);
    /* The title queries fontId with a null fontCode output when measuring text. */
    if (!font_ea || (uint64_t)font_ea + 16 > limit ||
        !font_id_ea || (uint64_t)font_id_ea + 4 > limit ||
        (font_code_ea && (uint64_t)font_code_ea + 4 > limit) ||
        !tj_read_cell_font(font_ea, &font)) {
        ctx->gpr[3] = 0x80540002u;
        return;
    }
    uint32_t font_id = 0;
    uint32_t font_code = 0;
    const int32_t rc = cellFontGetFontIdCode(
        &font, (uint32_t)ctx->gpr[4], &font_id, &font_code);
    if (rc == CELL_OK) {
        vm_write32(font_id_ea, font_id);
        if (font_code_ea) vm_write32(font_code_ea, font_code);
    }
    ctx->gpr[3] = (uint64_t)(int64_t)rc;
}

static void tj_cellFontGetRenderCharGlyphMetricsVertical(ppu_context* ctx)
{
    CellFont font = {};
    uint32_t metrics_ea = (uint32_t)ctx->gpr[5];
    CellFontGlyphMetrics metrics = {};
    if (!tj_read_cell_font((uint32_t)ctx->gpr[3], &font) || !metrics_ea) {
        ctx->gpr[3] = 0x80540002u;
        return;
    }
    int32_t rc = cellFontGetRenderCharGlyphMetricsVertical(&font,
                                                            (uint32_t)ctx->gpr[4],
                                                            &metrics);
    if (rc == 0) tj_write_font_metrics(metrics_ea, &metrics);
    ctx->gpr[3] = (uint64_t)(int64_t)rc;
}

static void tj_cellFontRenderCharGlyphImage(ppu_context* ctx)
{
    std::lock_guard<std::mutex> lock(g_tj_font_glyph_mutex);
    uint32_t font_ea = (uint32_t)ctx->gpr[3];
    uint32_t surface_ea = (uint32_t)ctx->gpr[5];
    uint32_t metrics_ea = (uint32_t)ctx->gpr[8];
    uint32_t image_ea = (uint32_t)ctx->gpr[9];
    CellFont font = {};
    if (!tj_read_cell_font(font_ea, &font) || !surface_ea) {
        ctx->gpr[3] = 0x80540002u;
        return;
    }

    uint32_t buffer_ea = vm_read32(surface_ea + 0);
    CellFontRenderSurface surface = {};
    surface.buffer = buffer_ea ? vm_base + buffer_ea : nullptr;
    surface.widthByte = (int32_t)vm_read32(surface_ea + 4);
    surface.pixelSizeByte = (int32_t)vm_read32(surface_ea + 8);
    surface.width = (int32_t)vm_read32(surface_ea + 12);
    surface.height = (int32_t)vm_read32(surface_ea + 16);
    CellFontGlyphMetrics metrics = {};
    CellFontGlyphImage image = {};
    int32_t rc = cellFontRenderCharGlyphImage(&font,
                                               (uint32_t)ctx->gpr[4],
                                               &surface,
                                               (float)ctx->fpr[1],
                                               (float)ctx->fpr[2],
                                               metrics_ea ? &metrics : nullptr,
                                               image_ea ? &image : nullptr);
    if (rc == 0 && metrics_ea) tj_write_font_metrics(metrics_ea, &metrics);
    if (rc == 0 && image_ea) {
        /* The title blends this alpha image into its packed-color atlas.
         * Return guest-owned bytes, not a host pointer or a null placeholder.
         * Both pointers describe the same clipped rectangle. */
        for (uint32_t off = 0; off < 24; off += 4) vm_write32(image_ea + off, 0);
        if (image.buffer && image.width > 0 && image.height > 0 &&
            image.widthByte >= image.width && surface.widthByte > 0 &&
            surface.pixelSizeByte > 0 && font.handle < g_tj_font_glyph_cache.size()) {
            int64_t left = image.x, top = image.y;
            int64_t right = left + image.width, bottom = top + image.height;
            const int64_t sc_left = vm_read32(surface_ea + 20);
            const int64_t sc_top = vm_read32(surface_ea + 24);
            const int64_t sc_right = vm_read32(surface_ea + 28);
            const int64_t sc_bottom = vm_read32(surface_ea + 32);
            if (left < 0) left = 0;
            if (top < 0) top = 0;
            if (left < sc_left) left = sc_left;
            if (top < sc_top) top = sc_top;
            if (right > surface.width) right = surface.width;
            if (bottom > surface.height) bottom = surface.height;
            if (right > sc_right) right = sc_right;
            if (bottom > sc_bottom) bottom = sc_bottom;
            const uint64_t bytes = (uint64_t)image.widthByte * image.height;
            if (right > left && bottom > top && bytes <= UINT32_MAX) {
                auto& cache = g_tj_font_glyph_cache[font.handle];
                if (cache.capacity < bytes) {
                    const uint32_t replacement = hle_malloc((uint32_t)bytes);
                    if (!replacement) {
                        ctx->gpr[3] = CELL_FONT_ERROR_ALLOCATION_FAILED;
                        return;
                    }
                    hle_free(cache.ea);
                    cache = {replacement, (uint32_t)bytes};
                }
                memcpy(vm_base + cache.ea, image.buffer, (size_t)bytes);
                const uint64_t destination = (uint64_t)buffer_ea +
                    top * surface.widthByte + left * surface.pixelSizeByte;
                const uint64_t end = destination + (bottom - top - 1) *
                    surface.widthByte + (right - left) * surface.pixelSizeByte;
                if (end > (UINT64_C(1) << 32)) {
                    ctx->gpr[3] = CELL_FONT_ERROR_INVALID_PARAMETER;
                    return;
                }
                vm_write32(image_ea, cache.ea + (uint32_t)((top - image.y) *
                    image.widthByte + left - image.x));
                vm_write32(image_ea + 4, (uint32_t)image.widthByte);
                vm_write32(image_ea + 8, (uint32_t)(right - left));
                vm_write32(image_ea + 12, (uint32_t)(bottom - top));
                vm_write32(image_ea + 16, (uint32_t)destination);
                vm_write32(image_ea + 20, (uint32_t)surface.widthByte);
            }
        }
    }
    ctx->gpr[3] = (uint64_t)(int64_t)rc;
}

static void tj_cellFontOpenFontsetOnMemory(ppu_context* ctx)
{
    /* Same guest ABI/layout as cellFontOpenFontset; the memory-backed library
     * selection changes firmware ownership, not the returned CellFont shape. */
    tj_cellFontOpenFontset(ctx);
}

static void tj_cellFontEndLibrary(ppu_context* ctx)
{
    ctx->gpr[3] = (uint32_t)ctx->gpr[3] ? CELL_OK
        : (uint64_t)(int64_t)(int32_t)CELL_FONT_ERROR_INVALID_PARAMETER;
}

static void tj_cellFontGetCharGlyphMetricsVertical(ppu_context* ctx)
{
    tj_cellFontGetRenderCharGlyphMetricsVertical(ctx);
}

static void tj_cellFontGenerateCharGlyph(ppu_context* ctx)
{
    const uint32_t font_ea      = (uint32_t)ctx->gpr[3];
    const uint32_t glyph_out_ea = (uint32_t)ctx->gpr[5];
    if (!font_ea || !glyph_out_ea) {
        ctx->gpr[3] = (uint64_t)(int64_t)(int32_t)CELL_FONT_ERROR_INVALID_PARAMETER;
        return;
    }
    const uint32_t glyph_ea = hle_malloc(0x80);
    if (!glyph_ea) {
        ctx->gpr[3] = (uint64_t)(int64_t)(int32_t)CELL_FONT_ERROR_ALLOCATION_FAILED;
        return;
    }
    for (uint32_t i = 0; i < 0x80; i += 4) vm_write32(glyph_ea + i, 0);
    vm_write32(glyph_ea, (uint32_t)ctx->gpr[4]); /* retain character code */
    vm_write32(glyph_out_ea, glyph_ea);
    ctx->gpr[3] = CELL_OK;
}

static void tj_cellFontDeleteGlyph(ppu_context* ctx)
{
    const uint32_t font_ea  = (uint32_t)ctx->gpr[3];
    const uint32_t glyph_ea = (uint32_t)ctx->gpr[4];
    if (!font_ea || !glyph_ea) {
        ctx->gpr[3] = (uint64_t)(int64_t)(int32_t)CELL_FONT_ERROR_INVALID_PARAMETER;
        return;
    }
    hle_free(glyph_ea);
    ctx->gpr[3] = CELL_OK;
}

static void tj_l10nSwapUnits(uint8_t* bytes, uint32_t size, uint32_t unit)
{
    for (uint32_t offset = 0; offset + unit <= size; offset += unit) {
        for (uint32_t left = 0, right = unit - 1; left < right; ++left, --right) {
            uint8_t tmp = bytes[offset + left];
            bytes[offset + left] = bytes[offset + right];
            bytes[offset + right] = tmp;
        }
    }
}

static uint32_t tj_l10nUnitSize(int32_t encoding)
{
    if (encoding == L10N_UTF16 || encoding == L10N_UCS2) return 2;
    if (encoding == L10N_UTF32) return 4;
    return 1;
}

static int32_t tj_l10nNativeEncoding(uint32_t encoding)
{
    if (encoding <= 3) return (int32_t)encoding;
    if (encoding == 4) return L10N_UTF32; /* SDK UCS-4 */
    if (encoding == 5) return L10N_ISO_8859_1;
    return -1;
}

static bool tj_l10nConverterSupported(uint32_t src, uint32_t dst)
{
    /* Advertise only pairs implemented by the native converter. The SDK's
     * code-page numbers diverge from this runtime after UCS-2. */
    return src <= 5 && dst <= 5;
}

static void tj_l10nGetConverter(ppu_context* ctx)
{
    const uint32_t src = (uint32_t)ctx->gpr[3];
    const uint32_t dst = (uint32_t)ctx->gpr[4];
    ctx->gpr[3] = tj_l10nConverterSupported(src, dst)
        ? (src << 16) | dst : UINT64_MAX;
}

static void tj_l10nConvertStr(ppu_context* ctx)
{
    /* Firmware ABI: (converter, source, sourceBytes, dest, destBytes).
     * The title passes the descriptor returned by l10n_get_converter in r3;
     * r8 is not an argument. Source length is input-only. */
    const uint32_t converter = (uint32_t)ctx->gpr[3];
    const uint32_t src_encoding = converter >> 16;
    const uint32_t dst_encoding = converter & 0xFFFFu;
    if (!tj_l10nConverterSupported(src_encoding, dst_encoding)) {
        ctx->gpr[3] = 3; /* ConverterUnknown */
        return;
    }
    const int32_t src_code = tj_l10nNativeEncoding(src_encoding);
    const int32_t dst_code = tj_l10nNativeEncoding(dst_encoding);
    const uint32_t src_ea = (uint32_t)ctx->gpr[4];
    const uint32_t src_len_ea = (uint32_t)ctx->gpr[5];
    const uint32_t dst_ea = (uint32_t)ctx->gpr[6];
    const uint32_t dst_len_ea = (uint32_t)ctx->gpr[7];
    const auto guest_range = [](uint32_t ea, uint32_t size) {
        const uint64_t limit = ppu_vm_size ? ppu_vm_size : (UINT64_C(1) << 32);
        return ea && (uint64_t)ea + size <= limit;
    };
    if (!guest_range(src_len_ea, 4) || !guest_range(dst_len_ea, 4)) {
        ctx->gpr[3] = (uint64_t)(int64_t)(int32_t)CELL_L10N_ERROR_INVALID_PARAM;
        return;
    }

    uint32_t src_len = vm_read32(src_len_ea);
    uint32_t dst_len = vm_read32(dst_len_ea);
    if (src_len > 16u * 1024u * 1024u ||
        (dst_ea && dst_len > 16u * 1024u * 1024u) ||
        !guest_range(src_ea, src_len) ||
        (dst_ea && !guest_range(dst_ea, dst_len))) {
        ctx->gpr[3] = (uint64_t)(int64_t)(int32_t)CELL_L10N_ERROR_INVALID_PARAM;
        return;
    }
    if (!dst_ea) dst_len = src_len * 4u; /* bounded length-only query */
    uint8_t* src = (uint8_t*)malloc(src_len ? src_len : 1u);
    uint8_t* dst = (uint8_t*)malloc(dst_len ? dst_len : 1u);
    if (!src || !dst) {
        free(src);
        free(dst);
        ctx->gpr[3] = (uint64_t)(int64_t)(int32_t)CELL_L10N_ERROR_INSUFFICIENT;
        return;
    }
    if (src_len) memcpy(src, vm_base + src_ea, src_len);
    tj_l10nSwapUnits(src, src_len, tj_l10nUnitSize(src_code));

    const int32_t rc = l10n_convert_str(
        src_code, src, &src_len, dst_code, dst, &dst_len);
    /* Native conversion reports only complete output characters, including
     * the valid prefix on source errors or destination exhaustion. */
    tj_l10nSwapUnits(dst, dst_len, tj_l10nUnitSize(dst_code));
    if (dst_ea && dst_len) memcpy(vm_base + dst_ea, dst, dst_len);
    vm_write32(dst_len_ea, dst_len);
    free(src);
    free(dst);
    ctx->gpr[3] = rc == CELL_OK ? 0u :
        rc == (int32_t)CELL_L10N_ERROR_INSUFFICIENT ? 2u :
        rc == (int32_t)CELL_L10N_ERROR_NOT_SUPPORTED ? 3u : 1u;
}

static void tj_cellRescGcmSurface2RescSrc(ppu_context* ctx)
{
    const uint32_t surface_ea = (uint32_t)ctx->gpr[3];
    const uint32_t src_ea     = (uint32_t)ctx->gpr[4];
    if (!surface_ea || !src_ea) {
        ctx->gpr[3] = (uint64_t)(int64_t)(int32_t)CELL_RESC_ERROR_BAD_ARGUMENT;
        return;
    }

    /* CellGcmSurface -> CellRescSrc, selecting color target zero. */
    vm_write32(src_ea + 0x00, vm_read8(surface_ea + 0x02));
    vm_write32(src_ea + 0x04, vm_read32(surface_ea + 0x18));
    vm_write16(src_ea + 0x08, vm_read16(surface_ea + 0x34));
    vm_write16(src_ea + 0x0A, vm_read16(surface_ea + 0x36));
    vm_write32(src_ea + 0x0C, vm_read32(surface_ea + 0x08));
    ctx->gpr[3] = CELL_OK;
}

static void tj_cellRescInit(ppu_context* ctx)
{
    const uint32_t config_ea = (uint32_t)ctx->gpr[3];
    if (!config_ea) {
        ctx->gpr[3] = (uint64_t)(int64_t)(int32_t)CELL_RESC_ERROR_BAD_ARGUMENT;
        return;
    }
    CellRescInitConfig config = {};
    config.size            = vm_read32(config_ea + 0x00);
    config.resourcePolicy  = vm_read32(config_ea + 0x04);
    config.supportModes    = vm_read32(config_ea + 0x08);
    config.ratioMode       = vm_read32(config_ea + 0x0C);
    config.palTemporalMode = vm_read32(config_ea + 0x10);
    config.interlaceMode   = vm_read32(config_ea + 0x14);
    config.flipMode        = vm_read32(config_ea + 0x18);
    ctx->gpr[3] = (uint64_t)(int64_t)cellRescInit(&config);
}

static void tj_cellRescGetBufferSize(ppu_context* ctx)
{
    const uint32_t color_ea = (uint32_t)ctx->gpr[3];
    const uint32_t vertex_ea = (uint32_t)ctx->gpr[4];
    const uint32_t fragment_ea = (uint32_t)ctx->gpr[5];
    uint32_t color = 0, vertex = 0, fragment = 0;
    const int32_t rc = cellRescGetBufferSize(
        color_ea ? &color : nullptr,
        vertex_ea ? &vertex : nullptr,
        fragment_ea ? &fragment : nullptr);
    if (rc == CELL_OK) {
        if (color_ea) vm_write32(color_ea, color);
        if (vertex_ea) vm_write32(vertex_ea, vertex);
        if (fragment_ea) vm_write32(fragment_ea, fragment);
    }
    ctx->gpr[3] = (uint64_t)(int64_t)rc;
}

static void tj_cellRescSetSrc(ppu_context* ctx)
{
    const uint32_t src_ea = (uint32_t)ctx->gpr[4];
    if (!src_ea) {
        ctx->gpr[3] = (uint64_t)(int64_t)(int32_t)CELL_RESC_ERROR_BAD_ARGUMENT;
        return;
    }
    CellRescSrc src = {};
    src.format = vm_read32(src_ea + 0x00);
    src.pitch  = vm_read32(src_ea + 0x04);
    src.width  = vm_read16(src_ea + 0x08);
    src.height = vm_read16(src_ea + 0x0A);
    src.offset = vm_read32(src_ea + 0x0C);
    ctx->gpr[3] = (uint64_t)(int64_t)cellRescSetSrc(
        (int32_t)ctx->gpr[3], &src);
}

static void tj_cellRescSetDsts(ppu_context* ctx)
{
    const uint32_t dsts_ea = (uint32_t)ctx->gpr[4];
    if (!dsts_ea) {
        ctx->gpr[3] = (uint64_t)(int64_t)(int32_t)CELL_RESC_ERROR_BAD_ARGUMENT;
        return;
    }
    CellRescDsts dsts = {};
    dsts.format      = vm_read32(dsts_ea + 0x00);
    dsts.pitch       = vm_read32(dsts_ea + 0x04);
    dsts.heightAlign = vm_read32(dsts_ea + 0x08);
    ctx->gpr[3] = (uint64_t)(int64_t)cellRescSetDsts(
        (uint32_t)ctx->gpr[3], &dsts);
}

static void tj_cellRescSetFlipHandler(ppu_context* ctx)
{
    const uint32_t handler_opd = (uint32_t)ctx->gpr[3];
    cellRescSetFlipHandler((void (*)(uint32_t))(uintptr_t)handler_opd);
    ctx->gpr[3] = CELL_OK;
}

static void tj_cellRescSetVBlankHandler(ppu_context* ctx)
{
    const uint32_t handler_opd = (uint32_t)ctx->gpr[3];
    cellRescSetVBlankHandler((void (*)(uint32_t))(uintptr_t)handler_opd);
    ctx->gpr[3] = CELL_OK;
}

static void tj_cellRescSetWaitFlip(ppu_context* ctx)
{
    ctx->gpr[3] = (uint64_t)(int64_t)cellRescSetWaitFlip(
        (void*)(uintptr_t)(uint32_t)ctx->gpr[3]);
}

static void tj_cellRescResetFlipStatus(ppu_context* ctx)
{
    cellRescResetFlipStatus();
    ctx->gpr[3] = CELL_OK;
}

static void tj_cellRescSetDisplayMode(ppu_context* ctx)
{
    ctx->gpr[3] = (uint64_t)(int64_t)cellRescSetDisplayMode(
        (uint32_t)ctx->gpr[3]);
}

static void tj_cellRescSetConvertAndFlip(ppu_context* ctx)
{
    ctx->gpr[3] = (uint64_t)(int64_t)cellRescSetConvertAndFlip(
        (void*)(uintptr_t)(uint32_t)ctx->gpr[3], (int32_t)ctx->gpr[4]);
}

static void tj_cellRescExit(ppu_context* ctx)
{
    cellRescExit();
    ctx->gpr[3] = CELL_OK;
}

static void tj_cellRescSetBufferAddress(ppu_context* ctx)
{
    ctx->gpr[3] = (uint64_t)(int64_t)cellRescSetBufferAddress(
        (void*)(uintptr_t)(uint32_t)ctx->gpr[3],
        (void*)(uintptr_t)(uint32_t)ctx->gpr[4],
        (void*)(uintptr_t)(uint32_t)ctx->gpr[5]);
}

static void tj_cellRescGetFlipStatus(ppu_context* ctx)
{
    ctx->gpr[3] = (uint64_t)(int64_t)cellRescGetFlipStatus();
}

static void tj_cellRescVideoOutResolutionId2RescBufferMode(ppu_context* ctx)
{
    const uint32_t resolution_id = (uint32_t)ctx->gpr[3];
    const uint32_t mode_out_ea   = (uint32_t)ctx->gpr[4];
    uint32_t mode = 0;
    switch (resolution_id) {
    case 1: mode = 0x01; break; /* 720x480 */
    case 2: mode = 0x02; break; /* 720x576 */
    case 4: mode = 0x04; break; /* 1280x720 */
    case 5: mode = 0x08; break; /* 1920x1080 */
    default: break;
    }
    if (!mode_out_ea || !mode) {
        ctx->gpr[3] = (uint64_t)(int64_t)(int32_t)CELL_RESC_ERROR_BAD_ARGUMENT;
        return;
    }
    vm_write32(mode_out_ea, mode);
    ctx->gpr[3] = CELL_OK;
}

static void tj_cellSailSoundAdapterFinalize(ppu_context* ctx)
{
    const uint32_t self_ea = (uint32_t)ctx->gpr[3];
    if (!self_ea || !vm_read8(self_ea)) {
        ctx->gpr[3] = (uint64_t)(int64_t)(int32_t)CELL_SAIL_ERROR_INVALID_STATE;
        return;
    }
    vm_write8(self_ea + 0, 0);
    vm_write8(self_ea + 1, 0);
    ctx->gpr[3] = CELL_OK;
}

static void tj_cellSailGraphicsAdapterFinalize(ppu_context* ctx)
{
    tj_cellSailSoundAdapterFinalize(ctx);
}

static void tj_cellSailPlayerFinalize(ppu_context* ctx)
{
    const uint32_t player_ea = (uint32_t)ctx->gpr[3];
    if (!player_ea) {
        ctx->gpr[3] = (uint64_t)(int64_t)(int32_t)CELL_SAIL_ERROR_INVALID_ARGUMENT;
        return;
    }
    uint32_t expected = player_ea;
    g_tj_deferred_stop_player_ea.compare_exchange_strong(
        expected, 0, std::memory_order_acq_rel);
    {
        std::lock_guard<std::mutex> lock(g_tj_media_guest_mutex);
        tj_cellSailStopMediaLocked();
        tj_pamf_close();
        g_tj_media_descriptor_ea.store(0, std::memory_order_release);
    }
    /* Player owns adapter registration.  Release both links before the
     * adapter objects are finalized or reused by a later movie session. */
    const uint32_t sound_adapter_ea = vm_read32(player_ea + 0x58);
    const uint32_t graphics_adapter_ea = vm_read32(player_ea + 0x5C);
    if (sound_adapter_ea) vm_write8(sound_adapter_ea + 1, 0);
    if (graphics_adapter_ea) vm_write8(graphics_adapter_ea + 1, 0);
    vm_write32(player_ea + 0x58, 0);
    vm_write32(player_ea + 0x5C, 0);
    vm_write8(player_ea + 84, 1);
    vm_write8(player_ea + 85, 0);
    ctx->gpr[3] = CELL_OK;
}

static void tj_cellSailSoundAdapterGetFormat(ppu_context* ctx)
{
    const uint32_t self_ea = (uint32_t)ctx->gpr[3];
    const uint32_t out_ea  = (uint32_t)ctx->gpr[4];
    if (!self_ea || !out_ea || !vm_read8(self_ea)) {
        ctx->gpr[3] = (uint64_t)(int64_t)(int32_t)CELL_SAIL_ERROR_INVALID_STATE;
        return;
    }
    for (uint32_t off = 0; off < 0x18; off += 4)
        vm_write32(out_ea + off, vm_read32(self_ea + 0x18 + off));
    ctx->gpr[3] = CELL_OK;
}

static void tj_cellSailGraphicsAdapterGetFormat(ppu_context* ctx)
{
    const uint32_t self_ea = (uint32_t)ctx->gpr[3];
    const uint32_t out_ea  = (uint32_t)ctx->gpr[4];
    if (!self_ea || !out_ea || !vm_read8(self_ea)) {
        ctx->gpr[3] = (uint64_t)(int64_t)(int32_t)CELL_SAIL_ERROR_INVALID_STATE;
        return;
    }
    for (uint32_t off = 0; off < 0x20; off += 4)
        vm_write32(out_ea + off, vm_read32(self_ea + 0x1C + off));
    ctx->gpr[3] = CELL_OK;
}

static void tj_cellSailSoundAdapterPtsToTimePosition(ppu_context* ctx)
{
    const uint32_t adapter_ea = (uint32_t)ctx->gpr[3];
    const uint64_t pts_90k = ctx->gpr[4];
    const uint32_t out_ea = (uint32_t)ctx->gpr[5];
    if (!adapter_ea || !out_ea) {
        ctx->gpr[3] = (uint64_t)(int64_t)(int32_t)CELL_SAIL_ERROR_INVALID_ARGUMENT;
        return;
    }
    if (!vm_read8(adapter_ea + 0)) {
        ctx->gpr[3] = (uint64_t)(int64_t)(int32_t)CELL_SAIL_ERROR_INVALID_STATE;
        return;
    }

    /* CellSailPts is 90 kHz; CellSailTimePosition is microseconds. Keep the
     * quotient/remainder form overflow-safe for the full u64 input range. */
    const uint64_t whole_ms = pts_90k / 90;
    const uint64_t remainder_us = (pts_90k % 90) * 1000 / 90;
    if (whole_ms > UINT64_MAX / 1000 ||
        whole_ms * 1000 > UINT64_MAX - remainder_us) {
        ctx->gpr[3] = (uint64_t)(int64_t)(int32_t)CELL_SAIL_ERROR_INVALID_ARGUMENT;
        return;
    }
    vm_write64(out_ea, whole_ms * 1000 + remainder_us);
    ctx->gpr[3] = CELL_OK;
}

static void tj_cellSailGraphicsAdapterUpdateAvSync(ppu_context* ctx)
{
    ctx->gpr[3] = ((uint32_t)ctx->gpr[3] && vm_read8((uint32_t)ctx->gpr[3]))
        ? CELL_OK
        : (uint64_t)(int64_t)(int32_t)CELL_SAIL_ERROR_INVALID_STATE;
}

static void tj_cellGameSetParamString(ppu_context* ctx)
{
    const int32_t id = (int32_t)ctx->gpr[3];
    ctx->gpr[3] = (uint64_t)(int64_t)cellGameSetParamString(
        id, (const char*)(uintptr_t)(uint32_t)ctx->gpr[4]);
}

static void tj_cellGameContentErrorDialog(ppu_context* ctx)
{
    /* Firmware only displays a system error overlay.  The recomp has no VSH;
     * validate its directory string and report the request as handled. */
    const uint32_t dir_ea = (uint32_t)ctx->gpr[5];
    ctx->gpr[3] = dir_ea ? CELL_OK
        : (uint64_t)(int64_t)(int32_t)CELL_GAME_ERROR_PARAM;
}

static void tj_cellKbRead(ppu_context* ctx)
{
    const uint32_t port    = (uint32_t)ctx->gpr[3];
    const uint32_t data_ea = (uint32_t)ctx->gpr[4];
    if (!data_ea) {
        ctx->gpr[3] = (uint64_t)(int64_t)(int32_t)CELL_KB_ERROR_INVALID_PARAMETER;
        return;
    }
    /* cellKbGetData already consumes a raw guest EA and serializes the SDK
     * structure through vm_write*.  The old wrapper passed it a host stack
     * object, so the HLE wrote through the truncated host address and this
     * wrapper then copied the still-zero local object back to the guest. */
    ctx->gpr[3] = (uint64_t)(int64_t)cellKbGetData(
        port, (CellKbData*)(uintptr_t)data_ea);
}

static void tj_cellMsgDialogOpenErrorCode(ppu_context* ctx)
{
    /* Use the existing deferred dialog queue. Reporting success while dropping
     * this callback leaves the title's error-dialog busy flag set forever. */
    printf("[cellMsgDialog] OpenErrorCode(error=0x%08X)\n", (u32)ctx->gpr[3]);
    const s32 rc = cellMsgDialogOpen2(CELL_MSGDIALOG_TYPE_BUTTON_TYPE_OK, nullptr,
        (CellMsgDialogCallback)(uintptr_t)(u32)ctx->gpr[4],
        (void*)(uintptr_t)(u32)ctx->gpr[5],
        (void*)(uintptr_t)(u32)ctx->gpr[6]);
    ctx->gpr[3] = (uint64_t)(int64_t)rc;
}

static void tj_cellMsgDialogClose(ppu_context* ctx)
{
    const s32 rc = cellMsgDialogClose((float)ctx->fpr[1]);
    ctx->gpr[3] = (uint64_t)(int64_t)rc;
}

static void tj_cellSslCertificateLoader(ppu_context* ctx)
{
    const s32 rc = cellSslCertificateLoader(ctx->gpr[3],
        (char*)(uintptr_t)(u32)ctx->gpr[4], (u32)ctx->gpr[5],
        (u32*)(uintptr_t)(u32)ctx->gpr[6]);
    ctx->gpr[3] = (uint64_t)(int64_t)rc;
}

static void tj_hleReturnOk(ppu_context* ctx)
{
    ctx->gpr[3] = CELL_OK;
}

/* sys_prx_register_library receives a guest descriptor, never a host pointer.
 * Keep the small accepted-library table host-owned: title PRXs may disappear
 * after registration, and unknown-but-well-formed libraries still succeed. */
struct tj_prx_library_entry {
    bool used;
    uint64_t owner;
    uint16_t version;
    uint16_t attributes;
    uint16_t function_count;
    uint16_t variable_count;
    uint16_t tls_variable_count;
    char name[128];
};

static std::array<tj_prx_library_entry, 64> g_tj_prx_libraries = {};
static std::mutex g_tj_prx_libraries_mutex;
static uint64_t g_tj_prx_next_owner = 1;

/* error_codes.h has no PRX-specific definitions in this runtime. */
static constexpr uint32_t kTjCellPrxErrorIllegalLibrary = 0x8001113Du;
static constexpr uint32_t kTjCellPrxErrorInval = 0x80011324u;

static void tj_prx_set_status(ppu_context* ctx, uint32_t status)
{
    ctx->gpr[3] = (uint64_t)(int64_t)(int32_t)status;
}

static bool tj_prx_guest_range_mapped(uint32_t ea, uint32_t size)
{
    if (!VM_READY || !ea || !size)
        return false;

    const uint64_t end = (uint64_t)ea + size;
    if (end > (UINT64_C(1) << 32) || (ppu_vm_size && end > ppu_vm_size))
        return false;

#ifdef _WIN32
    uintptr_t host = (uintptr_t)vm_base + ea;
    const uintptr_t host_end = (uintptr_t)vm_base + (uintptr_t)end;
    while (host < host_end) {
        MEMORY_BASIC_INFORMATION mbi = {};
        if (!VirtualQuery((const void*)host, &mbi, sizeof(mbi)) ||
            mbi.State != MEM_COMMIT ||
            (mbi.Protect & (PAGE_NOACCESS | PAGE_GUARD)) ||
            !(mbi.Protect & (PAGE_READONLY | PAGE_READWRITE | PAGE_WRITECOPY |
                             PAGE_EXECUTE_READ | PAGE_EXECUTE_READWRITE |
                             PAGE_EXECUTE_WRITECOPY)))
            return false;

        const uintptr_t region_end = (uintptr_t)mbi.BaseAddress + mbi.RegionSize;
        if (region_end <= host)
            return false;
        host = region_end < host_end ? region_end : host_end;
    }
#else
    for (uint64_t current = ea; current < end;
         current = (current & ~(uint64_t)(VM_PAGE_SIZE - 1)) + VM_PAGE_SIZE) {
        if (!vm_is_valid_addr((uint32_t)current))
            return false;
    }
#endif
    return true;
}

static void tj_sysPrxRegisterLibrary(ppu_context* ctx)
{
    constexpr uint32_t kMinimumRecordSize = 0x1Cu;
    constexpr uint32_t kFullRecordSize = 0x2Cu;
    constexpr uint32_t kMaxNameSize = 128u;
    constexpr uint32_t kMaxDynamicExports = 2048u;
    constexpr uint16_t kPrxExportLibraryFlag = 0x0001u;
    constexpr uint16_t kPrxManagementFlag = 0x8000u;

    const uint32_t descriptor_ea = (uint32_t)ctx->gpr[3];
    if (!tj_prx_guest_range_mapped(descriptor_ea, kMinimumRecordSize)) {
        tj_prx_set_status(ctx, CELL_EFAULT);
        return;
    }

    /* ppu_prx_module_info: the +0x14 NID and +0x18 address arrays contain
     * num_func + num_var exports. +0x1C/+0x20 are imported-variable refs. */
    const uint8_t advertised_size = vm_read8(descriptor_ea + 0x00);
    const uint32_t record_size = advertised_size ? advertised_size : kFullRecordSize;
    if (record_size < kMinimumRecordSize || record_size > kFullRecordSize) {
        tj_prx_set_status(ctx, kTjCellPrxErrorInval);
        return;
    }
    if (!tj_prx_guest_range_mapped(descriptor_ea, record_size)) {
        tj_prx_set_status(ctx, CELL_EFAULT);
        return;
    }

    const uint16_t version = vm_read16(descriptor_ea + 0x02);
    const uint16_t attributes = vm_read16(descriptor_ea + 0x04);
    const uint16_t function_count = vm_read16(descriptor_ea + 0x06);
    const uint16_t variable_count = vm_read16(descriptor_ea + 0x08);
    const uint16_t tls_variable_count = vm_read16(descriptor_ea + 0x0A);
    const uint32_t name_ea = vm_read32(descriptor_ea + 0x10);
    const uint32_t export_nids_ea = vm_read32(descriptor_ea + 0x14);
    const uint32_t export_addrs_ea = vm_read32(descriptor_ea + 0x18);

    /* Only library records have a name/export table. Management and other
     * records may be nameless; if both bits are present, library wins. */
    if (!(attributes & kPrxExportLibraryFlag)) {
        ctx->gpr[3] = CELL_OK;
        return;
    }

    if (tls_variable_count) {
        /* This title HLE has no TLS allocation/relocation model. */
        tj_prx_set_status(ctx, kTjCellPrxErrorIllegalLibrary);
        return;
    }
    if (!name_ea) {
        tj_prx_set_status(ctx, CELL_EFAULT);
        return;
    }

    char name[kMaxNameSize] = {};
    bool terminated = false;
    for (uint32_t i = 0; i < kMaxNameSize; ++i) {
        const uint64_t char_ea = (uint64_t)name_ea + i;
        if (char_ea > UINT32_MAX || !tj_prx_guest_range_mapped((uint32_t)char_ea, 1)) {
            tj_prx_set_status(ctx, CELL_EFAULT);
            return;
        }
        name[i] = (char)vm_read8((uint32_t)char_ea);
        if (!name[i]) {
            terminated = true;
            break;
        }
    }
    if (!terminated) {
        tj_prx_set_status(ctx, kTjCellPrxErrorIllegalLibrary);
        return;
    }

    const uint64_t function_array_size = (uint64_t)function_count * sizeof(uint32_t);
    const uint64_t variable_array_size = (uint64_t)variable_count * sizeof(uint32_t);
    const uint64_t total_export_count = (uint64_t)function_count + variable_count;
    const uint64_t combined_array_size = function_array_size + variable_array_size;
    if (function_array_size > UINT32_MAX || variable_array_size > UINT32_MAX ||
        combined_array_size > UINT32_MAX || total_export_count > UINT32_MAX) {
        tj_prx_set_status(ctx, kTjCellPrxErrorInval);
        return;
    }

    /* Fields not needed for import linkage are still deliberately consumed at
     * their ABI offsets: +1 unknown, +C hash/unknown, +24 and +28 trailing. */
    (void)vm_read8(descriptor_ea + 0x01);
    (void)vm_read32(descriptor_ea + 0x0C);
    if (record_size >= 0x28u)
        (void)vm_read32(descriptor_ea + 0x24);
    if (record_size >= kFullRecordSize)
        (void)vm_read32(descriptor_ea + 0x28);
    if (record_size >= 0x20u)
        (void)vm_read32(descriptor_ea + 0x1C); /* imported-variable NIDs */
    if (record_size >= 0x24u)
        (void)vm_read32(descriptor_ea + 0x20); /* imported-variable references */

    if (total_export_count > kMaxDynamicExports) {
        tj_prx_set_status(ctx, CELL_ENOMEM);
        return;
    }
    if (total_export_count && (!export_nids_ea || !export_addrs_ea ||
        !tj_prx_guest_range_mapped(export_nids_ea, (uint32_t)combined_array_size) ||
        !tj_prx_guest_range_mapped(export_addrs_ea, (uint32_t)combined_array_size))) {
        tj_prx_set_status(ctx, CELL_EFAULT);
        return;
    }

    /* Snapshot once. Validation and publication both consume these host bytes,
     * so guest mutation during this syscall cannot change what gets linked. */
    std::array<prx_dynamic_export, kMaxDynamicExports> exports = {};
    for (uint32_t i = 0; i < (uint32_t)total_export_count; ++i) {
        exports[i].nid = vm_read32(export_nids_ea + i * sizeof(uint32_t));
        exports[i].guest_addr = vm_read32(export_addrs_ea + i * sizeof(uint32_t));
        exports[i].kind = i < function_count
            ? PRX_DYNAMIC_EXPORT_FUNCTION : PRX_DYNAMIC_EXPORT_VARIABLE;
        if (!exports[i].nid) {
            tj_prx_set_status(ctx, kTjCellPrxErrorInval);
            return;
        }
        if (!exports[i].guest_addr || !tj_prx_guest_range_mapped(exports[i].guest_addr,
            exports[i].kind == PRX_DYNAMIC_EXPORT_FUNCTION ? 8 : 1)) {
            tj_prx_set_status(ctx, CELL_EFAULT);
            return;
        }
    }

    std::lock_guard<std::mutex> lock(g_tj_prx_libraries_mutex);
    for (const tj_prx_library_entry& entry : g_tj_prx_libraries) {
        if (entry.used && !std::memcmp(entry.name, name, sizeof(name))) {
            ctx->gpr[3] = CELL_OK; /* library identity is its name */
            return;
        }
    }
    for (tj_prx_library_entry& entry : g_tj_prx_libraries) {
        if (!entry.used) {
            const uint64_t owner = g_tj_prx_next_owner;
            const int register_result = total_export_count
                ? prx_register_dynamic_exports(owner, exports.data(), (uint32_t)total_export_count)
                : PRX_DYNAMIC_REGISTER_OK;
            if (register_result != PRX_DYNAMIC_REGISTER_OK) {
                tj_prx_set_status(ctx, register_result == PRX_DYNAMIC_REGISTER_FULL
                    ? CELL_ENOMEM : kTjCellPrxErrorInval);
                return;
            }

            entry.used = true;
            entry.owner = owner;
            entry.version = version;
            entry.attributes = attributes;
            entry.function_count = function_count;
            entry.variable_count = variable_count;
            entry.tls_variable_count = tls_variable_count;
            std::memcpy(entry.name, name, sizeof(name));
            if (++g_tj_prx_next_owner == 0)
                g_tj_prx_next_owner = 1;
            ctx->gpr[3] = CELL_OK;
            return;
        }
    }

    tj_prx_set_status(ctx, CELL_ENOMEM);
}

/* The callback chooses an exact enumerated save directory. The shared HLE
 * marshals the guest ABI, performs only a NOCONFIRM delete, then calls funcDone. */
static void tj_cellSaveDataFixedDelete(ppu_context* ctx)
{
    cellSaveDataFixedDelete_ctx(ctx);
}

/* Native recomp runs offline. Preserve NP object lifecycles so optional
 * leaderboard/store setup can unwind normally, while every operation that
 * requires Sony's retired service fails explicitly instead of returning fake
 * online success or leaving guest output memory uninitialized. */
static std::atomic<uint32_t> g_tj_np_context_id { 1 };

static void tj_npLifecycleOk(ppu_context* ctx)
{
    ctx->gpr[3] = CELL_OK;
}

static void tj_npCreateContext(ppu_context* ctx)
{
    ctx->gpr[3] = g_tj_np_context_id.fetch_add(1, std::memory_order_relaxed);
}

static void tj_npOffline(ppu_context* ctx)
{
    constexpr uint32_t kNpOffline = 0x8002AA0Cu;
    ctx->gpr[3] = (uint64_t)(int64_t)(int32_t)kNpOffline;
}

static void tj_npPollOffline(ppu_context* ctx)
{
    constexpr uint32_t kNpOffline = 0x8002AA0Cu;
    const uint32_t result_ea = (uint32_t)ctx->gpr[4];
    if (result_ea) vm_write32(result_ea, kNpOffline);
    ctx->gpr[3] = (uint64_t)(int64_t)(int32_t)kNpOffline;
}

static void tj_npManagerGetContentRatingFlag(ppu_context* ctx)
{
    const uint32_t restricted_ea = (uint32_t)ctx->gpr[3];
    const uint32_t age_ea = (uint32_t)ctx->gpr[4];
    if (!restricted_ea || !age_ea) {
        ctx->gpr[3] = (uint64_t)(int64_t)(int32_t)0x8002AA03u;
        return;
    }
    vm_write32(restricted_ea, 0);
    vm_write32(age_ea, 18);
    ctx->gpr[3] = CELL_OK;
}

static void tj_npManagerGetNetworkTime(ppu_context* ctx)
{
    constexpr uint32_t kNpOffline = 0x8002AA0Cu;
    const uint32_t tick_ea = (uint32_t)ctx->gpr[3];
    if (tick_ea) vm_write64(tick_ea, 0);
    ctx->gpr[3] = (uint64_t)(int64_t)(int32_t)kNpOffline;
}

static void tj_npBasicGetFriendListEntryCount(ppu_context* ctx)
{
    const uint32_t count_ea = (uint32_t)ctx->gpr[3];
    uint32_t count = 0;
    const int32_t rc = sceNpBasicGetFriendListEntryCount(&count);
    if (rc == CELL_OK && count_ea) vm_write32(count_ea, count);
    ctx->gpr[3] = (uint64_t)(int64_t)rc;
}

static void tj_npManagerGetNpId(ppu_context* ctx)
{
    const uint32_t np_id_ea = (uint32_t)ctx->gpr[3];
    if (!np_id_ea) {
        ctx->gpr[3] = (uint64_t)(int64_t)(int32_t)SCE_NP_ERROR_INVALID_ARGUMENT;
        return;
    }

    SceNpId np_id = {};
    const int32_t rc = sceNpManagerGetNpId(&np_id);
    if (rc == CELL_OK) {
        const uint8_t* bytes = reinterpret_cast<const uint8_t*>(&np_id);
        for (uint32_t i = 0; i < sizeof(np_id); ++i) vm_write8(np_id_ea + i, bytes[i]);
    }
    ctx->gpr[3] = (uint64_t)(int64_t)rc;
}

static void tj_npManagerGetOnlineName(ppu_context* ctx)
{
    const uint32_t name_ea = (uint32_t)ctx->gpr[3];
    if (!name_ea) {
        ctx->gpr[3] = (uint64_t)(int64_t)(int32_t)SCE_NP_ERROR_INVALID_ARGUMENT;
        return;
    }

    SceNpOnlineName name = {};
    const int32_t rc = sceNpManagerGetOnlineName(&name);
    if (rc == CELL_OK) {
        const uint8_t* bytes = reinterpret_cast<const uint8_t*>(&name);
        for (uint32_t i = 0; i < sizeof(name); ++i) vm_write8(name_ea + i, bytes[i]);
    }
    ctx->gpr[3] = (uint64_t)(int64_t)rc;
}

/* sceNpEulaCheckEulaStatus is asynchronous: firmware schedules its dedicated
 * callback for the next sysutil poll. Keep that ordering so the title never
 * re-enters guest code from inside the initiating import. */
static std::atomic<uint32_t> g_tj_np_eula_pending { 0 }; /* 0 idle, 1 filling, 2 ready */
static uint32_t g_tj_np_eula_callback = 0;
static uint32_t g_tj_np_eula_callback_arg = 0;

static void tj_sceNpEulaCheckEulaStatus(ppu_context* ctx)
{
    constexpr uint32_t SCE_NP_EULA_ERROR_INVALID_ARGUMENT = 0x8002E501u;
    constexpr uint32_t SCE_NP_EULA_ERROR_ALREADY_INITIALIZED = 0x8002E503u;
    const uint32_t communication_id_ea = (uint32_t)ctx->gpr[3];
    const uint32_t callback_opd = (uint32_t)ctx->gpr[6];
    const uint32_t callback_arg = (uint32_t)ctx->gpr[7];

    if (!communication_id_ea || !callback_opd) {
        ctx->gpr[3] = (uint64_t)(int64_t)(int32_t)SCE_NP_EULA_ERROR_INVALID_ARGUMENT;
        return;
    }

    uint32_t expected = 0;
    if (!g_tj_np_eula_pending.compare_exchange_strong(
            expected, 1, std::memory_order_acq_rel)) {
        ctx->gpr[3] = (uint64_t)(int64_t)(int32_t)SCE_NP_EULA_ERROR_ALREADY_INITIALIZED;
        return;
    }

    g_tj_np_eula_callback = callback_opd;
    g_tj_np_eula_callback_arg = callback_arg;
    g_tj_np_eula_pending.store(2, std::memory_order_release);
    ctx->gpr[3] = CELL_OK;
}

static void tj_cellSysutilCheckCallback(ppu_context* ctx)
{
    const int32_t rc = cellSysutilCheckCallback();

    uint32_t expected = 2;
    if (g_tj_np_eula_pending.compare_exchange_strong(
            expected, 0, std::memory_order_acq_rel)) {
        const uint32_t callback_opd = g_tj_np_eula_callback;
        const uint32_t callback_arg = g_tj_np_eula_callback_arg;
        g_tj_np_eula_callback = 0;
        g_tj_np_eula_callback_arg = 0;
        /* SCE_NP_EULA_ALREADY_ACCEPTED, CELL_OK, version 1, user argument. */
        ps3_invoke_guest(callback_opd, 2, CELL_OK, 1, callback_arg,
                         0, 0, 0, 0);
    }

    ctx->gpr[3] = (uint64_t)(int64_t)rc;
}

static void tj_npCommerceLifecycleOk(ppu_context* ctx)
{
    ctx->gpr[3] = CELL_OK;
}

static void tj_npCommerceOffline(ppu_context* ctx)
{
    constexpr uint32_t SCE_NP_COMMERCE2_ERROR_SERVER_ERROR = 0x80024410u;
    ctx->gpr[3] = (uint64_t)(int64_t)(int32_t)SCE_NP_COMMERCE2_ERROR_SERVER_ERROR;
}

#ifdef _WIN32
/* TJ_STDERR_FILE / TJ_STDOUT_FILE (set by launch_tokyojungle.ps1): write that
 * CRT stream straight to an append-only file instead of the inherited launcher
 * pipe. The pipe buffers only a few KB; whenever the launcher's async reader
 * fell behind, every thread that logged blocked inside fprintf -- the
 * presenter mid-frame, and SPU/job-chain perf reporters while holding locks
 * every SPU job takes -- giving 1.8-14.4 s whole-game freezes in the traced
 * run visible-user-test-20261017-012850. The streams stay unbuffered (setvbuf
 * in main), so the file still ends where the process did, including a crash or
 * the TerminateProcess window-close exit. Unset, or any failure, keeps the
 * inherited handle exactly as before. */
static void tj_redirect_std_stream_to_file(const wchar_t* env_name,
                                           const char* label, int fd,
                                           DWORD std_handle_id, FILE* stream)
{
    wchar_t path[2048];
    const DWORD capacity = (DWORD)(sizeof(path) / sizeof(path[0]));
    const DWORD length = GetEnvironmentVariableW(env_name, path, capacity);
    if (length == 0) return;
    if (length >= capacity) {
        fprintf(stderr, "[TJ] %s path too long; keeping the inherited handle\n", label);
        return;
    }
    SECURITY_ATTRIBUTES no_inherit = { (DWORD)sizeof(no_inherit), NULL, FALSE };
    HANDLE file = CreateFileW(path, FILE_APPEND_DATA | SYNCHRONIZE,
                              FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
                              &no_inherit, OPEN_ALWAYS, FILE_ATTRIBUTE_NORMAL, NULL);
    if (file == INVALID_HANDLE_VALUE) {
        fprintf(stderr, "[TJ] %s open failed (error %lu); keeping the inherited handle\n",
                label, GetLastError());
        return;
    }
    /* Text mode like the inherited descriptor: lines stay CRLF. */
    const int file_fd = _open_osfhandle((intptr_t)file, _O_TEXT);
    if (file_fd < 0) {
        CloseHandle(file);
        fprintf(stderr, "[TJ] %s has no CRT descriptor; keeping the inherited handle\n", label);
        return;
    }
    fflush(stream);
    const int saved_fd = _dup(fd);   /* restore point; -1 when nothing was inherited */
    if (_dup2(file_fd, fd) != 0) {
        if (saved_fd >= 0) _dup2(saved_fd, fd);
        if (saved_fd >= 0) _close(saved_fd);
        _close(file_fd);
        fprintf(stderr, "[TJ] %s switch failed; keeping the inherited handle\n", label);
        return;
    }
    /* Release the inherited pipe end so the launcher's copy sees EOF. */
    if (saved_fd >= 0) _close(saved_fd);
    _close(file_fd);
    SetStdHandle(std_handle_id, (HANDLE)_get_osfhandle(fd));
}
#endif

int main(int argc, char* argv[])
{
#ifdef _WIN32
    /* Before anything logs (see tj_redirect_std_stream_to_file). */
    tj_redirect_std_stream_to_file(L"TJ_STDERR_FILE", "TJ_STDERR_FILE", 2,
                                   STD_ERROR_HANDLE, stderr);
    tj_redirect_std_stream_to_file(L"TJ_STDOUT_FILE", "TJ_STDOUT_FILE", 1,
                                   STD_OUTPUT_HANDLE, stdout);
#endif
    // Explicit renderer-only diagnostic: no ELF, title threads, VFS or saves.
    if (argc == 3 && strcmp(argv[1], "--rsx-reference") == 0) {
        extern int tj_rsx_reference_main(const char*);
        return tj_rsx_reference_main(argv[2]);
    }
#ifdef _WIN32
    if (!IsProcessorFeaturePresent(PF_SSSE3_INSTRUCTIONS_AVAILABLE)) {
        fprintf(stderr, "[TJ] ERROR: this build requires an SSSE3-capable CPU\n");
        return EXIT_FAILURE;
    }
    /* TJ_HIGH_QOS=1: opt out of Windows power throttling (EcoQoS) so the
     * scheduler keeps the guest, SPU-job and renderer threads on full-speed
     * cores of hybrid CPUs even when the window is not focused. Threads
     * inherit the process setting. */
    if (const char* qos = getenv("TJ_HIGH_QOS"); qos && *qos && strcmp(qos, "0") != 0) {
        PROCESS_POWER_THROTTLING_STATE state = {};
        state.Version = PROCESS_POWER_THROTTLING_CURRENT_VERSION;
        state.ControlMask = PROCESS_POWER_THROTTLING_EXECUTION_SPEED;
        state.StateMask = 0;   /* controlled and off = always high QoS */
        const BOOL ok = SetProcessInformation(GetCurrentProcess(), ProcessPowerThrottling,
                                              &state, sizeof(state));
        fprintf(stderr, "[TJ] high QoS (power throttling off): %s\n", ok ? "on" : "failed");
    }
#endif

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

#ifdef _WIN32
    if (getenv("TJ_THREAD_CRASH_TRACE")) {
        void* handler = AddVectoredExceptionHandler(1, tj_vectored_crash_logger);
        fprintf(stderr, "[TJ] worker crash trace %s\n",
                handler ? "enabled" : "FAILED to install");
    }
#endif

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

    /* Reserve 16 MB for cellAudio's eight possible {buffer,index} pairs after
     * the HLE title heap (0x02000000..0x0BFFFFFF), before TLS at 0x0E000000. */
    cellAudioSetGuestMemoryBase(0x0C000000u);

    // 1b. Commit page 0 as null-pointer guard (reads return 0 instead of crashing)
    vm_commit(0, 0x10000);
    printf("[TJ] Page 0 guard committed (64KB)\n");
    if (const char* guard = getenv("TJ_GUARD_ADDR")) {
        uint32_t guest_ea = (uint32_t)strtoul(guard, nullptr, 16);
        ppu_guard_page(guest_ea);
        printf("[TJ] Write guard armed at guest 0x%08X\n", guest_ea);
    }

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
    vm_commit(TJ_FONT_LIBRARY_EA, 0x10000);

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
        ppu_vfs_root = vfs;
#ifdef _WIN32
        if (!getenv("PS3_HDD_GAME_ROOT")) _putenv_s("PS3_HDD_GAME_ROOT", vfs);
#else
        if (!getenv("PS3_HDD_GAME_ROOT")) setenv("PS3_HDD_GAME_ROOT", vfs, 0);
#endif
        cellfs_set_root_path(vfs);
        /* Map the whole PS3_GAME dir, not just USRDIR: the installer also
         * stats /dev_bdvd/PS3_GAME/ICON0.PNG and friends, and a USRDIR-only
         * mapping sent those to the default gamedata/dev_bdvd/ prefix, where
         * nothing exists. */
        cellfs_add_path_mapping("/dev_bdvd/PS3_GAME/", "");
        cellfs_add_path_mapping("/dev_hdd0/game/NPUA80523/", "");
        cellfs_add_path_mapping("/app_home/", "");
        cellGame_init_from_paramsfo("input/PARAM.SFO");
        printf("[TJ] VFS root=\"%s\" (package/disc roots configured)\n", vfs);
    }

    // 1c. Initialize LV2 syscall dispatch table
    lv2_register_all_syscalls(&g_lv2_syscalls);
    lv2_syscall_register(&g_lv2_syscalls, SYS_PPU_THREAD_JOIN, tj_sys_ppu_thread_join);
    if (!tj_reserve_fixed_guest_stacks(&g_vm_stack_alloc)) {
        fprintf(stderr, "[TJ] ERROR: Cannot reserve fixed guest stacks before worker creation.\n");
        vm_shutdown();
        return EXIT_FAILURE;
    }
    printf("[TJ] Guest stacks: main=%08X+%X callbacks=%08X+%X workers=%08X\n",
           VM_STACK_BASE, TJ_MAIN_STACK_SIZE, TJ_CB_STACK_BASE, TJ_CB_STACK_SPAN,
           g_vm_stack_alloc.next_addr);

    /* lv2_register_all_syscalls already aliases SDK-era syscall 988 to the
     * real sys_tty_write handler. Do not overwrite it with return-success:
     * the CRT uses its written-length out parameter while initializing. */

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
    /* cellFs pointer arguments are guest addresses. Use the context-aware VFS
     * bridge so paths and out-parameters are translated instead of being
     * dereferenced as native Windows pointers. */
    ppu_fs_register();
    ps3_hle_register_ctx(0xCC1633CCu, "l10n_get_converter", tj_l10nGetConverter);
    ps3_hle_register_ctx(0xA298CAD2u, "l10n_convert_str", tj_l10nConvertStr);
    ps3_hle_register(0xAD218FAFu, "sceNpDrmIsAvailable", (void*)sceNpDrmIsAvailable);
    ps3_hle_register_ctx(0x7663E368u, "cellAudioOutGetDeviceInfo",
                         tj_cellAudioOutGetDeviceInfo);
    ps3_hle_register_ctx(0x4692AB35u, "cellAudioOutConfigure",
                         tj_cellAudioOutConfigure);
    ps3_hle_register_ctx(0x346EBBA3u, "cellSailMemAllocatorInitialize",
                         tj_cellSailMemAllocatorInitialize);
    ps3_hle_register_ctx(0x23654375u, "cellSailPlayerInitialize2",
                         tj_cellSailPlayerInitialize2);
    ps3_hle_register_ctx(0x3D0D3B72u, "cellSailSoundAdapterInitialize",
                         tj_cellSailSoundAdapterInitialize);
    ps3_hle_register_ctx(0x1C983864u, "cellSailGraphicsAdapterInitialize",
                         tj_cellSailGraphicsAdapterInitialize);
    ps3_hle_register_ctx(0x0247C69Eu, "cellSailGraphicsAdapterGetFrame",
                         tj_cellSailGraphicsAdapterGetFrame);
    ps3_hle_register_ctx(0x7EB8D6B5u, "cellSailSoundAdapterGetFrame",
                         tj_cellSailSoundAdapterGetFrame);
    ps3_hle_register_ctx(0xEEC22809u, "cellSailSoundAdapterUpdateAvSync",
                         tj_cellSailSoundAdapterUpdateAvSync);
    ps3_hle_register_ctx(0x2E3CCB5Eu, "cellSailGraphicsAdapterSetPreferredFormat",
                         tj_cellSailGraphicsAdapterSetPreferredFormat);
    ps3_hle_register_ctx(0xD1462438u, "cellSailSoundAdapterFinalize",
                         tj_cellSailSoundAdapterFinalize);
    ps3_hle_register_ctx(0xF25F197Du, "cellSailSoundAdapterGetFormat",
                         tj_cellSailSoundAdapterGetFormat);
    ps3_hle_register_ctx(0x4AE979DFu, "cellSailSoundAdapterPtsToTimePosition",
                         tj_cellSailSoundAdapterPtsToTimePosition);
    ps3_hle_register_ctx(0x76488BB1u, "cellSailGraphicsAdapterFinalize",
                         tj_cellSailGraphicsAdapterFinalize);
    ps3_hle_register_ctx(0xFFD58AA4u, "cellSailGraphicsAdapterGetFormat",
                         tj_cellSailGraphicsAdapterGetFormat);
    ps3_hle_register_ctx(0x1872331Bu, "cellSailGraphicsAdapterPtsToTimePosition",
                          tj_cellSailSoundAdapterPtsToTimePosition);
    ps3_hle_register_ctx(0x44A20E79u, "cellSailGraphicsAdapterUpdateAvSync",
                         tj_cellSailGraphicsAdapterUpdateAvSync);
    ps3_hle_register_ctx(0x18B4629Du, "cellSailPlayerFinalize",
                         tj_cellSailPlayerFinalize);
    ps3_hle_register_ctx(0x1139A206u, "cellSailPlayerSetSoundAdapter",
                         tj_cellSailPlayerSetSoundAdapter);
    ps3_hle_register_ctx(0x18BCD21Bu, "cellSailPlayerSetGraphicsAdapter",
                         tj_cellSailPlayerSetGraphicsAdapter);
    ps3_hle_register_ctx(0xBDF21B0Fu, "cellSailPlayerBoot",
                         tj_cellSailPlayerBoot);
    ps3_hle_register_ctx(0x34ECC1B9u, "cellSailPlayerOpenStream",
                         tj_cellSailPlayerOpenStream);
    ps3_hle_register_ctx(0x85BEFFCCu, "cellSailPlayerCloseStream",
                         tj_cellSailPlayerCloseStream);
    ps3_hle_register_ctx(0xE535B0D3u, "cellSailPlayerStart",
                         tj_cellSailPlayerStart);
    ps3_hle_register_ctx(0xEBA8D4ECu, "cellSailPlayerStop",
                         tj_cellSailPlayerStop);
    ps3_hle_register_ctx(0xFC5BAF8Au, "cellSailPlayerSetRepeatMode",
                         tj_cellSailPlayerSetRepeatMode);
    ps3_hle_register_ctx(0x38144ECFu, "cellSailPlayerGetRepeatMode",
                         tj_cellSailPlayerGetRepeatMode);
    ps3_hle_register_ctx(0xD1D55A90u, "cellSailPlayerSetPaused",
                         tj_cellSailPlayerSetPaused);
    ps3_hle_register_ctx(0xD7938B8Du, "cellSailPlayerCreateDescriptor",
                         tj_cellSailPlayerCreateDescriptor);
    ps3_hle_register_ctx(0xFC839BD4u, "cellSailPlayerDestroyDescriptor",
                         tj_cellSailPlayerDestroyDescriptor);
    ps3_hle_register_ctx(0x7C8DFF3Bu, "cellSailPlayerAddDescriptor",
                         tj_cellSailPlayerAddDescriptor);
    ps3_hle_register_ctx(0x9897FBD1u, "cellSailPlayerRemoveDescriptor",
                         tj_cellSailPlayerRemoveDescriptor);
    ps3_hle_register_ctx(0x0ABB318Bu, "cellSailDescriptorCreateDatabase",
                         tj_cellSailDescriptorCreateDatabase);
    ps3_hle_register_ctx(0x28336E89u, "cellSailDescriptorDestroyDatabase",
                         tj_cellSailDescriptorDestroyDatabase);
    ps3_hle_register_ctx(0x4DE501B1u, "cellPamfReaderGetPresentationStartTime",
                         tj_cellPamfReaderGetPresentationStartTime);
    ps3_hle_register_ctx(0xF61609D6u, "cellPamfReaderGetPresentationEndTime",
                         tj_cellPamfReaderGetPresentationEndTime);
    ps3_hle_register_ctx(0xDB70296Cu, "cellPamfReaderGetMuxRateBound",
                         tj_cellPamfReaderGetMuxRateBound);
    ps3_hle_register_ctx(0x37F723F7u, "cellPamfReaderGetNumberOfStreams",
                         tj_cellPamfReaderGetNumberOfStreams);
    ps3_hle_register_ctx(0xD5F9A15Bu, "cellSailDescriptorGetStreamType",
                         tj_cellSailDescriptorGetStreamType);
    ps3_hle_register_ctx(0x76B1A425u, "cellSailDescriptorSetAutoSelection",
                         tj_cellSailDescriptorSetAutoSelection);
    ps3_hle_register_ctx(0x4C191088u, "cellSailDescriptorGetUri",
                         tj_cellSailDescriptorGetUri);
    ps3_hle_register_ctx(0xF03DCC29u, "cellFontInitializeWithRevision",
                         tj_cellFontInitializeWithRevision);
    ps3_hle_register_ctx(0x7A0A83C4u, "cellFontInitLibraryFreeTypeWithRevision",
                         tj_cellFontInitLibraryFreeTypeWithRevision);
    ps3_hle_register_ctx(0xA885CC9Bu, "cellFontOpenFontset", tj_cellFontOpenFontset);
    ps3_hle_register_ctx(0x7AB47F7Eu, "cellFontEnd", tj_cellFontEnd);
    ps3_hle_register_ctx(0x042E74E3u, "cellFontCreateRenderer", tj_cellFontCreateRenderer);
    ps3_hle_register_ctx(0x21EBB248u, "cellFontDestroyRenderer", tj_cellFontDestroyRenderer);
    ps3_hle_register_ctx(0x29329541u, "cellFontOpenFontInstance", tj_cellFontOpenFontInstance);
    ps3_hle_register_ctx(0xB276F1F6u, "cellFontCloseFont", tj_cellFontCloseFont);
    ps3_hle_register_ctx(0x66A23100u, "cellFontBindRenderer", tj_cellFontBindRenderer);
    ps3_hle_register_ctx(0xFB3341BAu, "cellFontSetResolutionDpi", tj_cellFontSetResolutionDpi);
    ps3_hle_register_ctx(0x70F3E728u, "cellFontSetScalePoint", tj_cellFontSetScalePoint);
    ps3_hle_register_ctx(0x297F0E93u, "cellFontSetScalePixel", tj_cellFontSetScalePixel);
    ps3_hle_register_ctx(0x25253FE4u, "cellFontSetEffectWeight", tj_cellFontSetEffectWeight);
    ps3_hle_register_ctx(0x8657C8F5u, "cellFontSetEffectSlant", tj_cellFontSetEffectSlant);
    ps3_hle_register_ctx(0x1387C45Cu, "cellFontGetHorizontalLayout", tj_cellFontGetHorizontalLayout);
    ps3_hle_register_ctx(0x698897F8u, "cellFontGetVerticalLayout", tj_cellFontGetVerticalLayout);
    ps3_hle_register_ctx(0xF16379FAu, "cellFontUnbindRenderer", tj_cellFontUnbindRenderer);
    ps3_hle_register_ctx(0x227E1E3Cu, "cellFontSetupRenderScalePixel", tj_cellFontSetupRenderScalePixel);
    ps3_hle_register_ctx(0xA6DC25D1u, "cellFontSetupRenderEffectWeight", tj_cellFontSetupRenderEffectWeight);
    ps3_hle_register_ctx(0x78D05E08u, "cellFontSetupRenderEffectSlant", tj_cellFontSetupRenderEffectSlant);
    ps3_hle_register_ctx(0x90B9465Eu, "cellFontRenderSurfaceInit", tj_cellFontRenderSurfaceInit);
    ps3_hle_register_ctx(0xB422B005u, "cellFontRenderSurfaceSetScissor", tj_cellFontRenderSurfaceSetScissor);
    ps3_hle_register_ctx(0x2DA9FD9Du, "cellFontGetRenderCharGlyphMetrics", tj_cellFontGetRenderCharGlyphMetrics);
    ps3_hle_register_ctx(0xD8EAEE9Fu, "cellFontGetCharGlyphMetrics",
                         tj_cellFontGetCharGlyphMetrics);
    ps3_hle_register_ctx(0x98AC5524u, "cellFontGetFontIdCode",
                         tj_cellFontGetFontIdCode);
    ps3_hle_register_ctx(0x700E6223u, "cellFontGetRenderCharGlyphMetricsVertical", tj_cellFontGetRenderCharGlyphMetricsVertical);
    ps3_hle_register_ctx(0x88BE4799u, "cellFontRenderCharGlyphImage", tj_cellFontRenderCharGlyphImage);
    ps3_hle_register_ctx(0x073FA321u, "cellFontOpenFontsetOnMemory",
                         tj_cellFontOpenFontsetOnMemory);
    ps3_hle_register_ctx(0x40D40544u, "cellFontEndLibrary", tj_cellFontEndLibrary);
    ps3_hle_register_ctx(0xC17259DEu, "cellFontGenerateCharGlyph",
                         tj_cellFontGenerateCharGlyph);
    ps3_hle_register_ctx(0xD62F5D76u, "cellFontDeleteGlyph", tj_cellFontDeleteGlyph);
    ps3_hle_register_ctx(0xFE9A6DD7u, "cellFontGetCharGlyphMetricsVertical",
                         tj_cellFontGetCharGlyphMetricsVertical);
    ps3_hle_register_ctx(0x01220224u, "cellRescGcmSurface2RescSrc",
                         tj_cellRescGcmSurface2RescSrc);
    ps3_hle_register_ctx(0x516EE89Eu, "cellRescInit", tj_cellRescInit);
    ps3_hle_register_ctx(0x5A338CDBu, "cellRescGetBufferSize",
                         tj_cellRescGetBufferSize);
    ps3_hle_register_ctx(0x6CD0F95Fu, "cellRescSetSrc", tj_cellRescSetSrc);
    ps3_hle_register_ctx(0x10DB5B1Au, "cellRescSetDsts", tj_cellRescSetDsts);
    ps3_hle_register_ctx(0x2EA94661u, "cellRescSetFlipHandler",
                         tj_cellRescSetFlipHandler);
    ps3_hle_register_ctx(0xD3758645u, "cellRescSetVBlankHandler",
                         tj_cellRescSetVBlankHandler);
    ps3_hle_register_ctx(0x0D3C22CEu, "cellRescSetWaitFlip",
                         tj_cellRescSetWaitFlip);
    ps3_hle_register_ctx(0x129922A0u, "cellRescResetFlipStatus",
                         tj_cellRescResetFlipStatus);
    ps3_hle_register_ctx(0x23134710u, "cellRescSetDisplayMode",
                         tj_cellRescSetDisplayMode);
    ps3_hle_register_ctx(0x25C107E6u, "cellRescSetConvertAndFlip",
                         tj_cellRescSetConvertAndFlip);
    ps3_hle_register_ctx(0x2EA3061Eu, "cellRescExit", tj_cellRescExit);
    ps3_hle_register_ctx(0x8107277Cu, "cellRescSetBufferAddress",
                         tj_cellRescSetBufferAddress);
    ps3_hle_register_ctx(0xC47C5C22u, "cellRescGetFlipStatus",
                         tj_cellRescGetFlipStatus);
    ps3_hle_register_ctx(0xD1CA0503u, "cellRescVideoOutResolutionId2RescBufferMode",
                         tj_cellRescVideoOutResolutionId2RescBufferMode);
    ps3_hle_register_ctx(0xDAA5CD20u, "cellGameSetParamString",
                         tj_cellGameSetParamString);
    ps3_hle_register_ctx(0xB0A1F8C6u, "cellGameContentErrorDialog",
                         tj_cellGameContentErrorDialog);
    ps3_hle_register_ctx(0xFF0A21B7u, "cellKbRead", tj_cellKbRead);
    ps3_hle_register_ctx(0x3E22CB4Bu, "cellMsgDialogOpenErrorCode",
                         tj_cellMsgDialogOpenErrorCode);
    ps3_hle_register_ctx(0x20543730u, "cellMsgDialogClose",
                         tj_cellMsgDialogClose);
    ps3_hle_register_ctx(0x571AFACAu, "cellSslCertificateLoader",
                         tj_cellSslCertificateLoader);
    ps3_hle_register_ctx(0x3A33C1FDu, "_cellGcmFunc15", tj_hleReturnOk);
    ps3_hle_register_ctx(0x42B23552u, "sys_prx_register_library",
                         tj_sysPrxRegisterLibrary);
    ps3_hle_register_ctx(0x8F122EF8u, "cellSpursTasksetAttributeSetTasksetSize",
                         tj_cellSpursTasksetAttributeSetTasksetSize);
    ps3_hle_register_ctx(0x9034E538u, "cellSpursTaskGetContextSaveAreaSize",
                         tj_cellSpursTaskGetContextSaveAreaSize);
    ps3_hle_register_ctx(0x3548F483u, "_cellSpursJobChainAttributeInitialize",
                         tj_cellSpursJobChainAttributeInitialize);
    ps3_hle_register_ctx(0xCED17573u, "cellSaveDataFixedDelete",
                         tj_cellSaveDataFixedDelete);

    ps3_hle_register_ctx(0x5F2D9257u, "sceNpLookupInit", tj_npLifecycleOk);
    ps3_hle_register_ctx(0x8440537Cu, "sceNpLookupTerm", tj_npLifecycleOk);
    ps3_hle_register_ctx(0xCE81C7F0u, "sceNpLookupCreateTitleCtx", tj_npCreateContext);
    ps3_hle_register_ctx(0xEA2E9FFCu, "sceNpLookupCreateTransactionCtx", tj_npCreateContext);
    ps3_hle_register_ctx(0x5DE61626u, "sceNpLookupDestroyTitleCtx", tj_npLifecycleOk);
    ps3_hle_register_ctx(0xFB87CF5Eu, "sceNpLookupDestroyTransactionCtx", tj_npLifecycleOk);
    ps3_hle_register_ctx(0x7508112Eu, "sceNpLookupPollAsync", tj_npPollOffline);
    ps3_hle_register_ctx(0x860B1756u, "sceNpLookupTitleSmallStorageAsync", tj_npOffline);

    ps3_hle_register_ctx(0xB9F93BBBu, "sceNpScoreCreateTitleCtx", tj_npCreateContext);
    ps3_hle_register_ctx(0x6F5E8143u, "sceNpScoreCreateTransactionCtx", tj_npCreateContext);
    ps3_hle_register_ctx(0x259113B8u, "sceNpScoreDestroyTitleCtx", tj_npLifecycleOk);
    ps3_hle_register_ctx(0xC5F4CF82u, "sceNpScoreDestroyTransactionCtx", tj_npLifecycleOk);
    ps3_hle_register_ctx(0xA7A090E5u, "sceNpScorePollAsync", tj_npPollOffline);
    ps3_hle_register_ctx(0x3DB7914Du, "sceNpScoreGetRankingByNpIdAsync", tj_npOffline);
    ps3_hle_register_ctx(0x21206642u, "sceNpScoreGetRankingByRangeAsync", tj_npOffline);
    ps3_hle_register_ctx(0xF0B1E399u, "sceNpScoreRecordScoreAsync", tj_npOffline);
    ps3_hle_register_ctx(0xEE5B20D9u, "sceNpScoreAbortTransaction",
                         tj_npLifecycleOk);

    ps3_hle_register_ctx(0x189A74DAu, "cellSysutilCheckCallback",
                         tj_cellSysutilCheckCallback);
    ps3_hle_register_ctx(0x59D1629Au, "sceNpEulaCheckEulaStatus",
                         tj_sceNpEulaCheckEulaStatus);

    ps3_hle_register_ctx(0x6EE62ED2u, "sceNpManagerGetContentRatingFlag",
                         tj_npManagerGetContentRatingFlag);
    ps3_hle_register_ctx(0xBDC07FD5u, "sceNpManagerGetNetworkTime",
                         tj_npManagerGetNetworkTime);
    ps3_hle_register_ctx(0xAFEF640Du, "sceNpBasicGetFriendListEntryCount",
                         tj_npBasicGetFriendListEntryCount);
    ps3_hle_register_ctx(0xFE37A7F4u, "sceNpManagerGetNpId",
                         tj_npManagerGetNpId);
    ps3_hle_register_ctx(0xF42C0DF8u, "sceNpManagerGetOnlineName",
                         tj_npManagerGetOnlineName);
    ps3_hle_register_ctx(0xCEEEBC7Au, "sceNpProfileCallGui", tj_npOffline);

    ps3_hle_register_ctx(0x62023E98u, "sceNpCommerce2CreateSessionAbort",
                         tj_npCommerceLifecycleOk);
    ps3_hle_register_ctx(0xF6139B58u, "sceNpCommerce2DestroyGetProductInfoListResult",
                         tj_npCommerceLifecycleOk);
    ps3_hle_register_ctx(0xB23E3BD1u, "sceNpCommerce2DoProductBrowseFinishAsync",
                         tj_npCommerceOffline);
    ps3_hle_register_ctx(0x9825A0FCu, "sceNpCommerce2DoProductBrowseStartAsync",
                         tj_npCommerceOffline);
    ps3_hle_register_ctx(0xD9956CE7u, "sceNpCommerce2GetGameProductInfoFromGetProductInfoListResult",
                         tj_npCommerceOffline);
    ps3_hle_register_ctx(0xE1E7B5ACu, "sceNpCommerce2GetProductInfoListCreateReq",
                         tj_npCommerceOffline);
    ps3_hle_register_ctx(0x146618DFu, "sceNpCommerce2GetProductInfoListGetResult",
                         tj_npCommerceOffline);
    ps3_hle_register_ctx(0x9CDE07CCu, "sceNpCommerce2GetProductInfoListStart",
                         tj_npCommerceOffline);
    ps3_hle_register_ctx(0xE0F90E44u, "sceNpCommerce2InitGetProductInfoListResult",
                         tj_npCommerceOffline);
    tj_spu_register_all();
    printf("[TJ] Lifted SPU workloads registered: %u\n", spu_workload_count());
    printf("[HLE] ps3recomp NID registry: %u generic + %u context handlers\n",
           ps3_hle_count(), ps3_hle_ctx_count());

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

    /* TJ_OPD_GUARD=1: write-protect the HLE OPD arena once it is built.
     * The arena is correct at startup (verified by peek) but some entries read
     * back as zero later, so an import that worked earlier starts dispatching
     * to a null address -- and a null bctr returns leaving r3 untouched, which
     * the guest reads as a garbage error code. Nothing should write here after
     * resolution, so a fault names the culprit via the crash filter. */
    if (getenv("TJ_OPD_GUARD")) {
        DWORD old = 0;
        if (VirtualProtect(vm_base + HLE_OPD_BASE, HLE_OPD_SIZE, PAGE_READONLY, &old))
            printf("[TJ] OPD arena 0x%08X+0x%X is now read-only\n",
                   HLE_OPD_BASE, HLE_OPD_SIZE);
        else
            printf("[TJ] OPD guard failed (err %lu)\n", GetLastError());
    }

    // 3. Initialize PPU context
    ppu_context_init(&g_main_ctx);
    /* Reserve thread id 1 before the title creates workers. Runtime ownership
     * primitives use zero as "unowned", and ps3_hle_call uses id 1 to route
     * main-thread-only guest display callbacks. Keep the TLS active-context
     * pointer on the real main context so nested HLE callbacks inherit that id. */
    g_main_ctx.thread_id = ppu_thread_register_main();
    if (!guest_world_register_current("ppu-main")) {
        fprintf(stderr, "[guest-world] main registration failed; refusing guest dispatch\n");
        vm_shutdown();
        return EXIT_FAILURE;
    }
    g_active_ctx = &g_main_ctx;
    uint32_t stack_size = TJ_MAIN_STACK_SIZE;
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
        const uint32_t TLS_POOL_SIZE = 0x00400000; // main + 63 private 64KB thread slots
        uint32_t span = TLS_BIAS + elf.tls_memsz + 0x1000;
        vm_commit(TLS_BASE, TLS_POOL_SIZE);
        memset(vm_base + TLS_BASE, 0, span);
        if (elf.tls_filesz)
            memcpy(vm_base + TLS_BASE, vm_base + elf.tls_vaddr, elf.tls_filesz);
        ppu_set_tls_image(TLS_BASE);
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
            bool guest_exited = tj_run_main_guest_entry(entry_func);
            printf(guest_exited ? "\n[TJ] Guest main thread exited. r3=0x%llX\n"
                                : "\n[TJ] Function returned. r3=0x%llX\n",
                   (unsigned long long)g_main_ctx.gpr[3]);
        }
        __except(GetExceptionCode() == EXCEPTION_ACCESS_VIOLATION ?
                 tj_crash_filter(GetExceptionCode(), GetExceptionInformation()) :
                 EXCEPTION_CONTINUE_SEARCH) {
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
        bool guest_exited = tj_run_main_guest_entry(entry_func);
        printf(guest_exited ? "\n[TJ] Guest main thread exited. r3=0x%llX\n"
                            : "\n[TJ] Function returned. r3=0x%llX\n",
               (unsigned long long)g_main_ctx.gpr[3]);
#endif
    } else {
        fprintf(stderr, "[TJ] ERROR: No valid entry point found.\n");
    }

    /* Present diagnostics may duplicate the watchdog's main-thread handle,
     * so join the ticker before closing that handle. */
    tj_stop_present_thread();
    tj_stop_watchdog();
    if (guest_world_registered_count() != 1) {
        fprintf(stderr, "[guest-world] refusing vm_shutdown with %u registered worker(s)\n",
                guest_world_registered_count());
        return EXIT_FAILURE;
    }
    guest_world_unregister_current();
    vm_shutdown();
    return EXIT_SUCCESS;
}
