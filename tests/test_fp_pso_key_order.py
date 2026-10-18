"""Compare the production FP-PSO key predicate with structural equality moved last."""
from pathlib import Path
import hashlib
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT.parent / "ps3recomp/libs/video/rsx_d3d12_backend.c"
BASELINE_BACKUP = ROOT.parent.parent / "backups/20261006-fp-key-order-dfbc/rsx_d3d12_backend.c"
BASELINE_SHA256 = "DFBCFABE2D74C8A0E9349E85EBD1D842EC27321D945CE942694F0C70746673C5"
FP_SOURCE = ROOT.parent / "ps3recomp/libs/video/rsx_fp_decompiler.c"
FP_HEADER_DIR = FP_SOURCE.parent
INCLUDE_DIR = ROOT.parent / "ps3recomp/include"


def extract_condition(source: str) -> str:
    anchor = "if (s_d3d.vp_fp[i].fp_addr == fp_addr"
    start = source.index(anchor)
    opening = source.index("(", start)
    depth = 0
    for end in range(opening, len(source)):
        if source[end] == "(":
            depth += 1
        elif source[end] == ")":
            depth -= 1
            if depth == 0:
                return source[opening + 1:end]
    raise AssertionError("production FP-PSO predicate is unterminated")


def split_and(expression: str) -> list[str]:
    pieces = []
    depth = 0
    start = 0
    i = 0
    while i < len(expression):
        char = expression[i]
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif expression[i:i + 2] == "&&" and depth == 0:
            pieces.append(expression[start:i].strip())
            start = i + 2
            i += 1
        i += 1
    pieces.append(expression[start:].strip())
    return pieces


def extract_entry_type(source: str) -> str:
    marker = "} VPFPEntry;"
    end = source.index(marker) + len(marker)
    start = source.rfind("typedef struct {", 0, end)
    if start < 0:
        raise AssertionError("production VPFPEntry declaration not found")
    return source[start:end]


LOOKUP_TEMPLATE = r"""
static ID3D12PipelineState* @NAME@(const VPFPEntry* q) {
    int vs_idx = q->vs_idx;
    u32 fp_addr = q->fp_addr, vs_hash = q->vs_hash, blend = q->blend;
    int nrt = q->nrt, exp32 = q->exp32;
    u32 rtfmt = q->rtfmt, structural_size = q->structural_size;
    u64 structural_hash = q->structural_hash;
    u32 inline_count = q->inline_count;
    const RSXFPStructuralProgram* program = &q->structural_program;
    u32 cmask = q->cmask, mrt_key = q->mrt_key;
    u8 logic_key = q->logic_key, cull_key = q->cull_key;
    INT depth_bias = q->depth_bias;
    u32 slope_bias_bits = q->slope_bias_bits, dsvfmt = q->dsvfmt;
    u32 depth_key = q->depth_key;
    u64 stencil_key = q->stencil_key, texture_state = q->texture_state;
    u32 topology_type = q->topology_type;
    s_d3d.vp_gen = q->gen;
    for (int i = 0; i < s_d3d.vp_fp_n; ++i)
        if (@CONDITION@)
            return s_d3d.vp_fp[i].pso;
    return NULL;
}
"""


def lookup(name: str, expression: str, equal_name: str) -> str:
    return (LOOKUP_TEMPLATE.replace("@NAME@", name)
            .replace("@CONDITION@", expression.replace(
                "rsx_fp_structural_equal", equal_name)))


HARNESS = r'''
#include <algorithm>
#include <chrono>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <vector>
#include "rsx_fp_decompiler.h"

typedef int INT;
struct ID3D12PipelineState { unsigned id; };

@ENTRY_TYPE@

static ID3D12PipelineState pso_storage[128];
static struct {
    VPFPEntry vp_fp[128];
    int vp_fp_n;
    u32 vp_gen;
} s_d3d;
static u64 structural_equal_calls;

static int counted_structural_equal(const RSXFPStructuralProgram* a,
                                    const RSXFPStructuralProgram* b) {
    ++structural_equal_calls;
    return rsx_fp_structural_equal(a, b);
}

@BASE_COUNTED@
@REORDER_COUNTED@
@BASE@
@REORDER@

static int failures;
static void check(bool condition, const char* label) {
    if (!condition) {
        std::fprintf(stderr, "FAIL: %s\n", label);
        ++failures;
    }
}
static void seed_program(RSXFPStructuralProgram* p, u32 bytes) {
    std::memset(p, 0, sizeof(*p));
    p->structural_hash = 0x123456789abcdef0ULL;
    p->size_bytes = bytes;
    p->raw_word_count = bytes / 4u;
    p->raw_words[p->raw_word_count - 4u] = 1u; /* FP_END */
}
static void seed_inline_program(RSXFPStructuralProgram* p, u32 payload) {
    std::memset(p, 0, sizeof(*p));
    p->structural_hash = 0x0f1e2d3c4b5a6978ULL;
    p->size_bytes = 48u;
    p->raw_word_count = 12u;
    p->inline_count = 1u;
    p->inline_word_index[0] = 4u;
    p->raw_words[1] = 2u; /* first source is CONST; the following block is payload */
    p->raw_words[4] = payload;
    p->raw_words[5] = payload ^ 0x13579bdfu;
    p->raw_words[8] = 1u; /* FP_END after inline payload */
}
static void seed_entry(VPFPEntry* e, const RSXFPStructuralProgram* p, int id) {
    std::memset(e, 0, sizeof(*e));
    e->fp_addr = 0x00123400u;
    e->vs_idx = 2;
    e->vs_hash = 0x76543210u;
    e->gen = 7u;
    e->blend = 0x123u;
    e->nrt = 3;
    e->rtfmt = 28u;
    e->exp32 = 1;
    e->structural_hash = p->structural_hash;
    e->structural_size = p->size_bytes;
    e->inline_count = p->inline_count;
    e->structural_program = *p;
    e->cmask = 0x0fu;
    e->mrt_key = 0x01020304u;
    e->logic_key = 3u;
    e->depth_bias = -7;
    e->slope_bias_bits = 0x3f800000u;
    e->dsvfmt = 45u;
    e->depth_key = 0x11223344u;
    e->stencil_key = 0x0102030405060708ULL;
    e->cull_key = 2u;
    e->texture_state = 0x0004000200010002ULL;
    e->topology_type = 3u;
    e->pso = &pso_storage[id];
    pso_storage[id].id = (unsigned)id + 1u;
}
static void expect_pair(const char* label, const VPFPEntry* query,
                        ID3D12PipelineState* expected,
                        long long expected_base_calls,
                        long long expected_reorder_calls) {
    structural_equal_calls = 0;
    ID3D12PipelineState* base = find_base_counted(query);
    u64 base_calls = structural_equal_calls;
    structural_equal_calls = 0;
    ID3D12PipelineState* reordered = find_reorder_counted(query);
    u64 reordered_calls = structural_equal_calls;
    check(base == expected, label);
    check(reordered == expected, label);
    if (expected_base_calls >= 0)
        check(base_calls == (u64)expected_base_calls, "baseline structural-equality call count");
    if (expected_reorder_calls >= 0)
        check(reordered_calls == (u64)expected_reorder_calls, "reordered structural-equality call count");
}
static void test_key_mismatches(void) {
    RSXFPStructuralProgram p;
    seed_program(&p, 128u);
    seed_entry(&s_d3d.vp_fp[0], &p, 0);
    s_d3d.vp_fp_n = 1;
    const VPFPEntry original = s_d3d.vp_fp[0];
#define MISMATCH(field, base_calls, reorder_calls) do { \
    VPFPEntry q = original; \
    q.field = static_cast<decltype(q.field)>(q.field ^ 1); \
    expect_pair("single key mismatch: " #field, &q, NULL, base_calls, reorder_calls); \
} while (0)
    MISMATCH(fp_addr, 0, 0);
    MISMATCH(vs_idx, 0, 0);
    MISMATCH(vs_hash, 0, 0);
    MISMATCH(gen, 0, 0);
    MISMATCH(blend, 0, 0);
    MISMATCH(nrt, 0, 0);
    MISMATCH(rtfmt, 0, 0);
    MISMATCH(exp32, 0, 0);
    MISMATCH(structural_hash, 0, 0);
    MISMATCH(structural_size, 0, 0);
    MISMATCH(inline_count, 0, 0);
    MISMATCH(cmask, 1, 0);
    MISMATCH(mrt_key, 1, 0);
    MISMATCH(logic_key, 1, 0);
    MISMATCH(depth_bias, 1, 0);
    MISMATCH(slope_bias_bits, 1, 0);
    MISMATCH(dsvfmt, 1, 0);
    MISMATCH(depth_key, 1, 0);
    MISMATCH(stencil_key, 1, 0);
    MISMATCH(cull_key, 1, 0);
    MISMATCH(texture_state, 1, 0);
    MISMATCH(topology_type, 1, 0);
#undef MISMATCH
}
static void test_hash_collision_and_inline_payload(void) {
    RSXFPStructuralProgram p, collision;
    seed_program(&p, 128u);
    seed_entry(&s_d3d.vp_fp[0], &p, 0);
    s_d3d.vp_fp_n = 1;
    VPFPEntry q = s_d3d.vp_fp[0];
    collision = p;
    collision.raw_words[4] ^= 0x100u; /* same supplied hash and shape, distinct code */
    q.structural_program = collision;
    expect_pair("same structural hash with different code misses", &q, NULL, 1, 1);

    RSXFPStructuralProgram inline_a, inline_b;
    seed_inline_program(&inline_a, 0x11112222u);
    seed_inline_program(&inline_b, 0x9999aaaau);
    check(rsx_fp_structural_equal(&inline_a, &inline_b) != 0,
          "actual structural equality ignores inline payload words");
    seed_entry(&s_d3d.vp_fp[0], &inline_a, 0);
    s_d3d.vp_fp_n = 1;
    q = s_d3d.vp_fp[0];
    q.structural_program = inline_b;
    expect_pair("inline payload-only change reuses same PSO", &q, &s_d3d.vp_fp[0].pso[0], 1, 1);
}
static void test_unique_pointer_and_order(void) {
    RSXFPStructuralProgram p;
    seed_program(&p, 128u);
    seed_entry(&s_d3d.vp_fp[0], &p, 0);
    seed_entry(&s_d3d.vp_fp[1], &p, 1);
    s_d3d.vp_fp_n = 2;
    VPFPEntry q = s_d3d.vp_fp[0];
    check(s_d3d.vp_fp[0].pso != s_d3d.vp_fp[1].pso,
          "duplicate cache keys use distinct PSO pointers");
    expect_pair("duplicate keys select first array entry", &q, s_d3d.vp_fp[0].pso, -1, -1);
    VPFPEntry swap = s_d3d.vp_fp[0];
    s_d3d.vp_fp[0] = s_d3d.vp_fp[1];
    s_d3d.vp_fp[1] = swap;
    expect_pair("reordered duplicate keys select new first entry",
                &q, s_d3d.vp_fp[0].pso, -1, -1);
}
static void setup_rotating_table(const RSXFPStructuralProgram* p,
                                 std::vector<VPFPEntry>* queries,
                                 bool early_mismatch) {
    s_d3d.vp_fp_n = 128;
    for (int i = 0; i < 128; ++i) {
        seed_entry(&s_d3d.vp_fp[i], p, i);
        s_d3d.vp_fp[i].topology_type = 1000u + (u32)i;
    }
    queries->resize(128);
    for (int i = 0; i < 128; ++i) {
        (*queries)[i] = s_d3d.vp_fp[i];
        if (early_mismatch) (*queries)[i].fp_addr ^= 1u;
    }
}
static void test_rotating_selections(void) {
    RSXFPStructuralProgram p;
    seed_program(&p, 512u);
    std::vector<VPFPEntry> queries;
    setup_rotating_table(&p, &queries, false);
    for (int i : {0, 1, 16, 64, 127})
        expect_pair("rotating late-key hit keeps selected unique PSO",
                    &queries[i], s_d3d.vp_fp[i].pso, -1, -1);
    setup_rotating_table(&p, &queries, true);
    for (int i : {0, 64, 127})
        expect_pair("early key mismatch preserves miss",
                    &queries[i], NULL, -1, -1);
}

typedef ID3D12PipelineState* (*Lookup)(const VPFPEntry*);
static volatile uintptr_t bench_sink;
static double run_bench(Lookup fn, const std::vector<VPFPEntry>& queries,
                        unsigned lookups) {
    uintptr_t sum = 0;
    const auto begin = std::chrono::steady_clock::now();
    for (unsigned i = 0; i < lookups; ++i) {
        ID3D12PipelineState* result = fn(&queries[i & 127u]);
        sum += reinterpret_cast<uintptr_t>(result) >> 4;
    }
    const auto end = std::chrono::steady_clock::now();
    bench_sink = bench_sink ^ sum;
    return (double)std::chrono::duration_cast<std::chrono::nanoseconds>(
        end - begin).count() / (double)lookups;
}
static double median3(double a, double b, double c) {
    if (a > b) std::swap(a, b);
    if (b > c) std::swap(b, c);
    if (a > b) std::swap(a, b);
    return b;
}
static void print_samples(const char* name, const double values[3]) {
    std::printf("%s=%.1f,%.1f,%.1f", name, values[0], values[1], values[2]);
}
static void bench_size(u32 bytes, unsigned lookups) {
    RSXFPStructuralProgram p;
    seed_program(&p, bytes);
    std::vector<VPFPEntry> late_queries, early_queries;
    setup_rotating_table(&p, &late_queries, false);
    setup_rotating_table(&p, &early_queries, true);
    (void)find_base(&late_queries[127]);
    (void)find_reorder(&late_queries[127]);
    double base_late[3], reorder_late[3], base_early[3], reorder_early[3];
    for (int round = 0; round < 3; ++round) {
        if ((round & 1) == 0) {
            base_late[round] = run_bench(find_base, late_queries, lookups);
            reorder_late[round] = run_bench(find_reorder, late_queries, lookups);
            base_early[round] = run_bench(find_base, early_queries, lookups);
            reorder_early[round] = run_bench(find_reorder, early_queries, lookups);
        } else {
            reorder_late[round] = run_bench(find_reorder, late_queries, lookups);
            base_late[round] = run_bench(find_base, late_queries, lookups);
            reorder_early[round] = run_bench(find_reorder, early_queries, lookups);
            base_early[round] = run_bench(find_base, early_queries, lookups);
        }
    }
    const double base_late_med = median3(base_late[0], base_late[1], base_late[2]);
    const double reorder_late_med = median3(reorder_late[0], reorder_late[1], reorder_late[2]);
    const double base_early_med = median3(base_early[0], base_early[1], base_early[2]);
    const double reorder_early_med = median3(reorder_early[0], reorder_early[1], reorder_early[2]);
    std::printf("bench program_bytes=%u entries=128 workload=rotating_late_key_hits lookups_per_round=%u ",
                bytes, lookups);
    print_samples("baseline_ns_per_call", base_late);
    std::printf(" ");
    print_samples("reordered_ns_per_call", reorder_late);
    std::printf(" median_saved_ns_per_call=%.1f projected_487_calls_us=%.2f",
                base_late_med - reorder_late_med,
                (base_late_med - reorder_late_med) * 487.0 / 1000.0);
    std::printf(" native_upper_bound_only=true\n");
    std::printf("bench program_bytes=%u entries=128 workload=early_fp_addr_mismatch lookups_per_round=%u ",
                bytes, lookups);
    print_samples("baseline_ns_per_call", base_early);
    std::printf(" ");
    print_samples("reordered_ns_per_call", reorder_early);
    std::printf(" median_delta_ns_per_call=%.1f\n",
                reorder_early_med - base_early_med);
}
static void test_benchmark_call_counts(void) {
    RSXFPStructuralProgram p;
    seed_program(&p, 4096u);
    std::vector<VPFPEntry> queries;
    setup_rotating_table(&p, &queries, false);
    structural_equal_calls = 0;
    for (const auto& q : queries) (void)find_base_counted(&q);
    const u64 baseline_calls = structural_equal_calls;
    structural_equal_calls = 0;
    for (const auto& q : queries) (void)find_reorder_counted(&q);
    const u64 reordered_calls = structural_equal_calls;
    check(baseline_calls == 8256u, "baseline calls equality once per preceding entry plus hit");
    check(reordered_calls == 128u, "reordered calls equality only on each hit");
    std::printf("counted_rotating_128_queries baseline_structural_equal_calls=%llu reordered_structural_equal_calls=%llu\n",
                (unsigned long long)baseline_calls, (unsigned long long)reordered_calls);
}
int main() {
    test_key_mismatches();
    test_hash_collision_and_inline_payload();
    test_unique_pointer_and_order();
    test_rotating_selections();
    test_benchmark_call_counts();
    if (failures) {
        std::fprintf(stderr, "%d native assertion(s) failed\n", failures);
        return 1;
    }
    std::printf("PASS native key-mismatch=22 collision=1 inline-payload=1 duplicate-order=2 rotating-hits=5 structural-equality-guard=actual\n");
    bench_size(128u, 10000u);
    bench_size(512u, 5000u);
    bench_size(4096u, 1000u);
    std::printf("benchmark_sink=%llu\n", (unsigned long long)bench_sink);
    return 0;
}
'''


def main() -> None:
    backend = BACKEND.read_text(encoding="utf-8")
    baseline_bytes = BASELINE_BACKUP.read_bytes()
    if hashlib.sha256(baseline_bytes).hexdigest().upper() != BASELINE_SHA256:
        raise AssertionError("frozen pre-edit predicate backup hash changed")
    baseline_source = baseline_bytes.decode("utf-8")
    baseline_clauses = split_and(extract_condition(baseline_source))
    active_clauses = split_and(extract_condition(backend))
    structural = [clause for clause in baseline_clauses if "rsx_fp_structural_equal" in clause]
    if len(baseline_clauses) != 23 or len(structural) != 1:
        raise AssertionError(f"expected 22 cheap key clauses plus one structural guard, got {len(baseline_clauses)} and {len(structural)}")
    reordered_clauses = [clause for clause in baseline_clauses if clause not in structural] + structural
    if active_clauses not in (baseline_clauses, reordered_clauses):
        raise AssertionError("active production predicate diverges from baseline or structural-last candidate")
    proposed = " &&\n            ".join(reordered_clauses)
    baseline = " &&\n            ".join(baseline_clauses)
    active_order = "reordered" if active_clauses == reordered_clauses else "baseline"
    print(f"predicate baseline_sha256={BASELINE_SHA256} active_order={active_order} cheap_key_clauses=22")
    entry_type = extract_entry_type(backend)
    harness = (HARNESS.replace("@ENTRY_TYPE@", entry_type)
               .replace("@BASE_COUNTED@", lookup("find_base_counted", baseline, "counted_structural_equal"))
               .replace("@REORDER_COUNTED@", lookup("find_reorder_counted", proposed, "counted_structural_equal"))
               .replace("@BASE@", lookup("find_base", baseline, "rsx_fp_structural_equal"))
               .replace("@REORDER@", lookup("find_reorder", proposed, "rsx_fp_structural_equal")))
    clang = shutil.which("clang") or r"C:\Program Files\LLVM\bin\clang.exe"
    clangxx = shutil.which("clang++") or r"C:\Program Files\LLVM\bin\clang++.exe"
    if not Path(clang).exists() or not Path(clangxx).exists():
        raise RuntimeError("clang/clang++ required to compile actual production decompiler equality")
    with tempfile.TemporaryDirectory(prefix="tj-fp-pso-key-order-") as directory:
        work = Path(directory)
        cpp = work / "fp_pso_key_order.cpp"
        obj = work / "rsx_fp_decompiler.o"
        exe = work / "fp_pso_key_order.exe"
        cpp.write_text(harness, encoding="utf-8")
        subprocess.run([clang, "-std=c11", "-O2", "-I", str(INCLUDE_DIR),
                        "-c", str(FP_SOURCE), "-o", str(obj)],
                       cwd=FP_HEADER_DIR, check=True, timeout=20)
        subprocess.run([clangxx, "-std=c++17", "-O2", "-I", str(INCLUDE_DIR),
                        "-I", str(FP_HEADER_DIR), str(cpp), str(obj), "-o", str(exe)],
                       cwd=FP_HEADER_DIR, check=True, timeout=20)
        result = subprocess.run([str(exe)], cwd=FP_HEADER_DIR, check=True,
                                timeout=35, text=True, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT)
        print(result.stdout, end="")


if __name__ == "__main__":
    main()