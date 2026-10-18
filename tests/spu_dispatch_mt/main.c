#include "spu_context.h"

#include <stdint.h>
#include <stdio.h>
#include <string.h>

typedef void (*spu_dispatch_fn)(spu_context*);

static int fast_calls;
static int slow_calls;

static void fast_target(spu_context* ctx)
{
    (void)ctx;
    fast_calls++;
}

spu_dispatch_fn spu_lookup(uint32_t addr, int image_id)
{
    (void)image_id;
    return addr == 0 || addr == 0x100 ? fast_target : NULL;
}

void spu_indirect_branch(spu_context* ctx)
{
    (void)ctx;
    slow_calls++;
}

void spu_indirect_branch_mt(spu_context* ctx);

static int expect(const char* name, int actual, int expected)
{
    if (actual == expected)
        return 0;
    fprintf(stderr, "FAIL: %s actual=%d expected=%d\n", name, actual, expected);
    return 1;
}

int main(void)
{
    int failed = 0;
    spu_context ctx;
    memset(&ctx, 0, sizeof(ctx));

    /* Tokyo's first SGX job ends by branching to LS 0. Even though image 1
     * has lifted code at zero, this must reach the full resolver's job-return
     * interception instead of restarting the job CRT. */
    ctx.image_id = 1;
    ctx.pc = 0;
    spu_indirect_branch_mt(&ctx);
    failed |= expect("direct job PC zero bypasses fast lookup", fast_calls, 0);
    failed |= expect("direct job PC zero reaches manager return", slow_calls, 1);

    ctx.pc = 0x100;
    spu_indirect_branch_mt(&ctx);
    failed |= expect("ordinary direct-job branch uses fast lookup", fast_calls, 1);
    failed |= expect("ordinary direct-job branch avoids slow resolver", slow_calls, 1);

    memset(&ctx, 0, sizeof(ctx));
    ctx.image_id = 0;
    ctx.pc = 0;
    spu_indirect_branch_mt(&ctx);
    failed |= expect("base image zero remains a valid fast target", fast_calls, 2);
    failed |= expect("base image zero does not fake a job return", slow_calls, 1);

    if (failed)
        return 1;

    puts("PASS: musttail SPU dispatcher preserves return-to-manager at LS 0");
    return 0;
}
