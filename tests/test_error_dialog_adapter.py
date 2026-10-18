"""Compile actual title adapter, dialog queue and original error callback."""
from pathlib import Path
import mmap
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
    adapter = function((ROOT / "src/main.cpp").read_text(),
                       "static void tj_cellMsgDialogOpenErrorCode(")
    with (ROOT / "generated/ppu_recomp_000.cpp").open("rb") as file:
        with mmap.mmap(file.fileno(), 0, access=mmap.ACCESS_READ) as mapped:
            start = mapped.find(b"void func_0015F20C(ppu_context* ctx)")
            assert start >= 0
            end = mapped.find(b"\nvoid func_", start + 20)
            callback = mapped[start:end].decode("latin1")
    prefix = r'''
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <windows.h>
#include "ppu_memory.h"
#include "cellMsgDialog.h"
#include "cellSysutil.h"
#include "ps3emu/guest_call.h"
static uint8_t arena[0x30000];
uint8_t* vm_base = arena;
int g_resv_store_active = 0;
void ppu_resv_break_store(uint64_t,uint32_t) { abort(); }
ps3_guest_caller_fn g_ps3_guest_caller = nullptr;
static int passed,failed,calls;
static DWORD dispatch_thread;
#define CHECK(x) do { if(x)++passed;else{++failed;fprintf(stderr,"FAIL line %d: %s\n",__LINE__,#x);} }while(0)
'''
    exercise = r'''
static void guest(uint32_t opd,uint64_t a0,uint64_t a1,uint64_t a2,uint64_t a3,
                  uint64_t a4,uint64_t a5,uint64_t a6,uint64_t a7) {
    ++calls;
    CHECK(GetCurrentThreadId()==dispatch_thread);
    CHECK(opd==0x349518&&a0==CELL_MSGDIALOG_BUTTON_OK&&a1==0x8000);
    CHECK(!(a2|a3|a4|a5|a6|a7));
    if(opd==0x349518&&a1==0x8000) {
        ppu_context c={};c.gpr[3]=a0;c.gpr[4]=a1;
        func_0015F20C(&c);
    }
}
static int64_t open_error(bool callback=true) {
    ppu_context c={};
    c.gpr[3]=0x80130108;
    c.gpr[4]=callback?0xCAFE000000349518ull:0;
    c.gpr[5]=0xDEADBEEF00008000ull;
    c.gpr[6]=0;
    tj_cellMsgDialogOpenErrorCode(&c);
    return (int64_t)c.gpr[3];
}
int main() {
    dispatch_thread=GetCurrentThreadId();g_ps3_guest_caller=guest;
    arena[0x2808C]=1;
    CHECK(open_error()==CELL_OK);
    CHECK(calls==0&&arena[0x2808C]==1);
    cellMsgDialogDispatchPending();
    CHECK(calls==1&&arena[0x2808C]==0);
    cellMsgDialogDispatchPending();CHECK(calls==1);

    // Queue capacity must reject cleanly and propagate signed BUSY to the guest.
    for(int i=0;i<31;++i)CHECK(open_error()==CELL_OK);
    CHECK(open_error()==(int64_t)(int32_t)CELL_SYSUTIL_ERROR_BUSY);
    CHECK(calls==1);
    cellMsgDialogDispatchPending();CHECK(calls==32);
    CHECK(open_error()==CELL_OK);
    g_ps3_guest_caller=nullptr;
    cellMsgDialogDispatchPending();CHECK(calls==32);
    g_ps3_guest_caller=guest;
    cellMsgDialogDispatchPending();CHECK(calls==33);

    CHECK(open_error(false)==CELL_OK);
    cellMsgDialogDispatchPending();CHECK(calls==33);
    CHECK(cellMsgDialogOpen2(CELL_MSGDIALOG_TYPE_PROGRESSBAR_SINGLE,
                            nullptr,nullptr,nullptr,nullptr)==CELL_OK);
    CHECK(open_error()==(int64_t)(int32_t)CELL_SYSUTIL_ERROR_BUSY);
    CHECK(cellMsgDialogAbort()==CELL_OK);
    CHECK(open_error()==CELL_OK);
    cellMsgDialogDispatchPending();CHECK(calls==34);
    printf("RESULT passed=%d failed=%d callbacks=%d\n",passed,failed,calls);
    return failed?1:0;
}
'''
    with tempfile.TemporaryDirectory(prefix="tj-error-dialog-") as directory:
        temp = Path(directory)
        source = temp / "adapter.cpp"
        source.write_text(prefix + callback + adapter + exercise)
        compiler = r"C:\Program Files\LLVM\bin\clang-cl.exe"
        includes = [f"/I{RUNTIME/path}" for path in ("include", "runtime/ppu", "libs/system")]
        common = [compiler,"/nologo","/O2","/D_CRT_SECURE_NO_WARNINGS"]+includes
        commands = [common+["/TC","/c",str(RUNTIME/"libs/system/cellMsgDialog.c"),f"/Fo{temp/'dialog.obj'}"],
                    common+["/std:c++17","/EHsc","/c",str(source),f"/Fo{temp/'adapter.obj'}"],
                    [compiler,"/nologo",str(temp/"adapter.obj"),str(temp/"dialog.obj"),
                     f"/Fe{temp/'adapter.exe'}"]]
        for command in commands:
            build = subprocess.run(command,capture_output=True,text=True)
            if build.returncode:
                print(build.stdout+build.stderr);return build.returncode
        run = subprocess.run([str(temp/"adapter.exe")],capture_output=True,text=True)
        print(next(line for line in run.stdout.splitlines() if line.startswith("RESULT")))
        if run.returncode: print(run.stderr[:800])
        return run.returncode


if __name__ == "__main__":
    raise SystemExit(main())
