"""Compile the actual opt-in sampler lifecycle without starting the game."""
from pathlib import Path
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def function(source, signature):
    start = source.index(signature)
    end = source.index("{", start) + 1
    depth = 1
    while depth:
        depth += (source[end] == "{") - (source[end] == "}")
        end += 1
    return source[start:end]


def main():
    source = (ROOT / "src/tj_present.cpp").read_text(encoding="utf-8")
    present = function(source, "static DWORD WINAPI tj_present_thread(")
    assert "tj_frame_prof_tick();" not in present, "sampler still waits behind renderer work"
    assert "tj_start_frame_prof_thread();" in function(source, 'extern "C" void tj_start_present_thread(')
    shutdown = function(source, 'extern "C" void tj_stop_present_thread(')
    assert shutdown.index("tj_stop_frame_prof_thread();") < shutdown.index("g_tj_present_stop"), "sampler must join before renderer teardown"
    tick = function(source, "static void tj_frame_prof_tick(")
    assert "tj_frame_prof_sample_thread(state, g_tj_present_thread," in tick
    assert "TJ_FRAME_PROF_RENDERER_SLOT = PPU_THREAD_MAX + 1" in source
    assert "last_cpu_us[TJ_FRAME_PROF_SLOT_COUNT]" in source
    assert "TJ_FRAME_PROF_SLOT_COUNT = PPU_THREAD_MAX + 2" in source

    fixture = r'''
#include <cstdio>
#include <cstdint>
#include <cstdlib>
#define WINAPI
using DWORD = unsigned long;
using HANDLE = void*;
using LPVOID = void*;
constexpr int TRUE=1, FALSE=0;
constexpr DWORD WAIT_OBJECT_0=0, WAIT_TIMEOUT=258, WAIT_FAILED=0xffffffffu, INFINITE=0xffffffffu;
static HANDLE s_tj_frame_prof_thread, s_tj_frame_prof_stop_event;
static bool enabled, event_ok=true, thread_ok=true, register_ok=true, wait_error;
static int creates, closes, signals, joins, ticks, registrations, unregistrations, wait_budget;
static const char* fake_getenv(const char*) { return enabled ? "1" : nullptr; }
#define getenv fake_getenv
static DWORD GetLastError() { return 5; }
static HANDLE CreateEventW(void*,int manual,int initial,const wchar_t*) {
    if(!manual || initial) std::abort();
    ++creates; return event_ok ? (HANDLE)0x10 : nullptr;
}
static HANDLE CreateThread(void*,size_t,DWORD(*)(LPVOID),LPVOID,DWORD,void*) {
    ++creates; return thread_ok ? (HANDLE)0x20 : nullptr;
}
static int CloseHandle(HANDLE h) { if(!h) std::abort(); ++closes; return 1; }
static int SetEvent(HANDLE h) { if(h!=(HANDLE)0x10) std::abort(); ++signals; return 1; }
static DWORD WaitForSingleObject(HANDLE h,DWORD ms) {
    if(h==(HANDLE)0x20) { if(ms!=INFINITE) std::abort(); ++joins; return WAIT_OBJECT_0; }
    if(h!=(HANDLE)0x10 || ms!=4) std::abort();
    if(wait_error) return WAIT_FAILED;
    return wait_budget-- > 0 ? WAIT_TIMEOUT : WAIT_OBJECT_0;
}
static int guest_world_register_current(const char*) { ++registrations; return register_ok; }
static int guest_world_unregister_current() { ++unregistrations; return 1; }
static void tj_frame_prof_tick() { ++ticks; }
WORKER
START
STOP
static int checks;
#define CHECK(c) do { ++checks; if(!(c)) { std::fprintf(stderr,"FAIL line %d: %s\n",__LINE__,#c); return 1; } } while(0)
int main() {
    tj_start_frame_prof_thread();
    CHECK(!creates && !s_tj_frame_prof_thread && !s_tj_frame_prof_stop_event);
    enabled=true; event_ok=false;
    tj_start_frame_prof_thread();
    CHECK(creates==1 && closes==0 && !s_tj_frame_prof_thread);
    event_ok=true; thread_ok=false;
    tj_start_frame_prof_thread();
    CHECK(creates==3 && closes==1 && !s_tj_frame_prof_stop_event);
    thread_ok=true;
    tj_start_frame_prof_thread();
    CHECK(creates==5 && s_tj_frame_prof_thread==(HANDLE)0x20 && s_tj_frame_prof_stop_event==(HANDLE)0x10);
    tj_start_frame_prof_thread();
    CHECK(creates==5);
    wait_budget=3;
    CHECK(tj_frame_prof_worker(nullptr)==0);
    CHECK(ticks==3 && registrations==1 && unregistrations==1);
    wait_error=true;
    CHECK(tj_frame_prof_worker(nullptr)==1);
    CHECK(ticks==3 && registrations==2 && unregistrations==2);
    wait_error=false; register_ok=false;
    CHECK(tj_frame_prof_worker(nullptr)==1);
    CHECK(ticks==3 && registrations==3 && unregistrations==2);
    tj_stop_frame_prof_thread();
    CHECK(signals==1 && joins==1 && closes==3);
    CHECK(!s_tj_frame_prof_thread && !s_tj_frame_prof_stop_event);
    tj_stop_frame_prof_thread();
    CHECK(signals==1 && joins==1 && closes==3);
    std::printf("PASS: %d sampler lifecycle checks, plus 7 renderer-independence/coverage checks\n",checks);
}
'''
    for marker, signature in (
        ("WORKER", "static DWORD WINAPI tj_frame_prof_worker("),
        ("START", "static void tj_start_frame_prof_thread("),
        ("STOP", "static void tj_stop_frame_prof_thread("),
    ):
        fixture = fixture.replace(marker + "\n", function(source, signature) + "\n")
    compiler = shutil.which("clang++") or r"C:\Program Files\LLVM\bin\clang++.exe"
    with tempfile.TemporaryDirectory(prefix="tj-frame-prof-") as temp:
        cpp, exe = Path(temp) / "test.cpp", Path(temp) / "test.exe"
        cpp.write_text(fixture, encoding="utf-8")
        subprocess.run([compiler, "-std=c++17", "-O2", str(cpp), "-o", str(exe)], check=True)
        subprocess.run([str(exe)], check=True)


if __name__ == "__main__":
    main()
