"""Opt-in native benchmark for production texture snapshot hashing.

This compares the saved pre-guard renderer with the current candidate by
extracting their real snapshot capture, write-watch, and hash functions. It
uses MEM_WRITE_WATCH guest memory and the production XXH64 implementation;
results measure snapshot work only and do not predict frame rate.
"""
from pathlib import Path
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile

from test_fifo_present_boundary import function

ROOT = Path(__file__).resolve().parents[1]
VIDEO = ROOT.parent / "ps3recomp/libs/video"
BACKEND = VIDEO / "rsx_d3d12_backend.c"
HASH_IMPL = VIDEO / "rsx_stream_hash.c"
BASELINE = Path(os.environ.get("TJ_SNAPSHOT_HASH_BASELINE", "renderer.before-snapshot-hash.c"))
SIZES = (4096, 16 * 1024, 256 * 1024, 1024 * 1024)
INITIAL_OPS = {4096: 800, 16 * 1024: 500, 256 * 1024: 30, 1024 * 1024: 25}
MAX_OPS = {4096: 16000, 16 * 1024: 6400, 256 * 1024: 640, 1024 * 1024: 120}
MIN_SAMPLE_MS = 10.0
RETAINED_MAX_OPS = {16 * 1024: 64000, 256 * 1024: 32000, 1024 * 1024: 16000}
RETIRED_MAX_OPS = {16 * 1024: 12800, 256 * 1024: 640, 1024 * 1024: 120}


def churn_helpers(source):
    start = source.find("#define VP_TEX_CHURN_PROBE_MAX_ATTEMPTS")
    if start < 0:
        start = source.find("typedef struct VPTexChurnCounters {")
    if start < 0:
        return r"""
static int vp_texture_churn_trace_enabled(void) { return 0; }
static void vp_texture_churn_trace_reset(void) {}
static void vp_texture_churn_record_new_snapshot(u64 bytes) { (void)bytes; }
static void vp_texture_churn_record_budget_reject(void) {}
static void vp_texture_churn_record_trim(u32 pass,u64 bytes,u64 freed_bytes)
{ (void)pass;(void)bytes;(void)freed_bytes; }
"""
    end = source.index("static void vp_texture_churn_trace_report(", start)
    retarget = (function(source, "static int vp_texture_snapshot_hint_retarget(") + "\n"
                if "static int vp_texture_snapshot_hint_retarget(" in source else "")
    return retarget + source[start:end]


def snapshot_hint_helpers(source):
    signatures = (
        "static u32 vp_texture_snapshot_hint_index(",
        "static VPTexSnapshot* vp_texture_snapshot_hint_find(",
        "static int vp_texture_snapshot_hint_remove(",
        "static int vp_texture_snapshot_hint_upsert(",
    )
    return "\n".join(function(source, signature) for signature in signatures
                     if signature in source)


def make_source(renderer, candidate, threshold_define):
    structure = re.search(r"typedef struct VPTexSnapshot \{.*?\} VPTexSnapshot;", renderer, re.S)
    assert structure, "production snapshot type missing"
    threshold = re.search(r"(?m)^#define VP_TEX_SNAPSHOT_HASH_REUSE_MIN_BYTES .+$", renderer)
    if candidate:
        assert threshold and threshold.group() == threshold_define
    else:
        assert threshold is None
    capture = function(renderer, "static int vp_capture_tex_snapshot_impl(")
    hash_function = function(renderer, "static u64 vp_tex_source_hash(")
    if candidate:
        assert "post_stamp == write_stamp" in capture
        assert "snapshot->source_write_stamp = write_stamp;" in capture
    else:
        assert "post_stamp == write_stamp" not in capture
        assert "snapshot->hash = vp_tex_source_hash(snapshot->data" in capture

    copy_call = "memcpy(snapshot->data, source, bytes);"
    assert capture.count(copy_call) == 1
    capture = capture.replace(
        copy_call,
        "bench_copy_bytes += bytes;\n    memcpy(snapshot->data, source, bytes);",
    )
    hash_marker = "    int perf = tj_perf_trace_enabled();"
    assert hash_function.count(hash_marker) == 1
    hash_function = hash_function.replace(
        hash_marker,
        "    ++bench_hash_calls;\n"
        "    bench_hash_bytes += (u64)row_bytes * rows;\n"
        + hash_marker,
    )

    helpers = churn_helpers(renderer) if candidate else churn_helpers("")
    return r'''
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <windows.h>
typedef uint8_t u8;
typedef uint32_t u32;
typedef uint64_t u64;
static u64 bench_hash_calls, bench_hash_bytes, bench_copy_bytes;
static u64 bench_write_watch_calls;
static UINT WINAPI bench_get_write_watch(DWORD flags, PVOID base, SIZE_T size,
    PVOID* addresses, ULONG_PTR* count, LPDWORD granularity)
{
    ++bench_write_watch_calls;
    return GetWriteWatch(flags,base,size,addresses,count,granularity);
}
#define GetWriteWatch bench_get_write_watch
#include "rsx_texture_write_watch.h"
#undef GetWriteWatch
#include "rsx_bytes_equal.h"
#include "rsx_stream_hash.h"
#define VP_TEX_SNAPSHOT_BUCKETS 1024u
#define VP_TEX_SNAPSHOT_HINTS 256u
#define VP_TEX_SNAPSHOT_HINT_WAYS 4u
#define VP_TEX_SNAPSHOT_MAX_BYTES (128ull * 1024ull * 1024ull)
#define VP_TEX_SNAPSHOT_SINGLE_MAX_BYTES (32ull * 1024ull * 1024ull)
#define VP_TEX_SNAPSHOT_RETAIN_MAX_BYTES (64ull * 1024ull * 1024ull)
#define VP_TEX_SNAPSHOT_RETAIN_MAX_ENTRIES 256u
HASH_REUSE_THRESHOLD
#define VP_TEX_SLOTS 96
STRUCTURE
typedef struct { VPTexSnapshot* source_snapshot; int used; } VPTexSlot;
static struct {
    u64 vp_tex_snapshot_bytes;
    VPTexSnapshot* vp_tex_snapshot_buckets[VP_TEX_SNAPSHOT_BUCKETS];
    VPTexSnapshot* vp_tex_snapshot_hints[VP_TEX_SNAPSHOT_HINTS];
    VPTexSnapshot* vp_tex_snapshot_pool[VP_TEX_SNAPSHOT_RETAIN_MAX_ENTRIES];
    u32 vp_tex_snapshot_pool_head, vp_tex_snapshot_pool_count;
    u64 vp_tex_snapshot_pool_bytes;
    int vp_tex_snapshot_pool_enabled;
    u32 vp_tex_snapshot_pool_suppress;
    VPTexSlot vp_tex[VP_TEX_SLOTS];
} s_d3d;
static u8* memory;
u8* vm_base;
u32 ppu_vm_size;
static RsxTextureWriteWatch s_vp_write_watch;
static u64 s_vp_watch_hits, s_vp_watch_bytes, s_vp_watch_mismatches;
typedef struct { u64 tex_hash_ticks,tex_hash_calls,tex_hash_bytes; } BenchPerfPhase;
static BenchPerfPhase s_tj_perf_phase;
static int tj_perf_trace_enabled(void) { return 0; }
static LONGLONG tj_perf_now(void) { return 0; }
static int vp_texture_churn_trace_enabled(void);
static void vp_texture_churn_trace_reset(void);
static void vp_texture_churn_record_new_snapshot(u64 bytes);
static void vp_texture_churn_record_budget_reject(void);
static void vp_texture_churn_record_trim(u32 pass,u64 bytes,u64 freed_bytes);
static int d3d12_prefix_owner_texture_reserved(u32 slot) { (void)slot; return 0; }
static int vp_tex_source_span(u32 w,u32 h,u32 depth,u32 fmt,u32 pitch,int cube,
    u32 dimension,u32 mips,int border,u32* bytes,u64* face_stride)
{
    (void)fmt;(void)pitch;(void)cube;(void)dimension;(void)mips;(void)border;
    (void)face_stride;
    if (!bytes || !w || !h || !depth) return -1;
    *bytes=w*h*depth; return 0;
}
HASH_FUNCTION
POOL_FORWARD
HINT
DISCARD
RELEASE
SET_SOURCE
CHURN_HELPERS
WATCH_MODE
STAMP
BYTE_EQUAL
TRIM
CAPTURE
static VPTexSnapshot* bench_capture(u32 off,u32 bytes)
{
    VPTexSnapshot* result=NULL;
    if(vp_capture_tex_snapshot_impl(off,bytes,1,1,0,bytes,0,2,1,0,&result)!=0 || !result){
        fprintf(stderr,"snapshot capture failed at offset %u\n",off); exit(3);
    }
    return result;
}
static void fill_source(u8* dst,u32 bytes,u32 salt)
{
    for(u32 i=0;i<bytes;++i) dst[i]=(u8)(i*37u+(i>>5)+(salt*73u));
}
static double qpc_ms(LARGE_INTEGER ticks,LARGE_INTEGER frequency)
{ return (double)ticks.QuadPart*1000.0/(double)frequency.QuadPart; }
static int measure_sample(const char* scenario,u32 bytes,u32 ops,u32 stride,
                          double* elapsed_ms)
{
    memset(&s_d3d,0,sizeof(s_d3d));
    memset(&s_tj_perf_phase,0,sizeof(s_tj_perf_phase));
    if(ResetWriteWatch(memory,ppu_vm_size)!=ERROR_SUCCESS){
        fprintf(stderr,"ResetWriteWatch failed\n"); return 0;
    }
    const u32 base=65536u;
    VPTexSnapshot* current=NULL;
    if(!strcmp(scenario,"fresh")){
        for(u32 i=0;i<ops;++i) fill_source(memory+base+i*stride,bytes,i+1u);
    }else{
        fill_source(memory+base,bytes,1u);
        if(!strcmp(scenario,"changed") || !strcmp(scenario,"retained-identical"))
            current=bench_capture(base,bytes); /* retain one warmup owner */
    }
    bench_hash_calls=bench_hash_bytes=bench_copy_bytes=bench_write_watch_calls=0;
    LARGE_INTEGER frequency,start,finish;
    QueryPerformanceFrequency(&frequency);
    QueryPerformanceCounter(&start);
    for(u32 i=0;i<ops;++i){
        const u32 off=!strcmp(scenario,"fresh")?base+i*stride:base;
        if(!strcmp(scenario,"changed")) memory[off+(i%bytes)]^=(u8)(0x5Bu+(i&0x0Fu));
        VPTexSnapshot* next=bench_capture(off,bytes);
        if(!strcmp(scenario,"fresh") || !strcmp(scenario,"retired-identical") ||
           !strcmp(scenario,"retained-identical")) vp_texture_snapshot_release(&next);
        else { vp_texture_snapshot_release(&current); current=next; }
    }
    QueryPerformanceCounter(&finish);
    *elapsed_ms=qpc_ms((LARGE_INTEGER){.QuadPart=finish.QuadPart-start.QuadPart},frequency);
    vp_texture_snapshot_release(&current);
    const int retained=!strcmp(scenario,"retained-identical");
    const u64 hashes_per_op=retained?0u:(BENCH_CANDIDATE &&
        bytes>=VP_TEX_SNAPSHOT_HASH_REUSE_MIN_BYTES?1u:2u);
    const u64 copy_bytes_per_op=retained?0u:bytes;
    const u64 watch_calls_per_op=retained?1u:(BENCH_CANDIDATE &&
        bytes>=VP_TEX_SNAPSHOT_HASH_REUSE_MIN_BYTES?2u:1u);
    if(bench_hash_calls!=(u64)ops*hashes_per_op ||
       bench_hash_bytes!=(u64)ops*bytes*hashes_per_op ||
       bench_copy_bytes!=(u64)ops*copy_bytes_per_op ||
       bench_write_watch_calls!=(u64)ops*watch_calls_per_op){
        fprintf(stderr,"counter mismatch hashes=%llu hash_bytes=%llu copy_bytes=%llu watch_calls=%llu ops=%u candidate=%d\n",
            (unsigned long long)bench_hash_calls,(unsigned long long)bench_hash_bytes,
            (unsigned long long)bench_copy_bytes,(unsigned long long)bench_write_watch_calls,
            ops,BENCH_CANDIDATE); return 0;
    }
    return 1;
}
int main(int argc,char**argv)
{
    if(argc!=4){fprintf(stderr,"usage: bench <size> <fresh|changed|retired-identical|retained-identical> <ops>\n");return 2;}
    const u32 bytes=(u32)strtoul(argv[1],NULL,10);
    const char* scenario=argv[2];
    const u32 ops=(u32)strtoul(argv[3],NULL,10);
    SYSTEM_INFO info;GetSystemInfo(&info);
    const u32 page=info.dwPageSize;
    const u32 stride=(bytes+page-1u)&~(page-1u);
    const u64 required=65536ull+(u64)(!strcmp(scenario,"fresh")?ops:1u)*stride+bytes;
    ppu_vm_size=(u32)((required+page-1u)&~((u64)page-1u));
    memory=(u8*)VirtualAlloc(NULL,ppu_vm_size,MEM_RESERVE|MEM_COMMIT|MEM_WRITE_WATCH,PAGE_READWRITE);
    if(!memory){fprintf(stderr,"MEM_WRITE_WATCH allocation failed (%lu bytes)\n",(unsigned long)ppu_vm_size);return 2;}
    vm_base=memory;
    vp_texture_churn_trace_reset();
    fill_source(memory+65536u,bytes,0xBEEFu);
    VPTexSnapshot* warmup=bench_capture(65536u,bytes);
    vp_texture_snapshot_release(&warmup);
    double samples[3];
    for(int i=0;i<3;++i) if(!measure_sample(scenario,bytes,ops,stride,&samples[i])) return 3;
    const int retained=!strcmp(scenario,"retained-identical");
    const u64 hashes_per_op=retained?0u:(BENCH_CANDIDATE &&
        bytes>=VP_TEX_SNAPSHOT_HASH_REUSE_MIN_BYTES?1u:2u);
    const u64 hash_bytes_per_op=hashes_per_op*bytes;
    const u64 copy_bytes_per_op=retained?0u:bytes;
    const u64 watch_calls_per_op=retained?1u:(BENCH_CANDIDATE &&
        bytes>=VP_TEX_SNAPSHOT_HASH_REUSE_MIN_BYTES?2u:1u);
    printf("{\"variant\":\"%s\",\"scenario\":\"%s\",\"size\":%u,\"ops\":%u,"
           "\"sample_ms\":[%.3f,%.3f,%.3f],\"hash_calls_per_op\":%llu,"
           "\"hash_bytes_per_op\":%llu,\"copy_bytes_per_op\":%llu,"
           "\"get_write_watch_calls_per_op\":%llu}\n",
        BENCH_CANDIDATE?"candidate":"baseline",scenario,bytes,ops,
        samples[0],samples[1],samples[2],(unsigned long long)hashes_per_op,
        (unsigned long long)hash_bytes_per_op,(unsigned long long)copy_bytes_per_op,
        (unsigned long long)watch_calls_per_op);
    rsx_texture_write_watch_destroy(&s_vp_write_watch);
    VirtualFree(memory,0,MEM_RELEASE);
    return 0;
}
'''.replace("STRUCTURE\n", structure.group() + "\n") \
      .replace("HASH_FUNCTION\n", hash_function + "\n") \
      .replace("POOL_FORWARD\n", "static int vp_texture_snapshot_pool_admit(VPTexSnapshot* snapshot);\n" if "static void vp_texture_snapshot_discard(" in renderer else "") \
      .replace("HINT\n", snapshot_hint_helpers(renderer) + "\n") \
      .replace("DISCARD\n", function(renderer, "static void vp_texture_snapshot_discard(") + "\n" if "static void vp_texture_snapshot_discard(" in renderer else "") \
      .replace("RELEASE\n", function(renderer, "static void vp_texture_snapshot_release(") + "\n") \
      .replace("SET_SOURCE\n", function(renderer, "static void vp_texture_cache_set_source(") + "\n") \
      .replace("CHURN_HELPERS\n", helpers + "\n") \
      .replace("WATCH_MODE\n", function(renderer, "static int vp_texture_write_watch_mode(") + "\n") \
      .replace("STAMP\n", function(renderer, "static u64 vp_texture_write_stamp(") + "\n") \
      .replace("BYTE_EQUAL\n", function(renderer, "static int vp_snapshot_bytes_equal(") + "\n") \
      .replace("TRIM\n", function(renderer, "static void vp_texture_cache_trim_sources(") + "\n") \
      .replace("CAPTURE\n", capture + "\n") \
      .replace("HASH_REUSE_THRESHOLD\n", threshold_define + "\n") \
      .replace("BENCH_CANDIDATE", "1" if candidate else "0")


def compile_variant(renderer, candidate, temp, clang, threshold_define):
    source = make_source(renderer, candidate, threshold_define)
    c_path = temp / ("snapshot_candidate.c" if candidate else "snapshot_baseline.c")
    exe_path = c_path.with_suffix(".exe")
    c_path.write_text(source, encoding="utf-8")
    subprocess.run(
        [clang, "-std=c11", "-O2", "-D_CRT_SECURE_NO_WARNINGS", "-I", str(VIDEO),
         str(c_path), str(HASH_IMPL), "-o", str(exe_path)],
        check=True,
    )
    return exe_path


def run_case(exe, variant, size, scenario, ops):
    env = dict(os.environ, TJ_TEXTURE_WRITE_WATCH="1", TJ_TEXTURE_CHURN_TRACE="0")
    result = subprocess.run(
        [str(exe), str(size), scenario, str(ops)],
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    data = json.loads(result.stdout)
    assert data["variant"] == variant
    return data


def median(values):
    ordered = sorted(values)
    return ordered[len(ordered) // 2]


def main():
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--run", action="store_true", help="compile and run the native benchmark")
    parser.add_argument("--log-out", help="write the complete console and JSON output to this path")
    parser.add_argument("--json-out", help="write the machine-readable result JSON to this path")
    args = parser.parse_args()
    if not args.run:
        parser.print_help()
        print("\nPass --run to execute; this benchmark does not launch the game or claim FPS impact.")
        return 0

    assert BASELINE.is_file(), f"saved baseline not found: {BASELINE}"
    assert HASH_IMPL.is_file(), f"production hash implementation not found: {HASH_IMPL}"
    baseline = BASELINE.read_text(encoding="utf-8")
    candidate = BACKEND.read_text(encoding="utf-8")
    threshold = re.search(r"(?m)^#define VP_TEX_SNAPSHOT_HASH_REUSE_MIN_BYTES .+$", candidate)
    assert threshold and threshold.group().endswith("(16u * 1024u)")
    threshold_define = threshold.group()
    summary_lines = []
    clang = shutil.which("clang") or r"C:\Program Files\LLVM\bin\clang.exe"
    records = []
    with tempfile.TemporaryDirectory(prefix="tj-snapshot-hash-bench-") as tmp:
        temp = Path(tmp)
        baseline_exe = compile_variant(baseline, False, temp, clang, threshold_define)
        candidate_exe = compile_variant(candidate, True, temp, clang, threshold_define)
        cases = [(size, scenario) for size in SIZES for scenario in ("fresh", "changed")]
        cases.extend(
            (size, scenario)
            for size in SIZES[1:]
            for scenario in ("retired-identical", "retained-identical")
        )
        for size, scenario in cases:
            ops = INITIAL_OPS[size]
            if scenario == "retained-identical":
                max_ops = RETAINED_MAX_OPS[size]
            elif scenario == "retired-identical":
                max_ops = RETIRED_MAX_OPS[size]
            else:
                max_ops = MAX_OPS[size]
            while True:
                old = run_case(baseline_exe, "baseline", size, scenario, ops)
                new = run_case(candidate_exe, "candidate", size, scenario, ops)
                shortest = min(old["sample_ms"] + new["sample_ms"])
                if shortest >= MIN_SAMPLE_MS or ops >= max_ops:
                    break
                ops = min(ops * 2, max_ops)
            old_median = median(old["sample_ms"]) * 1000.0 / ops
            new_median = median(new["sample_ms"]) * 1000.0 / ops
            records.append({
                "size_bytes": size,
                "scenario": scenario,
                "ops_per_sample": ops,
                "baseline_median_us_per_op": round(old_median, 3),
                "candidate_median_us_per_op": round(new_median, 3),
                "change_percent": round((new_median / old_median - 1.0) * 100.0, 2),
                "baseline_counts_per_op": {key: old[key] for key in (
                    "hash_calls_per_op", "hash_bytes_per_op", "copy_bytes_per_op",
                    "get_write_watch_calls_per_op")},
                "candidate_counts_per_op": {key: new[key] for key in (
                    "hash_calls_per_op", "hash_bytes_per_op", "copy_bytes_per_op",
                    "get_write_watch_calls_per_op")},
                "baseline_sample_ms": old["sample_ms"],
                "candidate_sample_ms": new["sample_ms"],
            })
            summary_lines.append(
                f"{size:>7} B {scenario:7} {ops:>5} ops: "
                f"{old_median:.3f} -> {new_median:.3f} us/op "
                f"({(new_median / old_median - 1.0) * 100.0:+.1f}%)"
            )
    report = {
        "baseline": str(BASELINE),
        "baseline_sha256": hashlib.sha256(BASELINE.read_bytes()).hexdigest(),
        "candidate": str(BACKEND),
        "candidate_sha256": hashlib.sha256(BACKEND.read_bytes()).hexdigest(),
        "hash_reuse_min_bytes": 16 * 1024,
        "hash": "production vp_tex_source_hash linked with rsx_stream_hash.c",
        "span_fixture": "deterministic contiguous span; w=size,h=depth=1",
        "scope": "CPU snapshot capture only; no FPS extrapolation",
        "lifetime_scenarios": {
            "retired-identical": "same unchanged guest source; each capture is released as its last owner",
            "retained-identical": "same unchanged guest source; one warmup owner keeps the immutable snapshot alive while each captured temporary reference is released",
            "limitation": "measures actual CPU snapshot lifetime behavior only; no snapshot pool or GPU cache proof",
        },
        "results": records,
    }
    report_json = json.dumps(report, indent=2)
    combined_output = "\n".join(summary_lines) + "\n" + report_json + "\n"
    print(combined_output, end="")
    if args.log_out:
        log_path = Path(args.log_out)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(combined_output, encoding="utf-8")
    if args.json_out:
        json_path = Path(args.json_out)
        json_path.parent.mkdir(parents=True, exist_ok=True)
        json_path.write_text(report_json + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
