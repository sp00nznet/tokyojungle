"""Replay the title's five-register l10n converter ABI and guest byte order."""
from pathlib import Path
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT.parent / "ps3recomp"


def main():
    text = (ROOT / "src/main.cpp").read_text(encoding="utf-8")
    bridge = text[text.index("static void tj_l10nSwapUnits("):
                  text.index("static void tj_cellRescGcmSurface2RescSrc(")]
    if "static void tj_l10nGetConverter(" not in bridge:
        bridge += """
static void tj_l10nGetConverter(ppu_context* c) {
    c->gpr[3]=(uint64_t)(int64_t)l10n_get_converter((uint32_t)c->gpr[3],(uint32_t)c->gpr[4]);
}
"""
    else:
        assert '"l10n_get_converter", tj_l10nGetConverter)' in text
    source = r'''
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include "cellL10n.h"
static uint8_t memory[4096], *vm_base=memory;
static uint32_t ppu_vm_size=sizeof(memory);
static uint32_t vm_read32(uint32_t ea) {
    return uint32_t(memory[ea])<<24|uint32_t(memory[ea+1])<<16|
           uint32_t(memory[ea+2])<<8|memory[ea+3];
}
static void vm_write32(uint32_t ea,uint32_t v) {
    for(unsigned i=0;i<4;++i) memory[ea+i]=uint8_t(v>>(24-8*i));
}
struct ppu_context { uint64_t gpr[32]={}; };
BRIDGE
static unsigned checks,failures;
static void check(bool ok,const char* label) {
    ++checks; if(!ok) { ++failures; fprintf(stderr,"FAIL: %s\n",label); }
}
static int32_t converter(uint32_t src,uint32_t dst) {
    ppu_context c; c.gpr[3]=src;c.gpr[4]=dst;tj_l10nGetConverter(&c);
    return int32_t(c.gpr[3]);
}
static int32_t convert(uint32_t cd,uint32_t src=0x100,uint32_t dst=0x200,
                       uint32_t slen=0x40,uint32_t dlen=0x44) {
    ppu_context c;c.gpr[3]=cd;c.gpr[4]=src;c.gpr[5]=slen;
    c.gpr[6]=dst;c.gpr[7]=dlen;c.gpr[8]=0; // unused by the real guest caller
    tj_l10nConvertStr(&c);return int32_t(c.gpr[3]);
}
int main() {
    memset(memory,0xA5,sizeof(memory));
    const int32_t utf8=converter(0,0), to16=converter(0,1), from16=converter(1,0);
    check(utf8==0 && to16==1 && from16==0x10000,"converter retains both encodings");
    check(converter(0xffffffffu,0)==-1 && converter(0,0xffffu)==-1,
          "unknown converter returns the title's -1 sentinel");
    memcpy(memory+0x100,"hello",5);vm_write32(0x40,5);vm_write32(0x44,16);
    check(convert(utf8)==0,"five-argument conversion succeeds with unused r8=0");
    check(!memcmp(memory+0x200,"hello",5) && vm_read32(0x44)==5 && vm_read32(0x40)==5,
          "title's output buffer and byte count are correct");
    check(memory[0x1ff]==0xA5 && memory[0x205]==0xA5 && memory[0x48]==0xA5,
          "conversion preserves surrounding memory");
    const uint8_t input[]={0x41,0xC3,0xA9,0xE6,0x97,0xA5};
    const uint8_t wide[]={0,0x41,0,0xE9,0x65,0xE5};
    memcpy(memory+0x100,input,sizeof(input));vm_write32(0x40,sizeof(input));vm_write32(0x44,16);
    check(convert(to16)==0 && vm_read32(0x44)==sizeof(wide) &&
          !memcmp(memory+0x200,wide,sizeof(wide)),"UTF-16 destination is guest big-endian");
    memcpy(memory+0x100,wide,sizeof(wide));vm_write32(0x40,sizeof(wide));vm_write32(0x44,16);
    check(convert(from16)==0 && vm_read32(0x44)==sizeof(input) &&
          !memcmp(memory+0x200,input,sizeof(input)),"UTF-16 guest source decodes correctly");
    vm_write32(0x44,0);
    check(convert(from16,0x100,0)==0 && vm_read32(0x44)==sizeof(input),
          "null destination queries required byte length");
    memory[0x100]=0xE9;vm_write32(0x40,1);vm_write32(0x44,16);
    check(convert(converter(5,0))==0 && vm_read32(0x44)==2 &&
          memory[0x200]==0xC3 && memory[0x201]==0xA9,"SDK encoding 5 is Latin-1");
    check(converter(24,0)==-1,"unsupported Shift-JIS is not advertised as UTF");
    vm_write32(0x40,3);vm_write32(0x44,2);memcpy(memory+0x100,"abc",3);
    memory[0x202]=0xA5;
    check(convert(utf8)==2 && vm_read32(0x44)==2 &&
          !memcmp(memory+0x200,"ab",2) && memory[0x202]==0xA5,
          "short destination reports exhaustion and its complete prefix");
    vm_write32(0x40,3);vm_write32(0x44,16);memory[0x100]=0xFF;
    check(convert(to16)==1,"invalid UTF returns SDK source-illegal result");
    check(convert(0xffffffffu)==3,"invalid descriptor returns converter-unknown");
    vm_write32(0x40,8);vm_write32(0x44,16);
    check(convert(utf8,sizeof(memory)-4)!=0,"source range outside VM rejected");
    check(convert(utf8,0x100,sizeof(memory)-4)!=0,"destination range outside VM rejected");
    check(convert(utf8,0x100,0x200,sizeof(memory)-2)!=0,"length pointer outside VM rejected");
    check(convert(utf8,0x100,0x200,0x40,0xfffffffeu)!=0,"wrapped length pointer rejected");
    const uint8_t accents[6][4]={{0xC3,0xA9},{0,0xE9},{0,0,0,0xE9},
                               {0,0xE9},{0,0,0,0xE9},{0xE9}};
    const uint32_t sizes[6]={2,2,4,2,4,1};
    for(uint32_t src=0;src<6;++src) for(uint32_t dst=0;dst<6;++dst) {
        memcpy(memory+0x100,accents[src],sizes[src]);
        memset(memory+0x200,0xA5,16);vm_write32(0x40,sizes[src]);vm_write32(0x44,16);
        check(convert(converter(src,dst))==0 && vm_read32(0x44)==sizes[dst] &&
              vm_read32(0x40)==sizes[src] && !memcmp(memory+0x200,accents[dst],sizes[dst]) &&
              memory[0x200+sizes[dst]]==0xA5,"Unicode/Latin-1 conversion matrix");
    }
    const uint8_t emoji[3][4]={{0xF0,0x9F,0x98,0x80},{0xD8,0x3D,0xDE,0},
                             {0,1,0xF6,0}};
    for(uint32_t src=0;src<3;++src) for(uint32_t dst=0;dst<3;++dst) {
        memcpy(memory+0x100,emoji[src],4);vm_write32(0x40,4);vm_write32(0x44,16);
        check(convert(converter(src,dst))==0 && vm_read32(0x44)==4 &&
              !memcmp(memory+0x200,emoji[dst],4),"supplementary Unicode conversion matrix");
    }
    memcpy(memory+0x100,emoji[1],4);vm_write32(0x40,4);vm_write32(0x44,16);
    check(convert(converter(3,0))==1 && vm_read32(0x44)==0,"UCS-2 rejects surrogate code units");
    memcpy(memory+0x100,emoji[0],4);vm_write32(0x40,4);vm_write32(0x44,16);
    check(convert(converter(0,3))==1 && vm_read32(0x44)==0,"UCS-2 rejects non-BMP output");
    memcpy(memory+0x100,"A\xC3\xA9",3);vm_write32(0x40,3);vm_write32(0x44,2);
    memory[0x201]=0xA5;
    check(convert(utf8)==2 && vm_read32(0x44)==1 && memory[0x200]=='A' && memory[0x201]==0xA5,
          "exhaustion never copies a partial UTF-8 character");
    memory[0x100]='A';memory[0x101]=0xFF;vm_write32(0x40,2);vm_write32(0x44,16);
    check(convert(utf8)==1 && vm_read32(0x44)==1 && memory[0x200]=='A',
          "source error retains only its valid prefix");
    vm_write32(0x100,0x110000);vm_write32(0x40,4);vm_write32(0x44,16);
    check(convert(converter(2,0))==1 && vm_read32(0x44)==0,"UTF-32 rejects out-of-range code point");
    vm_write32(0x100,0xD800);vm_write32(0x44,16);
    check(convert(converter(2,0))==1 && vm_read32(0x44)==0,"UTF-32 rejects surrogate code point");
    printf("l10n guest converter: %u checks, %u failures\n",checks,failures);
    return failures ? 1 : 0;
}
'''.replace("BRIDGE\n", bridge+"\n")
    cc = shutil.which("clang") or shutil.which("gcc") or r"C:\Program Files\LLVM\bin\clang.exe"
    cxx = shutil.which("clang++") or shutil.which("g++") or r"C:\Program Files\LLVM\bin\clang++.exe"
    with tempfile.TemporaryDirectory(prefix="tj-l10n-converter-") as tmp:
        cpp,obj,exe=(Path(tmp)/n for n in ("test.cpp","l10n.o","test.exe"))
        cpp.write_text(source,encoding="utf-8")
        includes=["-I",str(RUNTIME/"include"),"-I",str(RUNTIME/"libs/font")]
        subprocess.run([cc,"-std=c11",*includes,"-c",str(RUNTIME/"libs/font/cellL10n.c"),
                        "-o",str(obj)],check=True)
        subprocess.run([cxx,"-std=c++17",*includes,str(cpp),str(obj),"-o",str(exe)],check=True)
        subprocess.run([str(exe)],check=True)


if __name__ == "__main__":
    main()
