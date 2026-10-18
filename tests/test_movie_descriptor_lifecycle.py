"""Replay the title's add, close, destroy sequence using production handlers."""
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

from test_fifo_present_boundary import function

ROOT = Path(__file__).resolve().parents[1]


def main():
    text = (ROOT / "src/main.cpp").read_text(encoding="utf-8")
    destroy = function(text, "static void tj_cellSailPlayerDestroyDescriptor(")
    if "--baseline" in sys.argv:
        destroy = r'''
static void tj_cellSailPlayerDestroyDescriptor(ppu_context* ctx) {
    const uint32_t descriptor_ea=(uint32_t)ctx->gpr[4];
    if (!descriptor_ea || vm_read8(descriptor_ea+1)) {
        ctx->gpr[3]=(uint64_t)(int64_t)(int32_t)(descriptor_ea?
            CELL_SAIL_ERROR_INVALID_STATE:CELL_SAIL_ERROR_INVALID_ARGUMENT);
        return;
    }
    uint32_t expected=descriptor_ea;
    if (g_tj_media_descriptor_ea.compare_exchange_strong(expected,0)) {
        std::lock_guard<std::mutex> lock(g_tj_media_guest_mutex);
        tj_cellSailStopMediaLocked();tj_pamf_close();
    }
    hle_free(descriptor_ea);ctx->gpr[3]=CELL_OK;
}
'''
    handlers = "\n".join((
        function(text, "static void tj_cellSailPlayerAddDescriptor("),
        function(text, "static void tj_cellSailPlayerCloseStream("), destroy))
    source = r'''
#include <atomic>
#include <mutex>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <vector>
struct ppu_context { uint64_t gpr[32]={}; };
static const int32_t CELL_OK=0;
static const int32_t CELL_SAIL_ERROR_INVALID_ARGUMENT=(int32_t)0x80610701u;
static const int32_t CELL_SAIL_ERROR_INVALID_STATE=(int32_t)0x80610702u;
static const uint32_t TJ_SAIL_EVENT_PLAYER_CALL_COMPLETED=2,TJ_SAIL_CALL_CLOSE_STREAM=3;
static uint8_t memory[0x10000];
static std::mutex g_tj_media_guest_mutex;
static std::atomic<uint32_t> g_tj_media_descriptor_ea{0},g_tj_media_player_ea{0};
static unsigned checks=0,failures=0,closes=0,notifications=0;
static std::vector<uint32_t> freed;
static uint8_t vm_read8(uint32_t a){return memory[a];}
static uint32_t vm_read32(uint32_t a){return (uint32_t(memory[a])<<24)|(uint32_t(memory[a+1])<<16)|(uint32_t(memory[a+2])<<8)|memory[a+3];}
static void vm_write8(uint32_t a,uint8_t v){memory[a]=v;}
static void vm_write32(uint32_t a,uint32_t v){for(int i=3;i>=0;--i){memory[a+i]=(uint8_t)v;v>>=8;}}
static void tj_cellSailStopMediaLocked(){g_tj_media_player_ea.store(0);}
static void tj_pamf_close(){++closes;}
static void hle_free(uint32_t ea){freed.push_back(ea);}
static void tj_cellSailNotify(uint32_t,uint32_t,uint32_t,uint64_t,uint64_t){++notifications;}
HANDLERS
static void check(bool ok,const char* why){++checks;if(!ok){++failures;fprintf(stdout,"FAIL: %s\n",why);}}
static int32_t call(void(*fn)(ppu_context*),uint32_t player,uint32_t desc=0){ppu_context c;c.gpr[3]=player;c.gpr[4]=desc;fn(&c);return (int32_t)c.gpr[3];}
static void reset(){memset(memory,0,sizeof(memory));g_tj_media_descriptor_ea=0;g_tj_media_player_ea=0;freed.clear();closes=notifications=0;}
int main(){
 const uint32_t player=0x100,other=0x200,a=0x1000,b=0x1100,c=0x1200;
 reset();
 for(uint32_t i=0;i<32;++i){
  const uint32_t desc=0x2000+i*0x100;
  g_tj_media_descriptor_ea=desc;
  check(call(tj_cellSailPlayerAddDescriptor,player,desc)==CELL_OK,"next movie registers after earlier cleanup");
  g_tj_media_player_ea=player;
  check(call(tj_cellSailPlayerCloseStream,player)==CELL_OK,"original close-stream call succeeds");
  check(call(tj_cellSailPlayerDestroyDescriptor,player,desc)==CELL_OK,"closed registered descriptor can be destroyed");
  check(vm_read32(player+72)==0 && vm_read32(player+76)==0,"closed movie no longer occupies player registry");
  check(!vm_read8(desc+1) && freed.size()==i+1,"descriptor released exactly once per movie");
  check(!g_tj_media_descriptor_ea && closes==i+1,"host descriptor retired with guest object");
 }
 check(notifications==32,"all close completions delivered");
 reset();call(tj_cellSailPlayerAddDescriptor,player,a);g_tj_media_descriptor_ea=a;g_tj_media_player_ea=player;
 check(call(tj_cellSailPlayerDestroyDescriptor,player,a)==CELL_SAIL_ERROR_INVALID_STATE,"active descriptor cannot be freed");
 check(freed.empty() && vm_read32(player+72)==1 && vm_read8(a+1),"active rejection preserves ownership");
 g_tj_media_player_ea=0;
 check(call(tj_cellSailPlayerDestroyDescriptor,other,a)==CELL_SAIL_ERROR_INVALID_STATE,"another player cannot destroy registered descriptor");
 check(freed.empty() && vm_read32(player+72)==1,"wrong-owner rejection preserves registry");
 call(tj_cellSailPlayerAddDescriptor,player,b);
 check(call(tj_cellSailPlayerAddDescriptor,player,c)==CELL_SAIL_ERROR_INVALID_STATE,"simultaneous registration limit remains checked");
 g_tj_media_descriptor_ea=b;g_tj_media_player_ea=player;
 check(call(tj_cellSailPlayerDestroyDescriptor,player,a)==CELL_OK,"inactive older descriptor can retire while another plays");
 check(vm_read32(player+72)==1 && vm_read32(player+76)==b && vm_read32(player+80)==0,"registry compacts without losing other descriptor");
 check(g_tj_media_descriptor_ea==b && g_tj_media_player_ea==player && !closes,"retiring old descriptor preserves current playback");
 call(tj_cellSailPlayerCloseStream,player);
 check(call(tj_cellSailPlayerDestroyDescriptor,player,b)==CELL_OK && vm_read32(player+72)==0,"last slot also retires");
 reset();g_tj_media_descriptor_ea=a;
 check(call(tj_cellSailPlayerDestroyDescriptor,player,a)==CELL_OK && freed.size()==1 && closes==1,"unregistered creation-failure cleanup remains valid");
 check(call(tj_cellSailPlayerDestroyDescriptor,0,b)==CELL_SAIL_ERROR_INVALID_ARGUMENT,"null player rejected");
 check(call(tj_cellSailPlayerDestroyDescriptor,player,0)==CELL_SAIL_ERROR_INVALID_ARGUMENT,"null descriptor rejected");
 reset();vm_write32(player+72,3);vm_write8(a+1,1);
 check(call(tj_cellSailPlayerDestroyDescriptor,player,a)==CELL_SAIL_ERROR_INVALID_STATE && freed.empty(),"corrupt registry count rejected without freeing");
 printf("%s: %u descriptor lifecycle checks, %u failures\n",failures?"FAIL":"PASS",checks,failures);
 return failures?1:0;
}
'''.replace("HANDLERS", handlers)
    compiler = shutil.which("clang++") or r"C:\Program Files\LLVM\bin\clang++.exe"
    with tempfile.TemporaryDirectory(prefix="tj-movie-descriptor-") as temp:
        cpp, exe = Path(temp) / "lifecycle.cpp", Path(temp) / "lifecycle.exe"
        cpp.write_text(source, encoding="utf-8")
        subprocess.run([compiler, "-std=c++17", "-O2", str(cpp), "-o", str(exe)], check=True)
        result = subprocess.run([str(exe)], text=True, capture_output=True)
        print(result.stdout, end="")
        if result.returncode:
            print(result.stderr[-1200:], end="")
        raise SystemExit(result.returncode)


if __name__ == "__main__":
    main()
