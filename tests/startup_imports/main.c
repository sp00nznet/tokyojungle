#include "cellHttp.h"
#include "cellSysmodule.h"
#include "sceNp.h"
#include "sysPrxForUser.h"

#include <stdint.h>
#include <stdio.h>

u8* vm_base = 0;
int g_resv_store_active = 0;

void ppu_resv_break_store(uint64_t ea, uint32_t width)
{
    (void)ea; (void)width;
}

int ppu_fs_guest_exists(const char* guest_path)
{
    (void)guest_path;
    return 0;
}

void ppu_fs_set_npdrm_key(const uint8_t key[16])
{
    (void)key;
}

unsigned int ppu_active_lr(void)
{
    return 0;
}

#define CHECK_S32(expr, expected) do {                                      \
    s32 actual_ = (expr);                                                   \
    s32 expected_ = (s32)(expected);                                        \
    if (actual_ != expected_) {                                             \
        fprintf(stderr, "FAIL: %s -> 0x%08X, expected 0x%08X\n",          \
                #expr, (u32)actual_, (u32)expected_);                        \
        return 1;                                                           \
    }                                                                       \
} while (0)

#define CHECK_U32(expr, expected) do {                                      \
    u32 actual_ = (u32)(expr);                                              \
    u32 expected_ = (u32)(expected);                                        \
    if (actual_ != expected_) {                                             \
        fprintf(stderr, "FAIL: %s -> 0x%08X, expected 0x%08X\n",          \
                #expr, actual_, expected_);                                 \
        return 1;                                                           \
    }                                                                       \
} while (0)

int main(void)
{
    CHECK_S32(cellSysmoduleLoadModuleEx(CELL_SYSMODULE_SYSUTIL_NP_EULA), CELL_OK);
    CHECK_S32(cellSysmoduleIsLoaded(CELL_SYSMODULE_SYSUTIL_NP_EULA), CELL_OK);
    CHECK_S32(cellSysmoduleUnloadModuleEx(CELL_SYSMODULE_SYSUTIL_NP_EULA), CELL_OK);
    CHECK_S32(cellSysmoduleIsLoaded(CELL_SYSMODULE_SYSUTIL_NP_EULA),
              CELL_SYSMODULE_ERROR_UNLOADED);
    CHECK_S32(cellSysmoduleLoadModuleEx(0x0044), CELL_SYSMODULE_ERROR_UNKNOWN);

    CHECK_S32(cellHttpInitCookie(0, 0x1000), CELL_HTTP_ERROR_NO_BUFFER);
    CHECK_S32(cellHttpInitCookie((void*)(uintptr_t)0x1000, 0x1000), CELL_OK);
    CHECK_S32(cellHttpInitCookie((void*)(uintptr_t)0x1000, 0x1000),
              CELL_HTTP_ERROR_ALREADY_INITIALIZED);
    CHECK_S32(cellHttpEndCookie(), CELL_OK);
    CHECK_S32(cellHttpEndCookie(), CELL_HTTP_ERROR_NOT_INITIALIZED);

    CHECK_S32(sceNpScoreInit(), SCE_NP_ERROR_NOT_INITIALIZED);
    CHECK_S32(sceNpInit(0, 0), CELL_OK);
    CHECK_S32(sceNpScoreInit(), CELL_OK);
    CHECK_S32(sceNpScoreInit(), SCE_NP_COMMUNITY_ERROR_ALREADY_INITIALIZED);
    CHECK_S32(sceNpScoreTerm(), CELL_OK);
    CHECK_S32(sceNpScoreTerm(), SCE_NP_COMMUNITY_ERROR_NOT_INITIALIZED);
    CHECK_S32(sceNpScoreInit(), CELL_OK);
    CHECK_S32(sceNpTerm(), CELL_OK);
    CHECK_S32(sceNpScoreInit(), SCE_NP_ERROR_NOT_INITIALIZED);

    CHECK_S32(_sys_spu_printf_initialize(0x1000, 0x2000, 0x3000, 0x4000), CELL_OK);
    CHECK_U32(g_spu_printf_agcb, 0x1000);
    CHECK_U32(g_spu_printf_dgcb, 0x2000);
    CHECK_U32(g_spu_printf_atcb, 0x3000);
    CHECK_U32(g_spu_printf_dtcb, 0x4000);
    CHECK_S32(_sys_spu_printf_finalize(), CELL_OK);
    CHECK_U32(g_spu_printf_agcb | g_spu_printf_dgcb |
              g_spu_printf_atcb | g_spu_printf_dtcb, 0);

    puts("PASS: Tokyo Jungle startup imports preserve required lifecycle state");
    return 0;
}
