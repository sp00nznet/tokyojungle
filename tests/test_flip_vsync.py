"""Compile the production presenter flip step against deterministic display ticks."""
from pathlib import Path
import shutil
import subprocess
import tempfile

from test_fifo_present_boundary import function

ROOT = Path(__file__).resolve().parents[1]


def main():
    text = (ROOT / "src/tj_present.cpp").read_text(encoding="utf-8")
    complete = function(text, "static int tj_present_complete_ticket(")
    actual = function(text, "static int tj_present_flip(int vblank,")
    fixture = r'''
#include <stdint.h>
#include <stdio.h>
struct tj_gcm_flip_ticket { uint64_t generation; uint32_t buffer_id,source; };
struct tj_gcm_flip_publication_timing { uint64_t publication_qpc; uint32_t publication_qpc_state; };
static tj_gcm_flip_ticket pending={},last_presented={},last_completed={};
static bool has_pending=false,vsync=true;
static struct { int enabled; } s_tj_flip_timeline={};
static unsigned presents=0,completions=0,checks=0,failures=0;
static int cellGcm_flip_uses_vsync() { return vsync; }
static void tj_flip_timeline_take(const tj_gcm_flip_ticket&,const tj_gcm_flip_publication_timing* =nullptr) {}
static void tj_flip_timeline_present_return() {}
static void tj_flip_timeline_complete(const tj_gcm_flip_ticket&) {}
static void tj_flip_timeline_reject_completion(const tj_gcm_flip_ticket&) {}
static int cellGcm_take_flip_ticket(tj_gcm_flip_ticket* t) {
    if(!has_pending) return 0;
    *t=pending; has_pending=false; return 1;
}
static int cellGcm_take_flip_ticket_timed(tj_gcm_flip_ticket* t,tj_gcm_flip_publication_timing* timing) {
    if(timing) *timing={}; return cellGcm_take_flip_ticket(t);
}
static void rsx_d3d12_backend_present_buffer(uint64_t gen,uint32_t buffer) {
    ++presents; last_presented={gen,buffer,1};
}
static int cellGcm_complete_flip_ticket(const tj_gcm_flip_ticket* t) {
    if(t->generation!=last_presented.generation || t->buffer_id!=last_presented.buffer_id)
        ++failures;
    ++completions; last_completed=*t; return 1;
}
COMPLETE_TICKET
ACTUAL
#define CHECK(test,message) do { ++checks; if(!(test)) { ++failures; fprintf(stderr,"FAIL: %s\n",message); } } while(0)
static void request(uint64_t n) { pending={n,(uint32_t)(n%2),1}; has_pending=true; }
int main() {
    tj_gcm_flip_ticket awaiting={};
    CHECK(!tj_present_flip(0,&awaiting) && !tj_present_flip(1,&awaiting),"idle synthesized frame");
    CHECK(presents==0 && completions==0,"idle synthesized presentation/completion");
    request(1);
    CHECK(tj_present_flip(0,&awaiting)==1,"outer loop did not present ready frame");
    CHECK(presents==1 && completions==0 && awaiting.generation==1,"VSync frame completed outside vblank");
    for(int i=0;i<10;i++) tj_present_flip(0,&awaiting);
    CHECK(presents==1 && completions==0,"fast FIFO cadence bypassed VSync");
    request(2);
    tj_present_flip(0,&awaiting);
    CHECK(has_pending && awaiting.generation==1,"new request replaced in-flight completion");
    tj_present_flip(1,&awaiting);
    CHECK(completions==1 && last_completed.generation==1,"vblank failed to complete exact presented frame");
    CHECK(has_pending && presents==1,"one vblank consumed/completed a second frame");
    tj_present_flip(0,&awaiting);
    CHECK(presents==2 && completions==1 && awaiting.generation==2,"next frame did not await next vblank");
    tj_present_flip(1,&awaiting);
    CHECK(completions==2 && last_completed.generation==2,"second vblank did not complete second frame");
    tj_present_flip(1,&awaiting);
    CHECK(completions==2,"idle vblank replayed completion");
    request(3); tj_present_flip(1,&awaiting);
    CHECK(presents==3 && completions==3 && !awaiting.generation,"frame presented at vblank was delayed another tick");
    vsync=false; request(4); tj_present_flip(0,&awaiting);
    CHECK(presents==4 && completions==4 && !awaiting.generation,"non-VSync frame was forced to await vblank");
    vsync=true; request(5); tj_present_flip(0,&awaiting);
    CHECK(presents==5 && completions==4,"VSync mode switch was ignored");
    vsync=false; tj_present_flip(0,&awaiting);
    CHECK(completions==5 && !awaiting.generation,"switch away from VSync stranded an already presented frame");
    printf("flip VSync: %u/%u checks passed\n",checks-failures,checks);
    return failures ? 1 : 0;
}
'''.replace("COMPLETE_TICKET", complete).replace("ACTUAL", actual)
    compiler = shutil.which("clang++") or "C:/Program Files/LLVM/bin/clang++.exe"
    with tempfile.TemporaryDirectory(prefix="tj-flip-vsync-") as temp:
        source = Path(temp) / "test.cpp"
        exe = Path(temp) / "test.exe"
        source.write_text(fixture, encoding="utf-8")
        subprocess.run([compiler, "-std=c++17", "-O2", str(source), "-o", str(exe)], check=True)
        subprocess.run([str(exe)], check=True)


if __name__ == "__main__":
    main()
