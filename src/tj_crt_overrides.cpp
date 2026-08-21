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
#include <string.h>

void tj_crt_skip(ppu_context* ctx, const char* what)
{
    static int hits;
    if (hits < 64) { printf("[CRT] skip %s\n", what); hits++; }
    ctx->gpr[3] = 0;
}

/* ---------------------------------------------------------------------------
 * libc heap redirect
 *
 * The game's allocator is dlmalloc over a single mspace whose pointer is baked
 * into .data at TOC+0x2A98 = 0x0035BCB8 -> 0x00C0E000, the last object in BSS.
 * That malloc_state is never initialised: it reads as all zeroes for the whole
 * boot, so ok_magic() fails and every allocation returns 0. operator new
 * (func_0023D1FC) then throws and the game aborts with "exception: bad
 * allocation" before it ever reaches graphics init.
 *
 * Until the mspace is genuinely created, point the eight public heap wrappers
 * at the HLE bump allocator (import_resolver.h). Every one of them currently
 * returns 0, so this cannot regress anything.
 *
 * The wrappers were identified from the binary, not from the (wrong) comment
 * tables -- all eight load the mspace handle from TOC+0x2A98 as their first
 * argument:
 *   0x24DDC0 malloc(size)          25 call sites
 *   0x24DD94 free(ptr)            100 call sites; its inner func_0024A7E8 is
 *                                 the one that carries the "mspace_free" string
 *   0x24DCEC memalign(align,size)  50 call sites, both observed passing a
 *                                 constant r3=0x80 -- a 128-byte alignment
 *   0x24DD24 realloc(ptr,size)     its inner calls a free wrapper
 *   0x24DCBC free (internal)       only called from realloc's inner
 *
 * ponytail: bump allocator, no reuse. Fine for bring-up; give the guest mspace
 * real memory when the boot gets far enough to care about fragmentation.
 */
/* Defined in src/import_resolver.h, compiled into main.cpp as C++ -- so these
 * are C++-mangled, not extern "C". */
uint32_t hle_malloc(uint32_t size);
uint32_t hle_memalign(uint32_t align, uint32_t size);
uint32_t hle_calloc(uint32_t count, uint32_t size);
void     hle_free(uint32_t addr);

static void tj_heap_log(const char* what, uint32_t a, uint32_t b, uint32_t r)
{
    static int hits;
    if (hits < 24) { printf("[heap] %s(0x%X, 0x%X) -> 0x%08X\n", what, a, b, r); hits++; }
}

void tj_heap_malloc(ppu_context* ctx)
{
    uint32_t n = (uint32_t)ctx->gpr[3];
    uint32_t p = hle_malloc(n);
    tj_heap_log("malloc", n, 0, p);
    ctx->gpr[3] = p;
}

void tj_heap_free(ppu_context* ctx)
{
    hle_free((uint32_t)ctx->gpr[3]);
    ctx->gpr[3] = 0;
}

/* r3 is an alignment when it is a small power of two -- alignments always are,
 * and a calloc element count almost never is. Zero either way: memalign does
 * not require it, calloc does, and the cost is not worth a wrong guess. */
void tj_heap_memalign(ppu_context* ctx)
{
    uint32_t a = (uint32_t)ctx->gpr[3];
    uint32_t b = (uint32_t)ctx->gpr[4];
    bool align_like = a && a <= 0x1000 && (a & (a - 1)) == 0;
    uint32_t p = align_like ? hle_memalign(a, b) : hle_calloc(a, b);
    if (p && align_like) memset(vm_base + p, 0, b);
    tj_heap_log(align_like ? "memalign" : "calloc", a, b, p);
    ctx->gpr[3] = p;
}

/* Bump allocator never reuses, so realloc is allocate-and-copy. The old size
 * is unknown, so copy the new size -- safe here because the source is always
 * inside the committed 96 MB heap. */
void tj_heap_realloc(ppu_context* ctx)
{
    uint32_t old = (uint32_t)ctx->gpr[3];
    uint32_t n   = (uint32_t)ctx->gpr[4];
    uint32_t p   = hle_malloc(n);
    if (p && old) memcpy(vm_base + p, vm_base + old, n);
    tj_heap_log("realloc", old, n, p);
    ctx->gpr[3] = p;
}
