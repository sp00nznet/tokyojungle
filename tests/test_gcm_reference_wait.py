"""The real lifted fence must emit, flush and wait before buffer reuse."""
from pathlib import Path
import importlib.util
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("restore_wait", ROOT / "scripts/restore_gcm_reference_wait.py")
repair = importlib.util.module_from_spec(spec)
spec.loader.exec_module(repair)

HARNESS = r'''
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <cstdlib>
struct ppu_context { uint64_t gpr[32],lr; uint32_t cr; };
static unsigned char memory[0x10000];
static unsigned step, polls, threshold, failures;
static uint32_t expected;
static uint32_t vm_read32(uint64_t a) { uint32_t x; memcpy(&x,memory+a,4); return x; }
static uint64_t vm_read64(uint64_t a) { uint64_t x; memcpy(&x,memory+a,8); return x; }
static void vm_write32(uint64_t a,uint32_t x) { memcpy(memory+a,&x,4); }
static void vm_write64(uint64_t a,uint64_t x) { memcpy(memory+a,&x,8); }
static uint64_t ppc_rldicl(uint64_t x,int,int) { return (uint32_t)x; }
#define DRAIN_TRAMPOLINE(ctx) ((void)0)
static void require(bool value) { if(!value) failures++; }
static void func_001F8CCC(ppu_context* c) {
 require(step++==0 && c->gpr[3]==0x1000 && c->gpr[4]==expected);
}
static void func_001F8BF4(ppu_context* c) {
 require(step++==1 && c->gpr[3]==0x1000);
}
static void func_0025DD40(ppu_context* c) { require(step++==2); c->gpr[3]=0x2000; }
static void lv2_syscall(ppu_context* c) {
 require(step==3 && c->gpr[3]==30 && c->gpr[11]==141);
 if(++polls>10) exit(2);
 if(polls==threshold) vm_write32(0x2008,expected);
 c->gpr[3]=0;
}
BODY
int main() {
 for(uint32_t value: {0u,1u,0xffffffffu}) for(unsigned delay: {0u,3u}) {
  ppu_context c={}; memset(memory,0,sizeof(memory)); step=polls=0;
  expected=value; threshold=delay;
  vm_write32(0x2008,delay?value^0x80000000u:value);
  c.gpr[1]=0x8000; c.gpr[2]=0x359220; c.gpr[3]=0x1000; c.gpr[4]=value;
  c.gpr[29]=0x2929; c.gpr[30]=0x3030; c.gpr[31]=0x3131; c.lr=0x123456;
  func_001F8C4C(&c);
  require(step==3 && polls==delay && vm_read32(0x2008)==value);
  require(c.gpr[1]==0x8000 && c.gpr[2]==0x359220 && c.lr==0x123456);
  require(c.gpr[29]==0x2929 && c.gpr[30]==0x3030 && c.gpr[31]==0x3131);
 }
 printf("GCM reference fence: 6 cases, %u failures\n",failures); return failures?1:0;
}
'''.replace("#include <cstdlib>", "#include <cstdlib>\n#include <initializer_list>")


def main():
    body = repair.restored_function()
    clang = shutil.which("clang++") or r"C:\Program Files\LLVM\bin\clang++.exe"
    with tempfile.TemporaryDirectory(prefix="tj-reference-wait-") as temp:
        temp = Path(temp)
        for newline in (b"\n", b"\r\n"):
            fixture = temp / "fixture.cpp"
            original = b"// legacy \x96 untouched" + newline + repair.STUB + newline + b"// tail" + newline
            fixture.write_bytes(original)
            assert repair.repair(fixture, apply=True) == 1
            patched = fixture.read_bytes()
            assert patched.replace(body.replace(b"\n", newline), repair.STUB) == original
            assert repair.repair(fixture, apply=True) == 0
            assert fixture.read_bytes() == patched
        for name, code, expected in (("baseline", repair.STUB, 1), ("restored", body, 0)):
            source = temp / (name + ".cpp")
            source.write_text(HARNESS.replace("BODY", code.decode()))
            exe = temp / (name + ".exe")
            subprocess.run([clang, "-O2", str(source), "-o", str(exe)], check=True)
            result = subprocess.run([str(exe)])
            assert result.returncode == expected, (name, result.returncode)
    print("Reference-wait preservation/idempotence PASS")


if __name__ == "__main__":
    main()
