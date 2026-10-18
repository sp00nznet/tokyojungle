"""Native close-export gate: real events, completed rows, default-off, one dump."""
from pathlib import Path
import os
import shutil
import subprocess
import tempfile

from test_fifo_present_boundary import function

ROOT = Path(__file__).resolve().parents[1]


def main():
    source = (ROOT / "src/tj_present.cpp").read_text(encoding="utf-8")
    thread = function(source, "static DWORD WINAPI tj_present_thread(")
    assert thread.index("tj_flip_timeline_init(") < thread.index(
        "s_tj_flip_timeline.enabled ? tj_flip_timeline_close_dump : nullptr")
    assert "rsx_d3d12_backend_set_close_observer(nullptr);" in thread
    start = source.index("struct TjFlipTimelineRecord {")
    end = source.index("static uint64_t tj_flip_timeline_now(", start)
    types = source[start:end]
    signatures = (
        "static uint64_t tj_flip_timeline_now(",
        "static void tj_flip_timeline_init(",
        "static void tj_flip_timeline_dump(",
        "static void tj_flip_timeline_export_init(",
        "static void tj_flip_timeline_export_poll(",
        "static void tj_flip_timeline_close_dump(",
        "static void tj_flip_timeline_export_close(",
    )
    helpers = "\n".join(function(source, name) for name in signatures)
    cpp = r'''
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
TYPES
static HANDLE s_tj_flip_dump_request, s_tj_flip_dump_complete;
static unsigned diagnostic_dumps, acknowledgements, bad_ack;
static void rsx_d3d12_backend_dump_message_pump_trace(void) { ++diagnostic_dumps; }
static BOOL tracked_SetEvent(HANDLE h) {
    ++acknowledgements;
    if (h != s_tj_flip_dump_complete || !s_tj_flip_timeline.dumped ||
        diagnostic_dumps != 1) ++bad_ack;
    return ::SetEvent(h);
}
#define SetEvent tracked_SetEvent
HELPERS
#undef SetEvent
#define CHECK(c) do { if (!(c)) { fprintf(stderr,"FAIL line=%d\n",__LINE__); return 1; } } while (0)
int main(int argc, char** argv) {
    CHECK(argc == 2);
    const bool enabled = strcmp(argv[1], "off") != 0;
    tj_flip_timeline_init(1, 10000000);
    CHECK(s_tj_flip_timeline.enabled == enabled);
    tj_flip_timeline_export_init();
    if (!enabled) {
        SetLastError(0x1234);
        tj_flip_timeline_close_dump(); tj_flip_timeline_close_dump();
        CHECK(!s_tj_flip_timeline.dumped && !diagnostic_dumps && !acknowledgements);
        CHECK(!s_tj_flip_dump_request && !s_tj_flip_dump_complete);
        CHECK(GetLastError() == 0x1234);
        puts("PASS off: no export/events");
        return 0;
    }
    CHECK(s_tj_flip_dump_request && s_tj_flip_dump_complete);
    s_tj_flip_timeline.takes = 4;
    s_tj_flip_timeline.completions = 3;
    for (unsigned i=0; i<3; ++i) {
        auto& r = s_tj_flip_timeline.ring[i];
        r.seq = i+1; r.generation = 100+i; r.source = 2;
    }
    s_tj_flip_timeline.active.generation = 999; // not completed, never exported
    if (!strcmp(argv[1], "pending")) CHECK(::SetEvent(s_tj_flip_dump_request));
    tj_flip_timeline_close_dump();
    CHECK(s_tj_flip_timeline.dumped && diagnostic_dumps == 1 && !bad_ack);
    const unsigned expected_ack = !strcmp(argv[1], "pending");
    CHECK(acknowledgements == expected_ack);
    CHECK(::WaitForSingleObject(s_tj_flip_dump_complete, 0) ==
          (expected_ack ? WAIT_OBJECT_0 : WAIT_TIMEOUT));
    tj_flip_timeline_close_dump(); tj_flip_timeline_export_poll(); tj_flip_timeline_dump();
    CHECK(diagnostic_dumps == 1 && acknowledgements == expected_ack);
    tj_flip_timeline_export_close();
    CHECK(!s_tj_flip_dump_request && !s_tj_flip_dump_complete);
    puts("PASS close: one completed-only dump; pending request acknowledged after dump");
    return 0;
}
'''.replace("TYPES", types).replace("HELPERS", helpers)
    compiler = shutil.which("clang++") or r"C:\Program Files\LLVM\bin\clang++.exe"
    with tempfile.TemporaryDirectory(prefix="tj-close-export-") as directory:
        path = Path(directory)
        (path / "fixture.cpp").write_text(cpp, encoding="utf-8")
        exe = path / "fixture.exe"
        subprocess.run([compiler, "-std=c++17", "-O2", "-D_CRT_SECURE_NO_WARNINGS",
                        str(path / "fixture.cpp"), "-o", str(exe)], check=True)
        for mode in ("off", "close", "pending"):
            env = dict(os.environ)
            env.pop("TJ_FLIP_TIMELINE", None)
            if mode != "off":
                env["TJ_FLIP_TIMELINE"] = "1"
            result = subprocess.run([str(exe), mode], env=env, capture_output=True,
                                    text=True, check=True, timeout=10)
            if mode == "off":
                assert not result.stderr
            else:
                assert result.stderr.count("event=header") == 1
                assert result.stderr.count("event=flip ") == 3
                assert "generation=999" not in result.stderr
            print(result.stdout.strip())


if __name__ == "__main__":
    main()
