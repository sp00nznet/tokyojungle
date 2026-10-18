"""Compile production GCM2D HLE accounting and prove the lightweight cost OR gate."""
from pathlib import Path
import os
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
VIDEO = ROOT.parent / "ps3recomp" / "libs" / "video"


def definition(source, signature):
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
    raise AssertionError(f"production definition missing: {signature}")


def test_transfer_phase_mode():
    frontend = (VIDEO / "cellGcmSys.c").read_text(encoding="utf-8")
    backend = (VIDEO / "rsx_d3d12_backend.c").read_text(encoding="utf-8")
    begin = frontend.index("/* Default-off aggregate timing for the CPU NV3089 path.")
    end = frontend.index("\nstatic int gcm_scaled_image_in(u32 in_point)", begin)
    hle_accounting = frontend[begin:end]
    hle_callsite = frontend[end:frontend.index("\n/* Non-3D FIFO subchannel dispatch.", end)]

    # Keep byte accounting tied to the actual caller that snapshots guest data.
    assert "tj_gcm2d_perf_call.pre_snapshot_bytes +=" in hle_callsite
    assert "tj_gcm2d_perf_call.pre_src_snapshot_bytes += gpu.src_snapshot_bytes;" in hle_callsite
    assert "tj_gcm2d_perf_call.pre_dst_snapshot_bytes += gpu.dst_snapshot_bytes;" in hle_callsite
    assert "tj_gcm2d_perf_call.post_snapshot_bytes += packed;" in hle_callsite
    assert "tj_gcm2d_perf_record(&tj_gcm2d_perf_call, get_timestamp_ns());" in hle_callsite

    backend_cost_gate = definition(backend, "static int tj_gcm2d_backend_perf_enabled(void)")
    prefix_cost_gate = definition(backend, "static int tj_prefix_cost_enabled(void)")

    source = r'''
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdatomic.h>
#include "rsx_scaled_blit.h"
typedef uint64_t u64;
typedef int64_t LONGLONG;
typedef struct { LONGLONG QuadPart; } LARGE_INTEGER;
static LARGE_INTEGER s_tj_prefix_cost_frequency, s_tj_perf_frequency;
static int frequency_reads, checks, failures;
static u64 fake_ns = 1000000000ULL;
static u64 GetTickCount64(void) { return fake_ns / 1000000ULL; }
static int QueryPerformanceFrequency(LARGE_INTEGER* value) {
    ++frequency_reads; value->QuadPart = 1000000; return 1;
}
static u64 get_timestamp_ns(void) { return fake_ns; }
#define CHECK(c, label) do { ++checks; if (!(c)) { ++failures; fprintf(stderr, "FAIL %s\n", label); } } while (0)
PRODUCTION_HLE
PRODUCTION_BACKEND_COST
PRODUCTION_PREFIX_COST
int main(int argc, char** argv) {
    const int expected = argc > 1 ? atoi(argv[1]) : 0;
    CHECK(tj_prefix_cost_enabled() == expected, "strict TJ_PREFIX_COST gate");
    CHECK(tj_gcm2d_backend_perf_enabled() == 0, "legacy backend heavy mode remains off");
    CHECK(tj_gcm2d_perf_enabled() == expected, "HLE gate ORs exact TJ_PREFIX_COST=1");
    CHECK(frequency_reads == expected, "only prefix-cost gate initializes QPC frequency");
    if (expected) {
        TjGcm2dPerfCall first = {0}, second = {0};
        first.result = RSX_SCALED_BLIT_OK;
        first.phase_ns[TJ_GCM2D_PERF_RESOLVE_SETUP] = 1000;
        first.phase_ns[TJ_GCM2D_PERF_PRE_SNAPSHOT_ALLOC] = 2000;
        first.phase_ns[TJ_GCM2D_PERF_CPU_BLIT] = 3000;
        first.phase_ns[TJ_GCM2D_PERF_POST_SNAPSHOT] = 4000;
        first.phase_ns[TJ_GCM2D_PERF_BACKEND_RECORD] = 5000;
        first.phase_ns[TJ_GCM2D_PERF_BACKEND_COMPLETE] = 6000;
        first.phase_ns[TJ_GCM2D_PERF_GATE_END] = 7000;
        first.phase_ns[TJ_GCM2D_PERF_TOTAL] = 8000;
        first.pre_snapshot_bytes = 1200;
        first.pre_src_snapshot_bytes = 400;
        first.pre_dst_snapshot_bytes = 800;
        first.post_snapshot_bytes = 128;
        first.capture_attempted = first.capture_ready = 1;
        first.record_called = first.record_ok = 1;
        first.complete_called = first.complete_local_main = 1;
        first.gate_called = 1;

        second.result = RSX_SCALED_BLIT_UNSUPPORTED_OPERATION;
        second.phase_ns[TJ_GCM2D_PERF_RESOLVE_SETUP] = 4000;
        second.phase_ns[TJ_GCM2D_PERF_PRE_SNAPSHOT_ALLOC] = 3000;
        second.phase_ns[TJ_GCM2D_PERF_CPU_BLIT] = 2000;
        second.phase_ns[TJ_GCM2D_PERF_POST_SNAPSHOT] = 1000;
        second.phase_ns[TJ_GCM2D_PERF_BACKEND_RECORD] = 1000;
        second.phase_ns[TJ_GCM2D_PERF_BACKEND_COMPLETE] = 9000;
        second.phase_ns[TJ_GCM2D_PERF_GATE_END] = 11000;
        second.phase_ns[TJ_GCM2D_PERF_TOTAL] = 16000;
        second.pre_snapshot_bytes = 1800;
        second.pre_src_snapshot_bytes = 700;
        second.pre_dst_snapshot_bytes = 1100;
        second.post_snapshot_bytes = 256;
        second.return_failed = second.capture_attempted = 1;
        second.record_called = second.complete_called = second.complete_failed = 1;
        second.gate_called = second.gate_failed = 1;

        fake_ns = 1000000000ULL;
        tj_gcm2d_perf_record(&first, fake_ns);
        fake_ns = 2050000000ULL;
        tj_gcm2d_perf_record(&second, fake_ns);
        CHECK(s_tj_gcm2d_perf.calls == 0, "reported window resets aggregate calls");
        CHECK(s_tj_gcm2d_perf.report_start_ns == fake_ns, "next aggregate window starts at boundary");
    }
    printf("Transfer phase mode: %d checks, %d failures\n", checks, failures);
    return failures ? 1 : 0;
}
'''.replace("PRODUCTION_HLE", hle_accounting) \
   .replace("PRODUCTION_BACKEND_COST", backend_cost_gate) \
   .replace("PRODUCTION_PREFIX_COST", prefix_cost_gate)

    compiler = shutil.which("clang") or r"C:\Program Files\LLVM\bin\clang.exe"
    with tempfile.TemporaryDirectory(prefix="tj-transfer-phase-") as directory:
        c_file, exe = Path(directory) / "phase.c", Path(directory) / "phase.exe"
        c_file.write_text(source, encoding="utf-8")
        subprocess.run([compiler, "-std=c11", "-O2", "-D_CRT_SECURE_NO_WARNINGS",
                        "-I", str(VIDEO),
                        str(c_file), "-o", str(exe)], check=True)
        clean_env = dict(os.environ)
        for key in ("TJ_GCM2D_PERF", "TJ_PREFIX_COST", "TJ_PREFIX_GPU_COST",
                    "TJ_MAIN_DIRECT_READBACK", "TJ_TEXTURE_RELEASE_NOWAIT",
                    "TJ_PREFIX_COST_NOWAIT"):
            clean_env.pop(key, None)
        for value in (None, "", "0", "1x", "1"):
            env = dict(clean_env)
            if value is not None:
                env["TJ_PREFIX_COST"] = value
            expected = int(value == "1")
            result = subprocess.run([str(exe), str(expected)], env=env,
                                    check=False, capture_output=True, text=True)
            assert result.returncode == 0, result.stdout + result.stderr
            assert "0 failures" in result.stdout
            perf_lines = [line for line in result.stderr.splitlines()
                          if line.startswith("[TJ-GCM2D-PERF]")]
            if expected:
                assert len(perf_lines) == 1, result.stderr
                line = perf_lines[0]
                for expected_text in (
                    "calls=2 result={ok=1 empty=0 format=0 surface=0 operation=1}",
                    "record={calls=2 ok=1 fail=1} complete={calls=2 local_main=1 fail=1}",
                    "gate={calls=2 fail=1} bytes={pre=3000 post=384} pre_split={src=1100 dst=1900}",
                    "resolve={total_us=5 max_us=4}", "complete={total_us=15 max_us=9}",
                ):
                    assert expected_text in line, line
            else:
                assert not perf_lines and not result.stderr, result.stderr
            print(f"TJ_PREFIX_COST={value!r}: {result.stdout.strip()}")


if __name__ == "__main__":
    test_transfer_phase_mode()
