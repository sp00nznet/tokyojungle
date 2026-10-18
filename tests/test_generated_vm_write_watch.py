"""Check production fast stores preserve bytes and forward only watched writes.

Host-only memory/diagnostic proof. No game state or generated PPU source edits.
"""
from pathlib import Path
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def main():
    bridge = (ROOT / "src/recomp_bridge.h").read_text(encoding="utf-8")
    start = bridge.index("#if defined(TJ_GENERATED_VM_FAST_ACCESS)")
    body = bridge[bridge.index("\n", start) + 1:bridge.index("#define vm_read8", start)]
    source = r'''
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <intrin.h>
static uint8_t memory[256];
static uint8_t* vm_base=memory;
static uint64_t ppu_wwatch_word=UINT64_MAX;
static unsigned calls, last_width, checks, failures;
static uint64_t last_addr, last_value;
static uint16_t ps3_bswap16(uint16_t x) { return __builtin_bswap16(x); }
static uint32_t ps3_bswap32(uint32_t x) { return __builtin_bswap32(x); }
static uint64_t ps3_bswap64(uint64_t x) { return __builtin_bswap64(x); }
static uint32_t vm_read32(uint64_t) { return 0; }
static void external_store(uint64_t addr, uint64_t value, unsigned width) {
    ++calls; last_addr=addr; last_value=value; last_width=width;
    for(unsigned i=0;i<width;++i) memory[(uint32_t)addr+i]=(uint8_t)(value>>(8*(width-i-1)));
}
static void vm_write8(uint64_t a,uint8_t v) { external_store(a,v,1); }
static void vm_write16(uint64_t a,uint16_t v) { external_store(a,v,2); }
static void vm_write32(uint64_t a,uint32_t v) { external_store(a,v,4); }
static void vm_write64(uint64_t a,uint64_t v) { external_store(a,v,8); }
BODY
static void check(bool ok,const char* message,unsigned width,unsigned ea) {
    ++checks;
    if(!ok) { ++failures; printf("FAIL %s width=%u ea=%X\n",message,width,ea); }
}
static void probe(uint64_t address,unsigned width,bool watching) {
    const uint32_t ea=(uint32_t)address;
    const uint64_t value=0xFEDCBA9876543210ull;
    const uint64_t truncated=width==8?value:value&((1ull<<(8*width))-1);
    ppu_wwatch_word=watching?0x40:UINT64_MAX;
    memset(memory,0xA5,sizeof(memory)); calls=0;
    switch(width) {
        case 1: tj_generated_vm_write8(address,(uint8_t)value); break;
        case 2: tj_generated_vm_write16(address,(uint16_t)value); break;
        case 4: tj_generated_vm_write32(address,(uint32_t)value); break;
        case 8: tj_generated_vm_write64(address,value); break;
    }
    const bool overlaps=watching && ea<0x44 && (uint64_t)ea+width>0x40;
    check(calls==(overlaps?1u:0u),"exact diagnostic forwarding",width,ea);
    if(calls) check(last_addr==ea && last_width==width && last_value==truncated,
                    "address width and original value",width,ea);
    bool bytes=true;
    for(unsigned i=0;i<sizeof(memory);++i) {
        const uint8_t expected=i>=ea && i<ea+width
            ?(uint8_t)(truncated>>(8*(width-1-(i-ea)))):0xA5;
        bytes &= memory[i]==expected;
    }
    check(bytes,"big-endian store and untouched surroundings",width,ea);
}
int main() {
    for(unsigned width=1;width<=8;width*=2)
        for(unsigned ea=0x34;ea<=0x4C;++ea) {
            probe(ea,width,false);
            probe(ea,width,true);
            probe(0x100000000ull+ea,width,true);
        }
    printf("Generated VM write watch: %u checks, %u failures\n",checks,failures);
    return failures?1:0;
}
'''.replace("BODY\n", body + "\n")
    clang = shutil.which("clang++") or r"C:\Program Files\LLVM\bin\clang++.exe"
    with tempfile.TemporaryDirectory(prefix="tj-generated-write-watch-") as temp:
        path = Path(temp)
        cpp, exe = path / "watch.cpp", path / "watch.exe"
        cpp.write_text(source, encoding="utf-8")
        subprocess.run([clang, "-std=c++20", "-O2", str(cpp), "-o", str(exe)], check=True)
        result = subprocess.run([str(exe)], check=False)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
