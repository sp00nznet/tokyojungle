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
#include <stdlib.h>
#include <windows.h>
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
 * (func_0023D1FC) then throws and the game aborts with "exception: bad\n * allocation" before it ever reaches graphics init.
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

/* func_0024DCEC is memalign(alignment, size): both observed call sites pass a
 * constant r3=0x80. Treating a large alignment as a calloc element count sent
 * the 1 MB-aligned RSX IO region down the calloc path and returned an
 * unaligned pointer -- the FIFO then overlapped live objects and the game
 * walked its own command buffer as an array of object pointers.
 *
 * Zero the block anyway: memalign does not promise it, but the guest heap this
 * replaces handed back memory the game expected to be clean, and a stale
 * pointer read out of a fresh allocation is far more expensive to debug than
 * the memset. */
void tj_heap_memalign(ppu_context* ctx)
{
    uint32_t align = (uint32_t)ctx->gpr[3];
    uint32_t size  = (uint32_t)ctx->gpr[4];
    uint32_t p = hle_memalign(align, size);
    if (p) memset(vm_base + p, 0, size);
    tj_heap_log("memalign", align, size, p);
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

/* ---------------------------------------------------------------------------
 * abort() with a usable backtrace
 *
 * The guest's own abort prints "abort() is called from 0x..." by walking its
 * stack -- and when the reason for aborting is a corrupted stack, that walker
 * runs off into garbage and takes an access violation, so the crash we see is
 * the reporter, not the fault. Redirect abort to the runtime's own guest-stack
 * scanner (it resolves saved return addresses through function_table) and then
 * leave, so the log names the actual guest chain.
 */
extern "C" void ppu_dump_guest_stack(ppu_context* ctx, const char* tag);

/* The HOST stack is the guest call chain: lifted functions call each other
 * directly, so a host return address inside func_XXXXXXXX's body identifies
 * that guest function. Resolve each frame to the entry whose body address is
 * the largest <= the return address. This works where the guest-stack scan
 * does not, because the lifted ABI does not maintain guest lr. */
static uint32_t tj_guest_of_host_ra(uintptr_t ra, uintptr_t* off)
{
    uintptr_t best = 0;
    uint32_t best_guest = 0;
    for (uint64_t i = 0; i < function_table_count; i++) {
        uintptr_t hf = (uintptr_t)function_table[i].func;
        if (hf && hf <= ra && hf > best) {
            best = hf;
            best_guest = (uint32_t)function_table[i].addr;
        }
    }
    if (off) *off = ra - best;
    return best_guest;
}

void tj_abort(ppu_context* ctx)
{
    fprintf(stderr, "\n[TJ] guest abort() -- r3=0x%08X r4=0x%08X r31=0x%08X\n",
            (uint32_t)ctx->gpr[3], (uint32_t)ctx->gpr[4], (uint32_t)ctx->gpr[31]);

    /* The pure-virtual placeholder reaches abort with `this` still in r3.
     * Dump the object head and its vtable: a vtable slot pointing back at
     * func_0025279C is __cxa_pure_virtual, i.e. this object still carries an
     * abstract base's vptr because the concrete subclass was never
     * constructed (or its constructor did not complete). */
    {
        uint32_t obj = (uint32_t)ctx->gpr[3];
        uint32_t vt  = obj ? vm_read32(obj) : 0;
        fprintf(stderr, "[TJ] this=0x%08X vptr=0x%08X\n", obj, vt);
        if (obj) {
            fprintf(stderr, "      obj[0..7]:");
            for (int i = 0; i < 8; i++) fprintf(stderr, " %08X", vm_read32(obj + i*4));
            fprintf(stderr, "\n");
        }
        if (vt) {
            fprintf(stderr, "      vtable[0..9]:");
            for (int i = 0; i < 10; i++) fprintf(stderr, " %08X", vm_read32(vt + i*4));
            fprintf(stderr, "\n");
        }
    }

    void* bt[64];
    unsigned short n = RtlCaptureStackBackTrace(0, 64, bt, 0);
    fprintf(stderr, "[TJ] guest chain (resolved from the host stack):\n");
    for (unsigned short i = 0; i < n; i++) {
        uintptr_t off = 0;
        uint32_t g = tj_guest_of_host_ra((uintptr_t)bt[i], &off);
        if (g && off < 0x4000)
            fprintf(stderr, "      func_%08X+0x%llX\n", g,
                    (unsigned long long)off);
    }
    fflush(stderr);
    exit(4);
}

/* __cxa_pure_virtual (func_0025279C) -- called when a virtual dispatch lands
 * on a slot the class leaves pure. The display object at 0x00BCB2A0 carries
 * vtable 0x00342130, whose slots 5 and 6 are pure and for which this binary
 * contains no derived class, so reaching one means the boot took a path it
 * should not have. The real fix is upstream of here; treating it as fatal
 * just hides everything that follows, so log it (with `this`) and return 0
 * the way the runtime's garbage-vcall guard does.
 *
 * ponytail: diagnostic band-aid. Skipped methods leave state incomplete --
 * remove this once the path that reaches a pure slot is understood.
 */
void tj_pure_virtual(ppu_context* ctx)
{
    static int hits;
    if (hits < 16) {
        fprintf(stderr, "[TJ] pure virtual on this=0x%08X (vptr=0x%08X) lr=0x%08X -- returning 0\n",
                (uint32_t)ctx->gpr[3],
                ctx->gpr[3] ? vm_read32((uint32_t)ctx->gpr[3]) : 0, (uint32_t)ctx->lr);
        hits++;
    }
    ctx->gpr[3] = 0;
}

/* Guest chain for a host access violation.
 *
 * Must be called from the __except FILTER, not the handler body: the filter
 * runs on the faulting stack before unwinding, so the lifted guest frames are
 * still there to walk. By the time the handler body runs they are gone.
 */
/* TJ_PEEK=<hex>[,<words>]: dump guest memory at the fault. Most "wild pointer"
 * faults trace back to a table the guest built, and the fastest way to see
 * which entry is wrong is to look at it. */
static void tj_peek(void)
{
    const char* e = getenv("TJ_PEEK");
    if (!e || !*e) return;
    char* end = 0;
    unsigned long addr = strtoul(e, &end, 16);
    unsigned n = 16;
    if (end && *end == ',') n = (unsigned)strtoul(end + 1, 0, 0);
    if (n > 256) n = 256;
    fprintf(stderr, "[TJ] peek 0x%08lX:", addr);
    for (unsigned i = 0; i < n; i++) {
        if (i % 8 == 0) fprintf(stderr, "\n   +0x%03X:", i * 4);
        fprintf(stderr, " %08X", vm_read32((uint32_t)addr + i * 4));
    }
    fprintf(stderr, "\n");
}

extern "C" int tj_crash_filter(unsigned long code)
{
    tj_peek();
    void* bt[64];
    unsigned short n = RtlCaptureStackBackTrace(0, 64, bt, 0);
    fprintf(stderr, "\n[TJ] fault 0x%08lX -- guest chain:\n", code);
    for (unsigned short i = 0; i < n; i++) {
        uintptr_t off = 0;
        uint32_t g = tj_guest_of_host_ra((uintptr_t)bt[i], &off);
        if (g && off < 0x4000)
            fprintf(stderr, "      func_%08X+0x%llX\n", g,
                    (unsigned long long)off);
    }
    fflush(stderr);
    return 1;   /* EXCEPTION_EXECUTE_HANDLER */
}
