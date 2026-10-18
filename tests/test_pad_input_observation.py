"""Read-only observation of actual wrapper packet/decoded-axis field offsets."""
from pathlib import Path
import shutil
import subprocess
import tempfile

from test_fifo_present_boundary import function

ROOT = Path(__file__).resolve().parents[1]


def main():
    native = (ROOT.parent / "ps3recomp/runtime/ppu/ppu_hle.cpp").read_text(encoding="utf-8")
    begin = native.index("struct tj_pad_input_snapshot {")
    structure = native[begin:native.index("};", begin) + 2]
    observer = function(native, "static bool tj_pad_read_input_snapshot(")
    begin = native.index("struct tj_pad_action_snapshot {")
    action_structure = native[begin:native.index("};", begin) + 2]
    action_observer = function(native, "static unsigned tj_pad_read_action_snapshots(")
    source = r'''
#include <stdint.h>
#include <stdio.h>
#include <string.h>
struct ppu_context { uint64_t gpr[32],lr; };
static uint8_t memory[0x20000],*vm_base=memory;
static unsigned reads,checks,failures;
static uint16_t vm_read16(uint64_t a) { ++reads; return (uint16_t)(memory[a]<<8)|memory[a+1]; }
static uint32_t vm_read32(uint64_t a) { return ((uint32_t)vm_read16(a)<<16)|vm_read16(a+2); }
static void put16(uint32_t a,uint16_t v) { memory[a]=(uint8_t)(v>>8);memory[a+1]=(uint8_t)v; }
static void put32(uint32_t a,uint32_t v) { put16(a,(uint16_t)(v>>16));put16(a+2,(uint16_t)v); }
STRUCTURE
OBSERVER
ACTION_STRUCTURE
ACTION_OBSERVER
static void check(bool ok,const char* label) { ++checks;if(!ok){++failures;printf("FAIL %s\n",label);} }
int main() {
    ppu_context ctx={};ctx.gpr[30]=0x10000;ctx.gpr[4]=0x10004;
    put32(0x10004,8);put16(0x10014,128);put16(0x10016,128);
    put32(0x10488,1);put32(0x10494,1);put32(0x105C0,0x11000);put32(0x11004,0);
    for(uint32_t lr:{0x001F7248u,0x001F7620u,0x001F7640u}) {
        ctx.lr=lr;ppu_context before=ctx;tj_pad_input_snapshot input={};
        check(tj_pad_read_input_snapshot(&ctx,&input),"known wrapper accepted");
        check(input.data_ea==0x10004 && input.length==8 && input.left_x==128 &&
              input.left_y==128 && input.game_x==1 && input.game_y==1 && input.profile==0,
              "previous packet and decoded axes");
        check(!memcmp(&ctx,&before,sizeof(ctx)),"context preserved");
    }
    put16(0x10014,255);put32(0x10488,255);put32(0x10494,(uint32_t)-255);
    tj_pad_input_snapshot input={};
    check(tj_pad_read_input_snapshot(&ctx,&input) && input.left_x==255 &&
          input.game_x==255 && input.game_y==-255,"signed decoded axis and full stick");
    unsigned old_reads=reads;ctx.lr=0;
    check(!tj_pad_read_input_snapshot(&ctx,&input)&&reads==old_reads,"unknown wrapper unread");
    ctx.lr=0x001F7248;ctx.gpr[4]=0x10008;
    check(!tj_pad_read_input_snapshot(&ctx,&input)&&reads==old_reads,"wrong object unread");
    ctx.gpr[30]=0xFFFFFFFC;ctx.gpr[4]=0;
    check(!tj_pad_read_input_snapshot(&ctx,&input)&&reads==old_reads,"wrapped range unread");
    check(!tj_pad_read_input_snapshot(nullptr,&input)&&reads==old_reads,"null context unread");
    put32(0x10088,0x12000);put32(0x12000,0x10000);put32(0x12008,0);
    put32(0x1200C,0x3F800000);put32(0x12010,0);put32(0x12014,0x3E4CCCCD);
    put32(0x12018,0x3F800000);put32(0x1201C,255);
    put32(0x1008C,0x12100);put32(0x12100,0x10000);put32(0x12108,1);
    put32(0x12118,0xBF800000);put32(0x1211C,(uint32_t)-255);
    tj_pad_action_snapshot actions[16]={};
    check(tj_pad_read_action_snapshots(0x10000,actions,16)==2,"analog action inventory");
    check(actions[0].ea==0x12000 && actions[0].slot==0 && actions[0].input_type==0 &&
          actions[0].scale_bits==0x3F800000 && actions[0].bias_bits==0 &&
          actions[0].deadzone_bits==0x3E4CCCCD && actions[0].value_bits==0x3F800000 &&
          actions[0].raw_value==255,"scaled action fields match original offsets");
    check(actions[1].value_bits==0xBF800000 && actions[1].raw_value==-255,"negative action preserved");
    check(tj_pad_read_action_snapshots(0x10000,actions,1)==1,"action inventory bounded");
    put32(0x12100,0x11000);
    check(tj_pad_read_action_snapshots(0x10000,actions,16)==1,"foreign parent rejected");
    put32(0x12008,10);
    check(tj_pad_read_action_snapshots(0x10000,actions,16)==0,"digital actions excluded");
    put32(0x10088,0xFFFFFFFC);
    check(tj_pad_read_action_snapshots(0x10000,actions,16)==0,"wrapped action pointer rejected");
    old_reads=reads;
    check(!tj_pad_read_action_snapshots(0xFFFFFFFF,actions,16)&&reads==old_reads,"invalid pad unread");
    check(!tj_pad_read_action_snapshots(0x10000,nullptr,16)&&reads==old_reads,"null output unread");
    check(!tj_pad_read_action_snapshots(0x10000,actions,0)&&reads==old_reads,"zero capacity unread");
    printf("Pad input observation: %u checks, %u failures\n",checks,failures);
    return failures?1:0;
}
'''.replace("ACTION_STRUCTURE\n", action_structure + "\n").replace("ACTION_OBSERVER\n", action_observer + "\n").replace("STRUCTURE\n", structure + "\n").replace("OBSERVER\n", observer + "\n")
    source = "#include <initializer_list>\n" + source
    clang = shutil.which("clang++") or r"C:\Program Files\LLVM\bin\clang++.exe"
    with tempfile.TemporaryDirectory(prefix="tj-pad-observation-") as temp:
        cpp, exe = Path(temp) / "pad.cpp", Path(temp) / "pad.exe"
        cpp.write_text(source, encoding="utf-8")
        subprocess.run([clang, "-std=c++20", "-O2", str(cpp), "-o", str(exe)], check=True)
        result = subprocess.run([str(exe)], check=False)
    assert "site->input_reports < 32" in native
    assert "if (!enabled || !ctx) return;" in native
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
