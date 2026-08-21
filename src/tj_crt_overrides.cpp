/* CRT bring-up overrides.
 *
 * These five guest functions are the PS3 libc/CRT init chain. Their real
 * bodies make lv2 calls and touch kernel structures the HLE runtime does not
 * model, so the stdio initialiser abort()s partway through _start. Skipping
 * them (returning 0) is what the old generated/ppu_stubs.c did by replacing
 * the symbols at link time; the runtime is not yet far enough along to run
 * them for real.
 *
 * The CRT chain calls them DIRECTLY (`func_0024DE14(ctx)` in the lifted
 * code), so registering an override with ppu_register_function does not
 * help -- that only intercepts indirect calls through ppu_lookup. Instead
 * scripts/post_lift.py rewrites each of these bodies into a one-line
 * call to tj_crt_skip(), the same body-redirect patch Rubber Ducky uses for
 * its allocator overrides.
 *
 * ponytail: drop entries from post_lift.py's CRT_SKIPS as the runtime grows
 * to support them; each one removed is real CRT code that starts running.
 */
#include "recomp_bridge.h"
#include <stdio.h>

void tj_crt_skip(ppu_context* ctx, const char* what)
{
    static int hits;
    if (hits < 64) { printf("[CRT] skip %s\n", what); hits++; }
    ctx->gpr[3] = 0;
}
