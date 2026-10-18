"""Compile actual callback policy and check its raw-syscall/HLE wiring."""
from pathlib import Path
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
PPU = ROOT.parent / "ps3recomp/runtime/ppu"


def main():
    loader = (PPU / "ppu_loader.cpp").read_text(encoding="utf-8", errors="surrogateescape")
    hle = (PPU / "ppu_hle.cpp").read_text(encoding="utf-8", errors="surrogateescape")
    glue = (ROOT / "src/dispatch_glue.cpp").read_text(encoding="utf-8")
    adapter = glue[glue.index("static void tj_guest_caller("):glue.index('extern "C" void tj_install_guest_caller')]
    assert adapter.index("} callback_scope;") < adapter.index("ps3_indirect_call(&cb_ctx);")
    assert "CallbackScope() { ppu_guest_callback_enter(); }" in adapter
    assert "~CallbackScope() { ppu_guest_callback_leave(); }" in adapter
    assert "~ActiveContextScope() { g_active_ctx = saved; }" in adapter
    start = loader.index("/* Display callback safe point shared")
    policy = loader[start:loader.index("/* lv2 syscall dispatch:", start)]
    syscall = loader[loader.index('extern "C" void lv2_syscall(ppu_context* ctx)'):]
    assert syscall.index("ppu_poll_display_callbacks(ctx);") < syscall.index("uint64_t num =")
    for name in ("ppu_guest_call", "ppu_guest_call_ct"):
        body = loader[loader.index('extern "C" uint64_t ' + name + '('):]
        assert body.index("PpuCallbackScope callback_scope;") < body.index("fn(&ctx);")
    assert "if (ppu_poll_display_callbacks) ppu_poll_display_callbacks(ctx);" in hle
    source = r'''
#include <cstdint>
struct ppu_context { uint64_t thread_id; uint64_t gpr[32]; };
POLICY
static ppu_context main_ctx{1, {}};
static unsigned calls = 0;
static unsigned held_kernel_mutexes = 0;
extern "C" unsigned sys_mutex_current_thread_lock_count(void) { return held_kernel_mutexes; }
extern "C" void ppu_dispatch_display_callbacks(void) {
    ++calls;
    PpuCallbackScope callback_scope;
    ppu_poll_display_callbacks(&main_ctx); // must not nest on scratch stack
}
int main() {
    ppu_context worker{2,{}};
    main_ctx.gpr[3]=30; main_ctx.gpr[11]=141;
    ppu_poll_display_callbacks(nullptr);
    ppu_poll_display_callbacks(&worker);
    if(calls) return 1;
    ppu_poll_display_callbacks(&main_ctx);
    if(calls!=1 || main_ctx.gpr[3]!=30 || main_ctx.gpr[11]!=141) return 2;
    { PpuCallbackScope outer; PpuCallbackScope inner;
      ppu_poll_display_callbacks(&main_ctx); if(calls!=1) return 3; }
    try { PpuCallbackScope callback; throw 1; } catch(int) {}
    ppu_poll_display_callbacks(&main_ctx);
    if(calls!=2 || s_guest_callback_depth) return 4;
    // A title-owned, non-GCM callback must use the same nesting guard.
    ppu_guest_callback_enter();
    ppu_poll_display_callbacks(&main_ctx);
    if(calls!=2) return 5;
    ppu_guest_callback_leave();
    ppu_poll_display_callbacks(&main_ctx);
    if(calls!=3 || s_guest_callback_depth) return 6;
    return 0;
}
'''.replace("POLICY", policy)
    compiler = shutil.which("clang++") or "C:/Program Files/LLVM/bin/clang++.exe"
    with tempfile.TemporaryDirectory() as directory:
        cpp, exe = Path(directory)/"safe_point.cpp", Path(directory)/"safe_point.exe"
        cpp.write_text(source)
        subprocess.run([compiler,"-std=c++17","-O2","-DNDEBUG",str(cpp),"-o",str(exe)],check=True)
        subprocess.run([str(exe)],check=True)
    print("PPU callback safe point: PASS (main only, no nesting, exception restoration, syscall/HLE wiring)")


if __name__ == "__main__":
    main()
