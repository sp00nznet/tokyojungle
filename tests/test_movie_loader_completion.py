"""Exercise the title's real movie-completion bridge at the scene-loader join."""
from pathlib import Path
import shutil
import subprocess
import tempfile

from test_fifo_present_boundary import function

ROOT = Path(__file__).resolve().parents[1]


def main():
    text = (ROOT / "src/main.cpp").read_text(encoding="utf-8")
    signature = "static int64_t tj_sys_ppu_thread_join("
    # Before the repair, syscall 44 goes straight to the runtime join handler.
    wrapper = (function(text, signature) if signature in text else
               "static int64_t tj_sys_ppu_thread_join(ppu_context* c) { return sys_ppu_thread_join(c); }")
    bridge = "\n".join(function(text, name) for name in (
        "static void tj_cellSailCompleteStoppedPlayback(",
        "static void tj_cellSailTryCompleteStoppedPlaybackAfterLoad(",
        "void tj_cellSailCompleteNoMediaAfterLoad("))
    registered = "lv2_syscall_register(&g_lv2_syscalls, SYS_PPU_THREAD_JOIN, tj_sys_ppu_thread_join);" in text
    source = r'''
#include <atomic>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
struct ppu_context { uint64_t gpr[32],lr; };
constexpr int64_t CELL_OK=0;
constexpr uint32_t TJ_SAIL_EVENT_PLAYER_CALL_COMPLETED=2,TJ_SAIL_CALL_STOP=11;
constexpr uint32_t PLAYER=0x00B58A10,FADE=0x00B5D2C0;
static std::atomic<uint32_t> g_tj_deferred_stop_player_ea{0};
static std::atomic<bool> g_tj_media_load_complete{false};
static bool active;
static unsigned checks,failures,notifications,joins;
static uint8_t paused,fade_done;
static int64_t join_result;
static void check(bool ok,const char* label) {
    ++checks;if(!ok){++failures;fprintf(stderr,"FAIL: %s\n",label);}
}
static bool tj_pamf_active() { return active; }
static uint32_t vm_read32(uint32_t ea) {
    check(ea==0x0035A548,"read the existing fade owner");return FADE;
}
static uint8_t vm_read8(uint32_t ea) {
    check(ea==FADE+0x2C,"read existing fade completion");return fade_done;
}
static void vm_write8(uint32_t ea,uint8_t value) {
    if(ea==PLAYER+84)paused=value;
    else if(ea==FADE+0x2C)fade_done=value;
    else check(false,"no unrelated guest write");
}
static void tj_cellSailNotify(uint32_t player,uint32_t event,uint32_t call,uint32_t p0,uint32_t p1) {
    check(player==PLAYER && event==2 && call==11 && !p0 && !p1,"original STOP notification");
    ++notifications;
}
static int64_t sys_ppu_thread_join(ppu_context* ctx) {
    ++joins;
    check(!notifications,"STOP cannot precede the runtime join");
    check(ctx->gpr[3]==16 && ctx->gpr[4]==0xD00FFC60,"join arguments preserved");
    return join_result;
}
''' + bridge + "\n" + wrapper + r'''
static void reset() {
    g_tj_deferred_stop_player_ea.store(0);g_tj_media_load_complete.store(false);
    active=false;notifications=joins=paused=fade_done=0;join_result=CELL_OK;
}
static int64_t join(uint64_t lr=0x001F0EBC) {
    ppu_context ctx;memset(&ctx,0xA5,sizeof(ctx));
    ctx.lr=lr;ctx.gpr[3]=16;ctx.gpr[4]=0xD00FFC60;
    const ppu_context before=ctx;
    const int64_t result=tj_sys_ppu_thread_join(&ctx);
    check(!memcmp(&ctx,&before,sizeof(ctx)),"wrapper preserves guest registers and caller");
    check(joins==1,"runtime join called exactly once");return result;
}
int main() {
    check(REGISTERED,"title startup registers the loader-aware join");
    reset();g_tj_deferred_stop_player_ea.store(PLAYER);
    check(join()==CELL_OK,"successful join result preserved");
    check(g_tj_media_load_complete && notifications==1 && paused==1 && fade_done==1,
          "reported EOS-before-join state completes after the real loader joins");
    tj_cellSailCompleteNoMediaAfterLoad();tj_cellSailTryCompleteStoppedPlaybackAfterLoad();
    check(notifications==1 && !g_tj_deferred_stop_player_ea,"completion is consumed exactly once");

    reset();active=true;
    check(join()==CELL_OK && g_tj_media_load_complete && !notifications,
          "join-before-EOS waits for the running movie");
    g_tj_deferred_stop_player_ea.store(PLAYER);
    tj_cellSailTryCompleteStoppedPlaybackAfterLoad();
    check(!notifications && !paused && !fade_done,"active decoder still blocks STOP");
    active=false;tj_cellSailTryCompleteStoppedPlaybackAfterLoad();
    check(notifications==1 && paused && fade_done,"EOS completes an already-joined loader");

    reset();g_tj_deferred_stop_player_ea.store(PLAYER);join_result=-22;
    check(join()==-22 && !g_tj_media_load_complete && !notifications,
          "failed join cannot complete the load or hide its error");
    reset();g_tj_deferred_stop_player_ea.store(PLAYER);
    check(join(0x00150FC8)==CELL_OK && !g_tj_media_load_complete && !notifications,
          "unrelated asset-worker join cannot complete the scene load");
    reset();g_tj_deferred_stop_player_ea.store(PLAYER);
    check(join(0x1001F0EBCULL)==CELL_OK && !g_tj_media_load_complete && !notifications,
          "only the exact guest callsite matches");
    reset();tj_cellSailCompleteNoMediaAfterLoad();
    check(g_tj_media_load_complete && !notifications,"existing Survival loader hook remains valid");
    g_tj_deferred_stop_player_ea.store(PLAYER);tj_cellSailTryCompleteStoppedPlaybackAfterLoad();
    check(notifications==1,"existing hook still completes EOS/no-media playback");
    printf("%s: %u loader/movie checks, %u failures\n",failures?"FAIL":"PASS",checks,failures);
    return failures?1:0;
}
'''.replace("REGISTERED", "true" if registered else "false")
    compiler = shutil.which("clang++") or r"C:\Program Files\LLVM\bin\clang++.exe"
    with tempfile.TemporaryDirectory(prefix="tj-movie-loader-") as temp:
        cpp, exe = Path(temp) / "loader.cpp", Path(temp) / "loader.exe"
        cpp.write_text(source, encoding="utf-8")
        subprocess.run([compiler, "-std=c++17", "-O2", str(cpp), "-o", str(exe)], check=True)
        result = subprocess.run([str(exe)], text=True, capture_output=True)
        print(result.stdout, end="")
        if result.returncode:
            print(result.stderr, end="")
        raise SystemExit(result.returncode)


if __name__ == "__main__":
    main()
