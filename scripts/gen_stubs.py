#!/usr/bin/env python3
"""
Post-lift processing for Tokyo Jungle recompiled code.

After running the lifter (ppu_lifter.py), this script:
1. Patches ppu_recomp.h to use recomp_bridge.h
2. Fixes MSVC-incompatible __int128 mulhd patterns
3. Replaces stack-based TOC restores with constant TOC
4. Fixes bctr tail calls to use ppc_indirect_call()
5. Replaces bctrl indirect calls with ppc_indirect_call()
6. Removes CRT functions that are overridden in ppu_stubs.c
7. Generates dispatch_table.c
8. Generates ppu_stubs.c with manual CRT overrides

Usage: python scripts/gen_stubs.py [--toc 0x00359220]
"""
import re
import sys
import argparse

TOC_VALUE = 0x00359220

# Functions overridden in ppu_stubs.c (removed from ppu_recomp.c)
CRT_OVERRIDES = [
    'func_00010230',  # _start
    'func_0025FC00',  # __sys_initialize
    'func_0025FC20',  # __sys_init_tls
    'func_002448EC',  # __init_section
    'func_0025FBC0',  # __sys_init_prx
    'func_0025FB60',  # __sys_init_heap
    'func_0025FC40',  # __sys_init_spu
    'func_0024DE14',  # CRT stdio init
    'func_00249298',  # CRT memory region init
    'func_00248C8C',  # CRT exception init
    'func_00010200',  # __crt_atexit
]


def patch_header():
    """Replace lifter's ppu_context definition with recomp_bridge.h include."""
    with open('generated/ppu_recomp.h', 'r') as f:
        content = f.read()

    m = re.search(r'^void func_', content, re.MULTILINE)
    if not m:
        print("ERROR: No function declarations found in ppu_recomp.h")
        return
    func_decls = content[m.start():]
    content = '/* Patched header */\n#pragma once\n#include "recomp_bridge.h"\n\n' + func_decls

    with open('generated/ppu_recomp.h', 'w') as f:
        f.write(content)
    print(f"  Patched ppu_recomp.h ({func_decls.count(chr(10))} declarations)")


def patch_recomp():
    """Apply all fixes to ppu_recomp.c."""
    print("Reading ppu_recomp.c...")
    with open('generated/ppu_recomp.c', 'r') as f:
        content = f.read()

    # 1. Fix __int128 mulhd patterns (MSVC doesn't support __int128)
    signed_pattern = (r'\{ __int128 r = \(__int128\)\(int64_t\)(ctx->gpr\[\d+\]) '
                      r'\* \(__int128\)\(int64_t\)(ctx->gpr\[\d+\]); '
                      r'(ctx->gpr\[\d+\]) = \(uint64_t\)\(r >> 64\); \}')
    def signed_replace(m):
        return f'{{ {m.group(3)} = (uint64_t)ppc_mulhd((int64_t){m.group(1)}, (int64_t){m.group(2)}); }}'
    content, n1 = re.subn(signed_pattern, signed_replace, content)

    unsigned_pattern = (r'\{ unsigned __int128 r = \(unsigned __int128\)(ctx->gpr\[\d+\]) '
                        r'\* \(unsigned __int128\)(ctx->gpr\[\d+\]); '
                        r'(ctx->gpr\[\d+\]) = \(uint64_t\)\(r >> 64\); \}')
    def unsigned_replace(m):
        return f'{{ {m.group(3)} = ppc_mulhdu({m.group(1)}, {m.group(2)}); }}'
    content, n2 = re.subn(unsigned_pattern, unsigned_replace, content)
    print(f"  Fixed {n1} signed + {n2} unsigned __int128 mulhd patterns")

    # 2. Replace stack-based TOC restores with constant TOC
    old_toc = 'ctx->gpr[2] = vm_read64(ctx->gpr[1] + 0x28);'
    new_toc = f'ctx->gpr[2] = 0x{TOC_VALUE:08X}; /* TOC restore (constant for single-module recomp) */'
    n3 = content.count(old_toc)
    content = content.replace(old_toc, new_toc)
    print(f"  Replaced {n3} TOC restores with constant 0x{TOC_VALUE:08X}")

    # 3. Fix bctr tail calls
    old_bctr = '/* bctr -- indirect branch via CTR */;'
    new_bctr = 'ppc_indirect_call(ctx); return; /* bctr */'
    n4 = content.count(old_bctr)
    content = content.replace(old_bctr, new_bctr)
    print(f"  Fixed {n4} bctr tail calls")

    # 4. Fix bctrl indirect calls
    old_bctrl = '((void(*)(ppu_context*))ctx->ctr)(ctx);'
    new_bctrl = 'ppc_indirect_call(ctx);'
    n5 = content.count(old_bctrl)
    content = content.replace(old_bctrl, new_bctrl)
    print(f"  Fixed {n5} bctrl indirect calls")

    # 5. Remove CRT override functions
    removed = 0
    for func_name in CRT_OVERRIDES:
        pattern = r'^void ' + func_name + r'\(ppu_context\* ctx\) \{.*?\n\}\n'
        match = re.search(pattern, content, re.MULTILINE | re.DOTALL)
        if match:
            content = content[:match.start()] + f'/* {func_name} removed — overridden in ppu_stubs.c */\n' + content[match.end():]
            removed += 1
    print(f"  Removed {removed} CRT functions (overridden in ppu_stubs.c)")

    print("Writing ppu_recomp.c...")
    with open('generated/ppu_recomp.c', 'w') as f:
        f.write(content)


def gen_dispatch_table():
    """Generate dispatch_table.c from the defined + stub functions."""
    with open('generated/ppu_recomp.c') as f:
        content = f.read()
    defined = set(re.findall(r'^void (func_[0-9A-Fa-f]+)\(ppu_context\*', content, re.MULTILINE))
    called = set(re.findall(r'\b(func_[0-9A-Fa-f]+)\(ctx\)', content))

    # Add CRT overrides (defined in ppu_stubs.c)
    for func_name in CRT_OVERRIDES:
        defined.add(func_name)

    all_funcs = sorted(defined | called)

    lines = ['#include "ppu_recomp.h"', '']

    # Forward declarations for stub-only functions
    stubs_only = sorted((called | set(CRT_OVERRIDES)) - (defined - set(CRT_OVERRIDES)))
    for f in stubs_only:
        if f not in defined:
            lines.append(f'void {f}(ppu_context* ctx);')
    if stubs_only:
        lines.append('')

    lines.append(f'const int g_dispatch_table_size = {len(all_funcs)};')
    lines.append(f'const dispatch_entry_t g_dispatch_table[{len(all_funcs)}] = {{')
    for f in all_funcs:
        addr = int(f.replace('func_', ''), 16)
        lines.append(f'    {{ 0x{addr:08X}, {f} }},')
    lines.append('};')

    with open('generated/dispatch_table.c', 'w') as f:
        f.write('\n'.join(lines) + '\n')
    print(f"  Generated dispatch_table.c ({len(all_funcs)} entries)")


def gen_stubs():
    """Generate ppu_stubs.c with manual CRT overrides."""
    stub_content = r'''/* Manual stubs — functions that need host-side overrides.
 * These are removed from ppu_recomp.c to avoid duplicate symbols.
 * The real PS3 CRT functions make syscalls and access kernel structures
 * that would hang or crash — we replace them with safe HLE stubs.
 */
#include "ppu_recomp.h"
#include <stdio.h>

/* ========================================================================
 * CRT _start — sets up argc/argv/envp then calls __libc_start
 * ====================================================================== */
void func_00010230(ppu_context* ctx) {
    printf("[CRT] _start (0x00010230)\n");
    uint32_t argv_area = 0x00F00000;
    uint32_t envp_area = 0x00F00100;
    uint32_t progname_addr = 0x00F00200;
    const char* progname = "EBOOT.BIN";
    for (int i = 0; progname[i]; i++)
        vm_write8(progname_addr + i, (uint8_t)progname[i]);
    vm_write8(progname_addr + 9, 0);
    vm_write32(argv_area, progname_addr);
    vm_write32(argv_area + 4, 0);
    vm_write32(envp_area, 0);
    ctx->gpr[3] = 1;
    ctx->gpr[4] = argv_area;
    ctx->gpr[5] = envp_area;
    ctx->gpr[2] = 0x003428F0;
    ctx->gpr[2] = (uint64_t)vm_read32((uint32_t)(ctx->gpr[2] + 4));
    uint64_t old_sp = ctx->gpr[1];
    ctx->gpr[1] -= 0x70;
    vm_write64(ctx->gpr[1], old_sp);
    vm_write64(ctx->gpr[1] + 0x28, ctx->gpr[2]);
    ctx->gpr[14] = 0;
    vm_write64(ctx->gpr[1], 0);
    ctx->lr = 0x10250;
    printf("[CRT] _start: argc=%llu argv=0x%08X envp=0x%08X TOC=0x%08X\n",
           (unsigned long long)ctx->gpr[3],
           (uint32_t)ctx->gpr[4], (uint32_t)ctx->gpr[5],
           (uint32_t)ctx->gpr[2]);
    func_00010354(ctx);
}

/* ========================================================================
 * CRT init stubs
 * ====================================================================== */
void func_0025FC00(ppu_context* ctx) {
    printf("[CRT] __sys_initialize(r3=0x%08X, r4=0x%08X, r5=0x%08X)\n",
           (uint32_t)ctx->gpr[3], (uint32_t)ctx->gpr[4], (uint32_t)ctx->gpr[5]);
    ctx->gpr[3] = 0;
}
void func_0025FC20(ppu_context* ctx) {
    printf("[CRT] __sys_init_tls(tls_image=0x%08X, tls_size=0x%08X)\n",
           (uint32_t)ctx->gpr[3], (uint32_t)ctx->gpr[4]);
}
void func_002448EC(ppu_context* ctx) {
    printf("[CRT] __init_section(r3=0x%08X, r4=0x%08X) — static constructors skipped\n",
           (uint32_t)ctx->gpr[3], (uint32_t)ctx->gpr[4]);
}
void func_0025FBC0(ppu_context* ctx) {
    printf("[CRT] __sys_init_prx() — no PRX loading in static recomp\n");
    ctx->gpr[3] = 0;
}
void func_0025FB60(ppu_context* ctx) {
    printf("[CRT] __sys_init_heap(heap_size=0x%08X) — runtime bump allocator at 0x02000000\n",
           (uint32_t)ctx->gpr[3]);
    ctx->gpr[3] = 0;
}
void func_0025FC40(ppu_context* ctx) {
    printf("[CRT] __sys_init_spu() — no SPU needed yet\n");
    ctx->gpr[3] = 0;
}
void func_0024DE14(ppu_context* ctx) {
    printf("[CRT] func_0024DE14 (stdio init) — skipped\n");
    ctx->gpr[3] = 0;
}
void func_00249298(ppu_context* ctx) {
    printf("[CRT] func_00249298 (memory region init) — skipped\n");
    ctx->gpr[3] = 0;
}
void func_00248C8C(ppu_context* ctx) {
    printf("[CRT] func_00248C8C (exception init) — skipped\n");
    ctx->gpr[3] = 0;
}
void func_00010200(ppu_context* ctx) {
    printf("[CRT] __crt_atexit — skipped\n");
    ctx->gpr[3] = 0;
}
'''
    with open('generated/ppu_stubs.c', 'w') as f:
        f.write(stub_content)
    print(f"  Generated ppu_stubs.c ({len(CRT_OVERRIDES)} overrides)")


def main():
    parser = argparse.ArgumentParser(description='Post-lift processing for Tokyo Jungle')
    parser.add_argument('--toc', default=f'0x{TOC_VALUE:08X}', help='TOC value (default: 0x00359220)')
    args = parser.parse_args()

    global TOC_VALUE
    TOC_VALUE = int(args.toc, 16)

    print("=== Tokyo Jungle Post-Lift Processing ===\n")

    print("[1/5] Patching header...")
    patch_header()

    print("[2/5] Patching ppu_recomp.c...")
    patch_recomp()

    print("[3/5] Generating dispatch table...")
    gen_dispatch_table()

    print("[4/5] Generating stubs...")
    gen_stubs()

    print("\n=== Done! Run: cmake --build build --config Release ===")


if __name__ == '__main__':
    main()
