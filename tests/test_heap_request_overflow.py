"""Compile the production HLE heap and reject wrapped allocation sizes."""
from pathlib import Path
import shutil
import subprocess
import tempfile


def main():
    root = Path(__file__).resolve().parents[1]
    text = (root / "src/import_resolver.h").read_text(encoding="utf-8")
    start = text.index("static uint32_t g_heap_base")
    end = text.rfind("/*", start, text.index(" * Import Table Population"))
    source = r'''
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <mutex>
#include <initializer_list>
#include <utility>
static uint8_t* vm_base;
// The fixture owns ordinary host storage; only OS page commitment is mocked.
static void vm_commit(uint32_t, uint32_t) {}
PRODUCTION
static unsigned checks, failures;
static void check(bool ok, const char* label) {
    ++checks;
    if (!ok) { ++failures; std::fprintf(stderr, "FAIL: %s\n", label); }
}
int main() {
    vm_base = (uint8_t*)std::calloc(1, g_heap_end);
    if (!vm_base) return 2;
    hle_heap_init();
    uint32_t first = hle_malloc(33);
    check(first && hle_allocation_size(first) == 48, "ordinary aligned size");
    std::memset(vm_base + first, 0xA5, 33);
    for (uint32_t size : {UINT32_MAX, UINT32_MAX - 1, UINT32_MAX - 14}) {
        uint32_t allocation = hle_malloc(size);
        check(allocation == 0, "malloc rejects rounding overflow");
        hle_free(allocation);
        allocation = hle_memalign(128, size);
        check(allocation == 0, "memalign rejects rounding overflow");
        hle_free(allocation);
    }
    for (auto request : {std::pair<uint32_t,uint32_t>{0x80000001u, 2u},
                         {0x10000u, 0x10000u}, {UINT32_MAX, 16u}}) {
        uint32_t allocation = hle_calloc(request.first, request.second);
        check(allocation == 0, "calloc rejects multiplication overflow");
        hle_free(allocation);
    }
    check(hle_malloc(0xFFFFFFF0u) == 0, "large nonwrapped request fails");
    check(hle_allocation_size(first) == 48 && vm_base[first] == 0xA5 &&
          vm_base[first + 32] == 0xA5, "failed requests preserve live allocation");
    uint32_t aligned = hle_memalign(128, 13);
    check(aligned && !(aligned & 127u), "ordinary memalign retained");
    uint32_t zero = hle_calloc(7, 9);
    bool clear = zero != 0;
    for (unsigned i = 0; i < 63 && clear; ++i) clear = vm_base[zero + i] == 0;
    check(clear, "ordinary calloc retained");
    uint32_t empty = hle_malloc(0);
    check(empty && hle_allocation_size(empty) == 16, "zero-size behavior retained");
    hle_free(empty); hle_free(zero); hle_free(aligned); hle_free(first);
    check(g_heap_block_count == 2 && g_heap_blocks[0].free &&
          g_heap_blocks[1].free, "coalescing and reserved GCM hole retained");
    std::free(vm_base);
    std::printf("HLE heap requests: %u checks, %u failures\n", checks, failures);
    return failures ? 1 : 0;
}
'''.replace("PRODUCTION", text[start:end])
    compiler = shutil.which("clang++") or shutil.which("g++")
    if not compiler:
        compiler = r"C:\Program Files\LLVM\bin\clang++.exe"
    with tempfile.TemporaryDirectory(prefix="tj-heap-overflow-") as directory:
        cpp, exe = Path(directory) / "heap.cpp", Path(directory) / "heap.exe"
        cpp.write_text(source, encoding="utf-8")
        subprocess.run([compiler, "-std=c++17", "-O1", str(cpp), "-o", str(exe)], check=True)
        return subprocess.run([str(exe)], timeout=20).returncode


if __name__ == "__main__":
    raise SystemExit(main())
