#include "audio_pacing.h"

#include <stdint.h>
#include <stdio.h>

static int expect(const char* name, int actual, int expected)
{
    if (actual == expected)
        return 0;
    fprintf(stderr, "FAIL: %s actual=%d expected=%d\n", name, actual, expected);
    return 1;
}

static int expect_u64(const char* name, uint64_t actual, uint64_t expected)
{
    if (actual == expected)
        return 0;
    fprintf(stderr, "FAIL: %s actual=%llu expected=%llu\n", name,
            (unsigned long long)actual, (unsigned long long)expected);
    return 1;
}

int main(void)
{
    int failed = 0;

    failed |= expect("empty 20ms WASAPI buffer",
                     ps3_audio_full_block_fits(960, 0), 1);
    failed |= expect("exactly one 256-frame block free",
                     ps3_audio_full_block_fits(960, 704), 1);
    failed |= expect("255 frames free",
                     ps3_audio_full_block_fits(960, 705), 0);
    failed |= expect("full buffer",
                     ps3_audio_full_block_fits(960, 960), 0);
    failed |= expect("failed padding query sentinel",
                     ps3_audio_full_block_fits(960, UINT32_MAX), 0);

    failed |= expect_u64("block 0 tag at counter 17",
                         ps3_audio_port_block_tag(17, 1, 0), 16);
    failed |= expect_u64("current block tag at counter 17",
                         ps3_audio_port_block_tag(17, 1, 1), 17);
    failed |= expect_u64("last block tag at counter 17",
                         ps3_audio_port_block_tag(17, 1, 15), 31);

    failed |= expect_u64("ring position advances",
                         ps3_audio_next_ring_position(3, 16), 4);
    failed |= expect_u64("ring position wraps instead of publishing tag",
                         ps3_audio_next_ring_position(15, 16), 0);

    /* Tokyo Jungle keeps its catch-up cursors in the 0..15 ring domain. A
     * published monotonic counter can never compare equal after the first
     * wrap. Keep ring position and block-tag time as separate state. */
    uint64_t tokyo_tag = ps3_audio_port_block_tag(399, 0, 2);
    failed |= expect_u64("Tokyo two-block-ahead tag", tokyo_tag, 401);

    uint64_t stamp = 0;
    failed |= expect("past audio tag accepted",
                     ps3_audio_tag_timestamp(100, 98, 1000000, &stamp), 1);
    failed |= expect_u64("past audio tag timestamp", stamp, 989334);
    failed |= expect("future audio tag rejected",
                     ps3_audio_tag_timestamp(100, 101, 1000000, &stamp), 0);
    failed |= expect("Tokyo target initially pending",
                     ps3_audio_tag_timestamp(399, tokyo_tag, 1000000, &stamp), 0);
    failed |= expect("Tokyo target accepted two periods later",
                     ps3_audio_tag_timestamp(401, tokyo_tag, 1010666, &stamp), 1);
    failed |= expect_u64("Tokyo target timestamp", stamp, 1010666);

    if (failed)
        return 1;

    puts("PASS: cellAudio pacing and port tag/timestamp math");
    return 0;
}
