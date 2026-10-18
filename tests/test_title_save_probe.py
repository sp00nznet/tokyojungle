"""Compile actual diagnostic gates: never dispatch against user saves or stale OPDs."""
from pathlib import Path
import shutil
import subprocess
import tempfile

from test_fifo_present_boundary import function

ROOT = Path(__file__).resolve().parents[1]


def main():
    text = (ROOT / "src/tj_crt_overrides.cpp").read_text(encoding="utf-8")
    funcs = "\n".join(function(text, sig) for sig in (
        "static bool tj_title_save_probe_root_allowed(",
        "static int tj_title_save_probe_request("))
    source = r'''
#include <windows.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
static void* g_ps3_guest_caller=(void*)1;
static unsigned calls,reads,checks;
static int mismatch=-1,exists;
static uint32_t profile=0xBCC1E0;
static uint8_t starters[2]={4,4};
static uint32_t vm_read32(uint32_t a) {
    static const uint32_t ea[]={0x34C210,0x34C214,0x3591F8,0x34C248,0x34C24C};
    static const uint32_t value[]={0x1AC41C,0x359220,0x34C248,0x1ACEB8,0x359220};
    ++reads;
    if(a==0x35925C)return profile;
    for(int i=0;i<5;++i)if(a==ea[i])return value[i]^(i==mismatch?4:0);
    abort();
}
static uint8_t vm_read8(uint32_t a) {
    ++reads;
    if(a==0xBCC308)return starters[0];
    if(a==0xBCC30C)return starters[1];
    abort();
}
static DWORD fake_attributes(const char* path) {
    if(!strstr(path,"/NPUA80523-AUTO-/SYS-DATA"))abort();
    return exists?FILE_ATTRIBUTE_NORMAL:INVALID_FILE_ATTRIBUTES;
}
#define GetFileAttributesA fake_attributes
static void ps3_invoke_guest(uint32_t opd,uint64_t a,uint64_t b,uint64_t c,uint64_t d,
                             uint64_t e,uint64_t f,uint64_t g,uint64_t h) {
    if(opd!=0x34C210 || a!=1 || b||c||d||e||f||g||h)abort();
    ++calls;
}
FUNCTIONS
#define CHECK(c) do {++checks;if(!(c)){fprintf(stderr,"FAIL line %d\n",__LINE__);return 1;}}while(0)
int main(void) {
    const char* root="C:\\codex\\candidate-runtime\\savedata-menu-probe-test";
    CHECK(tj_title_save_probe_request(NULL,NULL)==0);
    CHECK(tj_title_save_probe_request("0",NULL)==0);
    CHECK(tj_title_save_probe_request("true",root)==0);
    CHECK(reads==0 && calls==0);
    CHECK(tj_title_save_probe_request("1",NULL)==-1);
    CHECK(tj_title_save_probe_request("1","C:\\codex\\work\\target-savedata")==-1);
    CHECK(tj_title_save_probe_request("1","C:\\codex\\candidate-runtime\\savedata-menu-probe-x\\..\\..\\target-savedata")==-1);
    CHECK(tj_title_save_probe_request("1","C:\\codex\\other\\savedata-menu-probe-test")==-1);
    CHECK(reads==0 && calls==0);
    g_ps3_guest_caller=NULL;
    CHECK(tj_title_save_probe_request("1",root)==-2 && calls==0);
    g_ps3_guest_caller=(void*)1;
    for(int i=0;i<5;++i){mismatch=i;CHECK(tj_title_save_probe_request("1",root)==-3 && calls==0);}
    mismatch=-1;exists=1;
    CHECK(tj_title_save_probe_request("1",root)==-4 && calls==0);
    exists=0;
    profile=0;
    CHECK(tj_title_save_probe_request("1",root)==-5 && calls==0);
    profile=0xBCC1E0;
    starters[0]=starters[1]=0;
    CHECK(tj_title_save_probe_request("1",root)==-5 && calls==0);
    starters[0]=4;
    CHECK(tj_title_save_probe_request("1",root)==-5 && calls==0);
    starters[0]=1;starters[1]=4;
    CHECK(tj_title_save_probe_request("1",root)==-5 && calls==0);
    starters[0]=4;
    CHECK(tj_title_save_probe_request("1",root)==1 && calls==1);
    CHECK(tj_title_save_probe_request("1","C:/codex/candidate-runtime/savedata-menu-probe-test")==1 && calls==2);
    printf("title save probe: %u/%u checks passed\n",checks,checks);
    return 0;
}
'''.replace("FUNCTIONS", funcs)
    compiler = shutil.which("clang++") or r"C:\Program Files\LLVM\bin\clang++.exe"
    with tempfile.TemporaryDirectory(prefix="tj-title-save-probe-") as temp:
        cpp, exe = Path(temp) / "probe.cpp", Path(temp) / "probe.exe"
        cpp.write_text(source, encoding="utf-8")
        subprocess.run([compiler, "-std=c++17", "-O2", "-D_CRT_SECURE_NO_WARNINGS",
                        str(cpp), "-o", str(exe)], check=True)
        subprocess.run([str(exe)], check=True)


if __name__ == "__main__":
    main()
