"""Compile and exercise the renderer's real texture-churn counter helpers."""
from pathlib import Path
import os
import re
import shutil
import subprocess
import tempfile

from test_present_interval_trace import function, type_declaration


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT.parent / "ps3recomp"
PREFIX = "[TJ-TEXTURE-CHURN]"
OCCUPANCY_FIELDS = (
    "occupied_complete_slots", "empty_slots", "incomplete_slots",
    "slots_with_source_identity", "snapshot_live_bytes",
)
COUNTERS = (
    "new_snapshots", "new_snapshot_bytes", "budget_rejections",
    "trim_p0_ref_drops", "trim_p0_ref_drop_bytes", "trim_p0_frees",
    "trim_p0_freed_bytes", "trim_p1_ref_drops", "trim_p1_ref_drop_bytes",
    "trim_p1_frees", "trim_p1_freed_bytes", "upload_misses",
    "upload_source_bytes", "same_snapshot_other_off_requests",
    "same_off_other_identity_hash_equal_requests",
    "same_off_other_identity_hash_different_requests", "fresh_allocations",
    "reused_storage_uploads", "shape_plain_linear_uploads",
    "shape_plain_linear_padded_uploads", "shape_other_uploads",
    "same_off_full_layout_candidate_uploads", "logical_compare_attempts",
    "logical_compare_equal", "logical_compare_different", "logical_compare_capped",
    "snapshot_pool_admissions", "snapshot_pool_admission_bytes",
    "snapshot_pool_hint_reuses", "snapshot_pool_hint_reuse_bytes",
    "snapshot_pool_hash_reuses", "snapshot_pool_hash_reuse_bytes",
    "snapshot_pool_hash_same_off_reuses", "snapshot_pool_hash_other_off_reuses",
    "snapshot_pool_hint_retargets",
    "snapshot_pool_cap_evictions", "snapshot_pool_cap_owner_drop_bytes",
    "snapshot_pool_budget_reclaims", "snapshot_pool_budget_owner_drop_bytes",
    "snapshot_pool_oversize_skips", "snapshot_pool_oversize_skip_bytes",
)
POOL_STATE_FIELDS = (
    "snapshot_pool_enabled", "snapshot_pool_entries", "snapshot_pool_bytes",
)


def fields(line):
    return dict(re.findall(r"(\w+)=([^ ]*)", line))


def run_case(exe, mode, enabled=None):
    env = dict(os.environ)
    env.pop("TJ_TEXTURE_CHURN_TRACE", None)
    if enabled is not None:
        env["TJ_TEXTURE_CHURN_TRACE"] = enabled
    result = subprocess.run([str(exe), mode], env=env, capture_output=True,
                            text=True, check=True)
    lines = [line for line in result.stderr.splitlines()
             if line.startswith(PREFIX)]
    return result.stdout, lines


def assert_zero_counters(record):
    assert {name: record[name] for name in COUNTERS} == {
        name: "0" for name in COUNTERS
    }, record


def assert_occupancy(record, expected):
    assert {name: record[name] for name in OCCUPANCY_FIELDS} == dict(
        zip(OCCUPANCY_FIELDS, map(str, expected))), record


def assert_pool_state(record, expected):
    assert {name: record[name] for name in POOL_STATE_FIELDS} == dict(
        zip(POOL_STATE_FIELDS, map(str, expected))), record


def main():
    backend = (RUNTIME / "libs/video/rsx_d3d12_backend.c").read_text(encoding="utf-8")
    counter_type = type_declaration(backend, "VPTexChurnCounters")
    enum_start = backend.index("typedef enum VPTexChurnProbeResult")
    enum_end = backend.index("} VPTexChurnProbeResult;", enum_start) + len(
        "} VPTexChurnProbeResult;"
    )
    probe_enum = backend[enum_start:enum_end]
    probe_constants = "\n".join(re.findall(
        r"(?m)^#define VP_TEX_CHURN_PROBE_[A-Z0-9_]+\s+[^\r\n]+",
        backend,
    ))
    budget_state = "\n".join(re.findall(
        r"(?m)^static (?:u32|u64) s_vp_texture_churn_probe_window_(?:attempts|bytes);",
        backend,
    ))
    assert probe_constants and len(budget_state.splitlines()) == 2
    upload = function(backend, "static int vp_upload_tex_slot(")
    assert upload.count("vp_texture_churn_probe_snapshot_rows(") == 1
    assert upload.count("vp_texture_churn_record_upload_probe(") == 1
    assert re.search(
        r"if\s*\(churn_trace\s*&&\s*churn_plain_linear_shape\s*&&\s*"
        r"churn_old_sameoff_snapshot\s*\)\s*"
        r"churn_probe_result\s*=\s*vp_texture_churn_probe_snapshot_rows\(",
        upload,
    ), "logical comparison must stay opt-in and after exact-hit return"
    assert re.search(
        r"if\s*\(churn_trace\)\s*\{(?:(?!\n\s{4}\}).)*?"
        r"vp_texture_churn_record_upload_probe\(", upload, re.S
    ), "shape and comparison counters must commit only at the completed-miss tail"
    assert re.search(
        r"if\s*\(churn_trace\s*&&\s*churn_plain_linear_shape\s*&&\s*"
        r"!churn_old_sameoff_snapshot\s*&&\s*cached->off\s*==\s*off\s*&&\s*"
        r"cached->source_hash\s*!=\s*source_hash\s*&&\s*cached->source_snapshot",
        upload,
    ), "candidate capture must be same-offset, changed-hash, and plain-linear"
    for layout_field in ("cached->dxgi == (u32)dxfmt", "cached->w == w",
                         "cached->h == h", "cached->depth == depth",
                         "cached->fmt == fmt", "cached->pitch == src_pitch",
                         "cached->mips == mip_count", "cached->cube == cube",
                         "cached->dimension == dimension",
                         "cached->has_border == has_border"):
        assert layout_field in upload, f"full-layout candidate gate lost {layout_field}"
    compare_pos = upload.index("vp_texture_churn_probe_snapshot_rows(")
    record_pos = upload.index("vp_texture_churn_record_upload_probe(")
    source_commit_pos = upload.index("vp_texture_cache_set_source(t, source_snapshot);")
    exact_hit_return_pos = upload.index("return i;")
    assert exact_hit_return_pos < compare_pos < source_commit_pos < record_pos
    signatures = (
        "static void vp_texture_churn_probe_budget_reset(",
        "static int vp_texture_churn_probe_budget_reserve(",
        "static int vp_texture_churn_trace_enabled(",
        "static VPTexChurnProbeResult vp_texture_churn_probe_snapshot_rows(",
        "static void vp_texture_churn_trace_reset(",
        "static u64 vp_texture_churn_take(",
        "static void vp_texture_churn_record_new_snapshot(",
        "static void vp_texture_churn_record_budget_reject(",
        "static void vp_texture_churn_record_trim(",
        "static void vp_texture_churn_record_upload_candidates(",
        "static void vp_texture_churn_record_upload_miss(",
        "static void vp_texture_churn_record_upload_probe(",
        "static void vp_texture_churn_trace_report(",
    )
    helpers = "\n".join(function(backend, signature) for signature in signatures)

    source = r'''
#include <windows.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
typedef uint32_t u32;
typedef uint64_t u64;
typedef uint8_t u8;
#define VP_TEX_SLOTS 96
/* Minimal shape read by the production report helper. */
typedef struct VPTexSlot {
    void* res;
    void* up;
    void* source_snapshot;
} VPTexSlot;
static struct {
    VPTexSlot vp_tex[VP_TEX_SLOTS];
    u64 vp_tex_snapshot_bytes;
    u32 vp_tex_snapshot_pool_count;
    u64 vp_tex_snapshot_pool_bytes;
    int vp_tex_snapshot_pool_enabled;
} s_d3d;
typedef struct VPTexSnapshot {
    u8* data;
    u32 bytes;
    u32 refs;
} VPTexSnapshot;
PROBE_CONSTANTS
PROBE_ENUM
COUNTER_TYPE
static VPTexChurnCounters s_vp_texture_churn;
static int s_vp_texture_churn_enabled;
PROBE_BUDGET_STATE
HELPERS
static int fixture_resource, fixture_upload, fixture_snapshot;
static void setup_texture_slots(void) {
    memset(&s_d3d, 0, sizeof(s_d3d));
    for (int i = 0; i < 60; ++i) {
        s_d3d.vp_tex[i].res = &fixture_resource;
        s_d3d.vp_tex[i].up = &fixture_upload;
        if (i < 20) s_d3d.vp_tex[i].source_snapshot = &fixture_snapshot;
    }
    for (int i = 60; i < 66; ++i) {
        if (i < 63) s_d3d.vp_tex[i].res = &fixture_resource;
        else s_d3d.vp_tex[i].up = &fixture_upload;
    }
    s_d3d.vp_tex[60].source_snapshot = &fixture_snapshot;
    s_d3d.vp_tex[63].source_snapshot = &fixture_snapshot;
    s_d3d.vp_tex_snapshot_bytes = 8192;
    s_d3d.vp_tex_snapshot_pool_enabled = 1;
    s_d3d.vp_tex_snapshot_pool_count = 4;
    s_d3d.vp_tex_snapshot_pool_bytes = 4096;
}
static void change_texture_slots(void) {
    s_d3d.vp_tex[0].res = NULL;
    s_d3d.vp_tex[0].up = NULL;
    s_d3d.vp_tex[60].up = &fixture_upload;
    s_d3d.vp_tex[66].source_snapshot = &fixture_snapshot;
    s_d3d.vp_tex_snapshot_bytes = 7777;
    s_d3d.vp_tex_snapshot_pool_count = 2;
    s_d3d.vp_tex_snapshot_pool_bytes = 1536;
}
static void seed_pool_counters(void) {
    InterlockedIncrement64(&s_vp_texture_churn.snapshot_pool_admissions);
    InterlockedAdd64(&s_vp_texture_churn.snapshot_pool_admission_bytes, 128);
    InterlockedIncrement64(&s_vp_texture_churn.snapshot_pool_hint_reuses);
    InterlockedAdd64(&s_vp_texture_churn.snapshot_pool_hint_reuse_bytes, 256);
    InterlockedIncrement64(&s_vp_texture_churn.snapshot_pool_hash_reuses);
    InterlockedAdd64(&s_vp_texture_churn.snapshot_pool_hash_reuses, 4);
    InterlockedAdd64(&s_vp_texture_churn.snapshot_pool_hash_same_off_reuses, 2);
    InterlockedAdd64(&s_vp_texture_churn.snapshot_pool_hash_other_off_reuses, 3);
    InterlockedAdd64(&s_vp_texture_churn.snapshot_pool_hint_retargets, 4);
    InterlockedAdd64(&s_vp_texture_churn.snapshot_pool_hash_reuse_bytes, 512);
    InterlockedIncrement64(&s_vp_texture_churn.snapshot_pool_cap_evictions);
    InterlockedAdd64(&s_vp_texture_churn.snapshot_pool_cap_owner_drop_bytes, 1024);
    InterlockedIncrement64(&s_vp_texture_churn.snapshot_pool_budget_reclaims);
    InterlockedAdd64(&s_vp_texture_churn.snapshot_pool_budget_owner_drop_bytes, 2048);
    InterlockedIncrement64(&s_vp_texture_churn.snapshot_pool_oversize_skips);
    InterlockedAdd64(&s_vp_texture_churn.snapshot_pool_oversize_skip_bytes, 4096);
}
#define CHECK(c, label) do { if (!(c)) { fprintf(stderr, "FAIL: %s\n", label); return 1; } } while (0)
int main(int argc, char** argv) {
    if (argc != 2) return 2;
    if (!strcmp(argv[1], "off")) {
        vp_texture_churn_trace_reset();
        CHECK(!vp_texture_churn_trace_enabled(), "trace enabled without environment opt-in");
        VPTexSnapshot* poison_old=(VPTexSnapshot*)(uintptr_t)1;
        VPTexSnapshot* poison_new=(VPTexSnapshot*)(uintptr_t)2;
        CHECK(vp_texture_churn_probe_snapshot_rows(poison_old,poison_new,4,2,8,16)==
              VP_TEX_CHURN_PROBE_NONE,
              "trace-off probe returns before dereferencing snapshots");
        CHECK(!s_vp_texture_churn_probe_window_attempts &&
              !s_vp_texture_churn_probe_window_bytes,
              "trace-off probe does not consume its comparison budget");
        vp_texture_churn_trace_report(123, 456, 7);
        puts("PASS: default-off report is suppressed");
        return 0;
    }
    if (!strcmp(argv[1], "reset")) {
        vp_texture_churn_trace_reset();
        CHECK(vp_texture_churn_trace_enabled(), "reset fixture needs trace opt-in");
        vp_texture_churn_record_new_snapshot(41);
        vp_texture_churn_record_budget_reject();
        vp_texture_churn_record_trim(0, 120, 0);
        vp_texture_churn_record_trim(1, 80, 80);
        vp_texture_churn_record_upload_candidates(1, 1, 1);
        vp_texture_churn_record_upload_miss(55, 1);
        vp_texture_churn_record_upload_miss(66, 0);
        seed_pool_counters();
        vp_texture_churn_trace_reset();
        CHECK(vp_texture_churn_trace_enabled(), "reset unexpectedly disabled opted-in trace");
        setup_texture_slots();
        vp_texture_churn_trace_report(777, 333, 44);
        puts("PASS: reset cleared seeded counters and kept absolute report metadata");
        return 0;
    }
    if (!strcmp(argv[1], "delta")) {
        vp_texture_churn_trace_reset();
        CHECK(vp_texture_churn_trace_enabled(), "delta fixture needs trace opt-in");
        vp_texture_churn_record_new_snapshot(64);
        vp_texture_churn_record_new_snapshot(256);
        vp_texture_churn_record_budget_reject();
        vp_texture_churn_record_budget_reject();
        vp_texture_churn_record_trim(0, 120, 0);
        vp_texture_churn_record_trim(0, 300, 300);
        vp_texture_churn_record_trim(1, 80, 80);
        /* A single miss may contribute to several independently useful candidate classes. */
        vp_texture_churn_record_upload_candidates(1, 1, 1);
        vp_texture_churn_record_upload_candidates(1, 0, 1);
        vp_texture_churn_record_upload_miss(1024, 1);
        vp_texture_churn_record_upload_miss(2048, 0);
        seed_pool_counters();
        setup_texture_slots();
        vp_texture_churn_trace_report(1234, 9001, 1000);
        change_texture_slots();
        vp_texture_churn_trace_report(2345, 9014, 1111);
        puts("PASS: event deltas, overlapping candidates, and report reset");
        return 0;
    }
    if (!strcmp(argv[1], "probe")) {
        vp_texture_churn_trace_reset();
        CHECK(vp_texture_churn_trace_enabled(), "probe fixture needs trace opt-in");
        u8 old_bytes[16],new_bytes[16];
        for (u32 i=0;i<16;++i) old_bytes[i]=new_bytes[i]=(u8)(i+1);
        for (u32 i=4;i<8;++i) new_bytes[i]^=0x5a; /* Row padding only. */
        for (u32 i=12;i<16;++i) new_bytes[i]^=0xa5; /* Final-row padding. */
        VPTexSnapshot old_snapshot={old_bytes,sizeof(old_bytes),1};
        VPTexSnapshot new_snapshot={new_bytes,sizeof(new_bytes),1};
        vp_texture_churn_probe_budget_reset();
        CHECK(vp_texture_churn_probe_snapshot_rows(&old_snapshot,&new_snapshot,4,2,8,16)==
              VP_TEX_CHURN_PROBE_EQUAL,
              "padding-only byte changes compare equal across logical rows");
        CHECK(s_vp_texture_churn_probe_window_attempts==1 &&
              s_vp_texture_churn_probe_window_bytes==8,
              "logical comparison budgets pixels, not row padding");

        vp_texture_churn_probe_budget_reset();
        new_bytes[9]^=1; /* A consumed pixel byte in row two. */
        CHECK(vp_texture_churn_probe_snapshot_rows(&old_snapshot,&new_snapshot,4,2,8,16)==
              VP_TEX_CHURN_PROBE_DIFFERENT,
              "consumed pixel mutation compares different");

        VPTexSnapshot short_old={old_bytes,11,1},short_new={new_bytes,11,1};
        vp_texture_churn_probe_budget_reset();
        CHECK(vp_texture_churn_probe_snapshot_rows(&short_old,&short_new,4,2,8,16)==
              VP_TEX_CHURN_PROBE_NONE,
              "short snapshot buffers reject before comparison");
        CHECK(!s_vp_texture_churn_probe_window_attempts &&
              !s_vp_texture_churn_probe_window_bytes,
              "invalid buffers do not consume probe budget");
        CHECK(vp_texture_churn_probe_snapshot_rows(&old_snapshot,&new_snapshot,4,2,3,16)==
              VP_TEX_CHURN_PROBE_NONE,
              "pitch shorter than logical row is rejected");
        CHECK(vp_texture_churn_probe_snapshot_rows(&old_snapshot,&old_snapshot,4,2,8,16)==
              VP_TEX_CHURN_PROBE_NONE,
              "same snapshot pointer is not compared against itself");
        VPTexSnapshot huge_old={old_bytes,UINT32_MAX,1},huge_new={new_bytes,UINT32_MAX,1};
        CHECK(vp_texture_churn_probe_snapshot_rows(&huge_old,&huge_new,UINT32_MAX,
              UINT32_MAX,UINT32_MAX,UINT32_MAX)==VP_TEX_CHURN_PROBE_NONE,
              "extreme row-span arithmetic rejects before touching buffers");

        VPTexSnapshot capped_old={old_bytes,UINT32_MAX,1};
        VPTexSnapshot capped_new={new_bytes,UINT32_MAX,1};
        vp_texture_churn_probe_budget_reset();
        CHECK(vp_texture_churn_probe_snapshot_rows(&capped_old,&capped_new,
              (1u<<20)+1u,1,(1u<<20)+1u,(1u<<20)+1u)==VP_TEX_CHURN_PROBE_CAPPED,
              "single logical comparison larger than one MiB is capped before reading");

        vp_texture_churn_probe_budget_reset();
        CHECK(!vp_texture_churn_probe_budget_reserve(0) &&
              !vp_texture_churn_probe_budget_reserve((1ull<<20)+1ull) &&
              !s_vp_texture_churn_probe_window_attempts &&
              !s_vp_texture_churn_probe_window_bytes,
              "zero-byte and over-one-MiB reservations are refused without budget use");
        for (u32 i=0;i<8;++i)
            CHECK(vp_texture_churn_probe_budget_reserve(1ull<<20),
                  "each one-MiB window reservation fits the eight-MiB cap");
        CHECK(!vp_texture_churn_probe_budget_reserve(1) &&
              s_vp_texture_churn_probe_window_attempts==8 &&
              s_vp_texture_churn_probe_window_bytes==(8ull<<20),
              "aggregate byte cap rejects a ninth MiB");
        vp_texture_churn_probe_budget_reset();
        for (u32 i=0;i<16;++i)
            CHECK(vp_texture_churn_probe_budget_reserve(1),
                  "each small reservation fits the attempt budget");
        CHECK(!vp_texture_churn_probe_budget_reserve(1) &&
              s_vp_texture_churn_probe_window_attempts==16,
              "seventeenth comparison is capped even below byte limit");

        vp_texture_churn_trace_reset();
        CHECK(vp_texture_churn_trace_enabled(), "trace reset unexpectedly disabled probe report");
        vp_texture_churn_record_upload_probe(1,0,1,VP_TEX_CHURN_PROBE_EQUAL);
        vp_texture_churn_record_upload_probe(1,1,1,VP_TEX_CHURN_PROBE_DIFFERENT);
        vp_texture_churn_record_upload_probe(0,0,1,VP_TEX_CHURN_PROBE_NONE);
        vp_texture_churn_record_upload_probe(1,0,0,VP_TEX_CHURN_PROBE_CAPPED);
        for (u32 i=0;i<16;++i)
            CHECK(vp_texture_churn_probe_budget_reserve(1),
                  "report-reset case fills attempt budget before reporting");
        CHECK(!vp_texture_churn_probe_budget_reserve(1),
              "report-reset case reaches attempt cap");
        setup_texture_slots();
        vp_texture_churn_trace_report(3456,12345,1000);
        CHECK(vp_texture_churn_probe_budget_reserve(1),
              "report resets the per-window comparison budget");
        setup_texture_slots();
        vp_texture_churn_trace_report(4567,12346,1000);
        puts("PASS: row comparisons, invalid spans, caps, and interval reset");
        return 0;
    }
    return 2;
}
'''.replace("PROBE_CONSTANTS", probe_constants).replace(
        "PROBE_ENUM", probe_enum
    ).replace("COUNTER_TYPE", counter_type).replace(
        "PROBE_BUDGET_STATE", budget_state
    ).replace("HELPERS", helpers)

    clang = shutil.which("clang") or r"C:\Program Files\LLVM\bin\clang.exe"
    with tempfile.TemporaryDirectory(prefix="tj-texture-churn-") as temp:
        c = Path(temp) / "texture_churn.c"
        exe = Path(temp) / "texture_churn.exe"
        c.write_text(source, encoding="utf-8")
        subprocess.run([clang, "-std=c11", "-O2", "-D_CRT_SECURE_NO_WARNINGS",
                        str(c), "-o", str(exe)], check=True)

        stdout, lines = run_case(exe, "off")
        assert not lines, lines
        assert "PASS:" in stdout

        stdout, lines = run_case(exe, "reset", "1")
        assert len(lines) == 1, lines
        reset_line = fields(lines[0])
        assert (reset_line["t_ms"], reset_line["frame_count"],
                reset_line["interval_ms"]) == ("777", "333", "44"), reset_line
        assert_zero_counters(reset_line)
        assert_occupancy(reset_line, (60, 30, 6, 22, 8192))
        assert_pool_state(reset_line, (1, 4, 4096))
        assert "PASS:" in stdout

        stdout, lines = run_case(exe, "delta", "1")
        assert len(lines) == 2, lines
        first, second = map(fields, lines)
        assert (first["t_ms"], first["frame_count"], first["interval_ms"]) == (
            "1234", "9001", "1000")
        assert_occupancy(first, (60, 30, 6, 22, 8192))
        assert_pool_state(first, (1, 4, 4096))
        expected = {
            "new_snapshots": "2", "new_snapshot_bytes": "320",
            "budget_rejections": "2",
            "trim_p0_ref_drops": "2", "trim_p0_ref_drop_bytes": "420",
            "trim_p0_frees": "1", "trim_p0_freed_bytes": "300",
            "trim_p1_ref_drops": "1", "trim_p1_ref_drop_bytes": "80",
            "trim_p1_frees": "1", "trim_p1_freed_bytes": "80",
            "upload_misses": "2", "upload_source_bytes": "3072",
            "same_snapshot_other_off_requests": "2",
            "same_off_other_identity_hash_equal_requests": "1",
            "same_off_other_identity_hash_different_requests": "2",
            "fresh_allocations": "1", "reused_storage_uploads": "1",
            "shape_plain_linear_uploads": "0",
            "shape_plain_linear_padded_uploads": "0", "shape_other_uploads": "0",
            "same_off_full_layout_candidate_uploads": "0",
            "logical_compare_attempts": "0", "logical_compare_equal": "0",
            "logical_compare_different": "0", "logical_compare_capped": "0",
            "snapshot_pool_admissions": "1", "snapshot_pool_admission_bytes": "128",
            "snapshot_pool_hint_reuses": "1", "snapshot_pool_hint_reuse_bytes": "256",
            "snapshot_pool_hash_reuses": "5", "snapshot_pool_hash_reuse_bytes": "512",
            "snapshot_pool_hash_same_off_reuses": "2",
            "snapshot_pool_hash_other_off_reuses": "3",
            "snapshot_pool_hint_retargets": "4",
            "snapshot_pool_cap_evictions": "1", "snapshot_pool_cap_owner_drop_bytes": "1024",
            "snapshot_pool_budget_reclaims": "1", "snapshot_pool_budget_owner_drop_bytes": "2048",
            "snapshot_pool_oversize_skips": "1", "snapshot_pool_oversize_skip_bytes": "4096",
        }
        assert {name: first[name] for name in COUNTERS} == expected, first
        assert (second["t_ms"], second["frame_count"], second["interval_ms"]) == (
            "2345", "9014", "1111")
        assert_zero_counters(second)
        assert_occupancy(second, (60, 31, 5, 23, 7777))
        assert_pool_state(second, (1, 2, 1536))
        assert "PASS:" in stdout

        stdout, lines = run_case(exe, "probe", "1")
        assert len(lines) == 2, lines
        first, second = map(fields, lines)
        assert (first["t_ms"], first["frame_count"], first["interval_ms"]) == (
            "3456", "12345", "1000")
        assert_occupancy(first, (60, 30, 6, 22, 8192))
        assert_pool_state(first, (1, 4, 4096))
        expected_probe = {
            "shape_plain_linear_uploads": "3",
            "shape_plain_linear_padded_uploads": "1",
            "shape_other_uploads": "1",
            "same_off_full_layout_candidate_uploads": "3",
            "logical_compare_attempts": "2", "logical_compare_equal": "1",
            "logical_compare_different": "1", "logical_compare_capped": "1",
        }
        assert {name: first[name] for name in expected_probe} == expected_probe, first
        assert {name: first[name] for name in COUNTERS if name not in expected_probe} == {
            name: "0" for name in COUNTERS if name not in expected_probe
        }, first
        assert (second["t_ms"], second["frame_count"], second["interval_ms"]) == (
            "4567", "12346", "1000")
        assert_zero_counters(second)
        assert_occupancy(second, (60, 30, 6, 22, 8192))
        assert_pool_state(second, (1, 4, 4096))
        assert "PASS:" in stdout

    print("PASS: 4 native texture-churn cases; production counters, row probes, and reports verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
