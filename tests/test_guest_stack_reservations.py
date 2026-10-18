"""Replay the tutorial crash allocations against production Windows VM guards."""
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

from test_fifo_present_boundary import function

ROOT = Path(__file__).resolve().parents[1]


def main():
    text = (ROOT / "src/main.cpp").read_text(encoding="utf-8")
    baseline = "--baseline" in sys.argv
    names = {"TJ_MAIN_STACK_SIZE": "0x00100000u",
             "TJ_CB_STACK_BASE": "0xD0400000u", "TJ_CB_STACK_SPAN": "0x00100000u"}
    defines = []
    for name, legacy in names.items():
        match = re.search(r"^#define\s+" + name + r"\s+(\w+)", text, re.M)
        assert match or baseline, name
        defines.append(f"#define {name} {match[1] if match else legacy}")
    if baseline:
        reservation = "static bool tj_reserve_fixed_guest_stacks(vm_stack_alloc*) { return true; }"
    else:
        reservation = function(text, "static bool tj_reserve_fixed_guest_stacks(")
        init = text.index("lv2_register_all_syscalls(&g_lv2_syscalls);")
        reserve = text.index("tj_reserve_fixed_guest_stacks(&g_vm_stack_alloc)", init)
        assert init < reserve < text.index("load_elf_into_vm(elf_path)", init)
        assert "uint32_t stack_size = TJ_MAIN_STACK_SIZE;" in text
    source = r'''
#include "vm.h"
#include <stdio.h>
uint8_t* vm_base;
uint32_t ppu_vm_size;
''' + "\n".join(defines) + "\n" + reservation + r'''
static unsigned checks, failures;
static void check(bool ok, const char* what) {
    ++checks;
    if (!ok && ++failures <= 8) fprintf(stderr, "FAIL: %s\n", what);
}
static DWORD protection(uint32_t ea) {
    MEMORY_BASIC_INFORMATION info = {};
    if (!VirtualQuery(vm_base + ea, &info, sizeof(info)) || info.State != MEM_COMMIT)
        return 0;
    return info.Protect;
}
int main() {
    vm_base = (uint8_t*)VirtualAlloc(nullptr, (SIZE_T)VM_TOTAL_SIZE, MEM_RESERVE, PAGE_NOACCESS);
    if (!vm_base || !VirtualAlloc(vm_base + VM_STACK_BASE, 0x01000000,
                                  MEM_COMMIT, PAGE_READWRITE)) return 2;
    vm_stack_alloc stacks;
    vm_stack_alloc_init(&stacks);
    check(tj_reserve_fixed_guest_stacks(&stacks), "fresh allocator reservation");
    // Exact sizes from visible-user-test-20260909-122407-863-51564:
    // these 12 followed by 45 x 16 KiB. PBgControl::load's guard hit D00FC000.
    const uint32_t initial[] = {0x4000,0x4000,0x8000,0x4000,0x4000,0x10000,
                                0x20000,0x4000,0x8000,0x4000,0x4000,0x8000};
    for (unsigned i = 0; i < 57 + 260; ++i) {
        const uint32_t size = i < 12 ? initial[i] : 0x4000;
        const uint32_t worker = vm_stack_allocate(&stacks, size);
        check(worker != 0, "worker allocation succeeds");
        if (!worker) break;
        const uint32_t guard = worker - VM_PAGE_SIZE;
        check(guard >= VM_STACK_BASE + TJ_MAIN_STACK_SIZE, "worker excludes main stack");
        check(worker + size <= TJ_CB_STACK_BASE ||
              guard >= TJ_CB_STACK_BASE + TJ_CB_STACK_SPAN, "worker excludes callback stacks");
        check(protection(guard) == PAGE_NOACCESS, "worker guard remains inaccessible");
        check(protection(worker) == PAGE_READWRITE, "worker stack remains writable");
        if (i == 56) {
            const DWORD prot = protection(0xD00FC2A0u);
            printf("Crash replay: guest D00FC2A0 protection=0x%lX cursor=0x%X\n", prot, stacks.next_addr);
            check(prot == PAGE_READWRITE, "exact tutorial fault address remains writable");
            if (prot == PAGE_READWRITE) {
                // Eight-byte stack-link write from the original stdu r1,-13280(r1).
                const uint64_t link = 0x80F60FD000000000ull;
                memcpy(vm_base + 0xD00FC2A0u, &link, sizeof(link));
                check(memcmp(vm_base + 0xD00FC2A0u, &link, sizeof(link)) == 0,
                      "original stack-link store succeeds");
            }
        }
    }
    for (uint32_t ea = VM_STACK_BASE; ea < VM_STACK_BASE + TJ_MAIN_STACK_SIZE; ea += VM_PAGE_SIZE)
        check(protection(ea) == PAGE_READWRITE, "every main stack page remains writable");
    for (uint32_t ea = TJ_CB_STACK_BASE; ea < TJ_CB_STACK_BASE + TJ_CB_STACK_SPAN; ea += VM_PAGE_SIZE)
        check(protection(ea) == PAGE_READWRITE, "every callback stack page remains writable");
    const uint32_t invalid_sizes[] = {0u, 0xFFFFFFF0u, 0xFFFFF000u, 0x10000000u};
    for (uint32_t size : invalid_sizes) {
        vm_stack_alloc bounded = {0xD0F00000u, 0xD0F04000u};
        check(vm_stack_allocate(&bounded, size) == 0 && bounded.next_addr == 0xD0F00000u,
              "zero/overflow/oversize stack rejects without advancing allocator");
    }
    vm_stack_alloc uncommitted = {0xD1000000u, 0xD1003000u};
    check(vm_stack_allocate(&uncommitted, 0x1000) == 0 && uncommitted.next_addr == 0xD1000000u,
          "failed guard protection cannot publish an unprotected stack");
    vm_stack_alloc exact = {0xD0F00000u, 0xD0F03000u};
    check(vm_stack_allocate(&exact, 0x2000) == 0xD0F01000u && exact.next_addr == exact.region_end,
          "exact region fit succeeds with space for its guard");
    check(vm_stack_allocate(&exact, 0x1000) == 0 && exact.next_addr == exact.region_end,
          "region exhaustion cannot wrap into another stack");
    const uint32_t used = stacks.next_addr;
    check(!tj_reserve_fixed_guest_stacks(&stacks) && stacks.next_addr == used,
          "late reservation rejects without rewinding live stacks");
    vm_stack_alloc_init(&stacks);
    stacks.region_end = TJ_CB_STACK_BASE + TJ_CB_STACK_SPAN - 1;
    check(!tj_reserve_fixed_guest_stacks(&stacks) && stacks.next_addr == VM_STACK_BASE,
          "insufficient region rejects without mutation");
    check(!tj_reserve_fixed_guest_stacks(nullptr), "null allocator rejects");
    vm_shutdown();
    printf("%s: %u native stack checks, %u failures\n", failures ? "FAIL" : "PASS", checks, failures);
    return failures ? 1 : 0;
}
'''
    compiler = shutil.which("clang++") or r"C:\Program Files\LLVM\bin\clang++.exe"
    with tempfile.TemporaryDirectory(prefix="tj-stack-") as tmp:
        cpp, exe = Path(tmp) / "stack.cpp", Path(tmp) / "stack.exe"
        cpp.write_text(source, encoding="utf-8")
        subprocess.run([compiler, "-std=c++17", "-O2", "-I",
                        str(ROOT.parent / "ps3recomp/runtime/memory"), str(cpp), "-o", str(exe)], check=True)
        result = subprocess.run([str(exe)])
        sys.exit(result.returncode)


if __name__ == "__main__":
    main()
