"""Native guard for the isolated reference replay completion boundary."""
from pathlib import Path
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
VIDEO = ROOT.parent / "ps3recomp/libs/video"


def function(text, signature):
    start = text.index(signature)
    brace = text.index("{", start)
    depth, end = 1, brace + 1
    while depth:
        depth += (text[end] == "{") - (text[end] == "}")
        end += 1
    return text[start:end]


def main():
    backend = (VIDEO / "rsx_d3d12_backend.c").read_text(encoding="utf-8")
    header = (VIDEO / "rsx_d3d12_backend.h").read_text(encoding="utf-8")
    reference = (ROOT / "src/tj_rsx_reference.cpp").read_text(encoding="utf-8")
    assert "int rsx_d3d12_backend_finish_reference_frame(void);" in header
    assert "int rsx_d3d12_backend_finish_reference_frame(void) { return -1; }" in backend
    finish = function(backend, "int rsx_d3d12_backend_finish_reference_frame(void)")
    assert "s_d3d.draw_count" in finish and "s_d3d.texture_label_count" in finish
    assert "s_d3d.texture_label_armed" in finish and "s_exact_writeback_count" in finish
    assert "s_d3d.prefix_owner_active" in finish and "s_d3d.prefix_prepare_owner" in finish
    assert "s_d3d.prefix_retired_sequence != s_d3d.prefix_submit_sequence" in finish
    assert "s_d3d.prefix_owners[0].phase" in finish and "s_d3d.prefix_owners[1].phase" in finish
    drain = finish.index("d3d12_prefix_overlap_drain(RSX_COMPLETION_CAPTURE)")
    wait = finish.index("wait_for_gpu_checked()")
    clear = finish.index("s_d3d.fifo_prefix_outstanding = 0")
    assert drain < wait < clear
    assert "reference frame pending work" in finish and "reference frame fence" in finish

    default = reference[reference.index("if (repeats==1)"):reference.index("} else {", reference.index("if (repeats==1)"))]
    assert "QueryPerformance" not in default and "present_once(result);" in default
    loop = reference[reference.index("for (u32 repeat=0; repeat<repeats; ++repeat)"):]
    assert loop.index("QueryPerformanceCounter(&marks[0])") < loop.index("dispatch_once(copy_timing)")
    assert loop.index("dispatch_once(copy_timing)") < loop.index("QueryPerformanceCounter(&marks[1])")
    assert loop.index("QueryPerformanceCounter(&marks[1])") < loop.index("present_once(current)")
    assert loop.index("present_once(current)") < loop.index("QueryPerformanceCounter(&marks[2])")
    assert loop.index("QueryPerformanceCounter(&marks[2])") < loop.index(
        "rsx_d3d12_backend_finish_reference_frame()")
    assert loop.index("rsx_d3d12_backend_finish_reference_frame()") < loop.index(
        "cellGcm_complete_flip_ticket(&current.ticket)") < loop.index(
        "QueryPerformanceCounter(&marks[3])")
    assert "hostelapsed_valid=1 prep_us=" in reference
    assert "hostelapsed_valid=0 prep_us=NA" in reference
    parser = function(reference, "u32 reference_repeat_count()")
    assert "*p<'0' || *p>'9'" in parser
    copy_gate = function(reference, "bool reference_copy_timing_enabled(u32 repeats)")
    assert 'repeats>1 ? getenv("TJ_RSX_REFERENCE_COPY_TIMING") : nullptr' in copy_gate
    assert '!strcmp(value,"1")' in copy_gate
    dispatch = reference[reference.index("auto dispatch_once ="):reference.index("auto present_once =")]
    assert "const bool time_blocks=time_block_copies && !c.blocks.empty();" in dispatch
    assert "++result.restore.block_commands;" in dispatch
    assert "++result.restore.block_refs;" in dispatch
    assert "result.restore.bytes+=b.bytes;" in dispatch
    assert dispatch.index("QueryPerformanceCounter(&copy_start)") < dispatch.index("memcpy(vm_base+b.ea,b.data,b.bytes)")
    assert dispatch.index("memcpy(vm_base+b.ea,b.data,b.bytes)") < dispatch.index("QueryPerformanceCounter(&copy_end)")
    assert "copy_end.QuadPart<copy_start.QuadPart" in dispatch
    assert "restore_valid=1 restore_us=" in reference
    assert "restore_valid=0 restore_us=NA" in reference

    fixture = r'''
#include <stdint.h>
#include <stdio.h>
#include <string.h>
typedef uint32_t u32;
typedef uint64_t u64;
typedef enum { RSX_COMPLETION_OTHER, RSX_COMPLETION_CAPTURE = 10 } RsxD3D12CompletionReason;
typedef struct { int phase; } D3D12PrefixOwner;
typedef struct {
    int initialized, gcm2d_backend_halted, texture_label_armed;
    u32 draw_count, texture_label_count, prefix_owner_active, prefix_prepare_owner;
    u32 prefix_deferred_owner, fifo_prefix_outstanding;
    u64 fifo_prefix_serial, prefix_retired_sequence, prefix_submit_sequence;
    D3D12PrefixOwner prefix_owners[2];
} D3D12State;
static D3D12State s_d3d;
static u32 s_exact_writeback_count;
static int drain_result, drain_clears, drain_calls, wait_result, wait_calls, halt_calls, last_reason;
static int d3d12_prefix_overlap_drain(RsxD3D12CompletionReason reason) {
    ++drain_calls; last_reason = reason;
    if (!drain_result && drain_clears) s_d3d.prefix_deferred_owner = 0;
    return drain_result;
}
static int wait_for_gpu_checked(void) { ++wait_calls; return wait_result; }
static void gcm2d_backend_halt(const char* reason) { (void)reason; ++halt_calls; s_d3d.gcm2d_backend_halted = 1; }
ACTUAL_FINISH
#define CHECK(x) do { if (!(x)) { fprintf(stderr, "FAIL:%d: %s\n", __LINE__, #x); return 1; } } while (0)
static void reset(void) {
    memset(&s_d3d, 0, sizeof(s_d3d)); s_d3d.initialized = 1;
    s_exact_writeback_count = drain_result = wait_result = 0; drain_clears = 1;
    drain_calls = wait_calls = halt_calls = last_reason = 0;
}
int main(void) {
    reset(); s_d3d.fifo_prefix_outstanding = 1;
    CHECK(rsx_d3d12_backend_finish_reference_frame() == 0 && drain_calls == 1 &&
          last_reason == RSX_COMPLETION_CAPTURE && wait_calls == 1 && !halt_calls &&
          !s_d3d.fifo_prefix_outstanding && s_d3d.fifo_prefix_serial == 1);
    reset(); s_d3d.prefix_deferred_owner = 1; s_d3d.fifo_prefix_outstanding = 1;
    CHECK(rsx_d3d12_backend_finish_reference_frame() == 0 && drain_calls == 1 &&
          wait_calls == 1 && !s_d3d.prefix_deferred_owner && s_d3d.fifo_prefix_serial == 1);
    reset(); s_d3d.draw_count = 1;
    CHECK(rsx_d3d12_backend_finish_reference_frame() == -1 && halt_calls == 1 &&
          !drain_calls && !wait_calls);
    reset(); s_d3d.texture_label_count = 1;
    CHECK(rsx_d3d12_backend_finish_reference_frame() == -1 && halt_calls == 1 &&
          !drain_calls && !wait_calls);
    reset(); s_exact_writeback_count = 1;
    CHECK(rsx_d3d12_backend_finish_reference_frame() == -1 && halt_calls == 1 &&
          !drain_calls && !wait_calls);
    reset(); s_d3d.prefix_owner_active = 1;
    CHECK(rsx_d3d12_backend_finish_reference_frame() == -1 && halt_calls == 1 &&
          !drain_calls && !wait_calls);
    reset(); drain_result = -1;
    CHECK(rsx_d3d12_backend_finish_reference_frame() == -1 && halt_calls == 1 &&
          drain_calls == 1 && !wait_calls);
    reset(); s_d3d.prefix_deferred_owner = 1; drain_clears = 0;
    CHECK(rsx_d3d12_backend_finish_reference_frame() == -1 && halt_calls == 1 &&
          drain_calls == 1 && !wait_calls);
    reset(); s_d3d.prefix_owners[0].phase = 2; s_d3d.prefix_submit_sequence = 1;
    CHECK(rsx_d3d12_backend_finish_reference_frame() == -1 && halt_calls == 1 &&
          drain_calls == 1 && !wait_calls);
    reset(); s_d3d.fifo_prefix_outstanding = 1; wait_result = -1;
    CHECK(rsx_d3d12_backend_finish_reference_frame() == -1 && halt_calls == 1 &&
          drain_calls == 1 && wait_calls == 1 && s_d3d.fifo_prefix_outstanding);
    reset(); s_d3d.initialized = 0;
    CHECK(rsx_d3d12_backend_finish_reference_frame() == -1 && !halt_calls &&
          !drain_calls && !wait_calls);
    puts("PASS reference finish: guard/drain/wait/failure cases");
    return 0;
}
'''.replace("ACTUAL_FINISH", finish)

    compiler = shutil.which("clang") or r"C:/Program Files/LLVM/bin/clang.exe"
    if not Path(compiler).exists() and not shutil.which("clang"):
        raise RuntimeError("clang is required for native reference-finish fixture")
    with tempfile.TemporaryDirectory(prefix="tj-reference-repeat-") as directory:
        source = Path(directory) / "fixture.c"
        exe = Path(directory) / "fixture.exe"
        source.write_text(fixture, encoding="utf-8")
        subprocess.run([compiler, "-std=c11", "-Wall", "-Werror", str(source), "-o", str(exe)],
                       check=True, timeout=30)
        subprocess.run([str(exe)], check=True, timeout=10)
        parser_source = Path(directory) / "repeat_parser.cpp"
        parser_exe = Path(directory) / "repeat_parser.exe"
        parser_fixture = parser.replace('getenv("TJ_RSX_REFERENCE_REPEAT")',
                                        'reference_repeat_fixture_value')
        parser_source.write_text(r'''
#include <cerrno>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <stdexcept>
typedef uint32_t u32;
static const char* reference_repeat_fixture_value = nullptr;
static const char* reference_copy_fixture_value = nullptr;
ACTUAL_PARSER
ACTUAL_COPY_GATE
static int check_value(const char* value, int expected) {
    reference_repeat_fixture_value = value;
    try { return reference_repeat_count() == (u32)expected ? 0 : 1; }
    catch (const std::runtime_error&) { return expected < 0 ? 0 : 1; }
}
static int check_copy(u32 repeats, const char* value, int expected) {
    reference_copy_fixture_value = value;
    return reference_copy_timing_enabled(repeats) == !!expected ? 0 : 1;
}
int main() {
    if (check_value(nullptr, 1) || check_value("1", 1) || check_value("120", 120) ||
        check_value("001", 1)) return 1;
    const char* const bad[] = {"", "0", "121", "+3", " 3", "3 ", "3x", "-1", "1.0"};
    for (unsigned i=0; i<sizeof(bad)/sizeof(bad[0]); ++i) if (check_value(bad[i], -1)) return 1;
    if (check_copy(1, nullptr, 0) || check_copy(1, "1", 0) ||
        check_copy(2, nullptr, 0) || check_copy(2, "", 0) ||
        check_copy(2, "01", 0) || check_copy(2, "true", 0) || check_copy(2, "1", 1)) return 1;
    puts("PASS reference repeat exact-decimal parser");
    puts("PASS reference copy timing exact opt-in gate");
    return 0;
}
'''.replace("ACTUAL_PARSER", parser_fixture).replace("ACTUAL_COPY_GATE", copy_gate.replace(
            'getenv("TJ_RSX_REFERENCE_COPY_TIMING")', 'reference_copy_fixture_value')),
            encoding="utf-8")
        cxx = shutil.which("clang++") or r"C:/Program Files/LLVM/bin/clang++.exe"
        if not Path(cxx).exists() and not shutil.which("clang++"):
            raise RuntimeError("clang++ is required for native repeat-parser fixture")
        subprocess.run([cxx, "-std=c++17", "-Wall", "-Werror", "-D_CRT_SECURE_NO_WARNINGS",
                        str(parser_source), "-o", str(parser_exe)],
                       check=True, timeout=30)
        subprocess.run([str(parser_exe)], check=True, timeout=10)
    print("PASS reference repeat source order and native finish fixture")


if __name__ == "__main__":
    main()
