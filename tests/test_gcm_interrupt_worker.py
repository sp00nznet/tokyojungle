"""Run the real interrupt worker and GCM pump while the main thread owns a lock.

Pre-repair cooperative dispatch deadlocks in the first scenario. The bounded
child is host-only: no window, game, guest asset or save is opened.
"""
from pathlib import Path
import shutil
import subprocess
import tempfile

from test_fifo_present_boundary import function

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT.parent / "ps3recomp"


def main():
    source = (RUNTIME / "libs/video/cellGcmSys.c").read_text(encoding="utf-8")
    queue = function(source, "static void cellGcmQueueUserInterrupt(u32 cause, u32 committed_get)")
    assert queue.rfind("gcm_wake_interrupt_dispatcher();") > queue.rfind("gcm_user_interrupt_unlock();")
    pending_set = [line for line in source.splitlines() if line.startswith("#define GCM_PENDING_SET(bits)")]
    assert len(pending_set) == 2 and all("gcm_wake_interrupt_dispatcher();" in line for line in pending_set)
    glue = (ROOT / "src/dispatch_glue.cpp").read_text(encoding="utf-8")
    assert "s_cb_thread_id = 0x7FFF0000ull + slot;" in glue
    assert "saved_active ? saved_active->thread_id : s_cb_thread_id;" in glue
    present = (ROOT / "src/tj_present.cpp").read_text(encoding="utf-8")
    assert 'if (tj_gcm_interrupt_worker_enabled() && !tj_start_gcm_interrupt_thread())' in present
    policy = function(present, "static int tj_gcm_interrupt_worker_enabled(void)")
    signatures = [
        "static void gcm_wake_interrupt_dispatcher(void)",
        "void cellGcmSetHostInterruptDispatcher(u32 host_thread_id, CellGcmInterruptWake wake)",
        "static int gcm_interrupt_dispatch_allowed(void)",
    ]
    if signatures[0] in source:
        hooks = "\n".join(function(source, signature) for signature in signatures)
    else:
        # Old source has no ownership guard. Supply only worker registration
        # so the production old pump demonstrates unsafe main-thread re-entry.
        hooks = r'''
static void gcm_wake_interrupt_dispatcher(void) {
    CellGcmInterruptWake wake=atomic_load(&s_gcm_interrupt_wake); if(wake) wake();
}
void cellGcmSetHostInterruptDispatcher(u32 tid,CellGcmInterruptWake wake) {
    atomic_store(&s_gcm_interrupt_host_tid,tid); atomic_store(&s_gcm_interrupt_wake,wake);
}
'''
    pump = function(source, "void ppu_gcm_pump(void)").replace(
        "void ppu_gcm_pump(void)", "static void production_pump(void)", 1
    )
    runtime = r'''
#include <windows.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
typedef uint32_t u32; typedef uint64_t u64;
typedef void (*CellGcmInterruptWake)(void);
typedef struct { u32 generation,opd,code,cause,sequence; } GcmUserInterrupt;
static atomic_uint s_gcm_interrupt_host_tid;
static _Atomic(CellGcmInterruptWake) s_gcm_interrupt_wake;
static volatile LONG s_gcm_pending;
static atomic_uint s_flip_callbacks_pending,s_user_interrupt_generation;
static u32 s_flip_handler_opd=0x100,s_flip_handler_code=0x104;
static u32 s_vblank_handler_opd=0x200,s_vblank_handler_code=0x204;
static u64 s_vblank_count;
static int s_tj_survival_flip_diag;
static HANDLE pump_idle;
static void tj_flip_trace(const char* s,u32 x) { (void)s; (void)x; }
static int gcm_user_interrupt_take(GcmUserInterrupt* e) { (void)e; return 0; }
static void ydkj_restore_handler_opd(u32 opd,u32 code) { (void)opd; (void)code; }
extern void fixture_callback(void);
extern void fixture_vblank_callback(void);
static void guest_call(u32 opd,u64 a,u64 b,u64 c,u64 d,u64 e,u64 f,u64 g,u64 h) {
    (void)opd;(void)a;(void)b;(void)c;(void)d;(void)e;(void)f;(void)g;(void)h;
    if(opd==s_vblank_handler_opd) fixture_vblank_callback();
    else fixture_callback();
}
static void (*g_ps3_guest_caller)(u32,u64,u64,u64,u64,u64,u64,u64,u64)=guest_call;
HOOKS
#define GCM_PENDING_TAKE() InterlockedExchange(&s_gcm_pending,0)
PUMP
void ppu_gcm_pump(void) { production_pump(); if(pump_idle) SetEvent(pump_idle); }
void fixture_set_idle(HANDLE h) { pump_idle=h; }
void fixture_queue(unsigned n,int wake) {
    atomic_fetch_add(&s_flip_callbacks_pending,n); InterlockedOr(&s_gcm_pending,2);
    if(wake) gcm_wake_interrupt_dispatcher();
}
void fixture_wake(void) { gcm_wake_interrupt_dispatcher(); }
void fixture_queue_vblank(void) {
    ++s_vblank_count; InterlockedOr(&s_gcm_pending,1); gcm_wake_interrupt_dispatcher();
}
u32 fixture_owner(void) { return atomic_load(&s_gcm_interrupt_host_tid); }
CellGcmInterruptWake fixture_wake_hook(void) { return atomic_load(&s_gcm_interrupt_wake); }
'''.replace("HOOKS", hooks).replace("PUMP", pump)
    driver = r'''
#include <windows.h>
#include <atomic>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
WORKER_POLICY
extern "C" int tj_start_gcm_interrupt_thread();
extern "C" int tj_stop_gcm_interrupt_thread();
extern "C" void ppu_gcm_pump();
extern "C" void fixture_set_idle(HANDLE);
extern "C" void fixture_queue(unsigned,int);
extern "C" void fixture_wake();
extern "C" void fixture_queue_vblank();
extern "C" uint32_t fixture_owner();
extern "C" void (*fixture_wake_hook())();
static SRWLOCK game_lock=SRWLOCK_INIT;
static HANDLE entered,done,vblank_done;
static std::atomic<unsigned> vblank_label{0},vblank_callbacks{0};
static std::atomic<DWORD> vblank_tid{0};
static std::atomic<unsigned> callbacks{0},registrations{0};
static std::atomic<DWORD> callback_tid{0};
static unsigned checks=0,failures=0;
extern "C" int guest_world_register_current(const char*) { ++registrations; return 1; }
extern "C" int guest_world_unregister_current() { --registrations; return 1; }
extern "C" void fixture_callback() {
    SetEvent(entered);
    AcquireSRWLockExclusive(&game_lock);
    callback_tid=GetCurrentThreadId(); ++callbacks;
    ReleaseSRWLockExclusive(&game_lock);
    SetEvent(done);
}
extern "C" void fixture_vblank_callback() {
    vblank_tid=GetCurrentThreadId(); ++vblank_callbacks;
    vblank_label.store(1,std::memory_order_release);
    SetEvent(vblank_done);
}
#define CHECK(test,message) do { ++checks; if(!(test)) { ++failures; std::fprintf(stderr,"FAIL: %s\n",message); } } while(0)
static bool wait_count(unsigned n) {
    auto until=GetTickCount64()+1000;
    while(callbacks.load()<n && GetTickCount64()<until) WaitForSingleObject(done,10);
    return callbacks.load()==n;
}
int main() {
    _putenv_s("TJ_GCM_INTERRUPT_WORKER", "");
    CHECK(tj_gcm_interrupt_worker_enabled(),"interrupt owner is not the default");
    _putenv_s("TJ_GCM_INTERRUPT_WORKER", "0");
    CHECK(!tj_gcm_interrupt_worker_enabled(),"explicit cooperative opt-out ignored");
    _putenv_s("TJ_GCM_INTERRUPT_WORKER", "1");
    CHECK(tj_gcm_interrupt_worker_enabled(),"existing worker opt-in compatibility lost");
    _putenv_s("TJ_GCM_INTERRUPT_WORKER", "");
    const DWORD main_tid=GetCurrentThreadId();
    HANDLE idle=CreateEventW(nullptr,TRUE,FALSE,nullptr);
    entered=CreateEventW(nullptr,TRUE,FALSE,nullptr);
    done=CreateEventW(nullptr,FALSE,FALSE,nullptr);
    vblank_done=CreateEventW(nullptr,FALSE,FALSE,nullptr);
    fixture_set_idle(idle);
    CHECK(tj_start_gcm_interrupt_thread(),"worker did not start");
    CHECK(WaitForSingleObject(idle,1000)==WAIT_OBJECT_0,"startup pump did not finish");
    CHECK(registrations==1 && fixture_owner()!=main_tid && fixture_owner()!=0,"worker does not own dispatch");
    CHECK(tj_start_gcm_interrupt_thread() && registrations==1,"start created a duplicate worker");
    AcquireSRWLockExclusive(&game_lock);
    fixture_queue(1,0);
    std::puts("main-held lock: attempting production safe-point dispatch"); std::fflush(stdout);
    ppu_gcm_pump(); // Old main-thread pump deadlocks here re-entering game_lock.
    CHECK(callbacks==0,"main stole a queued interrupt while holding game lock");
    ReleaseSRWLockExclusive(&game_lock);
    fixture_wake();
    CHECK(wait_count(1),"queued interrupt lost after main safe point");
    CHECK(callback_tid!=main_tid && callback_tid==fixture_owner(),"callback did not run on interrupt owner");
    ResetEvent(entered);
    AcquireSRWLockExclusive(&game_lock);
    fixture_queue(1,1);
    CHECK(WaitForSingleObject(entered,1000)==WAIT_OBJECT_0,"worker did not enter blocked callback");
    CHECK(callbacks==1,"callback bypassed game lock ownership");
    ppu_gcm_pump(); // Must return even while worker is waiting for this lock.
    ReleaseSRWLockExclusive(&game_lock);
    CHECK(wait_count(2),"callback remained blocked after main released its lock");
    fixture_queue(32,1);
    CHECK(wait_count(34),"completion burst was lost or duplicated");
    /* A main-thread HLE FIFO-space wait cannot call its next PPU safe point.
     * Its VBlank label producer must progress without a main-thread pump. */
    fixture_queue_vblank();
    CHECK(WaitForSingleObject(vblank_done,1000)==WAIT_OBJECT_0 && vblank_label.load()==1,
          "VBlank label producer starved while main waited without safe points");
    CHECK(vblank_callbacks==1 && vblank_tid==fixture_owner() && vblank_tid!=main_tid,
          "VBlank used the waiting main thread or delivered twice");
    vblank_label.store(0,std::memory_order_release); fixture_queue_vblank();
    CHECK(WaitForSingleObject(vblank_done,1000)==WAIT_OBJECT_0 && vblank_label.load()==1 && vblank_callbacks==2,
          "next VBlank failed after GPU reset its label");
    auto old_wake=fixture_wake_hook();
    CHECK(tj_stop_gcm_interrupt_thread(),"worker did not stop");
    CHECK(registrations==0 && fixture_owner()==0 && !fixture_wake_hook(),"stop retained dispatch ownership");
    old_wake(); // A producer may have loaded this function before unregister.
    CHECK(callbacks==34,"stale wake hook replayed work after stop");
    CHECK(tj_start_gcm_interrupt_thread(),"worker did not restart");
    fixture_queue(1,1);
    CHECK(wait_count(35) && callback_tid!=main_tid,"restarted worker failed delivery");
    CHECK(tj_stop_gcm_interrupt_thread() && registrations==0,"restarted worker leaked registration");
    fixture_queue(1,0); ppu_gcm_pump();
    CHECK(callbacks==36 && callback_tid==main_tid,"legacy cooperative fallback was lost");
    fixture_set_idle(nullptr);
    CloseHandle(idle); CloseHandle(entered); CloseHandle(done); CloseHandle(vblank_done);
    std::printf("GCM interrupt worker: %u/%u checks passed\n",checks-failures,checks);
    return failures?1:0;
}
'''.replace("\nWORKER_POLICY\n", "\n" + policy + "\n")
    clang = shutil.which("clang") or "C:/Program Files/LLVM/bin/clang.exe"
    clangpp = shutil.which("clang++") or "C:/Program Files/LLVM/bin/clang++.exe"
    with tempfile.TemporaryDirectory(prefix="tj-gcm-interrupt-") as temp:
        temp = Path(temp)
        c, cpp, obj, exe = (temp / name for name in ("pump.c", "test.cpp", "pump.obj", "test.exe"))
        c.write_text(runtime, encoding="utf-8")
        cpp.write_text(driver, encoding="utf-8")
        subprocess.run([clang,"-std=c11","-O2","-D_CRT_SECURE_NO_WARNINGS","-c",str(c),"-o",str(obj)],check=True)
        subprocess.run([clangpp,"-std=c++17","-O2",str(cpp),str(obj),
                        str(ROOT / "src/tj_gcm_interrupt.cpp"),
                        "-I"+str(RUNTIME / "runtime/memory"),"-o",str(exe)],check=True)
        try:
            subprocess.run([str(exe)],check=True,timeout=5)
        except subprocess.TimeoutExpired:
            raise SystemExit("FAIL: production main-thread callback dispatch deadlocked on the held game lock")


if __name__ == "__main__":
    main()
