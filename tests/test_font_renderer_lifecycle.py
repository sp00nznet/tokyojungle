"""Exercise the title's guest renderer ABI against the production font library."""
from pathlib import Path
import os
import shutil
import subprocess
import sys
import tempfile

from test_fifo_present_boundary import function

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT.parent / "ps3recomp"


def main():
    text = (ROOT / "src/main.cpp").read_text(encoding="utf-8")
    create = function(text, "static void tj_cellFontCreateRenderer(")
    bind = function(text, "static void tj_cellFontBindRenderer(")
    read = function(text, "static bool tj_read_cell_font(")
    font_id_code = function(text, "static void tj_cellFontGetFontIdCode(")
    signature = "static void tj_cellFontDestroyRenderer("
    if signature in text:
        destroy = function(text, signature)
        assert '"cellFontDestroyRenderer", tj_cellFontDestroyRenderer)' in text
    else:
        # Existing generic registration passes r3 directly as the native handle.
        destroy = """
static void tj_cellFontDestroyRenderer(ppu_context* ctx) {
    ctx->gpr[3] = (uint64_t)(int64_t)cellFontDestroyRenderer((uint32_t)ctx->gpr[3]);
}
"""
    source = r'''
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <initializer_list>
#include "cellFont.h"
static uint8_t memory[4096];
static uint32_t ppu_vm_size=sizeof(memory);
static unsigned invalid_accesses;
static uint32_t vm_read32(uint32_t ea) {
    if (uint64_t(ea)+4>sizeof(memory)) { ++invalid_accesses; return UINT32_MAX; }
    return uint32_t(memory[ea])<<24|uint32_t(memory[ea+1])<<16|
           uint32_t(memory[ea+2])<<8|memory[ea+3];
}
static void vm_write32(uint32_t ea,uint32_t value) {
    if (uint64_t(ea)+4>sizeof(memory)) { ++invalid_accesses; return; }
    for(unsigned i=0;i<4;++i) memory[ea+i]=uint8_t(value>>(24-8*i));
}
struct ppu_context { uint64_t gpr[32]={}; };
CREATE
DESTROY
READ
BIND
FONT_ID_CODE
static unsigned checks, failures;
static void check(bool ok,const char* why) {
    ++checks; if(!ok) { ++failures; fprintf(stderr,"FAIL: %s\n",why); }
}
static int32_t create(uint32_t ea) {
    ppu_context c; c.gpr[3]=0x100; c.gpr[4]=0x180; c.gpr[5]=ea;
    tj_cellFontCreateRenderer(&c); return int32_t(c.gpr[3]);
}
static int32_t destroy(uint32_t ea) {
    ppu_context c; c.gpr[3]=ea;
    tj_cellFontDestroyRenderer(&c); return int32_t(c.gpr[3]);
}
static int32_t font_id_code(uint32_t font_ea,uint32_t id_ea,uint32_t code_ea) {
    ppu_context c;c.gpr[3]=font_ea;c.gpr[4]='A';c.gpr[5]=id_ea;c.gpr[6]=code_ea;
    tj_cellFontGetFontIdCode(&c);return int32_t(c.gpr[3]);
}
static void reject_font_id_code(uint32_t font_ea,uint32_t id_ea,uint32_t code_ea) {
    if (id_ea && uint64_t(id_ea)+4<=sizeof(memory)) vm_write32(id_ea,0xA1B2C3D4);
    if (code_ea && uint64_t(code_ea)+4<=sizeof(memory)) vm_write32(code_ea,0x12345678);
    uint8_t before[sizeof(memory)];memcpy(before,memory,sizeof(memory));
    const unsigned accesses=invalid_accesses;
    check(font_id_code(font_ea,id_ea,code_ea)!=CELL_OK,"invalid font ID/code request accepted");
    check(!memcmp(before,memory,sizeof(memory)),"invalid font ID/code request changed guest memory");
    check(invalid_accesses==accesses,"invalid font ID/code request accessed outside VM");
}
int main(int argc,char** argv) {
    if(argc!=2) return 2;
    check(cellFontInit(nullptr)==CELL_OK,"font init");
    // Leave another renderer live while recycling one guest-owned object.
    check(create(0x200)==CELL_OK,"create persistent renderer");
    const uint32_t live=vm_read32(0x200);
    CellFont font={};
    check(cellFontOpenFontFile(&font,argv[1],0,0)==CELL_OK,"open fixture font for binding");
    vm_write32(0x700,font.handle);vm_write32(0x704,font.type);
    uint32_t expected_id=UINT32_MAX,expected_code=UINT32_MAX;
    check(cellFontGetFontIdCode(&font,'A',&expected_id,&expected_code)==CELL_OK,
          "native font ID/code reference");
    check(expected_id==font.type && expected_code=='A',"native font ID/code mapping");
    vm_write32(0,0xD4C3B2A1);
    memset(memory+0x8fc,0xA5,12);
    uint8_t before_optional[sizeof(memory)];memcpy(before_optional,memory,sizeof(memory));
    check(font_id_code(0x700,0x900,0)==CELL_OK,"title null fontCode output rejected");
    check(vm_read32(0x900)==expected_id && memory[0x900]==uint8_t(expected_id>>24) &&
          memory[0x901]==uint8_t(expected_id>>16) && memory[0x902]==uint8_t(expected_id>>8) &&
          memory[0x903]==uint8_t(expected_id),"optional call did not write BE font ID");
    memcpy(before_optional+0x900,memory+0x900,4);
    check(!memcmp(before_optional,memory,sizeof(memory)),"optional fontCode changed address zero or sentinels");
    uint8_t before_both[sizeof(memory)];memcpy(before_both,memory,sizeof(memory));
    check(font_id_code(0x700,0x900,0x910)==CELL_OK,"both font outputs rejected");
    check(vm_read32(0x900)==expected_id && vm_read32(0x910)==expected_code,
          "both font outputs did not preserve expected BE values");
    memcpy(before_both+0x900,memory+0x900,4);memcpy(before_both+0x910,memory+0x910,4);
    check(!memcmp(before_both,memory,sizeof(memory)),"both outputs changed unrelated guest memory");
    check(cellFontSetScalePixel(&font,24,24)==CELL_OK,"same font scale for glyph metrics");
    CellFontGlyphMetrics metrics={};
    check(cellFontGetCharGlyphMetrics(&font,expected_code,&metrics)==CELL_OK && metrics.h_advance>0,
          "same font glyph advance is unreachable or zero");
    reject_font_id_code(0x700,0,0x910);
    reject_font_id_code(0,0x900,0x910);
    for(uint32_t bad : {uint32_t(sizeof(memory)-2),0xfffffffeu}) {
        reject_font_id_code(0x700,bad,0x910);
        reject_font_id_code(0x700,0x900,bad);
    }
    for(uint32_t bad : {uint32_t(sizeof(memory)-14),0xfffffff2u})
        reject_font_id_code(bad,0x900,0x910);
    ppu_vm_size=0x902;
    reject_font_id_code(0x700,0x900,0);
    ppu_vm_size=0;
    reject_font_id_code(0x700,0xfffffffeu,0);
    reject_font_id_code(0x700,0x900,0xfffffffeu);
    reject_font_id_code(0xfffffff2u,0x900,0);
    check(font_id_code(0x700,0x900,0)==CELL_OK,"4-GiB VM convention rejected valid optional output");
    ppu_vm_size=sizeof(memory);
    vm_write32(0x720,CELL_FONT_MAX_OPENED);vm_write32(0x724,0);
    reject_font_id_code(0x720,0x900,0x910);
    const uint32_t invalid[]={uint32_t(sizeof(memory)-2),0xfffffffeu};
    for(uint32_t bad : invalid) {
        uint8_t before[sizeof(memory)];memcpy(before,memory,sizeof(memory));
        check(create(bad)!=CELL_OK && destroy(bad)!=CELL_OK,"renderer range rejected");
        ppu_context c;c.gpr[3]=0x700;c.gpr[4]=bad;tj_cellFontBindRenderer(&c);
        check(int32_t(c.gpr[3])!=CELL_OK,"bind renderer range rejected");
        c.gpr[3]=bad;c.gpr[4]=0x200;tj_cellFontBindRenderer(&c);
        check(int32_t(c.gpr[3])!=CELL_OK,"bind font range rejected");
        check(!memcmp(before,memory,sizeof(memory)),"invalid renderer request changed guest memory");
    }
    ppu_vm_size=0;
    check(create(0xfffffffeu)!=CELL_OK && destroy(0xfffffffeu)!=CELL_OK,
          "4-GiB VM convention rejects wrapped renderer range");
    ppu_vm_size=sizeof(memory);
    ppu_context bound;bound.gpr[3]=0x700;bound.gpr[4]=0x200;
    tj_cellFontBindRenderer(&bound);
    check(int32_t(bound.gpr[3])==CELL_OK,"valid renderer binding preserved");
    check(cellFontUnbindRenderer(&font)==CELL_OK,"unbind before renderer cleanup");
    for(unsigned i=0;i<32;++i) {
        vm_write32(0x2fc,0xA1B2C3D4); vm_write32(0x304,0x12345678);
        check(create(0x300)==CELL_OK,"renderer slots exhausted after destroy");
        check(vm_read32(0x300)!=live,"reused a live renderer slot");
        check(destroy(0x300)==CELL_OK,"guest renderer pointer rejected");
        check(vm_read32(0x300)==UINT32_MAX,"destroyed guest handle remained valid");
        check(vm_read32(0x2fc)==0xA1B2C3D4 && vm_read32(0x304)==0x12345678,
              "destroy changed adjacent guest memory");
    }
    check(create(0x400)==CELL_OK,"create after repeated recycling");
    const uint32_t reused=vm_read32(0x400);
    check(destroy(0x300)!=CELL_OK,"stale renderer destroyed a reused slot");
    check(vm_read32(0x400)==reused && vm_read32(0x200)==live,
          "invalid destroy changed a live guest object");
    check(destroy(0)!=CELL_OK,"null renderer accepted");
    vm_write32(0x500,CELL_FONT_MAX_RENDERERS);
    check(destroy(0x500)!=CELL_OK && vm_read32(0x500)==CELL_FONT_MAX_RENDERERS,
          "invalid handle accepted or overwritten");
    check(destroy(0x400)==CELL_OK && destroy(0x200)==CELL_OK,"live cleanup failed");
    check(cellFontEnd()==CELL_OK,"font end");
    reject_font_id_code(0x700,0x900,0x910);
    printf("font renderer lifecycle: %u checks, %u failures\n",checks,failures);
    return failures ? 1 : 0;
}
'''.replace("CREATE\n", create+"\n").replace("DESTROY\n", destroy+"\n")
    source=source.replace("READ\n",read+"\n").replace("BIND\n",bind+"\n")
    source=source.replace("FONT_ID_CODE\n",font_id_code+"\n")
    cc = shutil.which("clang") or shutil.which("gcc") or r"C:\Program Files\LLVM\bin\clang.exe"
    cxx = shutil.which("clang++") or shutil.which("g++") or r"C:\Program Files\LLVM\bin\clang++.exe"
    fonts=[Path(os.environ.get("WINDIR", "C:/Windows"))/"Fonts/segoeui.ttf",
           Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")]
    font=next((p for p in fonts if p.is_file()),None)
    if font is None:
        raise RuntimeError("An installed Segoe UI or DejaVu Sans fixture font is required")
    with tempfile.TemporaryDirectory(prefix="tj-font-renderer-") as temp:
        cpp, obj, exe = (Path(temp) / name for name in ("test.cpp", "font.o", "test.exe"))
        cpp.write_text(source, encoding="utf-8")
        includes = ["-D_CRT_SECURE_NO_WARNINGS", "-I", str(RUNTIME / "include"),
                    "-I", str(RUNTIME / "libs/font")]
        if sys.platform != "win32":
            # The Windows font-path scratch buffer is unrelated to this fixture.
            includes.append("-DMAX_PATH=260")
        subprocess.run([cc, "-std=c11", *includes, "-c",
                        str(RUNTIME / "libs/font/cellFont.c"), "-o", str(obj)], check=True)
        subprocess.run([cxx, "-std=c++17", *includes, str(cpp), str(obj),
                        "-o", str(exe)], check=True)
        subprocess.run([str(exe),str(font)], check=True)


if __name__ == "__main__":
    main()
