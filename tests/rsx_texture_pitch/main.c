#include "rsx_commands.h"

#include <stdint.h>
#include <stdio.h>

void vm_write32(uint32_t address, uint32_t value)
{
    (void)address;
    (void)value;
}

static int expect_u32(const char* name, uint32_t actual, uint32_t expected)
{
    if (actual == expected)
        return 0;

    fprintf(stderr, "FAIL: %s actual=0x%08X expected=0x%08X\n",
            name, actual, expected);
    return 1;
}

int main(void)
{
    rsx_state state;
    rsx_state_init(&state);

    const uint32_t unit = 7;
    const uint32_t pitch = 0x1234;
    const uint32_t depth = 0xABC;
    /* Bits 16..19 are reserved and must not leak into the 16-bit pitch. */
    const uint32_t value = (depth << 20) | 0xF0000u | pitch;

    if (rsx_process_method(&state,
                           NV4097_SET_TEXTURE_SIZE1 + unit * 4,
                           value) != 0) {
        fprintf(stderr, "FAIL: TEXTURE_SIZE1 method was rejected\n");
        return 1;
    }

    int failed = 0;
    failed |= expect_u32("selected pitch", state.textures[unit].pitch, pitch);
    failed |= expect_u32("selected depth", state.textures[unit].depth, depth);
    failed |= expect_u32("selected dirty", state.textures[unit].dirty, 1);
    failed |= expect_u32("global texture dirty", state.texture_dirty, 1);
    failed |= expect_u32("neighbor pitch", state.textures[unit - 1].pitch, 8);
    failed |= expect_u32("neighbor depth", state.textures[unit + 1].depth, 1);

    if (failed)
        return 1;

    puts("PASS: NV4097 TEXTURE_SIZE1 pitch/depth parsing");
    return 0;
}
