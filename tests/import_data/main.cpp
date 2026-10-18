// Exercise the real startup resolver without launching the game.
#include "ps3emu/ps3types.h"
#include "import_resolver.h"
#include <fstream>
#include <iterator>
#include <vector>

uint8_t* vm_base = nullptr;
uint32_t ppu_vm_size = 0;

static uint64_t read_be(const uint8_t* p, unsigned n) {
    uint64_t v = 0;
    while (n--) v = (v << 8) | *p++;
    return v;
}

int main(int argc, char** argv) {
    constexpr uint32_t begin = 0x340000, size = 0x8CE208;
    constexpr uint32_t toc = 0x359220;
    constexpr uint32_t constants[] = {
        0x35A944, 0x35AAA4, 0x35AE6C, 0x35AF3C, 0x35B390, 0x3B56A8
    };
#ifdef _WIN32
    vm_base = static_cast<uint8_t*>(VirtualAlloc(
        nullptr, VM_TOTAL_SIZE, MEM_RESERVE, PAGE_NOACCESS));
#else
    void* mapping = mmap(nullptr, VM_TOTAL_SIZE, PROT_NONE,
                         MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    if (mapping != MAP_FAILED) vm_base = static_cast<uint8_t*>(mapping);
#endif
    if (!vm_base || vm_commit(begin, size) != CELL_OK) return 2;
    if (argc > 1) {
        // Optional local NPUA80523 ELF: compare its actual data segment too.
        std::ifstream input(argv[1], std::ios::binary);
        std::vector<uint8_t> elf((std::istreambuf_iterator<char>(input)), {});
        if (elf.size() < 64 || memcmp(elf.data(), "\x7f" "ELF\x02\x02", 6))
            return 2;
        uint64_t ph = read_be(&elf[32], 8);
        uint64_t stride = read_be(&elf[54], 2), count = read_be(&elf[56], 2);
        bool loaded = false;
        for (uint64_t i = 0; i < count; ++i) {
            uint64_t at = ph + i * stride;
            if (stride < 56 || at > elf.size() || elf.size() - at < 56) return 2;
            const uint8_t* p = &elf[at];
            if (read_be(p, 4) != 1 || read_be(p + 16, 8) != begin) continue;
            uint64_t off = read_be(p + 8, 8), bytes = read_be(p + 32, 8);
            if (bytes > size || off > elf.size() || bytes > elf.size() - off)
                return 2;
            memcpy(vm_base + begin, &elf[off], static_cast<size_t>(bytes));
            loaded = true;
        }
        if (!loaded) return 2;
    } else {
        memset(vm_base + begin, 0xA5, size);
        for (uint32_t slot = 0x3423E0; slot <= 0x342910; slot += 4)
            import_write32(slot, 0x25FB00); // Real lazy-binding code pointer.
        for (uint32_t addr : constants) import_write32(addr, 0x39800000);
    }
    for (uint32_t addr : constants) {
        if (read_be(vm_base + addr, 4) != 0x39800000) {
            fprintf(stderr, "Wrong fixture constant at %08X\n", addr);
            return 2;
        }
    }
    // This import is outside the explicit module loops: test zero resolution.
    import_write32(0x3423E0, 0);
    std::vector<uint8_t> before(vm_base + begin, vm_base + begin + size);
    resolve_all_imports(toc);
    unsigned failures = 0, changed_bytes = 0, imports = 0, preserved = 0;
    for (uint32_t i = 0; i < size; ++i) {
        uint32_t addr = begin + i;
        if (addr >= 0x3423E0 && addr < 0x342914) continue;
        if (before[i] != vm_base[addr]) ++changed_bytes;
    }
    if (changed_bytes) {
        fprintf(stderr, "FAIL: %u non-import data bytes overwritten\n", changed_bytes);
        ++failures;
    }
    for (uint32_t addr : constants) {
        uint32_t bits = static_cast<uint32_t>(read_be(vm_base + addr, 4));
        float value;
        memcpy(&value, &bits, 4);
        if (value * 4096.0f != 1.0f) {
            fprintf(stderr, "FAIL: float constant %08X became %08X\n", addr, bits);
            ++failures;
        }
    }
    for (uint32_t slot = 0x3423E0; slot <= 0x342910; slot += 4) {
        uint32_t opd = static_cast<uint32_t>(read_be(vm_base + slot, 4));
        // The original table interval also contains valid data pointers/TOCs.
        // The resolver must leave these already-populated words alone.
        if (opd == read_be(&before[slot - begin], 4)) {
            if (opd == 0 || (opd >= 0x10000 && opd < 0x340000)) {
                fprintf(stderr, "FAIL: lazy import %08X was not resolved\n", slot);
                ++failures;
            }
            ++preserved;
            continue;
        }
        if (opd < HLE_OPD_BASE || opd + 8 > g_next_opd || (opd & 7) ||
            read_be(vm_base + opd + 4, 4) != toc) {
            fprintf(stderr, "FAIL: invalid resolved import %08X -> %08X\n", slot, opd);
            ++failures;
        }
        ++imports;
    }
    for (int i = 0; i < g_generic_count; ++i) {
        uint32_t opd = static_cast<uint32_t>(read_be(vm_base + g_generic_slot[i], 4));
        if (read_be(vm_base + opd, 4) != TJ_GENERIC_ADDR(i)) ++failures;
    }
    printf("Import-data regression: %u constants, %u imports, %u preserved words, %d generic routes, "
           "%u failures (%s)\n", unsigned(sizeof(constants) / sizeof(constants[0])),
           imports, preserved, g_generic_count, failures, argc > 1 ? "original ELF" : "synthetic");
    vm_shutdown();
    return failures ? 1 : 0;
}
