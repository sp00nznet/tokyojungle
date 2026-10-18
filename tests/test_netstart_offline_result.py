"""Native NetStart result, event order, and actual Tokyo Jungle callback regression."""
from pathlib import Path
import mmap
import re
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT.parent / "ps3recomp"


def function(text, signature):
    start = text.index(signature)
    brace = text.index("{", start)
    end, depth = brace + 1, 1
    while depth:
        depth += (text[end] == "{") - (text[end] == "}")
        end += 1
    return text[start:end]


def main():
    network = (RUNTIME / "libs/network/cellNetCtl.c").read_text()
    with (ROOT / "generated/ppu_recomp_000.cpp").open("rb") as file:
        with mmap.mmap(file.fileno(), 0, access=mmap.ACCESS_READ) as mapped:
            marker = b"void func_00160CF4(ppu_context* ctx)"
            start = mapped.find(marker)
            assert start >= 0
            end = mapped.find(b"\nvoid func_", start + len(marker))
            callback = mapped[start:end].decode("latin1")
    callback = "\n".join(line for line in callback.splitlines() if "PPU_TRACE" not in line)
    prefix = r'''
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "cellNetCtl.h"
#include "cellSysutil.h"
#include "ppu_context.h"
static unsigned char arena[0x30000];
static unsigned char* vm_base = arena;
static int passed, failed;
#define CHECK(x) do { if (x) ++passed; else { ++failed; fprintf(stderr, "FAIL line %d: %s\n", __LINE__, #x); } } while (0)
static uint32_t vm_read32(uint32_t a) {
    if (a > sizeof(arena)-4) abort();
    return ((uint32_t)arena[a]<<24)|((uint32_t)arena[a+1]<<16)|((uint32_t)arena[a+2]<<8)|arena[a+3];
}
static uint64_t vm_read64(uint32_t a) { return ((uint64_t)vm_read32(a)<<32)|vm_read32(a+4); }
static void vm_write32(uint32_t a, uint32_t v) {
    if (a > sizeof(arena)-4) abort();
    for (unsigned i=0;i<4;++i) arena[a+i]=(unsigned char)(v>>(24-i*8));
}
static void vm_write64(uint32_t a, uint64_t v) { vm_write32(a,(uint32_t)(v>>32)); vm_write32(a+4,(uint32_t)v); }
static void vm_write8(uint32_t a, uint8_t v) { if(a>=sizeof(arena))abort();arena[a]=v; }
// This callback uses exactly rldicl(value, 0, 32); reject any expanded fixture scope.
static uint64_t ppc_rldicl(uint64_t v, unsigned shift, unsigned mask) {
    if(shift!=0||mask!=32)abort(); return v&UINT32_MAX;
}
static char* option;
static char* test_getenv(const char* key) { if(strcmp(key,"PS3_NET_ONLINE"))abort();return option; }
#define getenv test_getenv
#define GUEST_PTR(p,T) ((T)((p)?(void*)(vm_base+(uint32_t)(uintptr_t)(p)):(void*)0))
struct Event { int slot; u32 status, param; };
static Event events[32];
static unsigned event_count;
void cellSysutilQueueEvent(int slot,u32 status,u32 param) {
    if(event_count>=32)abort(); events[event_count++]={slot,status,param};
}
'''
    native = "\n".join(function(network, name) for name in (
        "static int netctl_online(", "static void netstart_broadcast(",
        "s32 cellNetCtlNetStartDialogLoadAsync(",
        "s32 cellNetCtlNetStartDialogUnloadAsync("))
    bridge = r'''
#define DRAIN_TRAMPOLINE(ctx) ((void)0)
static unsigned unregisters, terms, unloads;
static void func_0025F580(ppu_context* c) {
    c->gpr[3]=(uint64_t)(int64_t)cellNetCtlNetStartDialogUnloadAsync(
        (CellNetCtlNetStartDialogResult*)(uintptr_t)c->gpr[3]);
}
static void func_0025DF00(ppu_context* c) { ++unregisters;c->gpr[3]=0; }
static void func_0025F5A0(ppu_context* c) { ++terms;c->gpr[3]=0; }
static void func_0025DEC0(ppu_context* c) { CHECK(c->gpr[3]==0x14);++unloads;c->gpr[3]=0; }
'''
    exercise = r'''
static void event_group(unsigned offset,u32 status) {
    for(unsigned slot=0;slot<CELL_SYSUTIL_MAX_CALLBACKS;++slot) {
        CHECK(events[offset+slot].slot==(int)slot);
        CHECK(events[offset+slot].status==status);
        CHECK(events[offset+slot].param==0);
    }
}
int main(int argc,char** argv) {
    const bool online=argc>1&&strcmp(argv[1],"absent");
    option=online?argv[1]:nullptr;
    const uint32_t expected=online?0:CELL_NET_CTL_ERROR_NOT_CONNECTED;
    for(unsigned repeat=0;repeat<5;++repeat) {
        memset(arena,0xA5,sizeof(arena)); event_count=0;
        vm_write32(0x100,8);
        CHECK(cellNetCtlNetStartDialogLoadAsync(nullptr)==CELL_OK);
        CHECK(event_count==2*CELL_SYSUTIL_MAX_CALLBACKS);
        event_group(0,CELL_SYSUTIL_NET_CTL_NETSTART_LOADED);
        event_group(CELL_SYSUTIL_MAX_CALLBACKS,CELL_SYSUTIL_NET_CTL_NETSTART_FINISHED);
        CHECK(cellNetCtlNetStartDialogUnloadAsync((CellNetCtlNetStartDialogResult*)0x100)==CELL_OK);
        CHECK(vm_read32(0x100)==8);
        CHECK(vm_read32(0x104)==expected);
        for(unsigned i=0;i<sizeof(arena);++i) {
            if(i>=0x100&&i<0x108)continue;
            if(arena[i]!=0xA5){CHECK(false);break;}
            if(i==sizeof(arena)-1)CHECK(true);
        }
        CHECK(event_count==3*CELL_SYSUTIL_MAX_CALLBACKS);
        event_group(2*CELL_SYSUTIL_MAX_CALLBACKS,CELL_SYSUTIL_NET_CTL_NETSTART_UNLOADED);

        // Original title callback (OPD00349608 -> ELF00160CF4) copies the BE
        // unload result into its object's +14 field, then clears +2008C.
        event_count=0;
        ppu_context c={}; c.gpr[1]=0x3000;c.gpr[3]=0x802;c.gpr[5]=0x8000;
        c.gpr[29]=0xABCDEF;c.gpr[31]=0x123456;c.lr=0x11223344;
        vm_write32(0x28090,1);vm_write8(0x2808C,1);
        func_00160CF4(&c);
        CHECK(vm_read32(0x8014)==expected);
        CHECK(c.gpr[1]==0x3000&&c.gpr[29]==0xABCDEF&&c.gpr[31]==0x123456&&c.lr==0x11223344);
        CHECK(event_count==CELL_SYSUTIL_MAX_CALLBACKS);
        event_group(0,CELL_SYSUTIL_NET_CTL_NETSTART_UNLOADED);
        c.gpr[3]=0x803; c.gpr[5]=0x8000;
        func_00160CF4(&c);
        CHECK(arena[0x2808C]==0);
        CHECK(unregisters==repeat+1&&terms==repeat+1&&unloads==repeat+1);
        CHECK(vm_read32(0x8014)==expected);
    }
    event_count=0;
    CHECK(cellNetCtlNetStartDialogUnloadAsync(nullptr)==CELL_OK);
    CHECK(event_count==CELL_SYSUTIL_MAX_CALLBACKS);
    printf("RESULT mode=%s passed=%d failed=%d\n",argc>1?argv[1]:"absent",passed,failed);
    return failed?1:0;
}
'''
    with tempfile.TemporaryDirectory(prefix="tj-netstart-") as directory:
        temp = Path(directory)
        source = temp / "netstart.cpp"
        source.write_text(prefix + native + bridge + callback + exercise)
        command = [r"C:\Program Files\LLVM\bin\clang-cl.exe", "/nologo", "/std:c++17", "/O2", "/EHsc",
                   "/D_CRT_SECURE_NO_WARNINGS", f"/Fo{temp/'netstart.obj'}", f"/Fe{temp/'netstart.exe'}"]
        command += [f"/I{RUNTIME/path}" for path in ("include", "runtime/ppu", "libs/network", "libs/system")]
        build = subprocess.run(command+[str(source)],capture_output=True,text=True)
        if build.returncode:
            print(build.stdout+build.stderr)
            return build.returncode
        failed = False
        for mode in ("absent", "1", "0", ""):
            run = subprocess.run([str(temp/"netstart.exe"),mode],capture_output=True,text=True)
            print(next(line for line in run.stdout.splitlines() if line.startswith("RESULT")))
            if run.returncode:
                print(run.stderr[:1400]); failed=True
        return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
