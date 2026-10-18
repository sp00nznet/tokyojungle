"""Exercise the production TLS pulse guard without game or GPU work."""
from pathlib import Path
import shutil
import subprocess
import tempfile

from test_fifo_present_boundary import function

ROOT = Path(__file__).resolve().parents[1]


def main():
    video = ROOT.parent / "ps3recomp/libs/video"
    backend = (video / "rsx_d3d12_backend.c").read_text(encoding="utf-8")
    begin = backend.index("static _Thread_local u32 (*s_d3d12_fence_wait_service)")
    end = backend.index("static int wait_for_fence_value_checked(", begin)
    production = backend[begin:end]
    pulse = function(production, "void rsx_d3d12_backend_pulse_fence_wait_service(")
    assert "WaitForSingleObject" not in pulse
    assert "QueryPerformanceCounter" not in pulse
    assert "GetCompletedValue" not in pulse
    fixture = r'''
#include <stdint.h>
#include <stdio.h>
#include <thread>
using u32=uint32_t;
static thread_local int faulted;
static int rsx_d3d12_backend_fifo_faulted(void) { return faulted; }
PRODUCTION
static thread_local int calls, reenter;
static thread_local void* seen_context;
static thread_local u32 seen_cap;
static u32 service(void* context,u32 cap) {
    ++calls; seen_context=context; seen_cap=cap;
    if (reenter) rsx_d3d12_backend_pulse_fence_wait_service();
    return UINT32_MAX; /* Returning a slice never creates a wait. */
}
#define CHECK(c,m) do { if (!(c)) { fprintf(stderr,"FAIL: %s\n",m); return 1; } } while (0)
int main(void) {
    int tag=42;
    rsx_d3d12_backend_set_fence_wait_fifo_pulse(1);
    rsx_d3d12_backend_pulse_fence_wait_service();
    CHECK(!calls,"pulse alone activated a missing service");
    rsx_d3d12_backend_set_fence_wait_service(service,&tag);
    rsx_d3d12_backend_pulse_fence_wait_service();
    CHECK(!calls,"registration inherited a previous pulse opt-in");
    rsx_d3d12_backend_set_fence_wait_fifo_pulse(1);
    reenter=1; rsx_d3d12_backend_pulse_fence_wait_service();
    CHECK(calls==1 && seen_context==&tag && seen_cap==1 &&
          !s_d3d12_fence_wait_service_busy,"pulse reentered or changed context/cap");
    faulted=1; rsx_d3d12_backend_pulse_fence_wait_service();
    CHECK(calls==1,"faulted backend still invoked service");
    faulted=0; s_d3d12_fence_wait_service_busy=1;
    rsx_d3d12_backend_pulse_fence_wait_service();
    CHECK(calls==1,"pulse reentered a checked wait service");
    s_d3d12_fence_wait_service_busy=0;
    bool foreign_ok=false;
    std::thread foreign([&] {
        rsx_d3d12_backend_pulse_fence_wait_service();
        foreign_ok=!calls && !s_d3d12_fence_wait_service &&
                   !s_d3d12_fence_wait_service_context && !s_d3d12_fence_wait_fifo_pulse;
    }); foreign.join();
    CHECK(foreign_ok && calls==1,"pulse registration escaped its presenter thread");
    rsx_d3d12_backend_set_fence_wait_fifo_pulse(0);
    rsx_d3d12_backend_pulse_fence_wait_service();
    CHECK(calls==1,"explicit OFF still pulsed");
    rsx_d3d12_backend_set_fence_wait_fifo_pulse(1);
    rsx_d3d12_backend_set_fence_wait_service(nullptr,&tag);
    rsx_d3d12_backend_pulse_fence_wait_service();
    CHECK(calls==1 && !s_d3d12_fence_wait_service_context &&
          !s_d3d12_fence_wait_fifo_pulse,"clear left a live stack context or pulse");
    rsx_d3d12_backend_set_fence_wait_service(service,&tag);
    rsx_d3d12_backend_pulse_fence_wait_service();
    CHECK(calls==1,"later registration unexpectedly reenabled pulses");
    puts("PASS: pulse OFF, TLS ownership, checked-wait busy guard, fault, reentry and clear");
}
'''.replace("PRODUCTION", production)
    compiler = shutil.which("clang++") or "C:/Program Files/LLVM/bin/clang++.exe"
    with tempfile.TemporaryDirectory(prefix="tj-fifo-deadline-pulse-") as temp:
        source = Path(temp) / "pulse.cpp"
        exe = Path(temp) / "pulse.exe"
        source.write_text(fixture, encoding="utf-8")
        subprocess.run([compiler, "-std=c++17", "-O2", str(source), "-o", str(exe)], check=True)
        subprocess.run([str(exe)], check=True)


if __name__ == "__main__":
    main()
