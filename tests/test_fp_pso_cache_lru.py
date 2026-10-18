"""Exercise the production FP-PSO LRU hit, insertion, overflow, and ownership paths."""
from pathlib import Path
import hashlib
import re
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT.parent / "ps3recomp/libs/video/rsx_d3d12_backend.c"
FP_SOURCE = ROOT.parent / "ps3recomp/libs/video/rsx_fp_decompiler.c"
FP_HEADER_DIR = FP_SOURCE.parent
INCLUDE_DIR = ROOT.parent / "ps3recomp/include"
BASELINE_BACKUP = ROOT / "diagnostics/fp_pso_lru/rsx_d3d12_backend.794B2D7D.before.c"
BASELINE_SHA256 = "794B2D7DBD66EEEB9551E75FAA10C91A1EFD3A2CCC30DDF09BB2D733890C4B96"


def extract_braced(source: str, start: int) -> str:
    opening = source.index("{", start)
    depth = 0
    for end in range(opening, len(source)):
        if source[end] == "{":
            depth += 1
        elif source[end] == "}":
            depth -= 1
            if depth == 0:
                return source[start:end + 1]
    raise AssertionError("unterminated production block")


def extract_function(source: str, name: str) -> str:
    matches = list(re.finditer(r"\b" + re.escape(name) + r"\s*\(", source))
    for match in matches:
        line_start = source.rfind("\n", 0, match.start()) + 1
        if source[line_start:match.start()].lstrip().startswith("static "):
            return extract_braced(source, line_start)
    raise AssertionError(f"production function {name} not found")


def extract_type(source: str, marker: str) -> str:
    end = source.index(marker) + len(marker)
    start = source.rfind("typedef struct {", 0, end)
    if start < 0:
        raise AssertionError(f"production type {marker} not found")
    return source[start:end]


def find_matching_paren(source: str, opening: int) -> int:
    depth = 0
    for end in range(opening, len(source)):
        if source[end] == "(":
            depth += 1
        elif source[end] == ")":
            depth -= 1
            if depth == 0:
                return end
    raise AssertionError("unterminated production condition")


def extract_lookup_loop(source: str) -> str:
    function_start = source.index("static ID3D12PipelineState* vp_get_fp_pso")
    start = source.index("for (int i = 0; i < s_d3d.vp_fp_n; i++)", function_start)
    if_start = source.index("if (s_d3d.vp_fp[i].fp_addr", start)
    closing_paren = find_matching_paren(source, source.index("(", if_start))
    body_start = source.index("{", closing_paren)
    # Extract the full hit body, including nested trace conditionals.
    return source[start:body_start] + extract_braced(source, body_start)


def extract_commit_path(source: str) -> str:
    function_start = source.index("static ID3D12PipelineState* vp_get_fp_pso")
    create = source.index("CreateGraphicsPipelineState(", function_start)
    failure = source.index("if (FAILED(hr)) {", create)
    end = source.index("return pso;", failure) + len("return pso;")
    return source[failure:end]


def extract_shutdown_cache(source: str) -> str:
    shutdown = source.index("void rsx_d3d12_backend_shutdown")
    start = source.index("for (int i = 0; i < s_d3d.vp_fp_n; i++) {", shutdown)
    end = source.index("vp_fp_blob_release_all();", start)
    return source[start:end]


def extract_owner_release_loop(source: str) -> str:
    release = source.index("static void d3d12_prefix_owner_release_uploads")
    start = source.index("for (u32 i = 0; i < owner->object_count; ++i)", release)
    end = source.index("owner->object_count = 0;", start) + len("owner->object_count = 0;")
    return source[start:end]


def main() -> None:
    source = BACKEND.read_text(encoding="utf-8")
    baseline_bytes = BASELINE_BACKUP.read_bytes()
    if hashlib.sha256(baseline_bytes).hexdigest().upper() != BASELINE_SHA256:
        raise AssertionError("captured pre-LRU renderer copy changed")
    baseline = baseline_bytes.decode("utf-8")
    if "Release(s_d3d.vp_fp[0].pso)" not in baseline or "memmove(&s_d3d.vp_fp[0]" not in baseline:
        raise AssertionError("captured baseline is not the original FIFO replacement")

    entry_type = extract_type(source, "} VPFPEntry;")
    hit_loop = extract_lookup_loop(source)
    if "rsx_fp_structural_equal" not in hit_loop:
        raise AssertionError("production hit block lost its exact structural guard")
    if not (hit_loop.index("rsx_fp_structural_equal") <
            hit_loop.index("last_use = vp_fp_cache_next_stamp()") <
            hit_loop.index("return s_d3d.vp_fp[i].pso")):
        raise AssertionError("hit stamp must follow the full exact-key match")
    find_helper = extract_function(source, "vp_fp_cache_next_stamp")
    slot_helper = extract_function(source, "vp_fp_cache_insert_slot")
    commit_path = extract_commit_path(source)
    if not (commit_path.index("if (FAILED(hr))") <
            commit_path.index("return NULL;") <
            commit_path.index("vp_fp_cache_insert_slot()")):
        raise AssertionError("failed PSO creation must return before cache admission")
    if "memmove" in commit_path or "memmove" in slot_helper:
        raise AssertionError("LRU replacement must overwrite its selected slot in place")
    for field in ("fp_addr", "vs_idx", "vs_hash", "gen", "blend", "nrt", "rtfmt",
                  "exp32", "structural_hash", "structural_size", "inline_count",
                  "structural_program", "cmask", "mrt_key", "logic_key", "depth_bias",
                  "slope_bias_bits", "dsvfmt", "depth_key", "stencil_key", "cull_key",
                  "texture_state", "topology_type", "pso", "last_use"):
        if f"s_d3d.vp_fp[fp_slot].{field}" not in commit_path:
            raise AssertionError(f"successful insertion no longer writes {field}")
    shutdown_path = extract_shutdown_cache(source)
    if "s_d3d.vp_fp_clock = 0;" not in shutdown_path:
        raise AssertionError("shutdown must reset the LRU clock with the cache")

    owner_capture = extract_function(source, "d3d12_prefix_owner_object_capture")
    owned_pso = extract_function(source, "d3d12_owned_pso")
    if not ("object->lpVtbl->AddRef(object)" in owner_capture and
            owned_pso.index("d3d12_prefix_owner_object_capture(pso)") <
            owned_pso.index("SetPipelineState")):
        raise AssertionError("recorded PSOs must keep the prefix-owner reference")
    owner_release = extract_owner_release_loop(source)
    prefix_cap = re.search(r"^#define PREFIX_OBJECT_CAP .*?$", source, re.MULTILINE)
    if not prefix_cap:
        raise AssertionError("production prefix-object capacity not found")

    harness = r'''
#include <algorithm>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <vector>
#include "rsx_fp_decompiler.h"

typedef int INT;
typedef long HRESULT;
typedef uint32_t ULONG;
#define FAILED(hr) ((hr) < 0)
#define E_FAIL (-1)
#define MAX_DRAWS 2048u
@PREFIX_OBJECT_CAP@
#define VP_FP_CACHE 128

struct IUnknown;
struct IUnknownVtbl {
    ULONG (*AddRef)(IUnknown*);
    ULONG (*Release)(IUnknown*);
};
struct IUnknown { IUnknownVtbl* lpVtbl; };
struct ID3D12PipelineState : IUnknown {};
struct FakePso {
    ID3D12PipelineState iface;
    int refs;
    int add_refs;
    int release_calls;
    unsigned id;
};

@ENTRY_TYPE@

struct D3D12PrefixOwner {
    IUnknown* objects[PREFIX_OBJECT_CAP];
    u32 object_count;
    int phase;
};
struct {
    VPFPEntry vp_fp[VP_FP_CACHE];
    int vp_fp_n;
    u64 vp_fp_clock;
    u32 vp_gen;
    u32 prefix_owner_active;
    int gcm2d_backend_halted;
    D3D12PrefixOwner prefix_owners[2];
} s_d3d;
struct { u64 fp_cache_hits, fp_cache_evictions; } s_tj_perf_phase;
struct {
    u64 fp_cache_hits, fp_cache_search_entries;
    u64 fp_cache_search_ticks, fp_cache_search_calls;
} s_tj_present_work_trace;

static ULONG fake_add_ref(IUnknown* object) {
    FakePso* p = reinterpret_cast<FakePso*>(object);
    ++p->add_refs;
    return (ULONG)++p->refs;
}
static ULONG fake_release(IUnknown* object) {
    FakePso* p = reinterpret_cast<FakePso*>(object);
    ++p->release_calls;
    return (ULONG)--p->refs;
}
static IUnknownVtbl fake_vtable = { fake_add_ref, fake_release };
static ID3D12PipelineState* as_pso(FakePso* p) { return &p->iface; }
static void init_pso(FakePso* p, unsigned id) {
    std::memset(p, 0, sizeof(*p));
    p->iface.lpVtbl = &fake_vtable;
    p->refs = 1;
    p->id = id;
}
static void gcm2d_backend_halt(const char*) { s_d3d.gcm2d_backend_halted = 1; }
static void d3d12_dump_device_failure(const char*, HRESULT) {}
typedef long long LONGLONG;
static void tj_present_work_trace_sync_finish(LONGLONG, u64*, u64* calls) {
    ++*calls;
}

@STAMP_HELPER@
@SLOT_HELPER@
@OWNER_CAPTURE@
static void release_owner_objects(D3D12PrefixOwner* owner) {
@OWNER_RELEASE@
}

static int failures;
static void check(bool ok, const char* label) {
    if (!ok) { std::fprintf(stderr, "FAIL: %s\n", label); ++failures; }
}
static void clear_state() {
    std::memset(&s_d3d, 0, sizeof(s_d3d));
    std::memset(&s_tj_perf_phase, 0, sizeof(s_tj_perf_phase));
    std::memset(&s_tj_present_work_trace, 0, sizeof(s_tj_present_work_trace));
}
static void seed_program(RSXFPStructuralProgram* p, u32 bytes) {
    std::memset(p, 0, sizeof(*p));
    p->structural_hash = 0x123456789abcdef0ULL;
    p->size_bytes = bytes;
    p->raw_word_count = bytes / 4u;
    p->raw_words[p->raw_word_count - 4u] = 1u;
}
static void seed_entry(VPFPEntry* e, const RSXFPStructuralProgram* p,
                       u32 fp_addr, ID3D12PipelineState* pso) {
    std::memset(e, 0, sizeof(*e));
    e->fp_addr = fp_addr;
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
    e->pso = pso;
}

@LOOKUP_TEMPLATE@

static ID3D12PipelineState* production_commit(HRESULT hr, const VPFPEntry* q,
                                               ID3D12PipelineState* pso,
                                               int perf_trace) {
    s_d3d.vp_gen = q->gen;
    u32 fp_addr = q->fp_addr;
    int vs_idx = q->vs_idx;
    u32 vs_hash = q->vs_hash, blend = q->blend;
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
@COMMIT_PATH@
}

static void production_shutdown_cache() {
@SHUTDOWN_CACHE@
}

static ID3D12PipelineState* request_key(const VPFPEntry* q, bool create_ok,
                                        FakePso* fresh) {
    ID3D12PipelineState* hit = production_find(q);
    if (hit) return hit;
    return production_commit(create_ok ? 0 : E_FAIL, q, as_pso(fresh), 0);
}

static void test_exact_hit_and_key_guards() {
    clear_state();
    RSXFPStructuralProgram p;
    seed_program(&p, 128u);
    FakePso cached;
    init_pso(&cached, 1);
    seed_entry(&s_d3d.vp_fp[0], &p, 0x00123400u, as_pso(&cached));
    s_d3d.vp_fp_n = 1;
    s_d3d.vp_fp_clock = 70;
    s_d3d.vp_fp[0].last_use = 70;
    VPFPEntry original = s_d3d.vp_fp[0];
    check(production_find(&original) == as_pso(&cached), "exact full-key hit returns cached PSO");
    check(s_d3d.vp_fp[0].last_use == 71 && s_d3d.vp_fp_clock == 71,
          "exact hit advances only the matched entry stamp");

    VPFPEntry mismatch = original;
    mismatch.cmask ^= 1u;
    s_d3d.vp_fp[0].last_use = 99; s_d3d.vp_fp_clock = 99;
    check(production_find(&mismatch) == NULL, "full-key mismatch remains a miss");
    check(s_d3d.vp_fp[0].last_use == 99 && s_d3d.vp_fp_clock == 99,
          "key mismatch does not touch recency");
    VPFPEntry collision = original;
    collision.structural_program.raw_words[4] ^= 0x100u;
    s_d3d.vp_fp[0].last_use = 99; s_d3d.vp_fp_clock = 99;
    check(production_find(&collision) == NULL,
          "same structural hash and shape with different code misses");
    check(s_d3d.vp_fp[0].last_use == 99 && s_d3d.vp_fp_clock == 99,
          "structural collision miss does not touch recency");

}

static void test_fifo_churn_vs_lru_hot_set() {
    clear_state();
    RSXFPStructuralProgram p;
    seed_program(&p, 128u);
    FakePso objects[VP_FP_CACHE + 3];
    for (unsigned i = 0; i < VP_FP_CACHE + 3; ++i) init_pso(&objects[i], i + 1u);
    u32 ids[VP_FP_CACHE];
    ids[0] = 0x100u; /* A */
    ids[1] = 0x200u; /* B */
    ids[2] = 0x300u; /* C */
    for (int i = 3; i < VP_FP_CACHE; ++i) ids[i] = 0x1000u + (u32)i;
    for (int i = 0; i < VP_FP_CACHE; ++i) {
        VPFPEntry q;
        seed_entry(&q, &p, ids[i], NULL);
        check(request_key(&q, true, &objects[i]) == as_pso(&objects[i]),
              "full-capacity setup inserts expected PSO");
    }
    check(s_d3d.vp_fp_n == VP_FP_CACHE, "cache fills to exactly 128 entries");

    VPFPEntry a;
    seed_entry(&a, &p, ids[0], NULL);
    check(request_key(&a, true, &objects[VP_FP_CACHE]) == as_pso(&objects[0]),
          "A hit preserves the original pointer");
    VPFPEntry d;
    seed_entry(&d, &p, 0xDEADu, NULL);
    check(request_key(&d, true, &objects[VP_FP_CACHE + 1]) ==
          as_pso(&objects[VP_FP_CACHE + 1]), "new D key inserts after success");
    check(s_d3d.vp_fp_n == VP_FP_CACHE, "full replacement remains capped at 128");
    check(s_d3d.vp_fp[0].fp_addr == ids[0] &&
          s_d3d.vp_fp[1].fp_addr == 0xDEADu,
          "replacement overwrites the selected slot without shifting survivors");
    check(objects[1].release_calls == 1 && objects[1].refs == 0,
          "least-recent B victim is released exactly once");
    check(objects[0].release_calls == 0 && objects[2].release_calls == 0,
          "hot A and C survive the selected eviction");
    check(request_key(&a, true, &objects[VP_FP_CACHE + 2]) == as_pso(&objects[0]),
          "final A request remains an exact hit after D insertion");

    std::vector<u32> fifo;
    for (u32 id : ids) fifo.push_back(id);
    check(std::find(fifo.begin(), fifo.end(), ids[0]) != fifo.end(),
          "A is resident before D in the FIFO baseline");
    fifo.erase(fifo.begin());
    fifo.push_back(0xDEADu);
    check(std::find(fifo.begin(), fifo.end(), ids[0]) == fifo.end(),
          "captured FIFO baseline misses the final A after D evicts it");
}

static void test_failed_create_at_capacity_is_inert() {
    clear_state();
    RSXFPStructuralProgram p;
    seed_program(&p, 128u);
    FakePso objects[VP_FP_CACHE + 1];
    for (unsigned i = 0; i < VP_FP_CACHE + 1; ++i) init_pso(&objects[i], i + 1u);
    for (int i = 0; i < VP_FP_CACHE; ++i) {
        VPFPEntry q;
        seed_entry(&q, &p, 0x100u + (u32)i, NULL);
        check(request_key(&q, true, &objects[i]) == as_pso(&objects[i]),
              "failed-create fixture capacity setup");
    }
    ID3D12PipelineState* before_pso[VP_FP_CACHE];
    u64 before_stamp[VP_FP_CACHE];
    for (int i = 0; i < VP_FP_CACHE; ++i) {
        before_pso[i] = s_d3d.vp_fp[i].pso;
        before_stamp[i] = s_d3d.vp_fp[i].last_use;
    }
    u64 before_clock = s_d3d.vp_fp_clock;
    VPFPEntry failed;
    seed_entry(&failed, &p, 0xFFFFu, NULL);
    check(request_key(&failed, false, &objects[VP_FP_CACHE]) == NULL,
          "failed PSO creation returns null");
    check(s_d3d.vp_fp_n == VP_FP_CACHE && s_d3d.vp_fp_clock == before_clock,
          "failed create leaves cache count and clock unchanged");
    check(objects[VP_FP_CACHE].refs == 1 && objects[VP_FP_CACHE].release_calls == 0,
          "failed create does not adopt or release a candidate PSO");
    for (int i = 0; i < VP_FP_CACHE; ++i)
        check(s_d3d.vp_fp[i].pso == before_pso[i] &&
              s_d3d.vp_fp[i].last_use == before_stamp[i] &&
              objects[i].release_calls == 0,
              "failed create does not evict or retimestamp a resident");
}

static void test_wrap_ties_and_owner_reference() {
    clear_state();
    RSXFPStructuralProgram p;
    seed_program(&p, 128u);
    FakePso objects[VP_FP_CACHE + 1];
    for (unsigned i = 0; i < VP_FP_CACHE + 1; ++i) init_pso(&objects[i], i + 1u);
    for (int i = 0; i < VP_FP_CACHE; ++i) {
        VPFPEntry q;
        seed_entry(&q, &p, 0x100u + (u32)i, as_pso(&objects[i]));
        check(production_commit(0, &q, as_pso(&objects[i]), 0) == as_pso(&objects[i]),
              "owner fixture fills from successful-create path");
    }
    for (int i = 0; i < VP_FP_CACHE; ++i)
        s_d3d.vp_fp[i].last_use = 100u + (u64)i;
    s_d3d.vp_fp[0].last_use = 30;
    s_d3d.vp_fp[1].last_use = 10;
    s_d3d.vp_fp[2].last_use = 10;
    s_d3d.vp_fp_clock = UINT64_MAX;

    s_d3d.prefix_owner_active = 1;
    s_d3d.prefix_owners[0].phase = 1;
    check(d3d12_prefix_owner_object_capture(as_pso(&objects[1])) == 0 &&
          objects[1].refs == 2,
          "recording owner keeps a separate PSO reference");

    VPFPEntry touched;
    seed_entry(&touched, &p, 0x102u, NULL);
    check(production_find(&touched) == as_pso(&objects[2]),
          "hit still resolves after the clock reaches its boundary");
    check(s_d3d.vp_fp_clock == 129 && s_d3d.vp_fp[0].last_use == 3 &&
          s_d3d.vp_fp[1].last_use == 1 && s_d3d.vp_fp[2].last_use == 129,
          "overflow rebase preserves order and resolves equal stamps by lower slot");

    VPFPEntry fresh;
    seed_entry(&fresh, &p, 0xFFFFu, NULL);
    check(production_commit(0, &fresh, as_pso(&objects[VP_FP_CACHE]), 0) ==
          as_pso(&objects[VP_FP_CACHE]), "full cache accepts a successful new PSO");
    check(s_d3d.vp_fp[1].fp_addr == 0xFFFFu && objects[1].release_calls == 1 &&
          objects[1].refs == 1,
          "tie-broken oldest slot is evicted while its owner reference stays live");
    release_owner_objects(&s_d3d.prefix_owners[0]);
    check(objects[1].refs == 0 && objects[1].release_calls == 2 &&
          s_d3d.prefix_owners[0].object_count == 0,
          "owner releases its retained PSO once after cache eviction");
    check(s_d3d.vp_fp_n == VP_FP_CACHE,
          "overflow and owner-retained replacement never exceed 128 entries");
}

static void test_shutdown_drains_cache_and_clock() {
    clear_state();
    RSXFPStructuralProgram p;
    seed_program(&p, 128u);
    FakePso objects[VP_FP_CACHE];
    for (unsigned i = 0; i < VP_FP_CACHE; ++i) {
        init_pso(&objects[i], i + 1u);
        VPFPEntry q;
        seed_entry(&q, &p, 0x100u + (u32)i, NULL);
        check(production_commit(0, &q, as_pso(&objects[i]), 0) == as_pso(&objects[i]),
              "shutdown fixture inserts through production commit body");
    }
    check(s_d3d.vp_fp_n == VP_FP_CACHE && s_d3d.vp_fp_clock == VP_FP_CACHE,
          "shutdown fixture reaches full cache and clock");
    production_shutdown_cache();
    check(s_d3d.vp_fp_n == 0 && s_d3d.vp_fp_clock == 0,
          "shutdown clears cache count and LRU clock");
    for (int i = 0; i < VP_FP_CACHE; ++i)
        check(objects[i].release_calls == 1 && objects[i].refs == 0 &&
              s_d3d.vp_fp[i].pso == NULL,
              "shutdown releases each resident PSO once");
}

int main() {
    test_exact_hit_and_key_guards();
    test_fifo_churn_vs_lru_hot_set();
    test_failed_create_at_capacity_is_inert();
    test_wrap_ties_and_owner_reference();
    test_shutdown_drains_cache_and_clock();
    if (failures) {
        std::fprintf(stderr, "%d native assertion(s) failed\n", failures);
        return 1;
    }
    std::printf("PASS native LRU exact-hit full-key-guards FIFO-hot-set failure retry capacity-128 wrap-ties owner-ref shutdown\n");
    return 0;
}
'''

    lookup_template = r'''
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
    int perf_trace = 0, draw_cost_trace = 0;
    LONGLONG fp_search_start = 0;
    @LOOP@
    return NULL;
}
'''
    clang = shutil.which("clang") or r"C:\Program Files\LLVM\bin\clang.exe"
    clangxx = shutil.which("clang++") or r"C:\Program Files\LLVM\bin\clang++.exe"
    if not Path(clang).exists() or not Path(clangxx).exists():
        raise RuntimeError("clang/clang++ required to compile production cache fragments")
    prefix_object_cap = prefix_cap.group(0)
    harness = (harness.replace("@PREFIX_OBJECT_CAP@", prefix_object_cap)
               .replace("@ENTRY_TYPE@", entry_type)
               .replace("@STAMP_HELPER@", find_helper)
               .replace("@SLOT_HELPER@", slot_helper)
               .replace("@OWNER_CAPTURE@", owner_capture)
               .replace("@OWNER_RELEASE@", owner_release)
               .replace("@LOOKUP_TEMPLATE@", lookup_template.replace(
                   "@NAME@", "production_find").replace("@LOOP@", hit_loop))
               .replace("@COMMIT_PATH@", commit_path)
               .replace("@SHUTDOWN_CACHE@", shutdown_path))
    with tempfile.TemporaryDirectory(prefix="tj-fp-pso-cache-lru-") as directory:
        work = Path(directory)
        cpp = work / "fp_pso_cache_lru.cpp"
        obj = work / "rsx_fp_decompiler.o"
        exe = work / "fp_pso_cache_lru.exe"
        cpp.write_text(harness, encoding="utf-8")
        subprocess.run([clang, "-std=c11", "-O2", "-I", str(INCLUDE_DIR),
                        "-c", str(FP_SOURCE), "-o", str(obj)],
                       cwd=FP_HEADER_DIR, check=True, timeout=20)
        subprocess.run([clangxx, "-std=c++17", "-O2", "-I", str(INCLUDE_DIR),
                        "-I", str(FP_HEADER_DIR), str(cpp), str(obj), "-o", str(exe)],
                       cwd=FP_HEADER_DIR, check=True, timeout=30)
        result = subprocess.run([str(exe)], cwd=FP_HEADER_DIR, check=True,
                                timeout=45, text=True, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT)
        print(f"renderer_baseline_sha256={BASELINE_SHA256}")
        print(result.stdout, end="")


if __name__ == "__main__":
    main()
