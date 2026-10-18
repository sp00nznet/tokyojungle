"""Compare the extracted SRV writer with its pre-cache body in a native fixture."""
from pathlib import Path
import hashlib
import re
import shutil
import subprocess
import tempfile

from test_fifo_present_boundary import function


ROOT = Path(__file__).resolve().parents[1]
VIDEO = ROOT.parent / "ps3recomp/libs/video"
BASELINE_HELPER_SHA256 = "B2A242CD58ADFF0C6C8FE6A6AF22F4CAA56A25C55E1837727CF4289C73372B10"


def macro_definition(name, *sources):
    for source in sources:
        lines = source.splitlines()
        for index, line in enumerate(lines):
            if re.match(rf"^#define\s+{re.escape(name)}\b", line):
                definition = line
                while definition.rstrip().endswith("\\"):
                    index += 1
                    definition += "\n" + lines[index]
                return definition
    raise AssertionError(f"missing production macro {name}")


def main():
    backend_path = VIDEO / "rsx_d3d12_backend.c"
    backend = backend_path.read_text(encoding="utf-8")
    commands = (VIDEO / "rsx_commands.h").read_text(encoding="utf-8")
    candidate = function(backend, "static void srv_write_dimension(")
    marker = re.compile(
        r"(?m)^[ \t]*/\* TJ_NULL_SRV_REUSE_BEGIN \*/\n"
        r".*?^[ \t]*/\* TJ_NULL_SRV_REUSE_END \*/\n",
        re.S,
    )
    baseline, removed = marker.subn("", candidate)
    assert removed == 2, "cache blocks must stay explicitly bounded"
    assert hashlib.sha256(baseline.encode("utf-8")).hexdigest().upper() == BASELINE_HELPER_SHA256
    assert "d3d12_prefix_owner_object_capture(res)" in candidate
    assert candidate.index("d3d12_prefix_owner_object_capture(res)") < candidate.index("TJ_NULL_SRV_REUSE_BEGIN")
    assert candidate.count("CreateShaderResourceView(") == 1
    assert candidate.index("CreateShaderResourceView(") < candidate.index("*cached_default_null = 1")
    assert "heap_slot >= DRAW_SRV_BASE && heap_slot < SRV_HEAP_SIZE" in candidate
    assert "owner->srv_heap == s_d3d.srv_heap" in candidate
    assert "nv.Format = DXGI_FORMAT_R8_UNORM;" in backend
    assert "static int d3d12_prefix_owner_destroy(" in backend
    destroy = function(backend, "static int d3d12_prefix_owner_destroy(")
    assert "memset(owner, 0, sizeof(*owner));" in destroy
    assert destroy.index("owner->phase >= 2") < destroy.index("memset(owner, 0, sizeof(*owner));")

    macro_names = (
        "RSX_MAX_TEXTURES", "RSX_MAX_VERTEX_TEXTURES", "MAX_DRAWS",
        "DRAW_TEXTURE_COUNT", "MAX_OFF_RTS", "VP_TEX_SLOTS",
        "LEGACY_ATLAS_SRV_SLOT", "VP_TEX_SRV_BASE", "RT_SRV_BASE",
        "DRAW_SRV_BASE", "SRV_HEAP_SIZE", "SAMPLER_HEAP_LIMIT",
        "SAMPLER_TABLE_CAP", "SAMPLER_HEAP_PAGE_COUNT",
    )
    constants = "\n".join(
        macro_definition(name, backend, commands) for name in macro_names
    )
    legacy = baseline.replace("static void srv_write_dimension(",
                              "static void srv_write_dimension_baseline(", 1)
    current = candidate.replace("static void srv_write_dimension(",
                                "static void srv_write_dimension_candidate(", 1)

    fixture = r'''
#include <stdint.h>
#include <stdio.h>
#include <string.h>
typedef uint8_t u8; typedef uint32_t u32; typedef uint32_t UINT;
typedef uint64_t u64; typedef int64_t LONGLONG; typedef uint32_t DXGI_FORMAT;
CONSTANTS
#define D3D12_DEFAULT_SHADER_4_COMPONENT_MAPPING 5768u
#define DXGI_FORMAT_R8_UNORM 61u
#define DXGI_FORMAT_R8G8B8A8_UNORM 28u
#define DXGI_FORMAT_R16_FLOAT 54u
#define DXGI_FORMAT_B8G8R8A8_UNORM 87u
#define D3D12_SRV_DIMENSION_TEXTURE1D 2u
#define D3D12_SRV_DIMENSION_TEXTURE2D 4u
#define D3D12_SRV_DIMENSION_TEXTURE3D 8u
#define D3D12_SRV_DIMENSION_TEXTURECUBE 9u

typedef struct ID3D12Resource { u32 id; } ID3D12Resource;
typedef struct D3D12_CPU_DESCRIPTOR_HANDLE { u64 ptr; } D3D12_CPU_DESCRIPTOR_HANDLE;
typedef struct D3D12_TEX1D { u32 MostDetailedMip, MipLevels; float ResourceMinLODClamp; } D3D12_TEX1D;
typedef struct D3D12_TEX2D { u32 MostDetailedMip, MipLevels, PlaneSlice; float ResourceMinLODClamp; } D3D12_TEX2D;
typedef struct D3D12_TEX3D { u32 MostDetailedMip, MipLevels; float ResourceMinLODClamp; } D3D12_TEX3D;
typedef struct D3D12_TEXCUBE { u32 MostDetailedMip, MipLevels; float ResourceMinLODClamp; } D3D12_TEXCUBE;
typedef struct D3D12_SHADER_RESOURCE_VIEW_DESC {
    DXGI_FORMAT Format;
    u32 ViewDimension;
    union { D3D12_TEX1D Texture1D; D3D12_TEX2D Texture2D;
            D3D12_TEX3D Texture3D; D3D12_TEXCUBE TextureCube; };
    u32 Shader4ComponentMapping;
} D3D12_SHADER_RESOURCE_VIEW_DESC;
typedef struct ID3D12DescriptorHeap ID3D12DescriptorHeap;
typedef struct ID3D12Device ID3D12Device;
typedef struct ID3D12CommandAllocator ID3D12CommandAllocator;
typedef struct ID3D12GraphicsCommandList ID3D12GraphicsCommandList;
typedef struct ID3D12GraphicsCommandList8 ID3D12GraphicsCommandList8;
typedef struct { u32 (*Release)(void*); } GenericVtbl;
struct ID3D12CommandAllocator { GenericVtbl* lpVtbl; };
struct ID3D12GraphicsCommandList { GenericVtbl* lpVtbl; };
struct ID3D12GraphicsCommandList8 { GenericVtbl* lpVtbl; };
typedef struct ID3D12DescriptorHeapVtbl {
    void (*GetCPUDescriptorHandleForHeapStart)(ID3D12DescriptorHeap*, D3D12_CPU_DESCRIPTOR_HANDLE*);
    u32 (*Release)(ID3D12DescriptorHeap*);
} ID3D12DescriptorHeapVtbl;
struct ID3D12DescriptorHeap { ID3D12DescriptorHeapVtbl* lpVtbl; u32 id; u64 base; };
typedef struct ID3D12DeviceVtbl {
    void (*CreateShaderResourceView)(ID3D12Device*, ID3D12Resource*,
                                     D3D12_SHADER_RESOURCE_VIEW_DESC*, D3D12_CPU_DESCRIPTOR_HANDLE);
} ID3D12DeviceVtbl;
struct ID3D12Device { ID3D12DeviceVtbl* lpVtbl; };

typedef struct {
    ID3D12CommandAllocator* allocator;
    ID3D12GraphicsCommandList* list;
    ID3D12GraphicsCommandList8* list8;
    ID3D12Resource* uploads[4];
    u64 upload_first[4], upload_bytes[4];
    ID3D12DescriptorHeap* srv_heap;
    u8 draw_null_srv_cached[MAX_DRAWS * DRAW_TEXTURE_COUNT];
    ID3D12DescriptorHeap* sampler_heaps[SAMPLER_HEAP_PAGE_COUNT];
    int phase;
} D3D12PrefixOwner;
typedef struct {
    ID3D12Device* device;
    ID3D12DescriptorHeap* srv_heap;
    u32 srv_inc;
    u32 prefix_owner_active;
    D3D12PrefixOwner prefix_owners[2];
    u8 draw_null_srv_cached[MAX_DRAWS * DRAW_TEXTURE_COUNT];
} D3D12State;
static D3D12State s_d3d;
typedef struct { u64 descriptor_ticks; u32 descriptor_calls; } BackendPerfSample;
static BackendPerfSample* s_tj_gcm2d_backend_perf_sample;
static int cache_capture_fail, capture_calls, handle_calls, create_calls, release_calls;
static LONGLONG perf_clock;
static BackendPerfSample perf_sample;
static int checks, failures;
#define CHECK(test, message) do { ++checks; if (!(test)) { ++failures; fprintf(stderr, "FAIL: %s\n", message); } } while (0)

static int d3d12_prefix_owner_object_capture(void* pointer) {
    ++capture_calls;
    return cache_capture_fail && pointer ? -1 : 0;
}
static void tj_prefix_gpu_release(u32 slot) { (void)slot; }
static void d3d12_prefix_owner_release_uploads(D3D12PrefixOwner* owner) { (void)owner; }
static LONGLONG tj_gcm2d_backend_perf_subphase_begin(void) { return ++perf_clock; }
static void tj_gcm2d_backend_perf_subphase_end(u64* ticks, LONGLONG begin) {
    *ticks += (u64)(++perf_clock - begin);
}

#define HEAP_COUNT 4u
#define HEAP_STRIDE_BASE (1ull << 40)
typedef struct { u32 resource_id; D3D12_SHADER_RESOURCE_VIEW_DESC desc; } CapturedSRV;
typedef struct { u32 heap_id, slot, resource_id; D3D12_SHADER_RESOURCE_VIEW_DESC desc; } TraceEntry;
#define TRACE_MAX 4096u
static CapturedSRV descriptors[HEAP_COUNT][SRV_HEAP_SIZE];
static CapturedSRV baseline_descriptors[HEAP_COUNT][SRV_HEAP_SIZE];
static CapturedSRV candidate_descriptors[HEAP_COUNT][SRV_HEAP_SIZE];
static TraceEntry baseline_trace[TRACE_MAX], candidate_trace[TRACE_MAX];
static u32 baseline_trace_count, candidate_trace_count, trace_count;
static int trace_overflow;
static ID3D12DescriptorHeap heaps[HEAP_COUNT];
static ID3D12Resource resource = {77};
static ID3D12DescriptorHeapVtbl heap_vtbl;
static ID3D12DeviceVtbl device_vtbl;
static ID3D12Device device;
typedef void (*Writer)(u32, ID3D12Resource*, DXGI_FORMAT, UINT, u32, u32, int);
static Writer active_writer;

static void get_heap_start(ID3D12DescriptorHeap* heap, D3D12_CPU_DESCRIPTOR_HANDLE* handle) {
    ++handle_calls;
    handle->ptr = heap->base;
}
static u32 heap_release(ID3D12DescriptorHeap* heap) { (void)heap; ++release_calls; return 1; }
static void create_srv(ID3D12Device* dev, ID3D12Resource* res,
                       D3D12_SHADER_RESOURCE_VIEW_DESC* desc,
                       D3D12_CPU_DESCRIPTOR_HANDLE handle) {
    (void)dev;
    ++create_calls;
    u32 heap_id = (u32)(handle.ptr / HEAP_STRIDE_BASE);
    u64 offset = handle.ptr - (u64)heap_id * HEAP_STRIDE_BASE;
    u32 slot = (u32)(offset / s_d3d.srv_inc);
    if (heap_id >= HEAP_COUNT || slot >= SRV_HEAP_SIZE) {
        ++failures;
        fprintf(stderr, "FAIL: invalid SRV handle\n");
        return;
    }
    CapturedSRV* dst = &descriptors[heap_id][slot];
    memset(dst, 0, sizeof(*dst));
    dst->resource_id = res ? res->id : 0;
    dst->desc = *desc;
}
static void seed_descriptor(ID3D12DescriptorHeap* heap, u32 slot) {
    CapturedSRV* dst = &descriptors[heap->id][slot];
    memset(dst, 0, sizeof(*dst));
    dst->desc.Format = DXGI_FORMAT_R8_UNORM;
    dst->desc.Shader4ComponentMapping = D3D12_DEFAULT_SHADER_4_COMPONENT_MAPPING;
    dst->desc.ViewDimension = D3D12_SRV_DIMENSION_TEXTURE2D;
    dst->desc.Texture2D.MipLevels = 1;
}
static void reset_fixture(void) {
    memset(&s_d3d, 0, sizeof(s_d3d));
    memset(descriptors, 0, sizeof(descriptors));
    memset(&perf_sample, 0, sizeof(perf_sample));
    cache_capture_fail = capture_calls = handle_calls = create_calls = release_calls = 0;
    perf_clock = 0;
    s_tj_gcm2d_backend_perf_sample = NULL;
    device_vtbl.CreateShaderResourceView = create_srv;
    device.lpVtbl = &device_vtbl;
    heap_vtbl.GetCPUDescriptorHandleForHeapStart = get_heap_start;
    heap_vtbl.Release = heap_release;
    s_d3d.device = &device;
    s_d3d.srv_inc = 16;
    for (u32 id = 1; id < HEAP_COUNT; ++id) {
        heaps[id].lpVtbl = &heap_vtbl;
        heaps[id].id = id;
        heaps[id].base = (u64)id * HEAP_STRIDE_BASE;
        for (u32 slot = 0; slot < SRV_HEAP_SIZE; ++slot) seed_descriptor(&heaps[id], slot);
    }
}
static void trace_writer(u32 slot, ID3D12Resource* res, DXGI_FORMAT fmt,
                        UINT mapping, u32 mips, u32 dim, int cube) {
    active_writer(slot, res, fmt, mapping, mips, dim, cube);
    if (trace_count >= TRACE_MAX || !s_d3d.srv_heap ||
        s_d3d.srv_heap->id >= HEAP_COUNT || slot >= SRV_HEAP_SIZE) {
        trace_overflow = 1;
        return;
    }
    TraceEntry* event = &candidate_trace[trace_count++];
    event->heap_id = s_d3d.srv_heap->id;
    event->slot = slot;
    event->resource_id = descriptors[event->heap_id][slot].resource_id;
    event->desc = descriptors[event->heap_id][slot].desc;
}

static void srv_write_dimension_baseline(u32, ID3D12Resource*, DXGI_FORMAT, UINT, u32, u32, int);
static void srv_write_dimension_candidate(u32, ID3D12Resource*, DXGI_FORMAT, UINT, u32, u32, int);
OWNER_DESTROY
LEGACY_HELPER
CANDIDATE_HELPER

static void write_default(Writer writer, u32 slot) {
    writer(slot, NULL, DXGI_FORMAT_R8G8B8A8_UNORM,
           D3D12_DEFAULT_SHADER_4_COMPONENT_MAPPING, 1, 2, 0);
}
static void case_mutations(Writer writer) {
    s_d3d.prefix_owner_active = 0;
    s_d3d.srv_heap = &heaps[1];
    const u32 slot = DRAW_SRV_BASE + 7;
    write_default(writer, slot);
    CHECK(descriptors[1][slot].desc.Format == DXGI_FORMAT_R8G8B8A8_UNORM,
          "first RGBA8 null replaces the heap's initial R8 null");
    write_default(writer, slot);
    writer(slot, NULL, DXGI_FORMAT_R8G8B8A8_UNORM,
           D3D12_DEFAULT_SHADER_4_COMPONENT_MAPPING, 0, 0, 0);
    /* NULL -> resource -> NULL must invalidate and rewrite the same slot. */
    write_default(writer, slot);
    writer(slot, &resource, DXGI_FORMAT_R8G8B8A8_UNORM,
           D3D12_DEFAULT_SHADER_4_COMPONENT_MAPPING, 1, 2, 0);
    write_default(writer, slot);
    write_default(writer, slot);
    struct Variant { ID3D12Resource* resource; DXGI_FORMAT format; UINT mapping; u32 mips, dim; int cube; };
    const struct Variant variants[] = {
        {NULL, DXGI_FORMAT_R8G8B8A8_UNORM, D3D12_DEFAULT_SHADER_4_COMPONENT_MAPPING, 1, 1, 0},
        {NULL, DXGI_FORMAT_R8G8B8A8_UNORM, D3D12_DEFAULT_SHADER_4_COMPONENT_MAPPING, 1, 3, 0},
        {NULL, DXGI_FORMAT_R8G8B8A8_UNORM, D3D12_DEFAULT_SHADER_4_COMPONENT_MAPPING, 1, 2, 1},
        {NULL, DXGI_FORMAT_R8G8B8A8_UNORM, D3D12_DEFAULT_SHADER_4_COMPONENT_MAPPING, 2, 2, 0},
        {NULL, DXGI_FORMAT_R16_FLOAT, D3D12_DEFAULT_SHADER_4_COMPONENT_MAPPING, 1, 2, 0},
        {NULL, DXGI_FORMAT_R8G8B8A8_UNORM, 0x12345678u, 1, 2, 0},
        {&resource, DXGI_FORMAT_R8G8B8A8_UNORM, D3D12_DEFAULT_SHADER_4_COMPONENT_MAPPING, 1, 2, 0},
    };
    for (u32 i = 0; i < sizeof(variants) / sizeof(variants[0]); ++i) {
        write_default(writer, slot);
        writer(slot, variants[i].resource, variants[i].format, variants[i].mapping,
               variants[i].mips, variants[i].dim, variants[i].cube);
        write_default(writer, slot);
        write_default(writer, slot);
    }
    /* Capture still runs first; a failed object capture leaves the old null descriptor valid. */
    const u32 failed_slot = DRAW_SRV_BASE + 8;
    write_default(writer, failed_slot);
    write_default(writer, failed_slot);
    cache_capture_fail = 1;
    writer(failed_slot, &resource, DXGI_FORMAT_R8G8B8A8_UNORM,
           D3D12_DEFAULT_SHADER_4_COMPONENT_MAPPING, 1, 2, 0);
    cache_capture_fail = 0;
    write_default(writer, failed_slot);
    const u32 before_draw = DRAW_SRV_BASE - 1;
    write_default(writer, before_draw); write_default(writer, before_draw);
    const u32 last_draw_slot = SRV_HEAP_SIZE - 1;
    write_default(writer, last_draw_slot); write_default(writer, last_draw_slot);
}
static void case_owner_overlap(Writer writer) {
    const u32 slot = DRAW_SRV_BASE + 12;
    s_d3d.prefix_owner_active = 1; s_d3d.prefix_owners[0].srv_heap = &heaps[2]; s_d3d.srv_heap = &heaps[2];
    write_default(writer, slot);
    s_d3d.prefix_owner_active = 2; s_d3d.prefix_owners[1].srv_heap = &heaps[3]; s_d3d.srv_heap = &heaps[3];
    write_default(writer, slot);
    s_d3d.prefix_owner_active = 1; s_d3d.srv_heap = &heaps[2]; write_default(writer, slot);
    s_d3d.prefix_owner_active = 2; s_d3d.srv_heap = &heaps[3]; write_default(writer, slot);
}
static void case_owner_mismatch(Writer writer) {
    const u32 slot = DRAW_SRV_BASE + 13;
    s_d3d.prefix_owner_active = 1; s_d3d.prefix_owners[0].srv_heap = &heaps[2]; s_d3d.srv_heap = &heaps[1];
    write_default(writer, slot); write_default(writer, slot);
    s_d3d.srv_heap = &heaps[2]; write_default(writer, slot); write_default(writer, slot);
}
static void case_owner_destroy_recreate(Writer writer) {
    const u32 slot = DRAW_SRV_BASE + 14;
    s_d3d.prefix_owner_active = 1; s_d3d.prefix_owners[0].srv_heap = &heaps[2];
    s_d3d.prefix_owners[0].phase = 1; s_d3d.srv_heap = &heaps[2];
    write_default(writer, slot); write_default(writer, slot);
    CHECK(d3d12_prefix_owner_destroy(0) == 0, "unsubmitted owner destroy succeeds");
    CHECK(s_d3d.prefix_owners[0].draw_null_srv_cached[slot - DRAW_SRV_BASE] == 0,
          "owner destroy clears per-heap cache metadata");
    for (u32 i = 0; i < SRV_HEAP_SIZE; ++i) seed_descriptor(&heaps[2], i);
    s_d3d.prefix_owners[0].srv_heap = &heaps[2]; s_d3d.prefix_owners[0].phase = 1;
    s_d3d.srv_heap = &heaps[2];
    write_default(writer, slot); write_default(writer, slot);
}
static void case_owner_checked_reuse(Writer writer) {
    const u32 slot = DRAW_SRV_BASE + 15;
    s_d3d.prefix_owner_active = 1; s_d3d.prefix_owners[0].srv_heap = &heaps[2];
    s_d3d.prefix_owners[0].phase = 1; s_d3d.srv_heap = &heaps[2];
    write_default(writer, slot); write_default(writer, slot);
    s_d3d.prefix_owners[0].phase = 0;
    s_d3d.prefix_owner_active = 0; s_d3d.srv_heap = &heaps[1];
    write_default(writer, slot); write_default(writer, slot);
    s_d3d.prefix_owners[0].phase = 1;
    s_d3d.prefix_owner_active = 1; s_d3d.srv_heap = &heaps[2];
    write_default(writer, slot);
}
static void case_common_ordinary(Writer writer) {
    s_d3d.prefix_owner_active = 0; s_d3d.srv_heap = &heaps[1];
    for (u32 batch = 0; batch < 100; ++batch)
        for (u32 slot = 0; slot < 20; ++slot) write_default(writer, DRAW_SRV_BASE + slot);
}
static void case_common_owner0(Writer writer) {
    s_d3d.prefix_owner_active = 1; s_d3d.prefix_owners[0].srv_heap = &heaps[2]; s_d3d.srv_heap = &heaps[2];
    for (u32 batch = 0; batch < 100; ++batch)
        for (u32 slot = 0; slot < 20; ++slot) write_default(writer, DRAW_SRV_BASE + slot);
}
static void case_common_owner1(Writer writer) {
    s_d3d.prefix_owner_active = 2; s_d3d.prefix_owners[1].srv_heap = &heaps[3]; s_d3d.srv_heap = &heaps[3];
    for (u32 batch = 0; batch < 100; ++batch)
        for (u32 slot = 0; slot < 20; ++slot) write_default(writer, DRAW_SRV_BASE + slot);
}
static int run_pair(const char* name, void (*scenario)(Writer), u32 expected_base, u32 expected_candidate) {
    reset_fixture(); active_writer = srv_write_dimension_baseline; trace_count = 0; trace_overflow = 0;
    scenario(trace_writer);
    const u32 base_calls = create_calls, base_handles = handle_calls, base_captures = capture_calls;
    baseline_trace_count = trace_count;
    const int base_trace_overflow = trace_overflow;
    memcpy(baseline_trace, candidate_trace, trace_count * sizeof(TraceEntry));
    memcpy(baseline_descriptors, descriptors, sizeof(descriptors));
    reset_fixture(); active_writer = srv_write_dimension_candidate; trace_count = 0; trace_overflow = 0;
    scenario(trace_writer);
    const u32 candidate_calls = create_calls, candidate_handles = handle_calls, candidate_captures = capture_calls;
    candidate_trace_count = trace_count;
    memcpy(candidate_descriptors, descriptors, sizeof(descriptors));
    CHECK(base_calls == expected_base, "baseline CreateSRV call count");
    CHECK(candidate_calls == expected_candidate, "candidate CreateSRV call count");
    CHECK(base_handles == base_calls && candidate_handles == candidate_calls,
          "GetCPU handle work occurs only with actual descriptor writes");
    CHECK(base_captures == candidate_captures, "object-capture order/work remains unchanged");
    CHECK(!base_trace_overflow && !trace_overflow, "every helper call has a trace record");
    CHECK(baseline_trace_count == candidate_trace_count,
          "baseline and candidate produce one descriptor state per helper call");
    CHECK(baseline_trace_count == candidate_trace_count &&
          memcmp(baseline_trace, candidate_trace,
                 baseline_trace_count * sizeof(TraceEntry)) == 0,
          "descriptor state matches after every helper call, not only final state");
    CHECK(memcmp(baseline_descriptors, candidate_descriptors, sizeof(descriptors)) == 0,
          "all final SRV descriptors equal baseline across every heap slot");
    printf("%s: baseline=%u candidate=%u traced=%u\n",
           name, base_calls, candidate_calls, candidate_trace_count);
    return failures == 0;
}
int main(void) {
    CHECK(run_pair("descriptor transitions", case_mutations, 42, 21), "transition case passed");
    CHECK(run_pair("overlapping owner heaps", case_owner_overlap, 4, 2), "overlap case passed");
    CHECK(run_pair("owner heap mismatch", case_owner_mismatch, 4, 3), "mismatch case passed");
    CHECK(run_pair("owner destroy/recreate", case_owner_destroy_recreate, 4, 2), "lifecycle case passed");
    CHECK(run_pair("checked owner heap reuse", case_owner_checked_reuse, 5, 2), "owner reuse case passed");
    CHECK(run_pair("common ordinary heap (20 slots x 100 batches)", case_common_ordinary, 2000, 20), "ordinary common case passed");
    CHECK(run_pair("common owner 0 heap (20 slots x 100 batches)", case_common_owner0, 2000, 20), "owner0 common case passed");
    CHECK(run_pair("common owner 1 heap (20 slots x 100 batches)", case_common_owner1, 2000, 20), "owner1 common case passed");
    printf("Native null SRV reuse: %d checks, %d failures\n", checks, failures);
    return failures ? 1 : 0;
}
'''.replace("CONSTANTS", constants).replace("OWNER_DESTROY", destroy)
    fixture = fixture.replace("LEGACY_HELPER", legacy).replace("CANDIDATE_HELPER", current)

    compiler = shutil.which("clang") or r"C:\Program Files\LLVM\bin\clang.exe"
    with tempfile.TemporaryDirectory(prefix="tj-null-srv-reuse-") as directory:
        c_file = Path(directory) / "null_srv_reuse.c"
        exe = Path(directory) / "null_srv_reuse.exe"
        c_file.write_text(fixture, encoding="utf-8")
        subprocess.run([compiler, "-std=c11", str(c_file), "-o", str(exe)], check=True)
        subprocess.run([str(exe)], check=True)


if __name__ == "__main__":
    main()
