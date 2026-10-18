#include "spu_smc_ops.h"
#include "spu_dma.h"
#include <stdio.h>
#include <string.h>

/* spu_context.h's diagnostic LS helpers reference the runtime-owned counters.
 * This standalone compile target does not link spu_channels.c. */
int g_wws_code_probe = 0;
int g_wws_read_probe = 0;

static int same_u128(u128 a, u128 b)
{
    return memcmp(&a, &b, sizeof(a)) == 0;
}

static int fail(const char* message)
{
    fprintf(stderr, "FAIL: %s\n", message);
    return 1;
}

int main(void)
{
    spu_context ctx;
    memset(&ctx, 0, sizeof(ctx));

    if (!mfc_ea_range_overlaps_reserved_hole(0x4FFFFFF0u, 0x20u) ||
        !mfc_ea_range_overlaps_reserved_hole(0x6FFFFFF0u, 0x20u) ||
        !mfc_ea_range_overlaps_reserved_hole(0xDFFFFFF0u, 0x20u) ||
        mfc_ea_range_overlaps_reserved_hole(0x4FFFE000u, 0x1000u) ||
        mfc_ea_range_overlaps_reserved_hole(0x6FFFE000u, 0x1000u) ||
        mfc_ea_range_overlaps_reserved_hole(0xDFFFE000u, 0x1000u))
        return fail("DMA reserved-hole overlap boundaries");

    ctx.gpr[1] = spu_splat_u32(0x1234);
    if (!spu_smc_execute_nonbranch(&ctx, 0x3E830084) ||
        !same_u128(ctx.gpr[4], spu_cbd(ctx.gpr[1], 12)))
        return fail("cbd $r4,$r1,12");
    if (!spu_smc_execute_nonbranch(&ctx, 0x3E834084) ||
        !same_u128(ctx.gpr[4], spu_cbd(ctx.gpr[1], 13)))
        return fail("cbd $r4,$r1,13");
    if (!spu_smc_execute_nonbranch(&ctx, 0x3EA200A7) ||
        !same_u128(ctx.gpr[39], spu_chd(ctx.gpr[1], 8)))
        return fail("chd $r39,$r1,8");
    for (int byte = 0; byte < 16; ++byte) {
        const uint8_t expected = byte == 12 ? 0x02 :
                                 byte == 13 ? 0x03 :
                                 (uint8_t)(0x10 + byte);
        if (ctx.gpr[39]._u8[SPU_W(byte)] != expected)
            return fail("chd runtime word produced wrong shuffle mask");
    }
    if (!spu_smc_execute_nonbranch(&ctx, (0x1F6u << 21) | (7u << 14) | (1u << 7) | 40u) ||
        !same_u128(ctx.gpr[40], spu_cwd(ctx.gpr[1], 7)))
        return fail("cwd $r40,$r1,7");
    if (!spu_smc_execute_nonbranch(&ctx, (0x1F7u << 21) | (6u << 14) | (1u << 7) | 41u) ||
        !same_u128(ctx.gpr[41], spu_cdd(ctx.gpr[1], 6)))
        return fail("cdd $r41,$r1,6");

    {
        for (int byte = 0; byte < 16; ++byte)
            ctx.gpr[47]._u8[SPU_W(byte)] = (uint8_t)(0x20 + byte);
        ctx.gpr[48] = spu_zero();
        ctx.gpr[48]._u32[0] = 5;
        if (!spu_smc_execute_nonbranch(&ctx, 0x3B8C17AD))
            return fail("rotqby $r45,$r47,$r48 was not accepted");
        for (int byte = 0; byte < 16; ++byte) {
            const uint8_t expected = (uint8_t)(0x20 + ((byte + 5) & 0x0F));
            if (ctx.gpr[45]._u8[SPU_W(byte)] != expected)
                return fail("rotqby runtime word produced wrong byte rotation");
        }
    }

    {
        static const uint8_t low_bytes[8] = {
            0x00, 0x01, 0x7F, 0x80, 0x81, 0xFE, 0xFF, 0x42,
        };
        u128 source = spu_zero();
        for (int half = 0; half < 8; ++half) {
            source._u8[2 * half] = low_bytes[half];
            source._u8[2 * half + 1] = (uint8_t)(0xA0 + half);
        }
        ctx.gpr[50] = source;
        if (!spu_smc_execute_nonbranch(&ctx, 0x56C01931) ||
            !same_u128(ctx.gpr[49], spu_xsbh(source)))
            return fail("Tokyo image2 xsbh $r49,$r50 word 0x56C01931");
    }

    {
        static const uint8_t control[16] = {
            0x00, 0x0F, 0x10, 0x1F, 0x80, 0xC0, 0xE0, 0xE1,
            0x02, 0x12, 0x0D, 0x1D, 0x9F, 0xDF, 0xFF, 0x07,
        };
        static const uint8_t expected[16] = {
            0x10, 0x1F, 0x40, 0x4F, 0x00, 0xFF, 0x80, 0x80,
            0x12, 0x42, 0x1D, 0x4D, 0x00, 0xFF, 0x80, 0x17,
        };

        for (int byte = 0; byte < 16; ++byte) {
            ctx.gpr[36]._u8[SPU_W(byte)] = (uint8_t)(0x10 + byte);
            ctx.gpr[38]._u8[SPU_W(byte)] = (uint8_t)(0x40 + byte);
            ctx.gpr[39]._u8[SPU_W(byte)] = control[byte];
        }
        if (!spu_smc_execute_nonbranch(&ctx, 0xB4A99227))
            return fail("shufb $r37,$r36,$r38,$r39 was not accepted");
        for (int byte = 0; byte < 16; ++byte) {
            if (ctx.gpr[37]._u8[SPU_W(byte)] != expected[byte])
                return fail("shufb runtime word produced wrong byte");
        }
    }

    {
        static const uint8_t bits[16] = {
            0xFF, 0x0F, 0x03, 0x01, 0, 0, 0, 0,
            0, 0, 0, 0, 0, 0, 0, 0,
        };
        u128 source = spu_zero();
        for (int byte = 0; byte < 16; ++byte)
            source._u8[SPU_W(byte)] = bits[byte];
        ctx.gpr[11] = spu_cntb(source);
        ctx.gpr[34] = spu_ila(0x00010203);
        if (!spu_smc_execute_nonbranch(&ctx, 0xBB4885A2) ||
            !same_u128(ctx.gpr[90], spu_splat_u32(0x08040201)))
            return fail("Tokyo image17 cntb/shufb popcount packing word 0xBB4885A2");
    }

    for (int i = 0; i < 16; i++)
        ctx.gpr[8]._u8[i] = (uint8_t)(i * 7 + 3);
    if (!spu_smc_execute_nonbranch(&ctx, 0x3FE0040D) ||
        !same_u128(ctx.gpr[13], spu_shlqbyi(ctx.gpr[8], 0)))
        return fail("shlqbyi $r13,$r8,0");

    if (!spu_smc_execute_nonbranch(&ctx, 0x127F250A) ||
        !spu_smc_execute_nonbranch(&ctx, 0x12000909) ||
        !spu_smc_execute_nonbranch(&ctx, 0x127DBE0A))
        return fail("hbrr hints");
    if (spu_smc_execute_nonbranch(&ctx, 0xFFFFFFFF))
        return fail("unknown opcode accepted");

    puts("PASS: runtime-generated SPU stub cbd/chd/cwd/cdd/rotqby/xsbh/shufb/shlqbyi/hbrr words");
    return 0;
}
