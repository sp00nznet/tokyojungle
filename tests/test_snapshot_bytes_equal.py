"""Exact comparison regression and bounded CPU-only comparison benchmark."""
from pathlib import Path
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
VIDEO = ROOT.parent / "ps3recomp/libs/video"


def main():
    source = r'''
#include <windows.h>
#include <stdio.h>
#include <stdint.h>
#include <stdlib.h>
#include "rsx_bytes_equal.h"
static unsigned checks, failures;
static int use_avx2;
#define CHECK(c) do {++checks; if(!(c)) {++failures; printf("FAIL line %d\n",__LINE__);}} while(0)
__declspec(noinline) static int exact(const void* a,const void* b,size_t n) {
    if (use_avx2) return rsx_bytes_equal_avx2(a,b,n);
    return rsx_bytes_equal(a,b,n);
}
static int (__cdecl * volatile crt_compare)(const void*,const void*,size_t)=memcmp;
__declspec(noinline) static int baseline(const void* a,const void* b,size_t n) {
    return crt_compare(a,b,n)==0;
}
typedef int (*Compare)(const void*,const void*,size_t);
static volatile unsigned sink;
static double bench(Compare compare,const unsigned char* a,unsigned char* b,size_t n) {
    LARGE_INTEGER frequency,start,end; QueryPerformanceFrequency(&frequency);
    QueryPerformanceCounter(&start);
    for(unsigned i=0;i<512;i++) { b[n-1]=a[n-1]^(i&1); sink+=compare(a,b,n); }
    QueryPerformanceCounter(&end);
    b[n-1]=a[n-1];
    return (double)(end.QuadPart-start.QuadPart)*1000.0/frequency.QuadPart;
}
int main(int argc, char** argv) {
    (void)argv;
    use_avx2 = argc > 1;
    if(use_avx2 && !IsProcessorFeaturePresent(PF_AVX2_INSTRUCTIONS_AVAILABLE)) {
        puts("AVX2 unavailable: skipped; SSE2 gate still required"); return 0;
    }
    unsigned char a[1152],b[1152];
    for(unsigned i=0;i<sizeof a;i++)a[i]=(unsigned char)(i*37);
    for(unsigned align=0;align<32;align++) {
        for(unsigned n=0;n<=1024;n++) {
            memcpy(b+31-align,a+align,n);
            CHECK(exact(a+align,b+31-align,n));
            if(n) { b[31-align+n-1]^=0x80; CHECK(!exact(a+align,b+31-align,n)); }
        }
    }
    for(unsigned n=1;n<=256;n++) {
        memcpy(b,a,n);
        for(unsigned pos=0;pos<n;pos++) {
            b[pos]^=1; CHECK(!exact(a,b,n)); b[pos]^=1;
        }
    }
    SYSTEM_INFO info; GetSystemInfo(&info); size_t page=info.dwPageSize;
    unsigned char* ga=VirtualAlloc(NULL,page*2,MEM_RESERVE|MEM_COMMIT,PAGE_READWRITE);
    unsigned char* gb=VirtualAlloc(NULL,page*2,MEM_RESERVE|MEM_COMMIT,PAGE_READWRITE);
    CHECK(ga && gb); if(!ga || !gb)return 1;
    DWORD old;
    CHECK(VirtualProtect(ga+page,page,PAGE_NOACCESS,&old));
    CHECK(VirtualProtect(gb+page,page,PAGE_NOACCESS,&old));
    memset(ga,0x5a,page); memset(gb,0x5a,page);
    for(size_t n=0;n<=page;n++) {
        CHECK(exact(ga+page-n,gb+page-n,n));
        if(n) { gb[page-1]^=1; CHECK(!exact(ga+page-n,gb+page-n,n)); gb[page-1]^=1; }
    }
    VirtualFree(ga,0,MEM_RELEASE); VirtualFree(gb,0,MEM_RELEASE);
    printf("Snapshot equality %s: %u checks, %u failures\n",use_avx2?"AVX2":"SSE2",checks,failures);
    const size_t sizes[]={65536,1048576,4194304};
    unsigned char* large_a=malloc(sizes[2]); unsigned char* large_b=malloc(sizes[2]);
    if(!large_a || !large_b)return 1;
    memset(large_a,0x37,sizes[2]); memcpy(large_b,large_a,sizes[2]);
    for(unsigned i=0;i<3;i++) {
        double old_ms=bench(baseline,large_a,large_b,sizes[i]);
        double new_ms=bench(exact,large_a,large_b,sizes[i]);
        printf("bytes=%zu CRT=%.3fms SIMD=%.3fms speedup=%.2fx\n",sizes[i],old_ms,new_ms,old_ms/new_ms);
    }
    free(large_a);free(large_b);
    return failures?1:0;
}
'''
    clang = shutil.which("clang") or r"C:\Program Files\LLVM\bin\clang.exe"
    with tempfile.TemporaryDirectory(prefix="tj-snapshot-equal-") as temp:
        c, exe = Path(temp)/"equal.c", Path(temp)/"equal.exe"
        c.write_text(source, encoding="utf-8")
        subprocess.run([clang, "-O2", "-std=c11", "-D_CRT_SECURE_NO_WARNINGS", "-I",
                        str(VIDEO), str(c), "-o", str(exe)], check=True)
        baseline = subprocess.run([str(exe)], check=False).returncode
        candidate = subprocess.run([str(exe), "--avx2"], check=False).returncode
        return baseline or candidate


if __name__ == "__main__":
    raise SystemExit(main())
