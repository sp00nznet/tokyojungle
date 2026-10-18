"""Native ownership and bounded-copy benchmark for the exact-blit byte pool.

The C fixture extracts the production allocator/release helpers verbatim. It
does not model D3D or claim a game-frame speedup.
"""
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
VIDEO = ROOT.parent / "ps3recomp" / "libs" / "video"
CELL = VIDEO / "cellGcmSys.c"
BACKEND = VIDEO / "rsx_d3d12_backend.c"
HEADER = VIDEO / "rsx_d3d12_backend.h"


def function(source, signature):
    start = source.find(signature)
    while start >= 0:
        brace = source.find("{", start)
        semi = source.find(";", start)
        if brace >= 0 and (semi < 0 or brace < semi):
            depth, end = 1, brace + 1
            while depth:
                depth += (source[end] == "{") - (source[end] == "}")
                end += 1
            return source[start:end]
        start = source.find(signature, start + len(signature))
    raise ValueError(f"definition not found: {signature}")


def production_pool_block(cell):
    marker = cell.index("#define GCM2D_BLIT_SNAPSHOT_POOL_MAX_ENTRIES")
    start = cell.rfind("#ifdef _WIN32", 0, marker)
    end_marker = "#endif\n\n/* ---------------------------------------------------------------------------\n * Timestamp"
    end = cell.index(end_marker, marker) + len("#endif")
    block = cell[start:end]
    assert "rsx_d3d12_blit_snapshot_acquire" in block
    assert "rsx_d3d12_blit_snapshot_release" in block
    return block


def structural_checks(cell, backend, header):
    assert re.search(r"#define GCM2D_BLIT_SNAPSHOT_POOL_MAX_ENTRIES 16u\b", cell)
    assert re.search(r"#define GCM2D_BLIT_SNAPSHOT_POOL_MAX_BYTES \(32ULL << 20\)", cell)
    assert "getenv(\"TJ_BLIT_SNAPSHOT_POOL\")" in cell
    assert "strcmp(value, \"1\") == 0" in cell
    for api in ("acquire", "release", "pool_init", "pool_shutdown"):
        assert f"rsx_d3d12_blit_snapshot_{api}" in header

    # The production callers still take a fresh byte copy after each acquire.
    capture_start = cell.index("int gpu_snapshot_attempted = 0;")
    record_at = cell.index("rsx_d3d12_backend_record_exact_blit(&gpu)", capture_start)
    capture_end = cell.index("gcm2d_out:", record_at)
    capture_all = cell[capture_start:capture_end]
    capture = cell[capture_start:record_at]
    src_acquire = capture.index("gpu.src_snapshot = rsx_d3d12_blit_snapshot_acquire(")
    dst_acquire = capture.index("gpu.dst_snapshot = rsx_d3d12_blit_snapshot_acquire(")
    src_copy = capture.index("memcpy(gpu.src_snapshot, vm_ptr8(src_ea)")
    dst_copy = capture.index("memcpy(gpu.dst_snapshot, vm_ptr8(dst_ea)")
    post_acquire = capture.index("gpu.dst_post_snapshot = rsx_d3d12_blit_snapshot_acquire(")
    post_copy = capture.index("memcpy(gpu.dst_post_snapshot +")
    assert src_acquire < src_copy and dst_acquire < dst_copy
    assert post_acquire < post_copy
    assert "rsx_d3d12_blit_snapshot_release(gpu.src_snapshot" in capture
    assert "rsx_d3d12_blit_snapshot_release(gpu.dst_snapshot" in capture
    record_tail = capture_all[record_at - capture_start:]
    failure_start = record_tail.index("if (!gpu_snapshot_ready || !gpu_record_ok)")
    failure_end = record_tail.index("if (!gpu_exact_fault)", failure_start)
    failure = record_tail[failure_start:failure_end]
    assert failure.index("if (!gpu_snapshot_ready)") < failure.index(
        "rsx_d3d12_blit_snapshot_release(gpu.src_snapshot")
    assert failure.count("rsx_d3d12_blit_snapshot_release(") == 3

    record = function(backend, "int rsx_d3d12_backend_record_exact_blit(")
    assert "dr->scaled_blit = *blit;" in record
    assert "reject:" in record and "rsx_d3d12_blit_snapshot_release(blit->src_snapshot" in record
    release_range = function(backend, "static void gpu_blit_record_snapshots_release_range(")
    for field in ("src_snapshot", "dst_snapshot", "dst_post_snapshot"):
        assert f"rsx_d3d12_blit_snapshot_release(dr->scaled_blit.{field}" in release_range
    assert "dr->scaled_blit.src_snapshot = dr->scaled_blit.dst_snapshot = NULL;" in release_range

    # Pre-copy source/destination bytes are consumed during record replay and
    # retired before command submission. Packed MAIN baseline ownership moves
    # to the writeback plan and is retired only after checked commit/failure.
    render = function(backend, "static void render_frame(")
    release_at = render.index("gpu_blit_record_snapshots_release();")
    execute_at = render.index("ExecuteCommandLists", release_at)
    assert render.index("gpu_blit_execute") < release_at < execute_at
    transfer = function(backend, "static void exact_writeback_transfer_baseline(")
    assert "p->baseline = b->dst_post_snapshot;" in transfer
    assert "b->dst_post_snapshot = NULL;" in transfer
    baseline_release = function(backend, "static void exact_writeback_release(")
    assert "rsx_d3d12_blit_snapshot_release(p->baseline, p->packed_bytes)" in baseline_release
    assert "p->packed_bytes" in function(backend, "static int exact_writeback_commit_all(") or \
           "exact_writeback_commit_all(" in render
    fence = render.index("int fence_ok = wait_for_gpu_checked() == 0;")
    map_convert = render.index("exact_writeback_map_convert_all(", fence)
    commit = render.index("exact_writeback_commit_all(", map_convert)
    baseline_drop = render.index("if (fence_ok) exact_writeback_release();", commit)
    assert fence < map_convert < commit < baseline_drop
    assert "rsx_d3d12_blit_snapshot_pool_init();" in function(backend, "int rsx_d3d12_backend_init(")
    assert "rsx_d3d12_blit_snapshot_pool_shutdown();" in function(backend, "void rsx_d3d12_backend_shutdown(")


def fixture_source(pool_block):
    return r'''
#include <stdint.h>
#include <stddef.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdatomic.h>
#include <windows.h>
typedef uint8_t u8; typedef uint32_t u32; typedef uint64_t u64;
static volatile LONG64 fixture_malloc_calls, fixture_free_calls;
static volatile LONG fixture_fail_next;
static void* fixture_malloc(size_t n) {
    InterlockedIncrement64(&fixture_malloc_calls);
    if (InterlockedExchange(&fixture_fail_next, 0)) return NULL;
    return malloc(n);
}
static void fixture_free(void* p) {
    if (p) InterlockedIncrement64(&fixture_free_calls);
    free(p);
}
#define malloc fixture_malloc
#define free fixture_free
POOL_BLOCK
#undef malloc
#undef free

static volatile LONG checks, failures;
static volatile LONG64 sink;
static CRITICAL_SECTION active_lock;
#define THREADS 6
#define THREAD_LOOPS 500
static void* active_buffers[THREADS];
static void check(int ok, const char* label) {
    InterlockedIncrement(&checks);
    if (!ok) { InterlockedIncrement(&failures); printf("FAIL %s\n", label); }
}
static void env_value(const char* value) {
    (void)_putenv_s("TJ_BLIT_SNAPSHOT_POOL", value ? value : "");
}
static void start_pool(const char* value, int expected) {
    rsx_d3d12_blit_snapshot_pool_shutdown();
    env_value(value);
    rsx_d3d12_blit_snapshot_pool_init();
    check(atomic_load_explicit(&s_gcm2d_blit_snapshot_pool_enabled,
                               memory_order_acquire) == expected,
          "exact opt-in setting");
}
static void test_options_and_exact_size(void) {
    start_pool(NULL, 0);
    const LONG64 free0 = InterlockedCompareExchange64(&fixture_free_calls, 0, 0);
    u8* off = rsx_d3d12_blit_snapshot_acquire(73);
    check(off != NULL, "disabled pool still allocates");
    rsx_d3d12_blit_snapshot_release(off, 73);
    check(InterlockedCompareExchange64(&fixture_free_calls, 0, 0) == free0 + 1,
          "disabled release frees immediately");
    const LONG64 zero_mallocs = InterlockedCompareExchange64(&fixture_malloc_calls, 0, 0);
    check(rsx_d3d12_blit_snapshot_acquire(0) == NULL,
          "zero-size acquire is rejected");
    rsx_d3d12_blit_snapshot_release(NULL, 0);
    check(InterlockedCompareExchange64(&fixture_malloc_calls, 0, 0) == zero_mallocs,
          "zero-size and null release do not allocate");
    InterlockedExchange(&fixture_fail_next, 1);
    check(rsx_d3d12_blit_snapshot_acquire(79) == NULL,
          "disabled allocation failure returns without pool retry");
    check(InterlockedCompareExchange64(&fixture_malloc_calls, 0, 0) == zero_mallocs + 1,
          "disabled allocation failure makes one allocator call");
    start_pool("0", 0); start_pool("01", 0); start_pool("true", 0);
    start_pool("1", 1);
    u8* a = rsx_d3d12_blit_snapshot_acquire(257);
    check(a != NULL, "opt-in allocation succeeds");
    if (!a) return;
    memset(a, 0x31, 257);
    rsx_d3d12_blit_snapshot_release(a, 257);
    check(s_gcm2d_blit_snapshot_pool_count == 1 &&
          s_gcm2d_blit_snapshot_pool_bytes == 257,
          "released allocation is retained with exact size");
    u8* other = rsx_d3d12_blit_snapshot_acquire(128);
    check(other && other != a, "different size does not consume cached entry");
    rsx_d3d12_blit_snapshot_release(other, 128);
    const LONG64 malloc0 = InterlockedCompareExchange64(&fixture_malloc_calls, 0, 0);
    u8* reused = rsx_d3d12_blit_snapshot_acquire(257);
    check(reused == a, "exact size returns cached malloc pointer");
    check(InterlockedCompareExchange64(&fixture_malloc_calls, 0, 0) == malloc0,
          "exact-size hit avoids allocator call");
    check(reused && reused[0] == 0x31 && reused[256] == 0x31,
          "pool leaves old bytes untouched for caller overwrite");
    u8 fresh[257];
    for (unsigned i = 0; i < sizeof(fresh); ++i) fresh[i] = (u8)(i * 19u + 7u);
    memcpy(reused, fresh, sizeof(fresh));
    check(memcmp(reused, fresh, sizeof(fresh)) == 0,
          "full caller recopy replaces stale bytes");
    rsx_d3d12_blit_snapshot_release(reused, 257);
    rsx_d3d12_blit_snapshot_pool_shutdown();
}
static void test_live_ownership_and_shutdown(void) {
    start_pool("1", 1);
    u8* a = rsx_d3d12_blit_snapshot_acquire(192);
    u8* b = rsx_d3d12_blit_snapshot_acquire(192);
    check(a && b && a != b, "simultaneous live buffers are distinct");
    if (!a || !b) return;
    memset(b, 0x5a, 192);
    rsx_d3d12_blit_snapshot_release(a, 192);
    u8* again = rsx_d3d12_blit_snapshot_acquire(192);
    check(again == a && again != b, "cached buffer reuse cannot alias another live buffer");
    check(b[0] == 0x5a && b[191] == 0x5a, "live buffer remains owned and unchanged");
    rsx_d3d12_blit_snapshot_release(again, 192);
    rsx_d3d12_blit_snapshot_release(b, 192);

    u8* live = rsx_d3d12_blit_snapshot_acquire(4096);
    u8* cached = rsx_d3d12_blit_snapshot_acquire(2048);
    check(live && cached, "live shutdown buffers allocated");
    if (!live || !cached) return;
    memset(live, 0xc7, 4096);
    rsx_d3d12_blit_snapshot_release(cached, 2048);
    const u32 cached_entries = s_gcm2d_blit_snapshot_pool_count;
    const LONG64 frees = InterlockedCompareExchange64(&fixture_free_calls, 0, 0);
    rsx_d3d12_blit_snapshot_pool_shutdown();
    const LONG64 after_shutdown = InterlockedCompareExchange64(&fixture_free_calls, 0, 0);
    check(!atomic_load_explicit(&s_gcm2d_blit_snapshot_pool_enabled,
                                memory_order_acquire) &&
          s_gcm2d_blit_snapshot_pool_count == 0,
          "shutdown disables and drains cache entries");
    check(after_shutdown == frees + cached_entries,
          "shutdown frees cached allocation only");
    check(live[0] == 0xc7 && live[4095] == 0xc7,
          "shutdown leaves active caller buffer owned");
    rsx_d3d12_blit_snapshot_release(live, 4096);
    const LONG64 after_live_release = InterlockedCompareExchange64(&fixture_free_calls, 0, 0);
    check(after_live_release == frees + cached_entries + 1,
          "post-shutdown live release frees directly");

    env_value("1"); rsx_d3d12_blit_snapshot_pool_init();
    check(atomic_load_explicit(&s_gcm2d_blit_snapshot_pool_enabled,
                               memory_order_acquire), "pool can reinitialize after shutdown");
    u8* reinit = rsx_d3d12_blit_snapshot_acquire(4096);
    check(reinit != NULL, "reinitialized pool allocates");
    rsx_d3d12_blit_snapshot_release(reinit, 4096);
    check(s_gcm2d_blit_snapshot_pool_count == 1, "reinitialized pool admits release");
    rsx_d3d12_blit_snapshot_pool_shutdown();
}
static void test_init_with_live_buffer(void) {
    start_pool("1", 1);
    u8* live = rsx_d3d12_blit_snapshot_acquire(512);
    u8* cached = rsx_d3d12_blit_snapshot_acquire(768);
    check(live && cached, "init test allocations succeed");
    if (!live || !cached) return;
    memset(live, 0x9d, 512);
    rsx_d3d12_blit_snapshot_release(cached, 768);
    const LONG64 frees = InterlockedCompareExchange64(&fixture_free_calls, 0, 0);
    env_value("1"); rsx_d3d12_blit_snapshot_pool_init();
    check(s_gcm2d_blit_snapshot_pool_count == 0 &&
          InterlockedCompareExchange64(&fixture_free_calls, 0, 0) == frees + 1,
          "reinit drains cached entries");
    check(live[0] == 0x9d && live[511] == 0x9d,
          "reinit leaves active caller buffer untouched");
    u8* second = rsx_d3d12_blit_snapshot_acquire(512);
    check(second && second != live, "reinit never aliases a live caller buffer");
    rsx_d3d12_blit_snapshot_release(second, 512);
    rsx_d3d12_blit_snapshot_release(live, 512);
    rsx_d3d12_blit_snapshot_pool_shutdown();
}
static void test_entry_and_byte_caps(void) {
    start_pool("1", 1);
    u8* entries[17] = {0};
    const LONG64 frees = InterlockedCompareExchange64(&fixture_free_calls, 0, 0);
    for (unsigned i = 0; i < 17; ++i) {
        entries[i] = rsx_d3d12_blit_snapshot_acquire(33 + i);
        check(entries[i] != NULL, "entry-cap allocation succeeds");
    }
    for (unsigned i = 0; i < 17; ++i)
        rsx_d3d12_blit_snapshot_release(entries[i], 33 + i);
    check(s_gcm2d_blit_snapshot_pool_count == 16,
          "pool retains no more than sixteen entries");
    check(InterlockedCompareExchange64(&fixture_free_calls, 0, 0) == frees + 1,
          "seventeenth released entry is freed");
    rsx_d3d12_blit_snapshot_pool_shutdown();

    start_pool("1", 1);
    const u64 cap = GCM2D_BLIT_SNAPSHOT_POOL_MAX_BYTES;
    u8* x = rsx_d3d12_blit_snapshot_acquire((u64)16 << 20);
    u8* y = rsx_d3d12_blit_snapshot_acquire((u64)16 << 20);
    u8* z = rsx_d3d12_blit_snapshot_acquire(1024);
    check(x && y && z, "byte-cap allocations succeed");
    if (x && y && z) {
        const LONG64 before = InterlockedCompareExchange64(&fixture_free_calls, 0, 0);
        rsx_d3d12_blit_snapshot_release(x, (u64)16 << 20);
        rsx_d3d12_blit_snapshot_release(y, (u64)16 << 20);
        rsx_d3d12_blit_snapshot_release(z, 1024);
        check(s_gcm2d_blit_snapshot_pool_bytes == cap &&
              s_gcm2d_blit_snapshot_pool_count == 2,
              "byte cap retains exact-fit entries and rejects excess");
        check(InterlockedCompareExchange64(&fixture_free_calls, 0, 0) == before + 1,
              "over-budget released allocation is freed");
    } else {
        if (x) rsx_d3d12_blit_snapshot_release(x, (u64)16 << 20);
        if (y) rsx_d3d12_blit_snapshot_release(y, (u64)16 << 20);
        if (z) rsx_d3d12_blit_snapshot_release(z, 1024);
    }
    rsx_d3d12_blit_snapshot_pool_shutdown();
}
static void test_oversize_and_oom_retry(void) {
    start_pool("1", 1);
    u8* large = rsx_d3d12_blit_snapshot_acquire(
        GCM2D_BLIT_SNAPSHOT_POOL_MAX_BYTES + 1);
    check(large != NULL, "oversize allocation still uses malloc");
    if (large) {
        const LONG64 before = InterlockedCompareExchange64(&fixture_free_calls, 0, 0);
        rsx_d3d12_blit_snapshot_release(large,
            GCM2D_BLIT_SNAPSHOT_POOL_MAX_BYTES + 1);
        check(s_gcm2d_blit_snapshot_pool_count == 0 &&
              InterlockedCompareExchange64(&fixture_free_calls, 0, 0) == before + 1,
              "oversize release bypasses retention and frees");
    }
    u8* cached = rsx_d3d12_blit_snapshot_acquire(93);
    check(cached != NULL, "OOM setup allocation succeeds");
    if (!cached) return;
    rsx_d3d12_blit_snapshot_release(cached, 93);
    const LONG64 frees = InterlockedCompareExchange64(&fixture_free_calls, 0, 0);
    const LONG64 allocs = InterlockedCompareExchange64(&fixture_malloc_calls, 0, 0);
    InterlockedExchange(&fixture_fail_next, 1);
    u8* retried = rsx_d3d12_blit_snapshot_acquire(177);
    check(retried != NULL, "one failed malloc drains cache and retries");
    check(InterlockedCompareExchange64(&fixture_malloc_calls, 0, 0) == allocs + 2,
          "OOM path makes one failed call and one retry");
    check(s_gcm2d_blit_snapshot_pool_count == 0 &&
          InterlockedCompareExchange64(&fixture_free_calls, 0, 0) == frees + 1,
          "OOM retry first frees detached cached entries");
    if (retried) rsx_d3d12_blit_snapshot_release(retried, 177);
    rsx_d3d12_blit_snapshot_pool_shutdown();
}
typedef struct { unsigned id; } ThreadArg;
static DWORD WINAPI ownership_worker(void* opaque) {
    ThreadArg* arg = (ThreadArg*)opaque;
    static const u64 sizes[] = {64, 128, 256, 512};
    for (unsigned i = 0; i < THREAD_LOOPS; ++i) {
        const u64 n = sizes[(arg->id + i) & 3u];
        u8* p = rsx_d3d12_blit_snapshot_acquire(n);
        check(p != NULL, "concurrent acquire succeeds");
        if (!p) continue;
        EnterCriticalSection(&active_lock);
        for (unsigned k = 0; k < THREADS; ++k)
            if (k != arg->id && active_buffers[k] == p)
                check(0, "pool never gives one live pointer to two threads");
        active_buffers[arg->id] = p;
        LeaveCriticalSection(&active_lock);
        memset(p, (int)(arg->id + 1), (size_t)n);
        if ((i & 7u) == 0) Sleep(0);
        EnterCriticalSection(&active_lock);
        active_buffers[arg->id] = NULL;
        LeaveCriticalSection(&active_lock);
        rsx_d3d12_blit_snapshot_release(p, n);
    }
    return 0;
}
static void test_concurrent_ownership(void) {
    start_pool("1", 1);
    InitializeCriticalSection(&active_lock);
    HANDLE threads[THREADS] = {0}; ThreadArg args[THREADS];
    for (unsigned i = 0; i < THREADS; ++i) {
        args[i].id = i;
        threads[i] = CreateThread(NULL, 0, ownership_worker, &args[i], 0, NULL);
        check(threads[i] != NULL, "worker thread starts");
    }
    const DWORD wait_result = WaitForMultipleObjects(THREADS, threads, TRUE, 30000);
    check(wait_result == WAIT_OBJECT_0, "all ownership workers finish before teardown");
    if (wait_result != WAIT_OBJECT_0)
        for (unsigned i = 0; i < THREADS; ++i)
            if (threads[i]) WaitForSingleObject(threads[i], INFINITE);
    for (unsigned i = 0; i < THREADS; ++i) if (threads[i]) CloseHandle(threads[i]);
    DeleteCriticalSection(&active_lock);
    check(s_gcm2d_blit_snapshot_pool_count <= GCM2D_BLIT_SNAPSHOT_POOL_MAX_ENTRIES &&
          s_gcm2d_blit_snapshot_pool_bytes <= GCM2D_BLIT_SNAPSHOT_POOL_MAX_BYTES,
          "concurrent pool remains inside both caps");
    rsx_d3d12_blit_snapshot_pool_shutdown();
}
static double elapsed_ms(LARGE_INTEGER a, LARGE_INTEGER b, LARGE_INTEGER freq) {
    return (double)(b.QuadPart - a.QuadPart) * 1000.0 / (double)freq.QuadPart;
}
static volatile u64 bench_sink;
u64 fixture_observe(const u8* p, size_t n);
static double bench_single(int pooled, u64 bytes, unsigned ops, u64* allocations) {
    env_value(pooled ? "1" : "0"); rsx_d3d12_blit_snapshot_pool_init();
    u8* src = (u8*)malloc((size_t)bytes);
    if (!src) return -1.0;
    for (u64 i = 0; i < bytes; ++i) src[i] = (u8)(i * 29u + 3u);
    LARGE_INTEGER freq, a, b; QueryPerformanceFrequency(&freq);
    const LONG64 before = InterlockedCompareExchange64(&fixture_malloc_calls, 0, 0);
    QueryPerformanceCounter(&a);
    for (unsigned i = 0; i < ops; ++i) {
        u8* dst = rsx_d3d12_blit_snapshot_acquire(bytes);
        if (!dst) { free(src); rsx_d3d12_blit_snapshot_pool_shutdown(); return -1.0; }
        memcpy(dst, src, (size_t)bytes);
        bench_sink += fixture_observe(dst, (size_t)bytes);
        rsx_d3d12_blit_snapshot_release(dst, bytes);
    }
    QueryPerformanceCounter(&b);
    *allocations = (u64)(InterlockedCompareExchange64(&fixture_malloc_calls, 0, 0) - before);
    free(src); rsx_d3d12_blit_snapshot_pool_shutdown();
    return elapsed_ms(a, b, freq);
}
static double bench_six(int pooled, unsigned groups, u64* allocations) {
    static const u64 sizes[6] = {
        3686400, 3686400, 3686400, 3686400, 1843200, 1843200
    };
    env_value(pooled ? "1" : "0"); rsx_d3d12_blit_snapshot_pool_init();
    u8* src[2] = {(u8*)malloc(3686400), (u8*)malloc(1843200)};
    if (!src[0] || !src[1]) { free(src[0]); free(src[1]); return -1.0; }
    memset(src[0], 0x36, 3686400); memset(src[1], 0x18, 1843200);
    LARGE_INTEGER freq, a, b; QueryPerformanceFrequency(&freq);
    const LONG64 before = InterlockedCompareExchange64(&fixture_malloc_calls, 0, 0);
    QueryPerformanceCounter(&a);
    for (unsigned g = 0; g < groups; ++g) {
        u8* live[6] = {0};
        for (unsigned i = 0; i < 6; ++i) {
            live[i] = rsx_d3d12_blit_snapshot_acquire(sizes[i]);
            if (!live[i]) { free(src[0]); free(src[1]); return -1.0; }
            memcpy(live[i], src[i < 4 ? 0 : 1], (size_t)sizes[i]);
            bench_sink += fixture_observe(live[i], (size_t)sizes[i]);
        }
        /* Synthetic aggregate schedule: all six remain live through every
         * copy, then retire together. It matches captured bytes, not call order. */
        for (unsigned i = 0; i < 6; ++i)
            rsx_d3d12_blit_snapshot_release(live[i], sizes[i]);
    }
    QueryPerformanceCounter(&b);
    *allocations = (u64)(InterlockedCompareExchange64(&fixture_malloc_calls, 0, 0) - before);
    free(src[0]); free(src[1]); rsx_d3d12_blit_snapshot_pool_shutdown();
    return elapsed_ms(a, b, freq);
}
static void sort3(double v[3]) {
    for (int i = 0; i < 3; ++i) for (int j = i + 1; j < 3; ++j)
        if (v[j] < v[i]) { double t = v[i]; v[i] = v[j]; v[j] = t; }
}
static void run_pair(const char* name, u64 bytes, unsigned ops, int six) {
    double off[3], on[3]; u64 off_alloc[3], on_alloc[3];
    for (unsigned round = 0; round < 3; ++round) {
        int pool_first = (round & 1u) != 0;
        if (pool_first) {
            on[round] = six ? bench_six(1, ops, &on_alloc[round]) :
                              bench_single(1, bytes, ops, &on_alloc[round]);
            off[round] = six ? bench_six(0, ops, &off_alloc[round]) :
                               bench_single(0, bytes, ops, &off_alloc[round]);
        } else {
            off[round] = six ? bench_six(0, ops, &off_alloc[round]) :
                               bench_single(0, bytes, ops, &off_alloc[round]);
            on[round] = six ? bench_six(1, ops, &on_alloc[round]) :
                              bench_single(1, bytes, ops, &on_alloc[round]);
        }
    }
    sort3(off); sort3(on);
    const u64 copied_bytes = six ? (u64)ops * 18432000ull : (u64)ops * bytes;
    printf("BENCH case=%s rounds=3 order=alternating ops=%u copied_bytes_per_sample=%llu "
           "median_off_ms=%.3f median_pool_ms=%.3f off_mallocs_per_sample=%llu "
           "pool_mallocs_per_sample=%llu sink=%llu full_copy=1\n", name, ops,
           (unsigned long long)copied_bytes, off[1], on[1],
           (unsigned long long)off_alloc[0], (unsigned long long)on_alloc[0],
           (unsigned long long)bench_sink);
}
static void run_benchmarks(void) {
    puts("BENCH scope=native_malloc_copy_release cpu_only no_D3D no_game_fps_claim=1");
    run_pair("single-3686400", 3686400, 96, 0);
    run_pair("single-1843200", 1843200, 192, 0);
    run_pair("six-allocation-aggregate", 0, 16, 1);
}
int main(int argc, char** argv) {
    test_options_and_exact_size();
    test_live_ownership_and_shutdown();
    test_init_with_live_buffer();
    test_entry_and_byte_caps();
    test_oversize_and_oom_retry();
    test_concurrent_ownership();
    printf("RESULT checks=%ld failures=%ld\n", checks, failures);
    if (argc > 1 && strcmp(argv[1], "--benchmark") == 0) run_benchmarks();
    rsx_d3d12_blit_snapshot_pool_shutdown();
    return failures ? 1 : 0;
}
'''.replace("POOL_BLOCK", pool_block)


OBSERVER_SOURCE = r'''#include <stddef.h>
#include <stdint.h>
__declspec(noinline) uint64_t fixture_observe(const uint8_t* p, size_t n) {
    volatile uint64_t value = (uint64_t)p[0] + p[n / 2] + p[n - 1];
    return value;
}
'''


def run():
    cell = CELL.read_text(encoding="utf-8")
    backend = BACKEND.read_text(encoding="utf-8")
    header = HEADER.read_text(encoding="utf-8")
    structural_checks(cell, backend, header)
    source = fixture_source(production_pool_block(cell))
    clang = shutil.which("clang") or r"C:\Program Files\LLVM\bin\clang.exe"
    with tempfile.TemporaryDirectory(prefix="blit-snapshot-pool-") as tmp:
        c_path = Path(tmp) / "pool_fixture.c"
        observer_path = Path(tmp) / "opaque_observer.c"
        c_obj = Path(tmp) / "pool_fixture.obj"
        observer_obj = Path(tmp) / "opaque_observer.obj"
        exe = Path(tmp) / "pool_fixture.exe"
        c_path.write_text(source, encoding="utf-8")
        observer_path.write_text(OBSERVER_SOURCE, encoding="utf-8")
        subprocess.run([clang, "-std=c11", "-O2", "-D_CRT_SECURE_NO_WARNINGS",
                        "-c", str(c_path), "-o", str(c_obj)], check=True)
        subprocess.run([clang, "-std=c11", "-O2", "-c", str(observer_path),
                        "-o", str(observer_obj)], check=True)
        subprocess.run([clang, str(c_obj), str(observer_obj), "-o", str(exe)], check=True)
        args = [str(exe)] + (["--benchmark"] if "--benchmark" in __import__("sys").argv else [])
        subprocess.run(args, check=True, timeout=120)


if __name__ == "__main__":
    run()
