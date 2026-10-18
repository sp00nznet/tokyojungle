"""Native Windows dirty-page coherence, overlapping ranges, and failure fallback."""
from pathlib import Path
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
VIDEO = ROOT.parent / "ps3recomp/libs/video"


def main():
    source = r'''
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
static int injected_error;
static unsigned query_calls;
static unsigned page_scans;
static UINT watched_query(DWORD flags,PVOID base,SIZE_T bytes,PVOID* pages,
                           ULONG_PTR* count,DWORD* granularity) {
    ++query_calls;
    if(flags!=WRITE_WATCH_FLAG_RESET)return ERROR_INVALID_PARAMETER;
    if(injected_error)return ERROR_INVALID_PARAMETER;
    return GetWriteWatch(flags,base,bytes,pages,count,granularity);
}
#define GetWriteWatch watched_query
#include "rsx_texture_write_watch.h"
#undef GetWriteWatch
static unsigned checks;
#define CHECK(x) do { ++checks; if(!(x)){fprintf(stderr,"FAIL %d: %s\n",__LINE__,#x);return 1;} } while(0)
static uint32_t rng=173;
static uint32_t next(void) {rng=rng*1664525u+1013904223u;return rng;}
static int cached_mode;
static uint64_t observe(RsxTextureWriteWatch* watch,const unsigned char* base,
    uint64_t span,uint32_t off,uint32_t bytes,RsxTextureWriteStampHint* hint) {
    return cached_mode ? rsx_texture_write_watch_stamp_cached(watch,base,span,off,bytes,hint)
                       : rsx_texture_write_watch_stamp(watch,base,span,off,bytes);
}
typedef struct { uint32_t off,bytes;uint64_t stamp;unsigned char saved[512];
                 RsxTextureWriteStampHint hint; } Capture;
typedef struct { volatile LONG* value; HANDLE ready,done; } Writer;
static DWORD WINAPI write_worker(void* raw) {
    Writer* writer=raw;
    for(int i=1;i<=300;++i) {
        if(WaitForSingleObject(writer->ready,5000)!=WAIT_OBJECT_0)return 1;
        InterlockedExchange(writer->value,i);
        SetEvent(writer->done);
    }
    return 0;
}
int main(int argc,char** argv) {
    (void)argv;cached_mode=argc>1;
    const uint32_t span=65536;
    unsigned char* memory=VirtualAlloc(NULL,span,MEM_RESERVE|MEM_COMMIT|MEM_WRITE_WATCH,PAGE_READWRITE);
    CHECK(memory);memset(memory,0x29,span);
    RsxTextureWriteWatch watch={0};
    Capture captures[32]={0};
    for(unsigned i=0;i<32;++i) {
        captures[i].off=(i*1871u)%59000u;captures[i].bytes=201u+i*7u;
        captures[i].stamp=observe(&watch,memory,span,captures[i].off,captures[i].bytes,&captures[i].hint);
        CHECK(captures[i].stamp);memcpy(captures[i].saved,memory+captures[i].off,captures[i].bytes);
    }
    Capture* clean=&captures[0];
    clean->stamp=observe(&watch,memory,span,clean->off,clean->bytes,&clean->hint);
    unsigned before_queries=query_calls;
    unsigned before_scans=page_scans;
    for(unsigned i=0;i<100;++i)
        CHECK(observe(&watch,memory,span,clean->off,clean->bytes,&clean->hint)==clean->stamp);
    CHECK(query_calls==before_queries+100); /* even cached maxima require the OS query */
    if(cached_mode)CHECK(page_scans==before_scans);
    RsxTextureWriteStampHint range_hint={0};
    CHECK(observe(&watch,memory,span,0,4096,&range_hint));
    uint64_t other=observe(&watch,memory,span,32768,4096,&range_hint);
    CHECK(other==rsx_texture_write_watch_stamp(&watch,memory,span,32768,4096));
    if(cached_mode)CHECK(range_hint.first==8 && range_hint.last==8);
    uint64_t prior_overlap=observe(&watch,memory,span,0,8192,&range_hint);
    memory[4097]^=1;before_scans=page_scans;
    CHECK(observe(&watch,memory,span,4096,4096,NULL)>prior_overlap);
    CHECK(page_scans==before_scans); /* the dirty query already knows the maximum */
    before_scans=page_scans;
    CHECK(observe(&watch,memory,span,0,8192,&range_hint)>prior_overlap);
    CHECK(page_scans==before_scans+2); /* another observer consumed the dirty flag */
    for(unsigned i=0;i<32;++i) { /* refresh byte oracle after the explicit mutation */
        captures[i].stamp=observe(&watch,memory,span,captures[i].off,captures[i].bytes,&captures[i].hint);
        memcpy(captures[i].saved,memory+captures[i].off,captures[i].bytes);
    }
    /* Byte comparisons are the oracle. Overlapping observations can reset
     * Windows' dirty flags, but must not hide writes from any older capture. */
    for(unsigned step=0;step<12000;++step) {
        Capture* changed=&captures[next()%32];
        memory[changed->off+next()%changed->bytes]^=0x41;
        uint32_t page=changed->off&~4095u;
        CHECK(observe(&watch,memory,span,page+3900,16,NULL));
        Capture* read=&captures[next()%32];
        uint64_t stamp=observe(&watch,memory,span,read->off,read->bytes,&read->hint);
        CHECK(stamp);
        if(stamp==read->stamp)CHECK(!memcmp(read->saved,memory+read->off,read->bytes));
        else {memcpy(read->saved,memory+read->off,read->bytes);read->stamp=stamp;}
    }
    CHECK(!observe(&watch,memory,span,span-1,2,&range_hint));
    CHECK(!observe(&watch,memory,span,0,0,&range_hint));
    CHECK(watch.state==1); /* bad individual range does not poison valid ranges */
    Writer writer={(volatile LONG*)(memory+63000),CreateEventA(NULL,FALSE,FALSE,NULL),CreateEventA(NULL,FALSE,FALSE,NULL)};
    CHECK(writer.ready && writer.done);
    HANDLE thread=CreateThread(NULL,0,write_worker,&writer,0,NULL);CHECK(thread);
    RsxTextureWriteStampHint writer_hint={0};
    uint64_t stamp=observe(&watch,memory,span,63000,4,&writer_hint);
    for(int i=1;i<=300;++i) {
        SetEvent(writer.ready);CHECK(WaitForSingleObject(writer.done,5000)==WAIT_OBJECT_0);
        uint64_t changed=observe(&watch,memory,span,63000,4,&writer_hint);
        CHECK(changed>stamp && *writer.value==i);stamp=changed;
        CHECK(observe(&watch,memory,span,63000,4,&writer_hint)==stamp);
    }
    CHECK(WaitForSingleObject(thread,5000)==WAIT_OBJECT_0);
    CloseHandle(thread);CloseHandle(writer.ready);CloseHandle(writer.done);
    /* Dirty ranges outside the capture must not invalidate it. */
    stamp=observe(&watch,memory,span,0,64,&range_hint);
    memory[50000]^=1;CHECK(observe(&watch,memory,span,50000,1,NULL));
    CHECK(observe(&watch,memory,span,0,64,&range_hint)==stamp);
    injected_error=1;CHECK(!observe(&watch,memory,span,0,64,&range_hint));
    CHECK(watch.state==-1);injected_error=0;
    CHECK(!observe(&watch,memory,span,0,64,&range_hint));
    rsx_texture_write_watch_destroy(&watch);
    CHECK(!watch.state && !watch.dirty_pages && !watch.page_epochs);
    memset(&range_hint,0,sizeof(range_hint)); /* hints share their watch owner's lifetime */
    CHECK(observe(&watch,memory,span,0,64,&range_hint));
    watch.epoch=UINT64_MAX-1;
    CHECK(!observe(&watch,memory,span,0,64,&range_hint));
    rsx_texture_write_watch_destroy(&watch);
    unsigned char* ordinary=VirtualAlloc(NULL,span,MEM_RESERVE|MEM_COMMIT,PAGE_READWRITE);
    CHECK(ordinary);memset(&range_hint,0,sizeof(range_hint));
    CHECK(!observe(&watch,ordinary,span,0,64,&range_hint));
    CHECK(watch.state==-1);rsx_texture_write_watch_destroy(&watch);
    VirtualFree(ordinary,0,MEM_RELEASE);VirtualFree(memory,0,MEM_RELEASE);
    printf("PASS: %u native write-watch coherence/fallback checks (%s)\n",checks,cached_mode?"cached":"uncached");
    return 0;
}
'''
    compiler=shutil.which("clang") or r"C:\Program Files\LLVM\bin\clang.exe"
    with tempfile.TemporaryDirectory(prefix="tj-write-watch-") as temp:
        c,exe=Path(temp)/"watch.c",Path(temp)/"watch.exe"
        # Count page reads in the production helper without adding a runtime
        # test hook. Only the loop body gains an observation counter.
        header=(VIDEO/"rsx_texture_write_watch.h").read_text(encoding="utf-8")
        walk="for (uint64_t page = first; page <= last; ++page)\n            if (watch->page_epochs[page] > latest) latest = watch->page_epochs[page];"
        assert header.count(walk)==1
        instrumented=header.replace(walk,"for (uint64_t page = first; page <= last; ++page) {\n            ++page_scans;\n            if (watch->page_epochs[page] > latest) latest = watch->page_epochs[page];\n        }")
        (Path(temp)/"rsx_texture_write_watch.h").write_text(instrumented,encoding="utf-8")
        c.write_text(source,encoding="utf-8")
        subprocess.run([compiler,"-std=c11","-O2","-I",str(VIDEO),str(c),"-o",str(exe)],check=True)
        subprocess.run([str(exe)],check=True)
        subprocess.run([str(exe),"--cached"],check=True)
        # Exercise the actual VM initializer, including failed tracked reserve.
        vm_source=r'''
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <stdio.h>
#include <stdlib.h>
static int reject_tracked,tracked_attempts;
static LPVOID reserve_probe(LPVOID address,SIZE_T size,DWORD type,DWORD protection) {
    if(type & MEM_WRITE_WATCH) {++tracked_attempts;if(reject_tracked)return NULL;}
    return VirtualAlloc(address,size,type,protection);
}
#define VirtualAlloc reserve_probe
#include "vm.h"
#undef VirtualAlloc
uint8_t* vm_base;
uint32_t ppu_vm_size;
static int run(const char* mode,int fail,int expected_tracked) {
    _putenv_s("TJ_TEXTURE_WRITE_WATCH",mode);reject_tracked=fail;
    if(vm_init()!=CELL_OK || !vm_base)return 1;
    vm_base[VM_MAIN_MEM_BASE]=17;vm_base[VM_STACK_BASE]=29;
    void* pages[2];ULONG_PTR count=2;DWORD granularity=0;
    UINT rc=GetWriteWatch(WRITE_WATCH_FLAG_RESET,vm_base+VM_MAIN_MEM_BASE,4096,pages,&count,&granularity);
    int bad=expected_tracked ? (rc || count!=1 || granularity!=4096) : rc==0;
    if(vm_base[VM_MAIN_MEM_BASE]!=17 || vm_base[VM_STACK_BASE]!=29)bad=1;
    vm_shutdown();if(vm_base)bad=1;
    return bad;
}
int main(void) {
    if(run("0",0,0) || run("1",0,1) || run("2",0,1) || run("1",1,0) || tracked_attempts!=3)return 1;
    puts("PASS: production VM off/on/audit/reservation-fallback and shutdown");return 0;
}
'''
        c.write_text(vm_source,encoding="utf-8")
        subprocess.run([compiler,"-std=c11","-O2","-D_CRT_SECURE_NO_WARNINGS","-I",
                        str(ROOT.parent/"ps3recomp/runtime/memory"),str(c),"-o",str(exe)],check=True)
        subprocess.run([str(exe)],check=True)


if __name__=="__main__":
    main()
