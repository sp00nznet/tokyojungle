"""Run the actual Sail adapter across decoder/display rate mismatch and EOS."""
from pathlib import Path
import shutil
import subprocess
import tempfile

from test_fifo_present_boundary import function

ROOT = Path(__file__).resolve().parents[1]


def main():
    text = (ROOT / "src/main.cpp").read_text(encoding="utf-8")
    adapter = function(text, "static void tj_cellSailGraphicsAdapterGetFrame(")
    release = function(text, "static void tj_cellSailReleaseMediaFramesLocked(")
    source = r'''
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <atomic>
#include <mutex>
struct ppu_context { uint64_t gpr[32]; };
static uint8_t memory[2048];
static uint8_t* vm_base = memory;
static void vm_write8(uint32_t ea,uint8_t v) { memory[ea]=v; }
static void vm_write32(uint32_t ea,uint32_t v) {
    for(int i=3;i>=0;--i) { memory[ea+i]=(uint8_t)v;v>>=8; }
}
static void vm_write64(uint32_t ea,uint64_t v) {
    for(int i=7;i>=0;--i) { memory[ea+i]=(uint8_t)v;v>>=8; }
}
static uint64_t read_be(uint32_t ea,unsigned bytes) {
    uint64_t v=0;for(unsigned i=0;i<bytes;++i)v=(v<<8)|memory[ea+i];return v;
}
static void hle_free(uint32_t) {}
constexpr uint32_t TJ_SAIL_GUEST_FRAME_COUNT=4,TJ_SAIL_EVENT_SOURCE_EOS=8;
static std::mutex g_tj_media_guest_mutex;
static std::atomic<uint32_t> g_tj_media_player_ea{17},g_tj_deferred_stop_player_ea{0};
static std::atomic<bool> g_tj_media_eos_sent{false};
static uint32_t g_tj_media_frame_ea[4]={256,320,384,448};
static uint32_t g_tj_media_audio_frame_ea[4]={};
static uint32_t g_tj_media_frame_bytes=64,g_tj_media_audio_frame_bytes;
static uint32_t g_tj_media_next_buffer,g_tj_media_audio_next_buffer;
static uint32_t g_tj_media_session_id=1,g_tj_media_last_frame=UINT32_MAX;
static uint64_t g_tj_media_last_pts_90k;
static bool due,eof;
static uint32_t next_frame,pops,stops,notifications,completions;
static bool tj_pamf_pop_argb(void* dst,size_t size,uint64_t* pts,uint32_t* frame) {
    if(!due)return false;
    due=false;++pops;*frame=next_frame;*pts=90000+next_frame*3003;
    memset(dst,(int)(++next_frame),size);return true;
}
static bool tj_pamf_eof() { return eof; }
static void tj_cellSailReachMediaEosLocked() { ++stops;g_tj_media_player_ea.store(0); }
static void tj_cellSailNotify(uint32_t,uint32_t,uint32_t,uint32_t,uint32_t) { ++notifications; }
static void tj_cellSailTryCompleteStoppedPlaybackAfterLoad() { ++completions; }
''' + release + "\n" + adapter + r'''
static unsigned checks,failures;
static void check(bool ok,const char* msg) {
    ++checks;if(!ok && ++failures<8)fprintf(stderr,"FAIL: %s\n",msg);
}
static uint64_t poll() {
    ppu_context c={};c.gpr[3]=64;c.gpr[4]=96;
    tj_cellSailGraphicsAdapterGetFrame(&c);return c.gpr[3];
}
int main() {
    check(poll()==0 && read_be(96,4)==0,"before first decoded frame remains empty");
    due=true;
    check(poll()==256,"first due frame published");
    uint8_t info[24];memcpy(info,memory+96,24);
    // Repeated display polls, including an extended decoder stall/pause, must
    // retain the same owned image and PTS without advancing the decode ring.
    for(unsigned i=0;i<180;++i) {
        check(poll()==256,"display poll retains current picture between due frames");
        check(!memcmp(info,memory+96,24),"held frame metadata and PTS unchanged");
        check(pops==1 && g_tj_media_next_buffer==1 && memory[256]==1,
              "hold does not decode, rotate or overwrite the owned image");
    }
    due=true;check(poll()==320 && read_be(112,8)==93003,"next due frame replaces hold at original PTS");
    check(poll()==320 && read_be(104,4)==1,"second frame also retained");
    // Cycle more than all four adapter buffers with faster display polls.
    for(unsigned f=2;f<14;++f) {
        due=true;uint32_t ea=256+(f%4)*64;
        check(poll()==ea && read_be(104,4)==f,"new frame keeps ring order and frame index");
        check(poll()==ea && read_be(112,8)==90000+f*3003,"ring wrap retains correct PTS");
    }
    eof=true;
    check(poll()==0 && stops==1 && notifications==1 && completions==1,
          "EOS is delivered once and cannot be hidden by held frame");
    check(poll()==0 && stops==1 && notifications==1 && completions==1,
          "ended session does not return an old frame or repeat EOS");
    check(g_tj_deferred_stop_player_ea.load()==17,"loader-completion handoff preserved");
    tj_cellSailReleaseMediaFramesLocked();
    check(g_tj_media_last_frame==UINT32_MAX && g_tj_media_last_pts_90k==0,
          "stop clears held frame metadata");
    eof=false;g_tj_media_player_ea.store(33);g_tj_media_session_id=2;
    g_tj_media_eos_sent.store(false);g_tj_media_frame_bytes=64;
    for(unsigned i=0;i<4;++i)g_tj_media_frame_ea[i]=512+i*64;
    check(poll()==0,"new session cannot reuse previous session picture");
    due=true;next_frame=0;
    check(poll()==512 && read_be(100,4)==2 && read_be(112,8)==90000,
          "new session publishes its own buffer, identity and timing");
    printf("%s: %u adapter checks, %u failures\n",failures?"FAIL":"PASS",checks,failures);
    return failures?1:0;
}
'''
    compiler = shutil.which("clang++") or r"C:\Program Files\LLVM\bin\clang++.exe"
    with tempfile.TemporaryDirectory(prefix="tj-movie-hold-") as tmp:
        cpp, exe = Path(tmp) / "hold.cpp", Path(tmp) / "hold.exe"
        cpp.write_text(source, encoding="utf-8")
        subprocess.run([compiler, "-std=c++17", "-O2", str(cpp), "-o", str(exe)], check=True)
        subprocess.run([str(exe)], check=True)


if __name__ == "__main__":
    main()
