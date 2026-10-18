"""Production reservation table: bulk writes, collisions, ABA and saturation."""
from pathlib import Path
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
SPU = ROOT.parent / "ps3recomp/runtime/spu"


def main():
    source = r'''
#include <stdio.h>
#include <string.h>
#include <windows.h>
#include "spu_reservation.c"
static uint8_t memory[512];
uint8_t* vm_base=memory;
static unsigned checks,failures;
#define CHECK(c) do { ++checks; if(!(c)){ ++failures; printf("FAIL line %d: %s\n",__LINE__,#c); } } while(0)
static unsigned used(void) {
    unsigned n=0;for(unsigned i=0;i<RESV_VERSION_SLOTS;++i)n+=g_resv_versions[i].used!=0;return n;
}
static void reset(void) { memset(g_resv_versions,0,sizeof(g_resv_versions)); }
static void image_writes(void) {
    const uint32_t bases[]={0x03FA3000,0x04363000};
    for(unsigned b=0;b<2;++b)for(unsigned i=0;i<28800;++i)
        spu_reservation_bump_locked(bases[b]+i*128);
}
static int benchmark(void) {
    LARGE_INTEGER hz,a,b;QueryPerformanceFrequency(&hz);
    double times[9];unsigned slots=0;
    spu_reservation_lock();
    for(unsigned trial=0;trial<9;++trial) {
        reset();for(unsigned i=0;i<2048;++i)spu_reservation_version_locked(0x01000000+i*128,NULL);
        image_writes();slots=used();QueryPerformanceCounter(&a);
        for(unsigned frame=0;frame<32;++frame)image_writes();
        QueryPerformanceCounter(&b);times[trial]=(double)(b.QuadPart-a.QuadPart)*1000/hz.QuadPart/32;
    }
    spu_reservation_unlock();
    for(unsigned i=0;i<9;++i)for(unsigned j=i+1;j<9;++j)if(times[j]<times[i]) {
        double t=times[i];times[i]=times[j];times[j]=t;
    }
    printf("Synthetic two-image invalidation: slots=%u median_ms/frame=%.6f min=%.6f max=%.6f\n",slots,times[4],times[0],times[8]);
    return 0;
}
int main(int argc,char** argv) {
    (void)argv;if(argc>1)return benchmark();
    uint64_t before=0,after=0;
    spu_reservation_lock();
    // A full image's unrelated writes must not consume future GETLLAR slots.
    for(unsigned i=0;i<RESV_VERSION_SLOTS;++i)spu_reservation_bump_locked(0x08000000+i*128);
    CHECK(used()==0);
    CHECK(spu_reservation_version_locked(0x01001000,&before));
    CHECK(used()==1);
    spu_reservation_bump_locked(0x01001040);spu_reservation_bump_locked(0x01001000);
    CHECK(spu_reservation_version_locked(0x01001000,&after) && after==before+2);
    reset();
    // Hash collisions cannot hide an existing reservation or insert a writer.
    CHECK(spu_reservation_version_locked(0x1000,&before));
    CHECK(spu_reservation_version_locked(0x801000,&after));
    spu_reservation_bump_locked(0x1001000);
    CHECK(used()==2);
    spu_reservation_bump_locked(0x801040);
    CHECK(spu_reservation_version_locked(0x801000,&after) && after==1);
    CHECK(spu_reservation_version_locked(0x1000,&after) && after==before);
    reset();CHECK(spu_reservation_version_locked(0,&before));
    spu_reservation_unlock();
    spu_reservation_notify_write(124,8); // Two lines; only the first is tracked.
    spu_reservation_lock();
    CHECK(used()==1);
    CHECK(spu_reservation_version_locked(0,&after) && after==before+1);
    CHECK(spu_reservation_version_locked(128,&after) && after==0);
    spu_reservation_unlock();
    memset(memory,0,sizeof(memory));uint64_t expected=0;
    CHECK(spu_reservation_compare_exchange64(0,&expected,0x1122334455667788ULL));
    CHECK(memory[0]==0x11 && memory[7]==0x88);
    spu_reservation_lock();CHECK(spu_reservation_version_locked(0,&before));spu_reservation_unlock();
    expected=0;CHECK(!spu_reservation_compare_exchange64(0,&expected,0) && expected==0x1122334455667788ULL);
    spu_reservation_lock();CHECK(spu_reservation_version_locked(0,&after) && before==after);spu_reservation_unlock();
    expected=0x1122334455667788ULL;
    CHECK(spu_reservation_compare_exchange64(0,&expected,0));
    expected=0;CHECK(spu_reservation_compare_exchange64(0,&expected,0x1122334455667788ULL));
    spu_reservation_lock();
    CHECK(spu_reservation_version_locked(0,&after) && after==before+2); // Restored bytes still invalidate ABA.
    reset();
    // Real queried reservations can still saturate; preserve fail-closed GETLLAR.
    for(unsigned i=0;i<RESV_VERSION_SLOTS;++i)CHECK(spu_reservation_version_locked(i*128,NULL));
    CHECK(!spu_reservation_version_locked(0x08000000,&after));
    CHECK(spu_reservation_version_locked(128,&before));spu_reservation_bump_locked(128);
    CHECK(spu_reservation_version_locked(128,&after) && after==before+1);
    spu_reservation_bump_locked(0x08000000);CHECK(used()==RESV_VERSION_SLOTS);
    spu_reservation_unlock();
    printf("Reservation tracking: %u checks, %u failures\n",checks,failures);
    return failures?1:0;
}
'''
    compiler = shutil.which("clang") or r"C:\Program Files\LLVM\bin\clang.exe"
    with tempfile.TemporaryDirectory(prefix="tj-reservation-tracking-") as temp:
        c, exe = Path(temp)/"tracking.c", Path(temp)/"tracking.exe"
        c.write_text(source, encoding="utf-8")
        subprocess.run([compiler, "-std=c11", "-O2", "-I", str(SPU), str(c), "-o", str(exe)], check=True)
        result = subprocess.run([str(exe)], check=False).returncode
        if result:
            return result
        # Compare the old create-on-every-write policy using the same production
        # code and copy addresses. This is synthetic CPU cost, not an FPS test.
        original = (SPU/"spu_reservation.c").read_text(encoding="utf-8")
        assert "version_slot_locked(line_ea, 0)" in original
        old_header = Path(temp)/"reservation_before.h"
        old_header.write_text(original.replace("version_slot_locked(line_ea, 0)",
                                               "version_slot_locked(line_ea, 1)"), encoding="utf-8")
        old_c, old_exe = Path(temp)/"before.c", Path(temp)/"before.exe"
        old_c.write_text(source.replace('#include "spu_reservation.c"',
                                       '#include "reservation_before.h"'), encoding="utf-8")
        subprocess.run([compiler, "-std=c11", "-O2", "-I", str(SPU), str(old_c), "-o", str(old_exe)], check=True)
        print("Before policy:", flush=True)
        subprocess.run([str(old_exe), "bench"], check=True)
        print("Current production:", flush=True)
        subprocess.run([str(exe), "bench"], check=True)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
