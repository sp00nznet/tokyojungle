"""Exercise the production FIFO recycler and its PPU callback without a game."""
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

from test_fifo_present_boundary import function

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT.parent / "ps3recomp"


def recycler(gcm):
    """Production text from the strict retirement probe through the NID wrapper."""
    start = gcm.index("static int gcm_fifo_recycle_ready(")
    last = "s32 cellGcm_fifo_recycle("
    stop = gcm.index(last)
    if stop < start:
        raise ValueError("recycler layout changed")
    return gcm[start:stop + len(function(gcm, last))]


def main():
    gcm = (RUNTIME / "libs/video/cellGcmSys.c").read_text(encoding="utf-8")
    packet_type = re.search(r"typedef struct GcmFifoPacket \{.*?\} GcmFifoPacket;", gcm, re.S).group()
    bridge = (RUNTIME / "runtime/ppu/ppu_sysprx.cpp").read_text(encoding="utf-8")
    fixture = r'''
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
typedef uint32_t u32; typedef int32_t s32; typedef uint64_t u64;
PACKET_TYPE
static GcmFifoPacket s_fifo_packet;
typedef u32 (*CellGcmGuestAlloc)(u32,u32);
typedef void (*CellGcmGuestWrite32)(u32,u32);
typedef struct { uint64_t gpr[32]; } ppu_context;
#define CELL_OK 0
#define CELL_GCM_ERROR_INVALID_VALUE (-3)
#define CELL_GCM_ERROR_FAILURE (-255)
#define GCM_CONTROL_GUEST_ADDR 0x20u
#define GCM_FIFO_CALLBACK_SENTINEL_EA 0x03002f00u
#define CTRL_PUT mem[GCM_CONTROL_GUEST_ADDR/4]
#define CTX_END mem[(context+4)/4]
#define CTX_CUR mem[(context+8)/4]
static u32 mem[8192], s_fifo_getoff, s_fifo_calloff, g_gcm_fifo_drained_ea;
static u32 s_gcm_context_ea, next_alloc, guard_lo, guard_hi;
static int s_gcm_fifo_lock, scenario, sleeps, premature, overwrites, checks, failures, follow;
static const u32 context=0x100, begin=0x1000, true_end=0x1ffc, tail=0x1f00, side=0x7000;
/* Scripted walker: from Sleep number `at`, apply the next step once PUT allows it. */
typedef struct { int at; u32 getoff, calloff, need_put; } WalkerStep;
static WalkerStep plan[4];
static int plan_len, plan_pos;
static u32 vm_read32(u32 ea) { return mem[ea/4]; }
static void vm_write32(u32 ea,u32 value) {
    if (ea==context+8 && value==begin) {
        /* Reuse needs a retired lap, or an end already below the first unread word. */
        u32 lap_tail=CTX_CUR, r=s_fifo_calloff ? s_fifo_calloff : s_fifo_getoff;
        int retired=s_fifo_getoff==lap_tail && !s_fifo_calloff;
        int windowed=r>=begin && r<=lap_tail && CTX_END+4<=r;
        if (!retired && !windowed) ++premature;
    }
    if (ea>=guard_lo && ea<guard_hi) ++overwrites;   /* words the walker has not read */
    mem[ea/4]=value;
}
static u32 gcm_ea2io(u32 ea) { return scenario==5 ? UINT32_MAX : ea; }
static u32 gcm_io2ea(u32 io) { return io<sizeof(mem) ? io : 0; }
static s32 cellGcmInit(u32 size,u32 io_size,u32 address) {
    (void)size; (void)io_size; (void)address; return CELL_OK;
}
static u32 allocate(u32 size,u32 align) {
    (void)size; (void)align; u32 result=next_alloc; next_alloc+=0x20; return result;
}
static unsigned long GetCurrentThreadId(void) { return 1; }
static int TryAcquireSRWLockShared(int* lock) {
    (void)lock;
    /* follow: an idle walker that has already drained every published word. */
    if (follow) { s_fifo_getoff=CTRL_PUT; s_fifo_calloff=0; }
    return scenario!=8 || sleeps>=2;
}
static void ReleaseSRWLockShared(int* lock) { (void)lock; }
static void Sleep(unsigned ms) {
    (void)ms; ++sleeps;
    if (scenario==1 || scenario==2 || scenario==8) {
        s_fifo_getoff=tail;
        /* First tick still has a live CALL return; only the second retires it. */
        s_fifo_calloff=sleeps<2 ? 0x1100 : 0;
        g_gcm_fifo_drained_ea=tail;
        mem[(GCM_CONTROL_GUEST_ADDR+4)/4]=tail;
    } else if (plan_pos<plan_len && sleeps>=plan[plan_pos].at &&
               (!plan[plan_pos].need_put || CTRL_PUT==plan[plan_pos].need_put)) {
        s_fifo_getoff=plan[plan_pos].getoff; s_fifo_calloff=plan[plan_pos].calloff; ++plan_pos;
    }
}
RECYCLER
CALLBACK
SETUP_CONTEXT
#define CHECK(condition, message) do { ++checks; if (!(condition)) { ++failures; fprintf(stderr,"FAIL case%d: %s\n",scenario,message); } } while (0)
static void setup(int which) {
    memset(mem,0,sizeof(mem)); scenario=which; sleeps=premature=overwrites=0;
    memset(&s_fifo_packet,0,sizeof(s_fifo_packet));
    memset(&s_fifo_window,0,sizeof(s_fifo_window));
    plan_len=plan_pos=0; guard_lo=guard_hi=0; follow=0;
    mem[context/4]=begin; CTX_END=true_end; CTX_CUR=tail;
    mem[begin/4]=0x00040a70; mem[begin/4+1]=1; /* pending depth restore */
    mem[tail/4]=0xabcdef01;
    CTRL_PUT=0x1800;
    s_fifo_getoff=0x7000; s_fifo_calloff=0x1100;
    g_gcm_fifo_drained_ea=s_fifo_getoff;
}
static int callback(u32 count) {
    ppu_context ctx={0}; ctx.gpr[3]=context; ctx.gpr[4]=count;
    hle_gcm_callback(&ctx); return (s32)ctx.gpr[3];
}
/* Old lap: distinct command words in [begin, tail). */
static void old_lap(void) { for (u32 ea=begin; ea<tail; ea+=4) mem[ea/4]=0xc0de0000u|(ea&0xffffu); }
static int old_lap_intact(u32 from) {
    for (u32 ea=from; ea<tail; ea+=4) if (mem[ea/4]!=(0xc0de0000u|(ea&0xffffu))) return 0;
    return 1;
}
/* Inline gcmReserve: write count words only when current + 4*count <= end. */
static int guest_write(u32 count) {
    u32 cur=CTX_CUR;
    if ((u64)cur+4ull*count>CTX_END) return 0;
    for (u32 i=0; i<count; ++i) vm_write32(cur+4*i,0x00040000u|i);
    CTX_CUR=cur+4*count; return 1;
}
/* Walker parked on a self-JUMP inside a CALLed side buffer; primary return r. */
static void parked_call(u32 r) {
    old_lap(); s_fifo_getoff=side; s_fifo_calloff=r; mem[side/4]=0x20000000u|side;
    guard_lo=r; guard_hi=tail;
}
static void step(int at,u32 getoff,u32 calloff,u32 need_put) {
    plan[0].at=at; plan[0].getoff=getoff; plan[0].calloff=calloff; plan[0].need_put=need_put;
    plan_len=1; plan_pos=0; sleeps=0;
}
int main(void) {
    setup(0); next_alloc=context;
    CHECK(cellGcmSetupContext(0x10,0x10000,0x20000,begin,allocate,vm_write32)==context,"context allocation changed");
    CHECK(mem[(context+4)/4]==begin+0x10000-4,"no in-allocation tail JUMP slot reserved");
    CHECK(mem[context/4]==begin && mem[(context+8)/4]==begin,"initial command head changed");
    CHECK(mem[0x10/4]==context && s_gcm_context_ea==context,"context publication failed");
    for (int which=1; which<=2; ++which) {
        setup(which);
        if (which==2) g_gcm_fifo_drained_ea=begin; /* stale publication */
        CHECK(callback(0x40)==CELL_OK,"completed drain failed");
        CHECK(sleeps>=2 && !premature,"reused primary while CALL/tail remained pending");
        CHECK(mem[GCM_CONTROL_GUEST_ADDR/4]==tail,"published wrapped PUT before tail drained");
        CHECK(mem[(context+8)/4]==begin,"completed ring was not recycled");
        CHECK(mem[tail/4]==(0x20000000u|begin),"tail JUMP missing");
        CHECK(mem[begin/4]==0x00040a70 && mem[begin/4+1]==1,"pending reset overwritten");
        CHECK(CTX_END==tail-4 && s_fifo_window.io_tail==tail,"unconsumed tail JUMP left reachable");
    }
    setup(3); s_fifo_getoff=tail; s_fifo_calloff=0; g_gcm_fifo_drained_ea=tail;
    CHECK(callback(0x40)==CELL_OK && sleeps==0 && !premature,"already-drained ring blocked");
    CHECK(CTX_END==tail-4 && CTX_CUR==begin,"already-drained wrap did not protect its tail JUMP");
    /* Stalled CALL whose return (0x1100) leaves no room for 0x40 words below it:
     * no libgcm window can open, so the old failure contract holds. */
    setup(4);
    CHECK(callback(0x40)==CELL_GCM_ERROR_FAILURE,"stalled recycler reported success");
    CHECK(mem[(context+8)/4]==tail && !premature,"timeout exposed pending storage for reuse");
    CHECK(s_fifo_getoff==0x7000 && s_fifo_calloff==0x1100,"timeout teleported GET/return");
    setup(5);
    CHECK(callback(0x40)==CELL_GCM_ERROR_INVALID_VALUE,"unmapped FIFO accepted");
    CHECK(mem[(context+8)/4]==tail && mem[tail/4]==0xabcdef01 && mem[GCM_CONTROL_GUEST_ADDR/4]==0x1800,"mapping failure changed FIFO");
    setup(6); mem[(context+8)/4]=0x2000;
    CHECK(callback(0x40)==CELL_GCM_ERROR_INVALID_VALUE,"out-of-range tail accepted");
    CHECK(mem[0x2000/4]==0,"out-of-range JUMP written");
    setup(7); mem[(context+8)/4]=begin;
    CHECK(callback(0x40)==CELL_OK && sleeps==0,"empty ring blocked");
    CHECK(mem[begin/4]==0x00040a70 && mem[GCM_CONTROL_GUEST_ADDR/4]==0x1800,"empty ring modified");
    setup(8); s_fifo_getoff=tail; s_fifo_calloff=0;
    CHECK(callback(0x40)==CELL_OK && sleeps>=2 && !premature,"busy walker state read without lock");
    setup(9); s_fifo_getoff=tail; s_fifo_calloff=0; s_fifo_packet.remaining=2;
    CHECK(!gcm_fifo_recycle_ready(tail),"unfinished method packet allowed ring reuse at PUT");
    CHECK(callback(0x40)==CELL_GCM_ERROR_FAILURE,"unfinished packet recycler reported success");
    CHECK(mem[(context+8)/4]==tail && s_fifo_packet.remaining==2,"timeout discarded pending packet");
    s_fifo_packet.remaining=0;
    CHECK(gcm_fifo_recycle_ready(tail),"finished method packet blocked ring reuse");

    /* (a) Walker parked inside a CALL, primary return mid-ring: after the short
     * strict grace, reuse only [begin, r) like libgcm; nothing at or above r moves. */
    setup(10); parked_call(0x1800);
    CHECK(callback(0x40)==CELL_OK,"parked CALL return blocked window reuse");
    CHECK(sleeps==GCM_FIFO_WINDOW_SPINS && sleeps<100,"window not opened right after the short strict grace");
    CHECK(CTX_END==0x17fc && CTX_CUR==begin && !premature,"window not bounded below the CALL return");
    CHECK(mem[tail/4]==(0x20000000u|begin) && CTRL_PUT==tail,"old-tail JUMP or PUT changed");
    CHECK(s_fifo_getoff==side && s_fifo_calloff==0x1800,"window path moved walker state");
    CHECK(guest_write(0x1ff) && !guest_write(1),"reserve contract did not stop below the return");
    CHECK(!overwrites && old_lap_intact(0x1800),"unread old-lap words overwritten");
    /* Parked mid-packet in the primary ring: GET is the next unread payload word. */
    setup(11); old_lap(); s_fifo_getoff=0x1800; s_fifo_calloff=0; s_fifo_packet.remaining=2;
    guard_lo=0x1800; guard_hi=tail;
    CHECK(callback(0x40)==CELL_OK && CTX_END==0x17fc && CTX_CUR==begin,"parked packet blocked window reuse");
    CHECK(!overwrites && !premature && s_fifo_packet.remaining==2,"window disturbed the parked packet");

    /* (b) Extensions: the walker first advances inside the old lap (end grows to
     * just below it), then crosses into the new lap (true end comes back). */
    setup(12); parked_call(0x1800);
    CHECK(callback(0x40)==CELL_OK && guest_write((0x17c0-begin)/4),"window setup failed");
    step(0,0x1c00,0,0);
    CHECK(callback(0x20)==CELL_OK && sleeps==1,"walker progress did not extend the window");
    CHECK(CTX_END==0x1bfc && CTX_CUR==0x17c0 && CTRL_PUT==0x17c0,"extension not bounded below GET or not flushed");
    guard_lo=0x1c00;
    CHECK(guest_write((0x1bf0-0x17c0)/4) && !guest_write(4),"guest could not use the extension");
    step(0,0x1bf0,0,0x1bf0);              /* crossing needs PUT off the old tail */
    CHECK(callback(0x10)==CELL_OK && plan_pos==1,"walker crossing did not release the window");
    CHECK(CTX_END==true_end && CTX_CUR==0x1bf0 && CTRL_PUT==0x1bf0,"true end not restored after crossing");
    CHECK(mem[0x1bf0/4]==(0xc0de0000u|0x1bf0),"extension wrote into the open lap");
    CHECK(!overwrites && !premature,"extension overwrote unread words");
    guard_lo=guard_hi=0;
    CHECK(guest_write((true_end-0x1bf0)/4) && CTX_CUR==true_end,"restored ring not usable");
    /* Crossed, but the request exceeds the true end: fall through to a wrap.
     * 0x300 words do not fit below the new tail JUMP, so the whole ring stays. */
    setup(13); parked_call(0x1800);
    CHECK(callback(0x40)==CELL_OK && guest_write((0x17c0-begin)/4),"window setup failed");
    step(0,0x17c0,0,0x17c0);
    CHECK(callback(0x300)==CELL_OK,"crossed window did not fall back to wrap");
    CHECK(CTX_END==true_end && CTX_CUR==begin && CTRL_PUT==0x17c0 && !s_fifo_window.ctx,"crossed wrap did not recycle");
    CHECK(mem[0x17c0/4]==(0x20000000u|begin) && !premature && !overwrites,"crossed wrap JUMP missing or early");
    /* Same fall-through with a request that fits: the new tail JUMP is protected. */
    setup(13); parked_call(0x1f00);
    CHECK(callback(0x40)==CELL_OK && CTX_END==0x1efc && guest_write((0x1ef0-begin)/4),"window setup failed");
    guard_lo=tail; guard_hi=tail+4;       /* old-tail JUMP until the walker takes it */
    step(0,0x1ef0,0,0x1ef0);
    CHECK(callback(0x50)==CELL_OK && CTX_CUR==begin && CTX_END==0x1eec,"crossed wrap left its tail JUMP reachable");
    CHECK(s_fifo_window.io_tail==0x1ef0 && s_fifo_window.true_end==true_end && !overwrites && !premature,"crossed wrap window state wrong");

    /* (c) No room below the return: failure as before; exact fit succeeds. */
    setup(14); parked_call(0x1800);
    CHECK(callback(0x1ff)==CELL_GCM_ERROR_FAILURE,"window opened without room for the request");
    CHECK(CTX_END==true_end && CTX_CUR==tail && mem[tail/4]==(0x20000000u|begin),"failed window changed the context");
    CHECK(s_fifo_getoff==side && s_fifo_calloff==0x1800 && !overwrites && !premature,"failed window touched walker or unread words");
    setup(14); parked_call(0x1800);
    CHECK(callback(0x1fe)==CELL_OK && CTX_END==0x17fc && guest_write(0x1fe) && !overwrites,"exact-fit window refused or overran");
    /* A live return while GET is inside the ring is stale or nested: untrusted. */
    setup(15); old_lap(); s_fifo_getoff=0x1c00; s_fifo_calloff=0x1800;
    CHECK(callback(0x40)==CELL_GCM_ERROR_FAILURE && CTX_END==true_end && CTX_CUR==tail,"ambiguous CALL return trusted");
    /* A return beyond this lap's tail is not in the lap being retired. */
    setup(16); old_lap(); s_fifo_calloff=0x1f80;
    CHECK(callback(0x40)==CELL_GCM_ERROR_FAILURE && CTX_END==true_end && CTX_CUR==tail,"return outside the old lap trusted");
    /* An open window must not take the empty-ring shortcut at current == begin. */
    setup(17); parked_call(0x1800);
    CHECK(callback(0x40)==CELL_OK && CTX_CUR==begin,"window setup failed");
    sleeps=0;
    CHECK(callback(0x300)==CELL_GCM_ERROR_FAILURE && sleeps>=2000,"open window took the empty-ring shortcut");
    CHECK(CTX_END==0x17fc && CTX_CUR==begin && CTRL_PUT==begin && !overwrites,"failed extension changed the window");

    /* Strict OK leaves GET parked ON the old-tail JUMP. The new lap stays below
     * it; with no flush during the lap the next callback flushes, waits one
     * walker step for the crossing, then restores the true end. */
    setup(18); old_lap(); s_fifo_getoff=tail; s_fifo_calloff=0;
    CHECK(callback(0x40)==CELL_OK && sleeps==0,"caught-up wrap waited");
    CHECK(CTX_CUR==begin && CTX_END==tail-4 && mem[tail/4]==(0x20000000u|begin) && CTRL_PUT==tail,"old-tail JUMP not protected after strict OK");
    guard_lo=tail; guard_hi=tail+4;           /* unconsumed old-tail JUMP */
    CHECK(guest_write((0x1ef0-begin)/4) && !guest_write(4),"protected end let the guest reach the old-tail JUMP");
    step(0,0x1ef0,0,0x1ef0);                  /* walker takes the JUMP once PUT leaves the tail */
    CHECK(callback(0x10)==CELL_OK && sleeps==1 && plan_pos==1,"protected lap did not flush and wait for the crossing");
    CHECK(CTX_END==true_end && CTX_CUR==0x1ef0 && CTRL_PUT==0x1ef0 && !s_fifo_window.ctx,"true end not restored after the crossing");
    CHECK(mem[tail/4]==(0x20000000u|begin) && !overwrites && !premature,"old-tail JUMP overwritten before the walker took it");
    guard_lo=guard_hi=0;
    CHECK(guest_write(0x10) && CTX_CUR==0x1f30,"restored ring not usable past the consumed JUMP");
    /* Same, but the guest flushed mid-lap and the walker already crossed: no wait. */
    setup(19); old_lap(); s_fifo_getoff=tail; s_fifo_calloff=0;
    CHECK(callback(0x40)==CELL_OK && CTX_END==tail-4 && guest_write((0x1ef0-begin)/4),"protected wrap failed");
    CTRL_PUT=0x1800; s_fifo_getoff=0x1800;   /* guest flush drained; JUMP taken */
    CHECK(callback(0x10)==CELL_OK && sleeps==0 && CTX_END==true_end && CTRL_PUT==0x1ef0,"caught-up walker waited at the protected end");
    /* Common case over several laps with an idle walker that drains every
     * flush at once: one extra callback per lap and never a sleep. */
    setup(20); old_lap(); follow=1;
    int calls=0, intact=1;
    for (int lap=0; lap<3; ++lap) {
        u32 lap_tail=CTX_CUR;
        ++calls; CHECK(callback(0x40)==CELL_OK && CTX_END==lap_tail-4 && CTX_CUR==begin,"idle wrap not protected");
        while (guest_write(0x40)) {}
        intact&=mem[lap_tail/4]==(0x20000000u|begin);
        ++calls; CHECK(callback(0x40)==CELL_OK && CTX_END==true_end,"idle protected lap not released");
        while (guest_write(0x40)) {}
    }
    CHECK(calls==6 && sleeps==0 && intact && !premature,"idle laps waited or took more than one extra callback");
    /* The NID entry has no size: no partial window, whole ring as before. */
    setup(21); s_fifo_getoff=tail; s_fifo_calloff=0;
    CHECK(cellGcm_fifo_recycle(context)==CELL_OK && CTX_CUR==begin && CTX_END==true_end && !s_fifo_window.ctx,"unknown-size NID recycle got a partial window");
    /* Window condition re-checked every spin after the grace: the return 0x1100
     * leaves no room for 0x40 words; at Sleep 100 the walker RETs and parks at
     * 0x1800 (e.g. a semaphore acquire) and the window opens right then. */
    setup(22); parked_call(0x1100);
    step(100,0x1800,0,0);
    CHECK(callback(0x40)==CELL_OK && sleeps==100,"window condition not re-checked after the grace");
    CHECK(CTX_END==0x17fc && CTX_CUR==begin && !premature && !overwrites,"late window exposed unread words");
    guard_lo=0x1800;                          /* [0x1100,0x1800) now consumed */
    CHECK(guest_write(0x1ff) && !guest_write(1) && !overwrites && old_lap_intact(0x1800),"late window overran the walker");
    printf("%d checks, %d failures\n",checks,failures);
    return failures ? 1 : 0;
}
'''.replace("\nPACKET_TYPE\n", "\n" + packet_type + "\n").replace(
        "\nRECYCLER\n", "\n" + recycler(gcm) + "\n").replace(
        "\nCALLBACK\n", "\n" + function(bridge, "static void hle_gcm_callback(") + "\n").replace(
        "\nSETUP_CONTEXT\n", "\n" + function(gcm, "u32 cellGcmSetupContext(") + "\n")
    compiler = shutil.which("clang") or r"C:\Program Files\LLVM\bin\clang.exe"
    with tempfile.TemporaryDirectory(prefix="tj-fifo-recycle-") as directory:
        source = Path(directory) / "recycle.c"
        executable = Path(directory) / "recycle.exe"
        source.write_text(fixture, encoding="utf-8")
        subprocess.run([compiler, "-std=c11", "-DNDEBUG", "-D_CRT_SECURE_NO_WARNINGS",
                        str(source), "-o", str(executable)], check=True)
        run = subprocess.run([str(executable)], capture_output=True, text=True)
    sys.stdout.write(run.stdout)
    sys.stdout.write(run.stderr)
    problems = []
    if run.stderr.count("fifo recycle: window reuse below") != 1:
        problems.append("window reuse must be logged exactly once")
    stalled = [line for line in run.stderr.splitlines() if "tail not retired" in line]
    fields = ("get=", "call=", "remaining=", "put=", "word@get=", "self_jump=")
    if not stalled or not all(all(field in line for field in fields) for line in stalled):
        problems.append("tail warning lacks walker state")
    if not any("self_jump=1" in line for line in stalled):
        problems.append("parked self-JUMP not identified")
    if "window not released" not in run.stderr:
        problems.append("failed window extension not reported")
    for problem in problems:
        print("FAIL log:", problem)
    return run.returncode or (1 if problems else 0)


if __name__ == "__main__":
    raise SystemExit(main())
