"""Compile the actual renderer coverage helpers without a GPU or game launch."""
from pathlib import Path
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT.parent / "ps3recomp/libs/video/rsx_d3d12_backend.c"


def main():
    text = BACKEND.read_text(encoding="utf-8", errors="surrogateescape")
    start = text.index("static void display_coverage_reset(")
    helpers = text[start:text.index("static void display_raster_coverage_note(", start)]
    source = r'''
#include <stdint.h>
#include <stdio.h>
typedef uint8_t u8;
typedef uint32_t u32;
typedef uint64_t u64;
typedef struct { u32 x,y,w,h,record; } DisplayCoverageRect;
#define MAX_DISPLAY_COVERAGE_RECTS 32
typedef struct {
    u8 fifo_coverage_count,fifo_coverage_overflow;
    u8 plan_coverage_count,plan_coverage_overflow;
    DisplayCoverageRect fifo_coverage[MAX_DISPLAY_COVERAGE_RECTS];
    DisplayCoverageRect plan_coverage[MAX_DISPLAY_COVERAGE_RECTS];
} DisplaySurface;
HELPERS
#define CHECK(c,label) do { if (!(c)) { fprintf(stderr,"FAIL: %s\n",label); return 1; } } while(0)
static void actual(DisplaySurface* s,u32 x,u32 y,u32 w,u32 h) {
    display_coverage_add(s->fifo_coverage,&s->fifo_coverage_count,&s->fifo_coverage_overflow,x,y,w,h,0,1);
}
static void plan(DisplaySurface* s,u32 x,u32 y,u32 w,u32 h,u32 record) {
    display_coverage_add(s->plan_coverage,&s->plan_coverage_count,&s->plan_coverage_overflow,x,y,w,h,record,0);
}
int main(void) {
    DisplaySurface s={0};
    actual(&s,0,0,1280,720);
    for (u32 i=0;i<128;++i) { actual(&s,0,0,1280,720); actual(&s,i,i,16,16); }
    CHECK(!s.fifo_coverage_overflow && s.fifo_coverage_count==1,"repeated/subset writes overflow full clear");
    CHECK(display_coverage_contains(&s,UINT32_MAX,0,0,0,1280,720),"full clear proof lost");
    display_coverage_reset(&s);
    actual(&s,0,0,1024,720); actual(&s,1024,0,256,720);
    CHECK(display_coverage_contains(&s,UINT32_MAX,0,0,0,1280,720),"adjacent split copy coverage");
    display_coverage_reset(&s);
    actual(&s,0,0,511,720); actual(&s,512,0,512,720);
    CHECK(!display_coverage_contains(&s,UINT32_MAX,0,0,0,1024,720),"gap incorrectly covered");
    display_coverage_reset(&s);
    for(u32 i=0;i<32;++i) actual(&s,i*2,0,1,1);
    actual(&s,0,0,1280,720);
    CHECK(!s.fifo_coverage_overflow && s.fifo_coverage_count==1,"superset should replace redundant rectangles");
    display_coverage_reset(&s);
    plan(&s,0,0,1280,720,40); plan(&s,0,0,1280,720,5);
    CHECK(!display_coverage_contains(&s,5,1,0,0,1280,720),"borrowed future planned writer");
    CHECK(display_coverage_contains(&s,6,1,0,0,1280,720),"earlier planned writer lost");
    display_coverage_reset(&s);
    for(u32 i=0;i<33;++i) actual(&s,i*2,0,1,1);
    CHECK(s.fifo_coverage_overflow && !display_coverage_contains(&s,UINT32_MAX,0,0,0,1,1),"true overflow must fail closed");
    display_coverage_reset(&s);
    actual(&s,UINT32_MAX,0,2,1); actual(&s,0,0,0,1);
    CHECK(s.fifo_coverage_count==0,"invalid rectangles accepted");
    puts("display coverage: PASS");
    return 0;
}
'''.replace("HELPERS", helpers)
    compiler = shutil.which("clang") or "C:/Program Files/LLVM/bin/clang.exe"
    with tempfile.TemporaryDirectory() as directory:
        c, exe = Path(directory)/"coverage.c", Path(directory)/"coverage.exe"
        c.write_text(source)
        subprocess.run([compiler,"-std=c17","-O2","-DNDEBUG",str(c),"-o",str(exe)],check=True)
        subprocess.run([str(exe)],check=True)


if __name__ == "__main__":
    main()
