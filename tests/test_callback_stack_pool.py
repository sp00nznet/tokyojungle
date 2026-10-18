"""Native live/nested callback stack ownership and worker-allocation contention."""
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from test_fifo_present_boundary import function

ROOT = Path(__file__).resolve().parents[1]


def main():
    glue = (ROOT / "src/dispatch_glue.cpp").read_text(encoding="utf-8")
    baseline = "--baseline" in sys.argv
    common = r'''
#include <atomic>
#include <future>
#include <thread>
#include <vector>
#include <mutex>
#include <stdio.h>
#include <stdint.h>
#include "vm.h"
uint8_t* vm_base;
uint32_t ppu_vm_size;
static vm_stack_alloc g_vm_stack_alloc;
static std::mutex worker_lock;
static void table_lock() { worker_lock.lock(); }
static void table_unlock() { worker_lock.unlock(); }
static unsigned checks,failures;
static void check(bool ok,const char* message) {
    ++checks;if(!ok && ++failures<=6)fprintf(stderr,"FAIL: %s\n",message);
}
'''
    if baseline:
        start = glue.index("static std::atomic<uint32_t> s_next_stack")
        end = glue.index("ppu_context cb_ctx;", start)
        implementation = "static uint32_t legacy_stack() {\n" + glue[start:end] + "return s_cb_stack; }\n"
        implementation += r'''
struct LegacyLease { uint32_t base; };
struct LegacyPool { LegacyLease acquire() { return {legacy_stack()}; } };
static LegacyPool pool;
'''
    else:
        runtime = (ROOT.parent / "ps3recomp/runtime/syscalls/sys_ppu_thread.c").read_text(encoding="utf-8")
        implementation = '#include "tj_callback_stack_pool.h"\n'
        implementation += function(runtime, "uint32_t ppu_thread_allocate_guest_stack(") + "\n"
        implementation += function(glue, "static uint32_t tj_allocate_callback_stack(") + "\n"
        implementation += "static TjCallbackStackPool pool(tj_allocate_callback_stack);\n"
        implementation += "static bool retry_allowed;\nstatic uint32_t retry_allocate(uint32_t size) { return retry_allowed ? ppu_thread_allocate_guest_stack(size) : 0; }\n"
        assert "auto stack_lease = s_stack_pool->acquire();" in glue
        assert "ppu_set_stack(&cb_ctx, stack_lease.base, 0x10000);" in glue
    source = common + implementation + r'''
int main() {
    vm_base=(uint8_t*)VirtualAlloc(nullptr,(SIZE_T)VM_TOTAL_SIZE,MEM_RESERVE,PAGE_NOACCESS);
    if(!vm_base || !VirtualAlloc(vm_base+VM_STACK_BASE,0x01000000,MEM_COMMIT,PAGE_READWRITE))return 2;
    vm_stack_alloc_init(&g_vm_stack_alloc);g_vm_stack_alloc.next_addr=0xD0500000u;
    uint32_t bases[64]={};std::atomic<unsigned> ready{0};std::promise<void> done;
    auto wait=done.get_future().share();std::vector<std::thread> threads;
    for(unsigned t=0;t<32;++t)threads.emplace_back([&,t] {
        auto outer=pool.acquire();auto nested=pool.acquire();
        bases[t*2]=outer.base;bases[t*2+1]=nested.base;
        ready.fetch_add(1);wait.wait();
    });
    while(ready.load()!=32)std::this_thread::yield();
    for(unsigned i=0;i<64;++i) {
        check(bases[i]!=0,"callback allocation succeeds");
        for(unsigned j=0;j<i;++j)check(bases[i]!=bases[j],"live and nested callbacks never share a stack");
    }
    done.set_value();for(auto& t:threads)t.join();
'''
    if not baseline:
        source += r'''
    {
        TjCallbackStackPool limited(retry_allocate);
        const uint32_t before=g_vm_stack_alloc.next_addr;
        { auto lease=limited.acquire();check(!lease.base,"allocation failure returns no callable stack"); }
        check(g_vm_stack_alloc.next_addr==before,"failed pool acquisition consumes no VM space");
        retry_allowed=true;uint32_t recovered=0;
        { auto lease=limited.acquire();recovered=lease.base;check(recovered!=0,"pool can recover after allocation failure"); }
        { auto lease=limited.acquire();check(lease.base==recovered,"recovered slot retains ordinary ownership/reuse"); }
    }
    for(uint32_t base:bases) {
        MEMORY_BASIC_INFORMATION guard={},data={};
        VirtualQuery(vm_base+base-VM_PAGE_SIZE,&guard,sizeof(guard));
        VirtualQuery(vm_base+base,&data,sizeof(data));
        check(guard.Protect==PAGE_NOACCESS && data.Protect==PAGE_READWRITE,
              "each callback has a guard and its full writable stack");
        check(base>=0xD0400000u+VM_PAGE_SIZE,"callbacks exclude the main stack");
    }
    const uint32_t cursor=g_vm_stack_alloc.next_addr;
    for(unsigned i=0;i<1000;++i) { auto lease=pool.acquire();check(lease.base!=0,"completed callback slot is reusable"); }
    check(g_vm_stack_alloc.next_addr==cursor,"sequential callbacks do not leak guest stacks");
    uint32_t thrown_base=0;
    try { auto lease=pool.acquire();thrown_base=lease.base;throw 7; } catch(int) {}
    { auto lease=pool.acquire();check(lease.base==thrown_base,"exception unwinding releases callback ownership"); }
    // Contend new worker allocations with callback reuse; production shared
    // allocator entrypoint must use the same table lock as thread creation.
    uint32_t workers[128]={};threads.clear();
    for(unsigned t=0;t<8;++t)threads.emplace_back([&,t] {
        for(unsigned i=0;i<16;++i) {
            workers[t*16+i]=ppu_thread_allocate_guest_stack(0x4000);
            auto a=pool.acquire();auto b=pool.acquire();
        }
    });
    for(auto& t:threads)t.join();
    for(unsigned i=0;i<128;++i) {
        check(workers[i]!=0,"concurrent worker allocation succeeds");
        for(unsigned j=0;j<i;++j)check(workers[i]!=workers[j],"worker allocations remain exclusive");
        for(uint32_t base:bases)check(workers[i]+0x4000<=base-VM_PAGE_SIZE ||
                                     workers[i]-VM_PAGE_SIZE>=base+0x10000,
                                     "workers exclude all callback stack ranges");
    }
'''
    source += r'''
    vm_shutdown();printf("%s: %u callback/worker checks, %u failures\n",failures?"FAIL":"PASS",checks,failures);
    return failures?1:0;
}
'''
    compiler = shutil.which("clang++") or r"C:\Program Files\LLVM\bin\clang++.exe"
    with tempfile.TemporaryDirectory(prefix="tj-callback-stack-") as tmp:
        cpp, exe = Path(tmp) / "pool.cpp", Path(tmp) / "pool.exe"
        cpp.write_text(source, encoding="utf-8")
        subprocess.run([compiler,"-std=c++17","-O2","-I",str(ROOT / "src"),"-I",
                        str(ROOT.parent / "ps3recomp/runtime/memory"),str(cpp),"-o",str(exe)],check=True)
        result = subprocess.run([str(exe)])
        sys.exit(result.returncode)


if __name__ == "__main__":
    main()
