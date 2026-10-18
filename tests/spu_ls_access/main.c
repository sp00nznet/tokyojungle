/* SPU LS 128-bit access parity and microbenchmark.
 *
 * The reference keeps the former four-word BE<->LE implementation.  The
 * production helpers are exercised separately so byte/lane semantics and LS
 * address alignment are checked before timing the swizzle core. */
#include "spu_context.h"

#include <stdio.h>
#include <string.h>
#ifdef _WIN32
#include <windows.h>
#endif

/* spu_context.h's optional WWS probes reference these when the helper is
 * inlined into this standalone executable.  image_id/policy_mode stay zero,
 * so the probes are never entered by this test. */
int g_wws_read_probe;
int g_wws_code_probe;

static u128 scalar_read128(const spu_context* ctx, uint32_t lsa)
{
    const uint8_t* p = &ctx->ls[(lsa & SPU_LS_MASK) & ~0xFu];
    u128 v;
    for (int i = 0; i < 4; ++i) {
        v._u32[i] = ((uint32_t)p[i * 4] << 24) |
                    ((uint32_t)p[i * 4 + 1] << 16) |
                    ((uint32_t)p[i * 4 + 2] << 8) |
                    (uint32_t)p[i * 4 + 3];
    }
    return v;
}

static void scalar_write128(spu_context* ctx, uint32_t lsa, u128 v)
{
    uint8_t* p = &ctx->ls[(lsa & SPU_LS_MASK) & ~0xFu];
    for (int i = 0; i < 4; ++i) {
        const uint32_t w = v._u32[i];
        p[i * 4] = (uint8_t)(w >> 24);
        p[i * 4 + 1] = (uint8_t)(w >> 16);
        p[i * 4 + 2] = (uint8_t)(w >> 8);
        p[i * 4 + 3] = (uint8_t)w;
    }
}

static uint32_t next_u32(uint32_t* state)
{
    *state = *state * 1664525u + 1013904223u;
    return *state;
}

static u128 next_u128(uint32_t* state)
{
    u128 v;
    for (int i = 0; i < 4; ++i) v._u32[i] = next_u32(state);
    return v;
}

static int equal_u128(u128 a, u128 b)
{
    return memcmp(&a, &b, sizeof(a)) == 0;
}

static double now_seconds(void)
{
#ifdef _WIN32
    LARGE_INTEGER frequency, counter;
    QueryPerformanceFrequency(&frequency);
    QueryPerformanceCounter(&counter);
    return (double)counter.QuadPart / (double)frequency.QuadPart;
#else
    return 0.0;
#endif
}

static volatile uint64_t g_sink;
static volatile uint8_t g_bench_byte;

static double bench_ssse3(uint32_t iterations)
{
    uint8_t ls[SPU_LS_SIZE] = {0};
    u128 input = {0};
    input._u32[0] = 0x01234567u;
    input._u32[1] = 0x89ABCDEFu;
    input._u32[2] = 0x13579BDFu;
    input._u32[3] = 0x2468ACE0u;
    const double start = now_seconds();
    for (uint32_t i = 0; i < iterations; ++i) {
#if defined(SPU_LS_SSSE3)
        uint8_t* p = &ls[(i * 16u) & SPU_LS_MASK];
        const __m128i lanes = _mm_loadu_si128((const __m128i*)&input);
        const __m128i word_reverse = _mm_setr_epi8(
            3, 2, 1, 0, 7, 6, 5, 4, 11, 10, 9, 8, 15, 14, 13, 12);
        _mm_storeu_si128((__m128i*)p, _mm_shuffle_epi8(lanes, word_reverse));
        /* A volatile byte makes each round observable to the compiler and
         * prevents it from proving the store/load shuffle pair is an identity. */
        p[0] = g_bench_byte++;
        const __m128i be = _mm_loadu_si128((const __m128i*)p);
        _mm_storeu_si128((__m128i*)&input, _mm_shuffle_epi8(be, word_reverse));
#else
        uint8_t* p = &ls[(i * 16u) & SPU_LS_MASK];
        for (int lane = 0; lane < 4; ++lane) {
            const uint32_t w = input._u32[lane];
            p[lane * 4] = (uint8_t)(w >> 24);
            p[lane * 4 + 1] = (uint8_t)(w >> 16);
            p[lane * 4 + 2] = (uint8_t)(w >> 8);
            p[lane * 4 + 3] = (uint8_t)w;
        }
        p[0] = g_bench_byte++;
        for (int lane = 0; lane < 4; ++lane) {
            input._u32[lane] = ((uint32_t)p[lane * 4] << 24) |
                                ((uint32_t)p[lane * 4 + 1] << 16) |
                                ((uint32_t)p[lane * 4 + 2] << 8) |
                                (uint32_t)p[lane * 4 + 3];
        }
#endif
    }
    g_sink ^= input._u64[0];
    return now_seconds() - start;
}

static double bench_scalar(uint32_t iterations)
{
    uint8_t ls[SPU_LS_SIZE] = {0};
    u128 input = {0};
    input._u32[0] = 0x01234567u;
    input._u32[1] = 0x89ABCDEFu;
    input._u32[2] = 0x13579BDFu;
    input._u32[3] = 0x2468ACE0u;
    const double start = now_seconds();
    for (uint32_t i = 0; i < iterations; ++i) {
        uint8_t* p = &ls[(i * 16u) & SPU_LS_MASK];
        for (int lane = 0; lane < 4; ++lane) {
            const uint32_t w = input._u32[lane];
            p[lane * 4] = (uint8_t)(w >> 24);
            p[lane * 4 + 1] = (uint8_t)(w >> 16);
            p[lane * 4 + 2] = (uint8_t)(w >> 8);
            p[lane * 4 + 3] = (uint8_t)w;
        }
        p[0] = g_bench_byte++;
        for (int lane = 0; lane < 4; ++lane) {
            input._u32[lane] = ((uint32_t)p[lane * 4] << 24) |
                                ((uint32_t)p[lane * 4 + 1] << 16) |
                                ((uint32_t)p[lane * 4 + 2] << 8) |
                                (uint32_t)p[lane * 4 + 3];
        }
    }
    g_sink ^= input._u64[0];
    return now_seconds() - start;
}

int main(void)
{
    static const uint32_t addresses[] = {
        0, 1, 15, 16, 17, 0xDF0, 0x1320,
        SPU_LS_SIZE - 17, SPU_LS_SIZE - 16, SPU_LS_SIZE - 1,
        SPU_LS_SIZE, SPU_LS_SIZE + 15,
    };
    spu_context fast = {0};
    spu_context reference = {0};
    uint32_t seed = 0xC0DEC0DEu;

    for (unsigned round = 0; round < 256; ++round) {
        const u128 value = next_u128(&seed);
        for (unsigned i = 0; i < sizeof(addresses) / sizeof(addresses[0]); ++i) {
            const uint32_t address = addresses[i];
            spu_ls_write128(&fast, address, value);
            scalar_write128(&reference, address, value);
            const uint32_t line = (address & SPU_LS_MASK) & ~0xFu;
            if (memcmp(&fast.ls[line], &reference.ls[line], 16) != 0 ||
                !equal_u128(spu_ls_read128(&fast, address),
                            scalar_read128(&reference, address))) {
                fprintf(stderr, "FAIL: LS 128-bit parity at round=%u address=0x%X\n",
                        round, address);
                return 1;
            }
        }
    }

#if defined(SPU_LS_SSSE3)
    const uint32_t iterations = 20000000u;
    const double scalar = bench_scalar(iterations);
    const double ssse3 = bench_ssse3(iterations);
    printf("PASS: %u LS read/write vectors match scalar reference\n",
           256u * (unsigned)(sizeof(addresses) / sizeof(addresses[0])));
    printf("LS swizzle microbenchmark: scalar %.3fs, SSSE3 %.3fs, %.2fx (sink=%llX)\n",
           scalar, ssse3, scalar / ssse3, (unsigned long long)g_sink);
#else
    puts("PASS: LS 128-bit parity (SSSE3 benchmark unavailable on this compiler target)");
#endif
    return 0;
}
