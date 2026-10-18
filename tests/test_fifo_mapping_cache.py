"""Exercise production FIFO translation with real publication locks.

Byte-address/remap proof and counted lock traffic; not an FPS benchmark.
"""
from pathlib import Path
import shutil
import subprocess
import tempfile
from test_fifo_present_boundary import function

ROOT = Path(__file__).resolve().parents[1]


def main():
    text = (ROOT.parent / "ps3recomp/libs/video/cellGcmSys.c").read_text(encoding="utf-8")
    functions = "\n".join(function(text, sig) for sig in (
        "u64 cellGcmMainMappingGeneration(",
        "static void cellGcmMainMappingWriteBeginLocked(",
        "static void cellGcmMainMappingWriteEndLocked(",
        "static void cellGcmMainMappingWriteBegin(",
        "static void cellGcmMainMappingWriteEnd(",
        "static u16 cellGcmMainEaPage(",
        "static u32 gcm_io2ea(",
        "int cellGcmResolveLocatedChecked("))
    source = r'''
#include <windows.h>
#include <stdint.h>
#include <stdio.h>
#include <stdatomic.h>
#include <string.h>
typedef uint32_t u32; typedef uint16_t u16; typedef uint64_t u64;
static u16 s_ea_address_table[65536];
static struct { u32 localAddress; } s_config={0xC0000000};
static atomic_ullong s_main_mapping_generation=ATOMIC_VAR_INIT(2);
static atomic_flag s_main_mapping_writer=ATOMIC_FLAG_INIT;
static SRWLOCK s_main_mapping_lock=SRWLOCK_INIT;
static _Thread_local unsigned reads;
static void counted_read(SRWLOCK* p) { ++reads; AcquireSRWLockShared(p); }
#define AcquireSRWLockShared counted_read
FUNCTIONS
static unsigned checks, failures;
#define CHECK(c,m) do { ++checks; if(!(c)) { ++failures; printf("FAIL: %s\n",m); } } while(0)
static void map(u32 page,u16 ea) {
    cellGcmMainMappingWriteBegin(); s_ea_address_table[page]=ea;
    cellGcmMainMappingWriteEnd();
}
static DWORD WINAPI reader(void* unused) {
    (void)unused;
    return gcm_io2ea(0x04300004)==0x0B000004 && reads==1 ? 0:1;
}
static DWORD WINAPI waiting_reader(void* unused) {
    (void)unused;
    /* An odd publication must wait for the writer, never reuse old data. */
    return gcm_io2ea(0x04300004)==0x0C000004 ? 0:1;
}
int main(void) {
    memset(s_ea_address_table,0xff,sizeof s_ea_address_table);
    CHECK(gcm_io2ea(0x04300004)==0,"unmapped lookup");
    map(0x43,0xA0);
    unsigned start=reads; int bad=0;
    for(u32 i=0;i<1000000;i++)
        if(gcm_io2ea(0x04300000|(i&0xFFFFF))!=(0x0A000000|(i&0xFFFFF))) bad=1;
    CHECK(!bad,"every offset within cached page");
    printf("one million same-page translations: %u shared lock acquisitions\n",reads-start);
    CHECK(reads-start==1,"stable page needs only one shared lock");
    start=reads; u32 ea=0;
    for(u32 i=0;i<1000000;i++)
        if(!cellGcmResolveLocatedChecked(0,0x04300000|(i&0xFFFFF),&ea) ||
           ea!=(0x0A000000|(i&0xFFFFF))) bad=1;
    CHECK(!bad,"checked MAIN addresses preserve every offset");
    CHECK(reads==start,"checked MAIN shares guarded page cache");
    map(0x43,0xB0);
    CHECK(gcm_io2ea(0x04300004)==0x0B000004,"remap invalidates cache");
    map(0x44,0xC0);
    CHECK(gcm_io2ea(0x04400000)==0x0C000000,"page crossing");
    CHECK(gcm_io2ea(0x043FFFFF)==0x0B0FFFFF,"return to prior page");
    map(0x43,0xFFFF);
    CHECK(gcm_io2ea(0x04300004)==0,"unmap invalidates cache");
    map(0x43,0xB0);
    CHECK(gcm_io2ea(0x04300004)==0x0B000004,"unmap-remap ABA");
    map(0,0);
    CHECK(gcm_io2ea(0)==0 && gcm_io2ea(4)==4,"mapped EA zero preserves legacy ABI");
    CHECK(cellGcmResolveLocatedChecked(0,0,&ea) && ea==0,"checked MAIN accepts mapped EA zero");
    ea=123;
    CHECK(!cellGcmResolveLocatedChecked(0,0x05000000,&ea) && ea==123,
          "unmapped checked MAIN preserves output on failure");
    CHECK(!cellGcmResolveLocatedChecked(0,0,NULL),"null output rejected");
    CHECK(cellGcmResolveLocatedChecked(1,0x04300004,&ea) && ea==0xC4300004,
          "explicit LOCAL ignores MAIN cache");
    map(4095,0xFFF);
    CHECK(gcm_io2ea(0xFFFFFFFF)==0xFFFFFFFF,"top address");
    HANDLE h=CreateThread(NULL,0,reader,NULL,0,NULL); DWORD code=1;
    CHECK(h!=NULL,"create independent reader");
    if(h) { WaitForSingleObject(h,INFINITE); GetExitCodeThread(h,&code); CloseHandle(h); }
    CHECK(code==0,"thread-local cache starts empty");
    cellGcmMainMappingWriteBegin(); s_ea_address_table[0x43]=0xC0;
    h=CreateThread(NULL,0,waiting_reader,NULL,0,NULL); code=1;
    CHECK(h!=NULL,"create reader during publication");
    if(h) CHECK(WaitForSingleObject(h,25)==WAIT_TIMEOUT,"odd generation reader waits");
    cellGcmMainMappingWriteEnd();
    if(h) { WaitForSingleObject(h,INFINITE); GetExitCodeThread(h,&code); CloseHandle(h); }
    CHECK(code==0,"reader receives fully published mapping");
    /* Interleaved attribute buffers are not a one-page FIFO workload. */
    for(u32 i=0;i<16;i++) map(0x100+i,(u16)(0x200+i));
    start=reads;bad=0;LARGE_INTEGER hz,t0,t1;
    QueryPerformanceFrequency(&hz);QueryPerformanceCounter(&t0);
    for(u32 i=0;i<1000000;i++) {
        u32 page=0x100+(i&15), low=i&0xFFFFF;
        if(gcm_io2ea((page<<20)|low)!=(((page+0x100)<<20)|low)) bad=1;
    }
    QueryPerformanceCounter(&t1);
    CHECK(!bad,"alternating mapped pages preserve all byte addresses");
    printf("one million alternating-page translations: %u locks, %.3fms\n",
           reads-start,(double)(t1.QuadPart-t0.QuadPart)*1000/hz.QuadPart);
    CHECK(reads-start==16,"sixteen stable pages need only sixteen shared locks");
    map(0x107,0x307); /* Invalidate every prior entry, not only the last page. */
    for(u32 i=0;i<16;i++) {
        u32 expected=i==7?0x307:0x200+i;
        CHECK(gcm_io2ea((0x100+i)<<20)==(expected<<20),"publication invalidates all cached pages");
    }
    map(0x120,0x320); /* Same direct-map bucket as 0x100. */
    for(u32 i=0;i<128;i++) {
        u32 page=(i&1)?0x120:0x100, expected=(i&1)?0x320:0x200;
        CHECK(gcm_io2ea((page<<20)|4)==((expected<<20)|4),"colliding pages retain distinct tags");
    }
    map(0x107,0xFFFF);
    CHECK(gcm_io2ea(0x10700004)==0,"unmap invalidates an older cached entry");
    printf("FIFO mapping cache: %u checks, %u failures\n",checks,failures);
    return failures?1:0;
}
'''.replace("FUNCTIONS", functions)
    clang = shutil.which("clang") or r"C:\Program Files\LLVM\bin\clang.exe"
    with tempfile.TemporaryDirectory(prefix="tj-fifo-map-") as temp:
        c, exe = Path(temp)/"mapping.c", Path(temp)/"mapping.exe"
        c.write_text(source,encoding="utf-8")
        subprocess.run([clang,"-std=c11","-O2",str(c),"-o",str(exe)],check=True)
        return subprocess.run([str(exe)],check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
