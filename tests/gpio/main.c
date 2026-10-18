#include "sys_gpio.h"
#include "vm.h"

#include <stdint.h>
#include <stdio.h>
#include <string.h>

uint8_t* vm_base = NULL;
uint32_t ppu_vm_size = 0;

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
    uint8_t guest[0x200];
    memset(guest, 0xA5, sizeof(guest));
    vm_base = guest;
    ppu_vm_size = (uint32_t)sizeof(guest);

    ppu_context ctx;
    ppu_context_init(&ctx);
    ctx.gpr[3] = SYS_GPIO_DIP_SWITCH_DEVICE_ID;
    ctx.gpr[4] = 0x80;

    int failed = 0;
    failed |= expect_u32("DIP read result", (uint32_t)sys_gpio_get(&ctx), CELL_OK);
    for (uint32_t i = 0; i < 8; i++)
        failed |= expect_u32("DIP output byte", guest[0x80 + i], 0);

    ppu_context_init(&ctx);
    ctx.gpr[3] = 99;
    ctx.gpr[4] = 0x90;
    failed |= expect_u32("unknown device", (uint32_t)sys_gpio_get(&ctx), CELL_ESRCH);
    failed |= expect_u32("unknown output untouched", guest[0x90], 0xA5);

    ppu_context_init(&ctx);
    ctx.gpr[3] = SYS_GPIO_DIP_SWITCH_DEVICE_ID;
    ctx.gpr[4] = 0;
    failed |= expect_u32("null output", (uint32_t)sys_gpio_get(&ctx), CELL_EFAULT);

    lv2_syscall_table table;
    lv2_syscall_table_init(&table);
    sys_gpio_init(&table);
    failed |= expect_u32("syscall 631 registered",
                         table.handlers[SYS_GPIO_GET] != lv2_syscall_unimplemented, 1);

    vm_base = NULL;
    ppu_vm_size = 0;
    if (failed)
        return 1;
    puts("PASS: syscall 631 zeros retail DIP output and validates errors");
    return 0;
}
