"""Compile actual title bridge and native font renderer against a bounded VM."""
from pathlib import Path
import shutil
import subprocess
import tempfile

from test_fifo_present_boundary import function

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT.parent / "ps3recomp"


def main():
    main_cpp = (ROOT / "src/main.cpp").read_text(encoding="utf-8")
    prefix = main_cpp[main_cpp.index("struct TjFontGlyphCache "):
                      main_cpp.index("static void tj_cellAudioOutGetDeviceInfo(")]
    bridge = function(main_cpp, "static void tj_cellFontRenderCharGlyphImage(")
    close = function(main_cpp, "static void tj_cellFontCloseFont(")
    end = function(main_cpp, "static void tj_cellFontEnd(")
    source = r'''
#include <array>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <mutex>
#include "cellFont.h"
static uint8_t storage[1024*1024],*vm_base=storage;
static uint32_t heap=0x80000,allocations,frees;
static uint32_t vm_read32(uint32_t ea) {
    return uint32_t(storage[ea])<<24|uint32_t(storage[ea+1])<<16|
           uint32_t(storage[ea+2])<<8|storage[ea+3];
}
static void vm_write32(uint32_t ea,uint32_t v) {
    for(int i=0;i<4;++i) storage[ea+i]=uint8_t(v>>(24-8*i));
}
static uint32_t hle_malloc(uint32_t size) {
    if(size>sizeof(storage)-heap) return 0;
    uint32_t ea=heap; heap+=(size+15)&~15u; ++allocations; return ea;
}
static void hle_free(uint32_t ea) { if(ea) ++frees; }
struct ppu_context { uint64_t gpr[32]={}; double fpr[32]={}; };
PREFIX
BRIDGE
CLOSE
END
#define CHECK(c,label) do { if(!(c)) { fprintf(stderr,"FAIL: %s\n",label); return 1; } } while(0)
int main() {
    CHECK(cellFontInit(nullptr)==CELL_OK,"font init");
    CellFontType type={0x201u,0}; CellFont font={};
    CHECK(cellFontOpenFontset(0,&type,&font)==CELL_OK,"font open");
    CHECK(cellFontSetScalePixel(&font,24,24)==CELL_OK,"font scale");
    tj_write_cell_font(0x100,&font);
    const uint32_t surface=0x200,buffer=0x1000,transfer=0x300;
    vm_write32(surface,buffer); vm_write32(surface+4,64*4);
    vm_write32(surface+8,4); vm_write32(surface+12,64); vm_write32(surface+16,64);
    vm_write32(surface+20,0); vm_write32(surface+24,0);
    vm_write32(surface+28,64); vm_write32(surface+32,64);
    memset(storage+buffer,0x33,64*64*4);
    ppu_context ctx;
    ctx.gpr[3]=0x100; ctx.gpr[4]='A'; ctx.gpr[5]=surface;
    ctx.gpr[8]=0x400; ctx.gpr[9]=transfer; ctx.fpr[1]=10; ctx.fpr[2]=0;
    tj_cellFontRenderCharGlyphImage(&ctx);
    CHECK(ctx.gpr[3]==CELL_OK,"bridge render");
    uint32_t alpha=vm_read32(transfer),pitch=vm_read32(transfer+4);
    uint32_t w=vm_read32(transfer+8),h=vm_read32(transfer+12),dst=vm_read32(transfer+16);
    CHECK(alpha>=0x80000 && w && h && pitch>=w,"invalid guest alpha pointer or shape");
    CHECK(dst>=buffer && dst+((h-1)*256+w*4)<=buffer+64*64*4,"destination outside surface");
    CHECK(vm_read32(transfer+20)==256,"wrong destination stride");
    unsigned nonzero=0;
    for(uint32_t y=0;y<h;++y) for(uint32_t x=0;x<w;++x) nonzero+=storage[alpha+y*pitch+x]!=0;
    CHECK(nonzero>0,"guest received no alpha coverage");
    for(uint32_t i=0;i<64*64*4;++i) CHECK(storage[buffer+i]==0x33,"bridge overwrote caller color atlas");
    uint32_t left=(dst-buffer)%256/4,top=(dst-buffer)/256;
    CHECK(w>2 && h>2,"glyph too small for clipping fixture");
    vm_write32(surface+20,left+1); vm_write32(surface+24,top+1);
    vm_write32(surface+28,left+w-1); vm_write32(surface+32,top+h-1);
    ctx.gpr[3]=0x100;
    tj_cellFontRenderCharGlyphImage(&ctx);
    CHECK(ctx.gpr[3]==CELL_OK && vm_read32(transfer)==alpha+pitch+1,
          "alpha pointer did not follow scissor crop");
    CHECK(vm_read32(transfer+8)==w-2 && vm_read32(transfer+12)==h-2 &&
          vm_read32(transfer+16)==dst+256+4,"source and destination crops disagree");
    CHECK(allocations==1,"repeated glyph grew guest allocations");
    /* Reproduce the game's actual caller-owned source-alpha blend. */
    alpha=vm_read32(transfer); dst=vm_read32(transfer+16);
    w=vm_read32(transfer+8); h=vm_read32(transfer+12);
    nonzero=0;
    for(uint32_t y=0;y<h;++y) for(uint32_t x=0;x<w;++x) {
        uint8_t a=storage[alpha+y*pitch+x];
        uint32_t value=(255u*a+0x33u*(255u-a))/255u;
        storage[dst+y*256+x*4]=uint8_t(value); nonzero+=value!=0x33;
    }
    CHECK(nonzero>0,"title-style alpha transfer painted no glyph");
    ctx.gpr[3]=0x100; ctx.gpr[4]=' ';
    tj_cellFontRenderCharGlyphImage(&ctx);
    CHECK(ctx.gpr[3]==CELL_OK && vm_read32(transfer)==0 && vm_read32(transfer+12)==0,
          "space retained a stale glyph transfer");
    ctx.gpr[3]=0x100; tj_cellFontCloseFont(&ctx);
    CHECK(ctx.gpr[3]==CELL_OK && frees==1,"font close leaked guest glyph buffer");
    tj_cellFontEnd(&ctx);
    CHECK(ctx.gpr[3]==CELL_OK && frees==1,"font end freed glyph cache twice");
    puts("PASS: actual guest font bridge, live alpha, crop, color blend, cache and close");
}
'''.replace("PREFIX\n", prefix).replace("BRIDGE\n", bridge+"\n")
    source = source.replace("CLOSE\n", close+"\n").replace("END\n", end+"\n")
    clang = shutil.which("clang") or r"C:\Program Files\LLVM\bin\clang.exe"
    clangxx = shutil.which("clang++") or r"C:\Program Files\LLVM\bin\clang++.exe"
    with tempfile.TemporaryDirectory(prefix="tj-font-transfer-") as temp:
        cpp = Path(temp) / "test.cpp"
        cpp.write_text(source, encoding="utf-8")
        obj = Path(temp) / "font.obj"
        include = ["-D_CRT_SECURE_NO_WARNINGS", "-I", str(RUNTIME / "include"), "-I", str(RUNTIME / "libs/font")]
        subprocess.run([clang, "-std=c11", *include, "-c", str(RUNTIME / "libs/font/cellFont.c"), "-o", str(obj)], check=True)
        exe = Path(temp) / "test.exe"
        subprocess.run([clangxx, "-std=c++17", *include, str(cpp), str(obj), "-o", str(exe)], check=True)
        subprocess.run([str(exe)], check=True)


if __name__ == "__main__":
    main()
