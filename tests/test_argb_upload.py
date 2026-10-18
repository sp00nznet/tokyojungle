"""Exact upload conversion and CPU cost; no GPU workload required."""
from pathlib import Path
import shutil
import subprocess
import tempfile

ROOT=Path(__file__).resolve().parents[1]
VIDEO=ROOT.parent/"ps3recomp/libs/video"


def main():
    source=r'''
#include <windows.h>
#include <stdio.h>
#include <stdlib.h>
#include "rsx_argb_upload.h"
#include "rsx_scaled_blit.h"
static unsigned checks,failures;
#define CHECK(c) do{++checks;if(!(c)){++failures;printf("FAIL line %d\n",__LINE__);}}while(0)
__declspec(noinline) static void old(uint8_t* d,uint32_t dp,const uint8_t* s,
    uint32_t sp,uint32_t w,uint32_t h) {
    memset(d,0,(size_t)dp*h);
    for(uint32_t y=0;y<h;y++)for(uint32_t x=0;x<w;x++) {
        uint8_t* dst=d+(size_t)y*dp;const uint8_t* src=s+(size_t)y*sp;
        dst[x*4]=src[x*4+1];dst[x*4+1]=src[x*4+2];
        dst[x*4+2]=src[x*4+3];dst[x*4+3]=src[x*4];
    }
}
__declspec(noinline) static void candidate(uint8_t* d,uint32_t dp,const uint8_t* s,
    uint32_t sp,uint32_t w,uint32_t h) { rsx_argb_upload_rows(d,dp,s,sp,w,h); }
typedef void(*Convert)(uint8_t*,uint32_t,const uint8_t*,uint32_t,uint32_t,uint32_t);
__declspec(noinline) static void old_reverse(uint8_t* d,uint32_t dp,const uint8_t* s,
    uint32_t sp,uint32_t w,uint32_t h) {
    for(uint32_t y=0;y<h;y++)for(uint32_t x=0;x<w;x++) {
        uint8_t* dst=d+(size_t)y*dp;const uint8_t* src=s+(size_t)y*sp;
        dst[x*4]=src[x*4+3];dst[x*4+1]=src[x*4];
        dst[x*4+2]=src[x*4+1];dst[x*4+3]=src[x*4+2];
    }
}
__declspec(noinline) static void reverse(uint8_t* d,uint32_t dp,const uint8_t* s,
    uint32_t sp,uint32_t w,uint32_t h) {
    rsx_exact_blit_rgba_rows_to_guest_argb(d,dp,s,sp,w,h);
}
static volatile unsigned sink;
static double bench(Convert f,uint8_t* d,const uint8_t* s,uint32_t pitch,uint32_t rows) {
    LARGE_INTEGER hz,t0,t1;QueryPerformanceFrequency(&hz);QueryPerformanceCounter(&t0);
    for(unsigned i=0;i<128;i++){f(d,pitch,s,pitch,pitch/4,rows);sink+=d[i%pitch];}
    QueryPerformanceCounter(&t1);return(double)(t1.QuadPart-t0.QuadPart)*1000/hz.QuadPart;
}
int main(void) {
    uint8_t src[8192],a[8192],b[8192];
    for(unsigned i=0;i<sizeof src;i++)src[i]=(uint8_t)(i*37+(i>>8));
    for(unsigned align=0;align<16;align++)for(unsigned w=0;w<=129;w++)
    for(unsigned pad=0;pad<=31;pad+=7) {
        unsigned sp=w*4+11,dp=w*4+pad,h=5;
        memset(a,0xCD,sizeof a);memset(b,0xCD,sizeof b);
        old(a+align,dp,src+15-align,sp,w,h);
        candidate(b+align,dp,src+15-align,sp,w,h);
        CHECK(!memcmp(a,b,sizeof a));
        memset(a,0xCD,sizeof a);memset(b,0xCD,sizeof b);
        old_reverse(a+align,dp,src+15-align,sp,w,h);
        reverse(b+align,dp,src+15-align,sp,w,h);
        CHECK(!memcmp(a,b,sizeof a));
    }
    for(int shift=-19;shift<=19;shift++)for(unsigned w=0;w<32;w++) {
        memcpy(a,src,sizeof a);memcpy(b,src,sizeof b);
        old_reverse(a+64+shift,w*4,a+64,w*4,w,1);
        reverse(b+64+shift,w*4,b+64,w*4,w,1);
        CHECK(!memcmp(a,b,sizeof a));
    }
    SYSTEM_INFO info;GetSystemInfo(&info);size_t page=info.dwPageSize;
    uint8_t* gs=VirtualAlloc(NULL,page*2,MEM_RESERVE|MEM_COMMIT,PAGE_READWRITE);
    uint8_t* gd=VirtualAlloc(NULL,page*2,MEM_RESERVE|MEM_COMMIT,PAGE_READWRITE);
    DWORD prior;CHECK(gs&&gd);if(!gs||!gd)return 1;
    CHECK(VirtualProtect(gs+page,page,PAGE_NOACCESS,&prior));
    CHECK(VirtualProtect(gd+page,page,PAGE_NOACCESS,&prior));
    for(size_t i=0;i<page;i++)gs[i]=(uint8_t)i;
    for(unsigned w=0;w<=page/4;w++) {
        unsigned bytes=w*4;memset(gd,0xCD,page);
        candidate(gd+page-bytes,bytes,gs+page-bytes,bytes,w,1);
        old(a,bytes,gs+page-bytes,bytes,w,1);
        CHECK(!memcmp(a,gd+page-bytes,bytes));
        memset(gd,0xCD,page);
        reverse(gd+page-bytes,bytes,gs+page-bytes,bytes,w,1);
        old_reverse(a,bytes,gs+page-bytes,bytes,w,1);
        CHECK(!memcmp(a,gd+page-bytes,bytes));
    }
    VirtualFree(gs,0,MEM_RELEASE);VirtualFree(gd,0,MEM_RELEASE);
    printf("ARGB upload: %u checks, %u failures\n",checks,failures);
    const unsigned pitches[]={5120,4096};const unsigned rows[]={720,1024};
    uint8_t* large=malloc(4194304),*out=malloc(4194304);
    if(!large||!out)return 1;memset(large,0x37,4194304);
    for(unsigned i=0;i<2;i++) {
        double before=bench(old,out,large,pitches[i],rows[i]);
        double after=bench(candidate,out,large,pitches[i],rows[i]);
        printf("%ux%u x128: old=%.3fms new=%.3fms ratio=%.2fx\n",
               pitches[i]/4,rows[i],before,after,before/after);
        before=bench(old_reverse,out,large,pitches[i],rows[i]);
        after=bench(reverse,out,large,pitches[i],rows[i]);
        printf("reverse %ux%u x128: old=%.3fms new=%.3fms ratio=%.2fx\n",
               pitches[i]/4,rows[i],before,after,before/after);
    }
    free(large);free(out);return failures?1:0;
}
'''
    clang=shutil.which("clang") or r"C:\Program Files\LLVM\bin\clang.exe"
    with tempfile.TemporaryDirectory(prefix="tj-argb-upload-") as temp:
        c,exe=Path(temp)/"upload.c",Path(temp)/"upload.exe"
        c.write_text(source,encoding="utf-8")
        subprocess.run([clang,"-std=c11","-O2","-I",str(VIDEO),str(c),"-o",str(exe)],check=True)
        return subprocess.run([str(exe)],check=False).returncode


if __name__=="__main__":
    raise SystemExit(main())
