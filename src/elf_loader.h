/**
 * Tokyo Jungle Recompiled — ELF Loader
 *
 * Loads the decrypted EBOOT.ELF segments into the ps3recomp virtual memory
 * at their correct PS3 virtual addresses.
 */

#pragma once

#include <cstdint>
#include <cstdio>
#include <cstring>

extern "C" {
#include "ppu_context.h"
#include "vm.h"
}

extern uint8_t* vm_base;

// ELF64 structures (big-endian)
#pragma pack(push, 1)

struct Elf64_Ehdr {
    uint8_t  e_ident[16];
    uint16_t e_type;
    uint16_t e_machine;
    uint32_t e_version;
    uint64_t e_entry;
    uint64_t e_phoff;
    uint64_t e_shoff;
    uint32_t e_flags;
    uint16_t e_ehsize;
    uint16_t e_phentsize;
    uint16_t e_phnum;
    uint16_t e_shentsize;
    uint16_t e_shnum;
    uint16_t e_shstrndx;
};

struct Elf64_Phdr {
    uint32_t p_type;
    uint32_t p_flags;
    uint64_t p_offset;
    uint64_t p_vaddr;
    uint64_t p_paddr;
    uint64_t p_filesz;
    uint64_t p_memsz;
    uint64_t p_align;
};

#pragma pack(pop)

// Big-endian read helpers
static inline uint16_t be16(const void* p) {
    const uint8_t* b = (const uint8_t*)p;
    return (uint16_t)(b[0] << 8 | b[1]);
}
static inline uint32_t be32(const void* p) {
    const uint8_t* b = (const uint8_t*)p;
    return (uint32_t)(b[0] << 24 | b[1] << 16 | b[2] << 8 | b[3]);
}
static inline uint64_t be64(const void* p) {
    const uint8_t* b = (const uint8_t*)p;
    return ((uint64_t)be32(b) << 32) | be32(b + 4);
}

struct ElfLoadResult {
    uint64_t entry_point;     // Entry from ELF header (function descriptor addr)
    uint64_t func_addr;       // Actual code entry (read from descriptor)
    uint64_t toc;             // TOC pointer (read from descriptor)
    uint32_t code_base;       // Start of code segment
    uint32_t code_size;       // Size of code segment
    uint32_t data_base;       // Start of data segment
    uint32_t data_size;       // Size of data segment (file)
    uint32_t bss_size;        // Size of BSS (memsz - filesz)
    bool success;
};

/**
 * Load an ELF file's segments into ps3recomp virtual memory.
 */
static inline ElfLoadResult load_elf_into_vm(const char* elf_path)
{
    ElfLoadResult result = {};

    FILE* fp = fopen(elf_path, "rb");
    if (!fp) {
        fprintf(stderr, "[ELF] Cannot open: %s\n", elf_path);
        return result;
    }

    // Get file size
    fseek(fp, 0, SEEK_END);
    long file_size = ftell(fp);
    fseek(fp, 0, SEEK_SET);

    // Read entire file
    uint8_t* elf_data = (uint8_t*)malloc(file_size);
    if (!elf_data) {
        fprintf(stderr, "[ELF] Cannot allocate %ld bytes\n", file_size);
        fclose(fp);
        return result;
    }
    fread(elf_data, 1, file_size, fp);
    fclose(fp);

    // Verify ELF magic
    if (memcmp(elf_data, "\x7f""ELF", 4) != 0) {
        fprintf(stderr, "[ELF] Invalid ELF magic\n");
        free(elf_data);
        return result;
    }

    // Parse ELF header (big-endian)
    const Elf64_Ehdr* ehdr = (const Elf64_Ehdr*)elf_data;
    uint64_t entry   = be64(&ehdr->e_entry);
    uint64_t phoff   = be64(&ehdr->e_phoff);
    uint16_t phnum   = be16(&ehdr->e_phnum);
    uint16_t phentsz = be16(&ehdr->e_phentsize);

    printf("[ELF] Entry: 0x%08llX, %d program headers\n",
           (unsigned long long)entry, phnum);

    result.entry_point = entry;

    // Load program headers
    int segments_loaded = 0;
    for (int i = 0; i < phnum; i++) {
        const uint8_t* ph_raw = elf_data + phoff + i * phentsz;
        uint32_t p_type  = be32(ph_raw + 0);
        uint32_t p_flags = be32(ph_raw + 4);
        uint64_t p_offset = be64(ph_raw + 8);
        uint64_t p_vaddr  = be64(ph_raw + 16);
        uint64_t p_filesz = be64(ph_raw + 32);
        uint64_t p_memsz  = be64(ph_raw + 40);

        // Only load PT_LOAD segments with data
        if (p_type != 1) continue;  // PT_LOAD = 1
        if (p_memsz == 0) continue;

        uint32_t vaddr = (uint32_t)p_vaddr;
        uint32_t filesz = (uint32_t)p_filesz;
        uint32_t memsz = (uint32_t)p_memsz;

        // Commit VM pages for this segment
        uint32_t aligned_addr = vaddr & ~(VM_PAGE_SIZE - 1);
        uint32_t aligned_end = VM_ALIGN_UP(vaddr + memsz, VM_PAGE_SIZE);
        uint32_t commit_size = aligned_end - aligned_addr;

        int32_t rc = vm_commit(aligned_addr, commit_size);
        if (rc != 0) {
            fprintf(stderr, "[ELF] vm_commit(0x%08X, 0x%X) failed: 0x%08X\n",
                    aligned_addr, commit_size, (unsigned)rc);
            // Non-fatal: memory might already be committed
        }

        // Copy file data into VM
        if (filesz > 0 && p_offset + filesz <= (uint64_t)file_size) {
            memcpy(vm_base + vaddr, elf_data + p_offset, filesz);
        }

        // Zero BSS (memsz > filesz)
        if (memsz > filesz) {
            memset(vm_base + vaddr + filesz, 0, memsz - filesz);
        }

        const char* flags_str = (p_flags & 1) ? "X" : " ";
        const char* write_str = (p_flags & 2) ? "W" : " ";
        const char* read_str  = (p_flags & 4) ? "R" : " ";

        printf("[ELF] Loaded segment %d: vaddr=0x%08X filesz=0x%X memsz=0x%X [%s%s%s]\n",
               i, vaddr, filesz, memsz, read_str, write_str, flags_str);

        // Track code vs data segments
        if (p_flags & 1) {  // Executable
            result.code_base = vaddr;
            result.code_size = filesz;
        } else if (p_flags & 2) {  // Writable (data)
            result.data_base = vaddr;
            result.data_size = filesz;
            result.bss_size = memsz - filesz;
        }

        segments_loaded++;
    }

    printf("[ELF] Loaded %d segments\n", segments_loaded);

    // Read the entry point function descriptor (OPD format)
    // PS3 PPC64 uses OPD: each descriptor is {func_addr: u32, toc: u32}
    if (entry >= result.data_base && entry < result.data_base + result.data_size + result.bss_size) {
        uint8_t* desc = vm_base + (uint32_t)entry;
        // OPD is two big-endian 32-bit values
        result.func_addr = be32(desc);
        result.toc = be32(desc + 4);
        printf("[ELF] Function descriptor (OPD) at 0x%08llX:\n", (unsigned long long)entry);
        printf("[ELF]   Code entry: 0x%08X\n", (uint32_t)result.func_addr);
        printf("[ELF]   TOC:        0x%08X\n", (uint32_t)result.toc);
    } else {
        result.func_addr = entry;
        result.toc = 0;
        printf("[ELF] Direct entry at 0x%08llX\n", (unsigned long long)entry);
    }

    result.success = true;
    free(elf_data);
    return result;
}
