#include "sys_memory.h"

#include <stdint.h>
#include <stdio.h>
#include <string.h>

uint8_t* vm_base = NULL;
uint32_t ppu_vm_size = 0;

static uint32_t read_be32(uint32_t addr)
{
    const uint8_t* p = vm_base + addr;
    return ((uint32_t)p[0] << 24) | ((uint32_t)p[1] << 16) |
           ((uint32_t)p[2] << 8) | p[3];
}

static void write_be32_test(uint32_t addr, uint32_t value)
{
    uint8_t* p = vm_base + addr;
    p[0] = (uint8_t)(value >> 24);
    p[1] = (uint8_t)(value >> 16);
    p[2] = (uint8_t)(value >> 8);
    p[3] = (uint8_t)value;
}

static int expect_u32(const char* name, uint32_t actual, uint32_t expected)
{
    if (actual == expected)
        return 0;
    fprintf(stderr, "FAIL: %s actual=0x%08X expected=0x%08X\n",
            name, actual, expected);
    return 1;
}

int main(void)
{
    const uint32_t out_page = 0x00010000u;
    const size_t reserve_size = 0x50000000u;
#ifdef _WIN32
    vm_base = (uint8_t*)VirtualAlloc(NULL, reserve_size, MEM_RESERVE, PAGE_NOACCESS);
    if (!vm_base || !VirtualAlloc(vm_base + out_page, VM_PAGE_SIZE,
                                  MEM_COMMIT, PAGE_READWRITE)) {
        fputs("FAIL: test guest VM reservation\n", stderr);
        return 1;
    }
#else
    vm_base = (uint8_t*)mmap(NULL, reserve_size, PROT_NONE,
                             MAP_PRIVATE | MAP_ANONYMOUS | MAP_NORESERVE, -1, 0);
    if (vm_base == MAP_FAILED ||
        mprotect(vm_base + out_page, VM_PAGE_SIZE, PROT_READ | PROT_WRITE) != 0) {
        fputs("FAIL: test guest VM reservation\n", stderr);
        return 1;
    }
#endif
    ppu_vm_size = (uint32_t)reserve_size;

    lv2_syscall_table table;
    sys_memory_init(&table);
    int failed = 0;
    failed |= expect_u32("syscall 350 registered",
                         table.handlers[SYS_MEMORY_ALLOCATE_FROM_CONTAINER] !=
                             lv2_syscall_unimplemented,
                         1);

    ppu_context ctx;
    ppu_context_init(&ctx);
    ctx.gpr[3] = out_page;
    ctx.gpr[4] = 0x00200000u;
    failed |= expect_u32("create container",
                         (uint32_t)sys_memory_container_create(&ctx), CELL_OK);
    uint32_t cid = read_be32(out_page);
    failed |= expect_u32("container id", cid, 1);

    write_be32_test(out_page + 4, 0xDEADBEEFu);
    ppu_context_init(&ctx);
    ctx.gpr[3] = 0x00100000u;
    ctx.gpr[4] = cid;
    ctx.gpr[5] = SYS_MEMORY_PAGE_SIZE_1M;
    ctx.gpr[6] = out_page + 4;
    failed |= expect_u32("allocate from container",
                         (uint32_t)sys_memory_allocate_from_container(&ctx), CELL_OK);
    uint32_t alloc_addr = read_be32(out_page + 4);
    failed |= expect_u32("allocated address", alloc_addr, 0x40000000u);
    failed |= expect_u32("container used", g_sys_mem_containers[0].used_size,
                         0x00100000u);
    failed |= expect_u32("allocation owner", (uint32_t)g_sys_mem_allocs[0].container_id,
                         cid);

    ppu_context_init(&ctx);
    ctx.gpr[3] = alloc_addr;
    failed |= expect_u32("free container allocation",
                         (uint32_t)sys_memory_free(&ctx), CELL_OK);
    failed |= expect_u32("container used after free",
                         g_sys_mem_containers[0].used_size, 0);

    write_be32_test(out_page + 4, 0xA5A5A5A5u);
    ppu_context_init(&ctx);
    ctx.gpr[3] = 0x00300000u;
    ctx.gpr[4] = cid;
    ctx.gpr[5] = SYS_MEMORY_PAGE_SIZE_1M;
    ctx.gpr[6] = out_page + 4;
    failed |= expect_u32("over-capacity result",
                         (uint32_t)sys_memory_allocate_from_container(&ctx), CELL_ENOMEM);
    failed |= expect_u32("failure preserves output", read_be32(out_page + 4),
                         0xA5A5A5A5u);

#ifdef _WIN32
    VirtualFree(vm_base, 0, MEM_RELEASE);
#else
    munmap(vm_base, reserve_size);
#endif
    vm_base = NULL;
    ppu_vm_size = 0;

    if (failed)
        return 1;
    puts("PASS: syscall 350 allocates, accounts, returns, and frees container memory");
    return 0;
}
