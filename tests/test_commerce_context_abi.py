"""Replay the imported five-argument commerce context call without networking."""
from pathlib import Path
import shutil
import subprocess
import tempfile

ROOT=Path(__file__).resolve().parents[1]
RUNTIME=ROOT.parent/"ps3recomp"


def main():
    source=r'''
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include "sceNpCommerce2.h"
static unsigned char memory[4096];
uint32_t ppu_vm_size=sizeof(memory);
void vm_write32(unsigned long long ea,unsigned int value) {
    for(unsigned i=0;i<4;++i) memory[ea+i]=(unsigned char)(value>>(24-8*i));
}
static uint32_t read32(uint32_t ea) {
    return (uint32_t)memory[ea]<<24|(uint32_t)memory[ea+1]<<16|
           (uint32_t)memory[ea+2]<<8|memory[ea+3];
}
static unsigned checks,failures;
static void check(int ok,const char* label) {
    ++checks;if(!ok){++failures;fprintf(stderr,"FAIL: %s\n",label);}
}
static s32 create(uint32_t output) {
    // Match generic HLE register forwarding; an old 3-argument implementation
    // mistakenly treats the callback OPD in r5 as the output in r7.
    typedef s32 (*GuestCall)(u32,const void*,const void*,const void*,SceNpCommerce2Context*);
    GuestCall call=(GuestCall)sceNpCommerce2CreateCtx;
    return call(2,(const void*)(uintptr_t)0x100,(const void*)(uintptr_t)0x200,
                (const void*)(uintptr_t)0x280,(SceNpCommerce2Context*)(uintptr_t)output);
}
int main(void) {
    check(sceNpCommerce2Init()==CELL_OK,"commerce init");
    for(unsigned i=0;i<16;++i) {
        memset(memory,0xA5,sizeof(memory));vm_write32(0x200,0x00158000);vm_write32(0x204,0x00359220);
        unsigned char callback[8];memcpy(callback,memory+0x200,8);
        check(create(0x300)==CELL_OK,"create context with firmware ABI");
        uint32_t handle=read32(0x300);
        check(handle<4,"context returned through fifth argument");
        check(!memcmp(callback,memory+0x200,8),"callback OPD was overwritten");
        check(memory[0x2ff]==0xA5 && memory[0x304]==0xA5 && memory[0x280]==0xA5,
              "out pointer or callback userdata neighbours changed");
        check(sceNpCommerce2CreateSessionStart(handle)==SCE_NP_COMMERCE2_ERROR_SERVER_ERROR,
              "offline session behavior changed");
        check(sceNpCommerce2DestroyCtx(handle)==CELL_OK,"destroy/recycle context");
    }
    const uint32_t invalid[]={0,sizeof(memory)-2,0xfffffffeu};
    for(unsigned i=0;i<3;++i) {
        unsigned char before[sizeof(memory)];memcpy(before,memory,sizeof(memory));
        check(create(invalid[i])==SCE_NP_COMMERCE2_ERROR_INVALID_ARGUMENT,
              "invalid output address rejected before allocating");
        check(!memcmp(before,memory,sizeof(memory)),"invalid call changed guest memory");
    }
    ppu_vm_size=0;
    check(create(0xfffffffeu)==SCE_NP_COMMERCE2_ERROR_INVALID_ARGUMENT,
          "4-GiB convention rejects wrapped output");
    ppu_vm_size=sizeof(memory);
    check(create(sizeof(memory)-4)==CELL_OK && read32(sizeof(memory)-4)<4,
          "exact-end four-byte output remains valid");
    check(sceNpCommerce2Term()==CELL_OK,"commerce term");
    printf("commerce context ABI: %u checks, %u failures\n",checks,failures);
    return failures?1:0;
}
'''
    cc=shutil.which("clang") or shutil.which("gcc") or r"C:\Program Files\LLVM\bin\clang.exe"
    with tempfile.TemporaryDirectory(prefix="tj-commerce-context-") as tmp:
        c,exe=Path(tmp)/"test.c",Path(tmp)/"test.exe"
        c.write_text(source,encoding="utf-8")
        subprocess.run([cc,"-std=c11","-I",str(RUNTIME/"include"),"-I",str(RUNTIME/"libs/network"),
                        str(c),str(RUNTIME/"libs/network/sceNpCommerce2.c"),"-o",str(exe)],check=True)
        subprocess.run([str(exe)],check=True)


if __name__=="__main__":
    main()
