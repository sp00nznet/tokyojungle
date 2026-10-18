#include "rsx_stream_hash.h"

#include <stdint.h>
#include <stdio.h>
#include <string.h>

static int fail(const char* test)
{
    fprintf(stderr, "FAIL: %s\n", test);
    return 1;
}

static uint64_t hash_rows(const uint8_t* source, size_t pitch,
                          size_t row_bytes, size_t rows)
{
    rsx_stream_hash64_state state;
    rsx_stream_hash64_init(&state);
    for (size_t row = 0; row < rows; ++row)
        rsx_stream_hash64_update(&state, source + row * pitch, row_bytes);
    return rsx_stream_hash64_digest(&state);
}

int main(void)
{
    static const uint8_t short_a[] = {'a'};
    if (rsx_stream_hash64(NULL, 0) != UINT64_C(0xEF46DB3751D8E999) ||
        rsx_stream_hash64(short_a, sizeof(short_a)) != UINT64_C(0xD24EC4F1A98C6E5B))
        return fail("known empty/short XXH64 vectors");

    uint8_t bytes[129];
    for (size_t i = 0; i < sizeof(bytes); ++i) bytes[i] = (uint8_t)(i * 37u + 11u);
    const uint64_t whole = rsx_stream_hash64(bytes, sizeof(bytes));
    for (size_t split = 0; split <= sizeof(bytes); ++split) {
        rsx_stream_hash64_state state;
        rsx_stream_hash64_init(&state);
        rsx_stream_hash64_update(&state, bytes, split);
        rsx_stream_hash64_update(&state, bytes + split, sizeof(bytes) - split);
        if (rsx_stream_hash64_digest(&state) != whole)
            return fail("one-shot versus every two-chunk split");
    }

    uint8_t changed[sizeof(bytes)];
    memcpy(changed, bytes, sizeof(changed));
    changed[0] ^= 1;
    if (rsx_stream_hash64(changed, sizeof(changed)) == whole)
        return fail("first-byte mutation");
    memcpy(changed, bytes, sizeof(changed));
    changed[sizeof(changed) / 2] ^= 1;
    if (rsx_stream_hash64(changed, sizeof(changed)) == whole)
        return fail("middle-byte mutation");
    memcpy(changed, bytes, sizeof(changed));
    changed[sizeof(changed) - 1] ^= 1;
    if (rsx_stream_hash64(changed, sizeof(changed)) == whole)
        return fail("last-byte mutation");

    uint8_t pitched[3][8] = {
        {1, 2, 3, 4, 5, 0xA1, 0xA2, 0xA3},
        {6, 7, 8, 9, 10, 0xB1, 0xB2, 0xB3},
        {11, 12, 13, 14, 15, 0xC1, 0xC2, 0xC3}
    };
    uint8_t packed[15];
    for (size_t row = 0; row < 3; ++row)
        memcpy(packed + row * 5, pitched[row], 5);
    const uint64_t rows_hash = hash_rows(&pitched[0][0], 8, 5, 3);
    if (rows_hash != rsx_stream_hash64(packed, sizeof(packed)))
        return fail("pitched logical rows match packed source");
    pitched[1][7] ^= 0xFF;
    if (hash_rows(&pitched[0][0], 8, 5, 3) != rows_hash)
        return fail("pitched padding excluded");
    pitched[2][4] ^= 0xFF;
    if (hash_rows(&pitched[0][0], 8, 5, 3) == rows_hash)
        return fail("pitched logical byte included");

    uint8_t unaligned[66];
    for (size_t i = 0; i < sizeof(unaligned); ++i) unaligned[i] = (uint8_t)(0xD0u + i);
    const uint64_t unaligned_whole = rsx_stream_hash64(unaligned + 1, 64);
    rsx_stream_hash64_state unaligned_state;
    rsx_stream_hash64_init(&unaligned_state);
    rsx_stream_hash64_update(&unaligned_state, NULL, 0);
    rsx_stream_hash64_update(&unaligned_state, unaligned + 1, 17);
    rsx_stream_hash64_update(&unaligned_state, unaligned + 18, 47);
    if (rsx_stream_hash64_digest(&unaligned_state) != unaligned_whole)
        return fail("unaligned input plus zero-length update");

    puts("PASS: rsx streaming XXH64-style content hash");
    return 0;
}
