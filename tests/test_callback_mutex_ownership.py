"""Exercise the production callback policy against real kernel mutex syscalls.

Host-only. The callback reproduces the game's unchecked lock/read/unlock
sequence; running it inside an existing non-recursive lock steals ownership.
"""
from pathlib import Path
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT.parent / "ps3recomp"


def main():
    loader = (RUNTIME / "runtime/ppu/ppu_loader.cpp").read_text(
        encoding="utf-8", errors="surrogateescape"
    )
    start = loader.index("/* Display callback safe point shared")
    policy = loader[start:loader.index("/* lv2 syscall dispatch:", start)]
    driver = r'''
#include <windows.h>
#include <atomic>
#include <cstdint>
#include <cstdio>
#include <thread>
#include "sys_mutex.h"
extern "C" {
uint8_t* vm_base = nullptr;
uint64_t ppu_vm_size = 0;
int64_t lv2_usec_deadline(uint64_t us) { return (int64_t)(GetTickCount64()*1000+us); }
int lv2_deadline_passed(int64_t end) { return (int64_t)(GetTickCount64()*1000)>=end; }
}
POLICY
static unsigned checks=0,failures=0,callbacks=0;
static int64_t callback_lock=0,callback_unlock=0;
static ppu_context main_ctx{};
#define CHECK(test,message) do { ++checks; if(!(test)) { ++failures; std::fprintf(stderr,"FAIL: %s\n",message); } } while(0)
static int64_t call(int64_t (*fn)(ppu_context*),ppu_context& ctx,unsigned id) {
    ctx.gpr[3]=id; ctx.gpr[4]=0; return fn(&ctx);
}
extern "C" void ppu_dispatch_display_callbacks(void) {
    PpuCallbackScope scope;
    ppu_context cb{}; cb.thread_id=1;
    ++callbacks;
    callback_lock=call(sys_mutex_lock,cb,1);
    // Actual guest func_001818EC ignores lock failure and still unlocks.
    callback_unlock=call(sys_mutex_unlock,cb,1);
}
static void reset_mutex(unsigned id,bool recursive=false) {
    auto& m=g_sys_mutexes[id-1]; m={}; m.active=1; m.recursive=recursive;
    InitializeCriticalSection(&m.cs);
}
static void finish_mutex(unsigned id) {
    auto& m=g_sys_mutexes[id-1];
    if(m.owner_tid==1 && m.lock_count>0)
        while(m.lock_count>0) call(sys_mutex_unlock,main_ctx,id);
    call(sys_mutex_destroy,main_ctx,id);
}
int main() {
    main_ctx.thread_id=1;
    reset_mutex(1);
    CHECK(call(sys_mutex_lock,main_ctx,1)==CELL_OK,"main lock failed");
    ppu_poll_display_callbacks(&main_ctx);
    CHECK(callbacks==0,"callback ran while main owned the movie mutex");
    CHECK(g_sys_mutexes[0].owner_tid==1 && g_sys_mutexes[0].lock_count==1,
          "callback released the main thread's movie mutex");
    if(failures) {
        std::fprintf(stderr,"observed callback lock=0x%08X unlock=0x%08X\n",
                     (unsigned)callback_lock,(unsigned)callback_unlock);
        finish_mutex(1);
        return 1;
    }
    CHECK(call(sys_mutex_destroy,main_ctx,1)==(int32_t)CELL_EBUSY,
          "destroy erased a held mutex and its callback-deferral lifetime");
    CHECK(g_sys_mutexes[0].active && g_sys_mutexes[0].lock_count==1,
          "busy destroy altered held ownership");
    CHECK(call(sys_mutex_unlock,main_ctx,1)==CELL_OK,"main unlock failed");
    ppu_poll_display_callbacks(&main_ctx);
    CHECK(callbacks==1 && callback_lock==CELL_OK && callback_unlock==CELL_OK,
          "callback did not run safely after main unlock");
    reset_mutex(2,true);
    call(sys_mutex_lock,main_ctx,1); call(sys_mutex_lock,main_ctx,2);
    call(sys_mutex_trylock,main_ctx,2);
    ppu_poll_display_callbacks(&main_ctx);
    CHECK(callbacks==1,"nested/recursive locks did not defer callback");
    call(sys_mutex_unlock,main_ctx,2); call(sys_mutex_unlock,main_ctx,1);
    ppu_poll_display_callbacks(&main_ctx);
    CHECK(callbacks==1,"partial recursive unlock enabled callback");
    call(sys_mutex_unlock,main_ctx,2); ppu_poll_display_callbacks(&main_ctx);
    CHECK(callbacks==2,"final recursive unlock did not enable callback");
    CHECK(call(sys_mutex_trylock,main_ctx,1)==CELL_OK,"trylock failed");
    ppu_context other_ctx{}; other_ctx.thread_id=2;
    CHECK(call(sys_mutex_unlock,other_ctx,1)==(int32_t)CELL_EPERM,"non-owner unlock changed contract");
    CHECK(call(sys_mutex_lock,main_ctx,1)==(int32_t)CELL_EDEADLK,"non-recursive lock changed contract");
    ppu_poll_display_callbacks(&main_ctx);
    CHECK(callbacks==2,"failed lock/unlock changed callback deferral");
    call(sys_mutex_unlock,main_ctx,1); ppu_poll_display_callbacks(&main_ctx);
    CHECK(callbacks==3,"trylock ownership count was not released");
    CHECK(call(sys_mutex_lock,main_ctx,0)==(int32_t)CELL_ESRCH,"invalid lock changed contract");
    ppu_poll_display_callbacks(&main_ctx);
    CHECK(callbacks==4,"invalid lock leaked callback deferral");
    HANDLE entered=CreateEventW(nullptr,TRUE,FALSE,nullptr);
    HANDLE release=CreateEventW(nullptr,TRUE,FALSE,nullptr);
    std::thread worker([&] {
        ppu_context ctx{}; ctx.thread_id=2;
        call(sys_mutex_lock,ctx,2); SetEvent(entered);
        WaitForSingleObject(release,2000); call(sys_mutex_unlock,ctx,2);
    });
    CHECK(WaitForSingleObject(entered,1000)==WAIT_OBJECT_0,"worker did not acquire its mutex");
    ppu_poll_display_callbacks(&main_ctx);
    CHECK(callbacks==5,"another host thread's mutex blocked main callbacks");
    SetEvent(release); worker.join(); CloseHandle(entered); CloseHandle(release);
    finish_mutex(2); finish_mutex(1);
    std::printf("callback mutex ownership: %u/%u checks passed\n",checks-failures,checks);
    return failures?1:0;
}
'''.replace("POLICY", policy)
    clang = shutil.which("clang") or "C:/Program Files/LLVM/bin/clang.exe"
    clangpp = shutil.which("clang++") or "C:/Program Files/LLVM/bin/clang++.exe"
    with tempfile.TemporaryDirectory(prefix="tj-callback-mutex-") as temp:
        temp = Path(temp)
        cpp, obj, exe = (temp / n for n in ("test.cpp", "mutex.obj", "test.exe"))
        cpp.write_text(driver, encoding="utf-8")
        subprocess.run([clang,"-std=c11","-O2","-D_CRT_SECURE_NO_WARNINGS",
                        "-I"+str(RUNTIME / "include"),"-c",
                        str(RUNTIME / "runtime/syscalls/sys_mutex.c"),"-o",str(obj)],check=True)
        subprocess.run([clangpp,"-std=c++17","-O2",str(cpp),str(obj),
                        "-I"+str(RUNTIME / "include"),
                        "-I"+str(RUNTIME / "runtime/syscalls"),"-o",str(exe)],check=True)
        subprocess.run([str(exe)],check=True,timeout=10)


if __name__ == "__main__":
    main()
