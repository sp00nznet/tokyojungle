"""Execute the real reference replay body against bounded native stubs."""
from pathlib import Path
import os
import re
import shutil
import struct
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src/tj_rsx_reference.cpp"


def function(text, signature):
    start = text.index(signature)
    brace = text.index("{", start)
    depth, end = 1, brace + 1
    while depth:
        depth += (text[end] == "{") - (text[end] == "}")
        end += 1
    return text[start:end]


def reference_fixture():
    source = SOURCE.read_text(encoding="utf-8")
    ns_start = source.index("namespace {")
    main_start = source.index("int tj_rsx_reference_main(const char* path)", ns_start)
    helpers = source[ns_start:main_start]
    replay_main = function(source, "int tj_rsx_reference_main(const char* path)")
    fixture = r'''
#include <cerrno>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <memory>
#include <stdexcept>
#include <string>
#include <vector>
#include <windows.h>

#ifndef _WIN32
#define _WIN32 1
#endif
using u8 = uint8_t;
using u32 = uint32_t;
using u64 = uint64_t;
struct CellGcmFlipTicket { u64 generation; u32 buffer_id, source; };
struct rsx_state {
    u8 vp_ucode[544 * 16];
    u32 vp_ucode_bytes, vp_dirty, depth_test_enable, depth_mask;
};

static constexpr u64 kVmReserveBytes = 0x18000000ull;
static constexpr u64 kFixtureBlockEa = 0x10000000ull;
static u8* vm_base;
static unsigned vm_init_calls, vm_commit_calls, vm_shutdown_calls;
static unsigned register_calls, state_init_calls, draw_method_calls;
static unsigned replay_begin_calls, present_calls, finish_calls, completion_calls;
static unsigned backend_init_calls, backend_shutdown_calls, register_owner_calls;
static unsigned unregister_owner_calls, ticket_mismatches, vm_block_copy_calls;
static int finish_fail_at, qpc_fail_at, qpc_backwards_at, qpf_fail;
static int qpf_calls, qpc_calls;
static long long qpc_value = 1000000;
static u64 presented_generation;

static void* fixture_memcpy(void* dst, const void* src, size_t bytes) {
    const uintptr_t address = reinterpret_cast<uintptr_t>(dst);
    const uintptr_t base = reinterpret_cast<uintptr_t>(vm_base);
    if (vm_base && address >= base + kFixtureBlockEa &&
        address < base + kFixtureBlockEa + 0x10000)
        ++vm_block_copy_calls;
    return std::memcpy(dst, src, bytes);
}
static BOOL fixture_query_frequency(LARGE_INTEGER* value) {
    ++qpf_calls; value->QuadPart = 1000000;
    return qpf_fail ? FALSE : TRUE;
}
static BOOL fixture_query_counter(LARGE_INTEGER* value) {
    ++qpc_calls;
    qpc_value += 1000;
    if (qpc_backwards_at == qpc_calls) qpc_value -= 5000;
    if (qpc_fail_at == qpc_calls) return FALSE;
    value->QuadPart = qpc_value;
    return TRUE;
}
static int fixture_putenv_s(const char*, const char*) { return 0; }

int vm_init(void) {
    ++vm_init_calls;
    if (sizeof(void*) < 8) return -1;
    vm_base = static_cast<u8*>(VirtualAlloc(nullptr, (SIZE_T)kVmReserveBytes,
                                             MEM_RESERVE, PAGE_NOACCESS));
    return vm_base ? 0 : -1;
}
int vm_commit(u64 address, u64 bytes) {
    ++vm_commit_calls;
    if (!vm_base) return -1;
    if (address <= kFixtureBlockEa && kFixtureBlockEa < address + bytes) {
        return VirtualAlloc(vm_base + kFixtureBlockEa, 0x2000,
                            MEM_COMMIT, PAGE_READWRITE) ? 0 : -1;
    }
    return 0;
}
void vm_shutdown(void) {
    ++vm_shutdown_calls;
    if (vm_base) VirtualFree(vm_base, 0, MEM_RELEASE);
    vm_base = nullptr;
}
int guest_world_register_current(const char*) { ++register_owner_calls; return 1; }
int guest_world_unregister_current(void) { ++unregister_owner_calls; return 1; }
int cellGcmInit(u32, u32, u32) { return 0; }
int cellGcmSetDisplayBuffer(u32, u32, u32, u32, u32) { return 0; }
int cellGcmSetTileInfo(u8, u32, u32, u32, u32, u32, u32, u32) { return 0; }
int cellGcmBindTile(u8) { return 0; }
int cellGcmFifoSubchannelMethod(u32, u32, u32) { return 0; }
int cellGcmReplayBeginFrame(u32 flip, CellGcmFlipTicket* out) {
    ++replay_begin_calls;
    *out = {replay_begin_calls, flip, 1};
    return 1;
}
void rsx_state_init(rsx_state* state) {
    ++state_init_calls;
    *state = {};
}
void rsx_process_method(rsx_state* state, u32 method, u32 value) {
    ++register_calls;
    if (method == 0x1808) {
        if (value) ++draw_method_calls;
        state->depth_test_enable = value != 0;
        state->depth_mask = value != 0;
    }
}
int rsx_d3d12_backend_init(u32, u32, const char*) { ++backend_init_calls; return 0; }
void rsx_d3d12_arm_survival_trace(void) {}
void rsx_d3d12_backend_present_buffer(u64 generation, u32) {
    ++present_calls; presented_generation = generation;
}
int rsx_d3d12_backend_fifo_faulted(void) { return 0; }
int rsx_d3d12_backend_finish_reference_frame(void) {
    ++finish_calls;
    return finish_calls == (unsigned)finish_fail_at ? -1 : 0;
}
void rsx_d3d12_backend_shutdown(void) { ++backend_shutdown_calls; }
int cellGcm_complete_flip_ticket(const CellGcmFlipTicket* ticket) {
    ++completion_calls;
    if (!ticket || ticket->generation != presented_generation) {
        ++ticket_mismatches; return 0;
    }
    return 1;
}

#define memcpy fixture_memcpy
#define QueryPerformanceFrequency fixture_query_frequency
#define QueryPerformanceCounter fixture_query_counter
#define _putenv_s fixture_putenv_s
HELPERS
BODY
#undef memcpy
#undef QueryPerformanceFrequency
#undef QueryPerformanceCounter
#undef _putenv_s

int main(int argc, char** argv) {
    if (argc != 3) return 2;
    const std::string mode = argv[1];
    if (mode == "finish_fail_1") finish_fail_at = 1;
    if (mode == "finish_fail_2") finish_fail_at = 2;
    if (mode == "qpc_failure") qpc_fail_at = 2;
    if (mode == "qpc_backwards") qpc_backwards_at = 2;
    if (mode == "copy_qpc_failure") qpc_fail_at = 3;
    if (mode == "copy_qpc_backwards") qpc_backwards_at = 3;
    if (mode == "qpf_failure") qpf_fail = 1;
    const int result = tj_rsx_reference_main(argv[2]);
    std::printf("[FIXTURE] mode=%s result=%d vm_init=%u vm_commit=%u vm_shutdown=%u "
                "state_init=%u method_calls=%u block_copies=%u draws=%u begins=%u presents=%u "
                "finishes=%u completions=%u ticket_mismatches=%u qpf=%d qpc=%d "
                "backend_init=%u backend_shutdown=%u owner_register=%u owner_unregister=%u\n",
                mode.c_str(), result, vm_init_calls, vm_commit_calls, vm_shutdown_calls,
                state_init_calls, register_calls, vm_block_copy_calls, draw_method_calls,
                replay_begin_calls, present_calls, finish_calls, completion_calls,
                ticket_mismatches, qpf_calls, qpc_calls, backend_init_calls,
                backend_shutdown_calls, register_owner_calls, unregister_owner_calls);
    return 0;
}
'''.replace("HELPERS", helpers).replace("BODY", replay_main)
    return fixture


def write_capture(path):
    with path.open("wb") as out:
        out.write(b"TJRSX001")
        out.write(struct.pack("<III", 2, 2, 0))
        out.write(bytes(16384 * 4))
        out.write(bytes(544 * 16))
        displays = [(1, 1, 4, 0)] + [(0, 0, 0, 0)] * 7
        for display in displays:
            out.write(struct.pack("<IIII", *display))
        out.write(struct.pack("<I", 1))
        out.write(bytes(15 * 4 * 4))
        out.write(struct.pack("<II", 0x10000000, 4))
        out.write(b"\x11\x22\x33\x44")
        out.write(struct.pack("<II", 0x10001000, 8))
        out.write(b"\x55\x66\x77\x88\x99\xAA\xBB\xCC")
        out.write(struct.pack("<III", 0x1808, 1, 2))
        out.write(struct.pack("<II", 0, 1))
        out.write(struct.pack("<III", 0xE944, 0, 0))


def parse_summary(stdout):
    line = next((line for line in stdout.splitlines() if line.startswith("[FIXTURE] ")), None)
    if line is None:
        raise AssertionError(f"native harness did not report counters:\n{stdout}")
    return dict(re.findall(r"([A-Za-z_]+)=([^ ]+)", line))


def fields(line):
    return dict(re.findall(r"([A-Za-z_]+)=([^ ]+)", line))


def run_case(exe, capture, mode, repeat, copy_timing=None):
    env = dict(os.environ)
    env.pop("TJ_RSX_REFERENCE_REPEAT", None)
    env.pop("TJ_RSX_REFERENCE_DRAW_LIMIT", None)
    env.pop("TJ_RSX_REFERENCE_COPY_TIMING", None)
    if repeat is not None:
        env["TJ_RSX_REFERENCE_REPEAT"] = repeat
    if copy_timing is not None:
        env["TJ_RSX_REFERENCE_COPY_TIMING"] = copy_timing
    result = subprocess.run([str(exe), mode, str(capture)], env=env,
                            capture_output=True, text=True, check=False, timeout=30)
    if result.returncode:
        raise AssertionError(f"harness {mode} failed ({result.returncode}):\n"
                             f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}")
    return parse_summary(result.stdout), result.stdout, result.stderr


def expect(summary, **values):
    for key, value in values.items():
        assert summary[key] == str(value), (key, value, summary)


def main():
    fixture_source = reference_fixture()
    compiler = shutil.which("clang++") or r"C:/Program Files/LLVM/bin/clang++.exe"
    with tempfile.TemporaryDirectory(prefix="tj-reference-loop-") as temp_name:
        temp = Path(temp_name)
        source = temp / "repeat_loop.cpp"
        exe = temp / "repeat_loop.exe"
        capture = temp / "minimal.tjrsx"
        source.write_text(fixture_source, encoding="utf-8")
        write_capture(capture)
        subprocess.run([compiler, "-std=c++17", "-O2", "-Wall", "-Werror",
                        "-D_CRT_SECURE_NO_WARNINGS",
                        str(source), "-o", str(exe)], check=True, timeout=45)

        one_frame_method_calls = None
        for mode, repeat in (("default_absent", None), ("default_one", "1")):
            summary, stdout, _ = run_case(exe, capture, mode, repeat, "1")
            expect(summary, result=0, vm_init=1, state_init=1, block_copies=2,
                   draws=1, begins=1, presents=1, finishes=0, completions=0,
                   qpf=0, qpc=0, backend_shutdown=1, vm_shutdown=1,
                   owner_unregister=1, ticket_mismatches=0)
            if one_frame_method_calls is None:
                one_frame_method_calls = int(summary["method_calls"])
            else:
                expect(summary, method_calls=one_frame_method_calls)
            assert "[TJ-REFERENCE] complete methods=2 draws=1 tested_depth_writes=1" in stdout

        summary, _, stderr = run_case(exe, capture, "bad_count", "3x")
        expect(summary, result=1, vm_init=0, vm_commit=0, state_init=0,
               method_calls=0, block_copies=0, begins=0, finishes=0, qpf=0, qpc=0,
               backend_init=0, vm_shutdown=0, owner_register=0)
        assert "invalid reference replay repeat count" in stderr

        summary, stdout, _ = run_case(exe, capture, "repeat_success", "3")
        expect(summary, result=0, vm_init=1, state_init=3, block_copies=6,
               draws=3, begins=3, presents=3, finishes=3, completions=3,
               qpf=1, qpc=12, ticket_mismatches=0, vm_shutdown=1)
        expect(summary, method_calls=3 * one_frame_method_calls)
        rows = [line for line in stdout.splitlines() if line.startswith("[TJ-REFERENCE] repeat=")]
        assert len(rows) == 3 and all("methods=2 draws=1 tested_depth_writes=1" in row for row in rows)
        assert all(f"repeat={i}/3 " in rows[i - 1] for i in range(1, 4))
        assert all("restore_valid=" not in row for row in rows)

        summary, stdout, _ = run_case(exe, capture, "copy_timing_on", "3", "1")
        expect(summary, result=0, vm_init=1, state_init=3, block_copies=6,
               draws=3, begins=3, presents=3, finishes=3, completions=3,
               qpf=1, qpc=18, ticket_mismatches=0, vm_shutdown=1)
        rows = [line for line in stdout.splitlines() if line.startswith("[TJ-REFERENCE] repeat=")]
        assert len(rows) == 3
        for row in rows:
            values = fields(row)
            assert (values["restore_valid"], values["restore_us"],
                    values["restore_bytes"], values["block_refs"],
                    values["block_commands"]) == ("1", "1000", "12", "2", "1"), row

        summary, stdout, _ = run_case(exe, capture, "copy_timing_nonexact", "3", "01")
        expect(summary, result=0, state_init=3, block_copies=6,
               finishes=3, completions=3, qpf=1, qpc=12)
        rows = [line for line in stdout.splitlines() if line.startswith("[TJ-REFERENCE] repeat=")]
        assert len(rows) == 3 and all("restore_valid=" not in row for row in rows)

        for mode, fail_at, expected_state, expected_qpc in (
                ("finish_fail_1", 1, 1, 3), ("finish_fail_2", 2, 2, 7)):
            summary, _, stderr = run_case(exe, capture, mode, "3")
            expect(summary, result=1, vm_init=1, state_init=expected_state,
                   block_copies=2 * expected_state, draws=expected_state,
                   begins=expected_state, presents=expected_state, finishes=fail_at,
                   completions=fail_at - 1, qpf=1, qpc=expected_qpc,
                   ticket_mismatches=0, vm_shutdown=1, backend_shutdown=1,
                   owner_unregister=1)
            expect(summary, method_calls=expected_state * one_frame_method_calls)
            assert "reference replay completion failed" in stderr

        for mode in ("qpc_failure", "qpc_backwards", "qpf_failure"):
            summary, stdout, _ = run_case(exe, capture, mode, "3")
            expect(summary, result=0, state_init=3, block_copies=6,
                   begins=3, finishes=3, completions=3, ticket_mismatches=0)
            expect(summary, method_calls=3 * one_frame_method_calls)
            rows = [line for line in stdout.splitlines() if line.startswith("[TJ-REFERENCE] repeat=")]
            assert len(rows) == 3 and "hostelapsed_valid=0" in rows[0], (mode, rows)

        for mode in ("copy_qpc_failure", "copy_qpc_backwards"):
            summary, stdout, _ = run_case(exe, capture, mode, "3", "1")
            expect(summary, result=0, state_init=3, block_copies=6,
                   begins=3, finishes=3, completions=3, ticket_mismatches=0,
                   qpf=1, qpc=18)
            rows = [line for line in stdout.splitlines() if line.startswith("[TJ-REFERENCE] repeat=")]
            assert len(rows) == 3, (mode, rows)
            first = fields(rows[0])
            assert (first["restore_valid"], first["restore_us"], first["restore_bytes"],
                    first["block_refs"], first["block_commands"]) == (
                        "0", "NA", "12", "2", "1"), (mode, rows[0])
            for row in rows[1:]:
                values = fields(row)
                assert (values["restore_valid"], values["restore_us"],
                        values["restore_bytes"], values["block_refs"],
                        values["block_commands"]) == ("1", "1000", "12", "2", "1"), (mode, row)

    print("PASS: actual reference loop ordering, failure stop, repeat counts, default-off costs, exact copy-timing gate, restore totals, bad-env pre-VM, and invalid-QPC containment")


if __name__ == "__main__":
    main()
