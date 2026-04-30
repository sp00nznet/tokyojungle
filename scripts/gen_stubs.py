#!/usr/bin/env python3
"""
Post-lift processing for Tokyo Jungle recompiled code.

ps3recomp v0.5.1+ folds many former post-lift fix-ups into the lifter
itself: bctr/bctrl emit ps3_indirect_call directly, the prelude uses
_mul128/_umul128 on MSVC instead of __int128, and DRAIN_TRAMPOLINE is
auto-inserted after every direct call.

What this script does NOW:
  1. Patch ppu_recomp.h to include recomp_bridge.h (TJ's bridge has the
     vm_read/write helpers, ppu_context, etc.)
  2. Replace stack-based TOC restores with the constant TOC (TJ-only
     optimization since we ship a single module)
  3. Remove CRT functions overridden in ppu_stubs.c
  4. Rename ppu_recomp.c to ppu_recomp.cpp (the new lifter emits
     unconditional `extern "C"` in the source preamble)
  5. Generate dispatch_table.c and ppu_stubs.c

Usage: python scripts/gen_stubs.py [--toc 0x00359220]
"""
import os
import re
import argparse

TOC_VALUE = 0x00359220

# Functions overridden in ppu_stubs.c (removed from ppu_recomp.cpp)
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


def regen_header(src_path):
    """Regenerate ppu_recomp.h from scratch.

    The new lifter only declares functions that have bodies it emitted, but
    its trampoline emit references many addresses (mid-fragment entries) it
    never wrapped. We scan the .cpp directly for every `func_*` symbol used
    and declare them all, so the missing ones can be defined in
    missing_stubs.cpp.

    Idempotent.
    """
    with open(src_path, 'r') as f:
        cpp = f.read()
    referenced = set(re.findall(r'\b(func_[0-9A-Fa-f]+)\b', cpp))
    referenced |= set(CRT_OVERRIDES)

    decls = '\n'.join(f'void {fn}(ppu_context* ctx);'
                      for fn in sorted(referenced))

    content = ('/* Patched header */\n'
               '#pragma once\n'
               '#include "recomp_bridge.h"\n\n'
               '#ifdef __cplusplus\n'
               'extern "C" {\n'
               '#endif\n\n'
               + decls
               + '\n\n#ifdef __cplusplus\n'
               '}\n'
               '#endif\n')

    with open('generated/ppu_recomp.h', 'w') as f:
        f.write(content)
    print(f"  Regenerated ppu_recomp.h ({len(referenced)} declarations)")


def rename_source_to_cpp():
    """Rename ppu_recomp.c -> ppu_recomp.cpp.

    The new lifter emits unconditional `extern "C"` in the source preamble,
    which only parses as C++. Renaming forces MSVC/Clang to compile as C++.
    """
    src_c = 'generated/ppu_recomp.c'
    src_cpp = 'generated/ppu_recomp.cpp'
    if os.path.isfile(src_c):
        if os.path.isfile(src_cpp):
            os.remove(src_cpp)
        os.rename(src_c, src_cpp)
        print(f"  Renamed ppu_recomp.c -> ppu_recomp.cpp")
        return src_cpp
    if os.path.isfile(src_cpp):
        print(f"  ppu_recomp.cpp already present")
        return src_cpp
    raise FileNotFoundError("No ppu_recomp.c or ppu_recomp.cpp in generated/")


def patch_recomp(path):
    """Apply remaining fix-ups to the lifted source."""
    print(f"Reading {path}...")
    with open(path, 'r') as f:
        content = f.read()

    # 0. Strip the lifter's preamble helpers (__builtin_clz, ppc_mulhd,
    #    ppc_mulhdu). recomp_bridge.h already provides static-inline
    #    versions; the lifter's non-static versions cause duplicate-body
    #    errors and overload ambiguity in MSVC.
    preamble_pat = (r'/\* MSVC compatibility helpers \*/\n#ifdef _MSC_VER\n'
                    r'.*?#endif\n')
    new_content, n_strip = re.subn(preamble_pat,
                                    '/* MSVC helpers stripped — provided by recomp_bridge.h */\n',
                                    content, count=1, flags=re.DOTALL)
    if n_strip:
        content = new_content
    print(f"  Stripped {n_strip} lifter preamble helper block(s)")

    # 0b. Body overrides: replace specific lifted bodies that are known to
    # crash during early game init (vsnprintf varargs, NULL vtable in object
    # constructors, recursive registration loops). All return CELL_OK.
    # Restoring these from no-op-return-0 stubs got the old pipeline to the
    # main-loop-vsync milestone; they need to be re-applied after every
    # re-lift since the lifter overwrites ppu_recomp.cpp.
    body_overrides = {
        'func_001F3D78': 'subsystem registration loop (vsnprintf crash in ctors)',
        'func_0023D1FC': 'memory pool init (NULL fn ptr in allocator vtable)',
        'func_00245AC0': 'sprintf dispatcher (vsnprintf varargs crash)',
        # Wait-for-state polls — busy-loop on sys_timer_usleep + memory read
        # waiting for an LV2 object state field to flip. Without a real LV2
        # kernel updating the field, the loop never exits; short-circuit to
        # success.
        'func_001F8C4C': 'lv2 wait-for-state spin (sys_timer_usleep + poll)',
    }
    n_overridden = 0
    n_already = 0
    for fn, why in body_overrides.items():
        # Idempotency: skip if the function is already a single-line override.
        # Otherwise the multi-line regex (which only terminates on `\n}\n`)
        # would extend past the one-liner and eat the next function body.
        if re.search(r'^void ' + fn + r'\(.*OVERRIDE:.*\}$',
                     content, re.MULTILINE):
            n_already += 1
            continue
        pattern = r'^void ' + fn + r'\(ppu_context\* ctx\) \{.*?\n\}\n'
        replacement = (f'void {fn}(ppu_context* ctx) {{ '
                       f'/* OVERRIDE: {why} */ '
                       f'ctx->gpr[3] = 0; }}\n')
        new_content, n = re.subn(pattern, replacement, content,
                                  count=1, flags=re.MULTILINE | re.DOTALL)
        if n:
            content = new_content
            n_overridden += 1
    print(f"  Applied {n_overridden}/{len(body_overrides)} body overrides "
          f"({n_already} already overridden)")

    # 1. Replace stack-based TOC restores with constant TOC. TJ ships a
    #    single module, so r2 is invariant across calls and the lifter's
    #    `ld r2,0x28(r1)` reads can short-circuit to the known value.
    old_toc = 'ctx->gpr[2] = vm_read64(ctx->gpr[1] + 0x28);'
    new_toc = (f'ctx->gpr[2] = 0x{TOC_VALUE:08X}; '
               '/* TOC restore (constant for single-module recomp) */')
    n_toc = content.count(old_toc)
    content = content.replace(old_toc, new_toc)
    print(f"  Replaced {n_toc} TOC restores with constant 0x{TOC_VALUE:08X}")

    # 2. Remove CRT override functions (defined in ppu_stubs.c instead)
    removed = 0
    for func_name in CRT_OVERRIDES:
        pattern = r'^void ' + func_name + r'\(ppu_context\* ctx\) \{.*?\n\}\n'
        match = re.search(pattern, content, re.MULTILINE | re.DOTALL)
        if match:
            content = (content[:match.start()]
                       + f'/* {func_name} removed -- overridden in ppu_stubs.c */\n'
                       + content[match.end():])
            removed += 1
    print(f"  Removed {removed} CRT functions (overridden in ppu_stubs.c)")

    print(f"Writing {path}...")
    with open(path, 'w') as f:
        f.write(content)


def gen_dispatch_table(src_path):
    """Generate dispatch_table.c from the defined + called functions."""
    with open(src_path) as f:
        content = f.read()
    defined = set(re.findall(r'^void (func_[0-9A-Fa-f]+)\(ppu_context\*',
                             content, re.MULTILINE))
    called = set(re.findall(r'\b(func_[0-9A-Fa-f]+)\(ctx\)', content))

    # Add CRT overrides (defined in ppu_stubs.c)
    for func_name in CRT_OVERRIDES:
        defined.add(func_name)

    all_funcs = sorted(defined | called)

    lines = ['#include "ppu_recomp.h"', '']

    # Forward declarations for stub-only functions
    stubs_only = sorted((called | set(CRT_OVERRIDES))
                        - (defined - set(CRT_OVERRIDES)))
    for f in stubs_only:
        if f not in defined:
            lines.append(f'void {f}(ppu_context* ctx);')
    if stubs_only:
        lines.append('')

    lines.append(f'const int g_dispatch_table_size = {len(all_funcs)};')
    lines.append(f'const dispatch_entry_t g_dispatch_table[{len(all_funcs)}] = {{')
    for fn in all_funcs:
        addr = int(fn.replace('func_', ''), 16)
        lines.append(f'    {{ 0x{addr:08X}, {fn} }},')
    lines.append('};')

    with open('generated/dispatch_table.c', 'w') as f:
        f.write('\n'.join(lines) + '\n')
    print(f"  Generated dispatch_table.c ({len(all_funcs)} entries)")


def gen_missing_stubs(src_path):
    """Auto-generate empty stubs for trampoline targets the lifter referenced
    but never defined.

    The new lifter emits `g_trampoline_fn = (void(*)(void*))func_XXXX;` for
    cross-fragment branches. `generate_mid_function_entries` creates wrappers
    for most such targets, but iteration converges before catching all of
    them — TJ ends up with ~2000 referenced-but-undefined symbols.

    These addresses live inside other function bodies, so a precise stub
    would `ctx->cia = ADDR; ps3_indirect_call(ctx);` to bounce back through
    the dispatcher (which runs the surrounding lifted function from the
    closest entry). For now, we emit logging no-ops — most are on cold
    paths and won't fire; we can promote specific ones to real bridges as
    crashes surface.
    """
    import re as _re
    with open(src_path, 'r') as f:
        cpp = f.read()
    with open('generated/ppu_recomp.h', 'r') as f:
        hdr = f.read()

    defined = set(_re.findall(r'^void (func_[0-9A-Fa-f]+)\(ppu_context\*',
                              cpp, _re.MULTILINE))
    declared = set(_re.findall(r'\bvoid (func_[0-9A-Fa-f]+)\(ppu_context\*',
                               hdr))
    referenced = set(_re.findall(r'\b(func_[0-9A-Fa-f]+)\b', cpp))

    # CRT overrides are defined in ppu_stubs.c — count as defined
    for fn in CRT_OVERRIDES:
        defined.add(fn)

    missing = sorted(referenced - defined)
    needs_decl = sorted(referenced - declared)

    lines = ['/* Auto-generated stubs for trampoline targets the lifter',
             ' * referenced but never defined. Most are mid-fragment',
             ' * entries on cold paths. Logging stub fires once per name. */',
             '#include "ppu_recomp.h"',
             '#include <stdio.h>',
             '',
             '#ifdef __cplusplus',
             'extern "C" {',
             '#endif',
             '']
    for fn in needs_decl:
        if fn not in defined:
            lines.append(f'void {fn}(ppu_context* ctx);')
    lines.append('')
    lines.append('#ifdef __cplusplus')
    lines.append('}')
    lines.append('#endif')
    lines.append('')
    lines.append('static int g_missing_stub_hits = 0;')
    lines.append('static void missing_stub_hit(const char* name) {')
    lines.append('    if (g_missing_stub_hits < 50) {')
    lines.append('        fprintf(stderr, "[TJ:missing-stub] %s\\n", name);')
    lines.append('        g_missing_stub_hits++;')
    lines.append('    }')
    lines.append('}')
    lines.append('')
    for fn in missing:
        lines.append(f'void {fn}(ppu_context* ctx) {{ (void)ctx; missing_stub_hit("{fn}"); }}')

    with open('generated/missing_stubs.cpp', 'w') as f:
        f.write('\n'.join(lines) + '\n')
    print(f"  Generated missing_stubs.cpp ({len(missing)} stubs)")


def gen_stubs():
    """Generate ppu_stubs.c with manual CRT overrides."""
    stub_content = r'''/* Manual stubs -- functions that need host-side overrides.
 * These are removed from ppu_recomp.cpp to avoid duplicate symbols.
 * The real PS3 CRT functions make syscalls and access kernel structures
 * that would hang or crash -- we replace them with safe HLE stubs.
 */
#include "ppu_recomp.h"
#include <stdio.h>

/* ========================================================================
 * CRT _start -- sets up argc/argv/envp then calls __libc_start
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
 * CRT init stubs -- all return r3=0 to keep CRT off the error path
 * ====================================================================== */
void func_0025FC00(ppu_context* ctx) {
    printf("[CRT] __sys_initialize(r3=0x%08X, r4=0x%08X, r5=0x%08X)\n",
           (uint32_t)ctx->gpr[3], (uint32_t)ctx->gpr[4], (uint32_t)ctx->gpr[5]);
    ctx->gpr[3] = 0;
}
void func_0025FC20(ppu_context* ctx) {
    printf("[CRT] __sys_init_tls(tls_image=0x%08X, tls_size=0x%08X)\n",
           (uint32_t)ctx->gpr[3], (uint32_t)ctx->gpr[4]);
    ctx->gpr[3] = 0;
}
void func_002448EC(ppu_context* ctx) {
    printf("[CRT] __init_section(r3=0x%08X, r4=0x%08X) -- static constructors skipped\n",
           (uint32_t)ctx->gpr[3], (uint32_t)ctx->gpr[4]);
    ctx->gpr[3] = 0;
}
void func_0025FBC0(ppu_context* ctx) {
    printf("[CRT] __sys_init_prx() -- no PRX loading in static recomp\n");
    ctx->gpr[3] = 0;
}
void func_0025FB60(ppu_context* ctx) {
    printf("[CRT] __sys_init_heap(heap_size=0x%08X) -- runtime bump allocator at 0x02000000\n",
           (uint32_t)ctx->gpr[3]);
    ctx->gpr[3] = 0;
}
void func_0025FC40(ppu_context* ctx) {
    printf("[CRT] __sys_init_spu() -- no SPU needed yet\n");
    ctx->gpr[3] = 0;
}
void func_0024DE14(ppu_context* ctx) {
    printf("[CRT] func_0024DE14 (stdio init) -- skipped\n");
    ctx->gpr[3] = 0;
}
void func_00249298(ppu_context* ctx) {
    printf("[CRT] func_00249298 (memory region init) -- skipped\n");
    ctx->gpr[3] = 0;
}
void func_00248C8C(ppu_context* ctx) {
    printf("[CRT] func_00248C8C (exception init) -- skipped\n");
    ctx->gpr[3] = 0;
}
void func_00010200(ppu_context* ctx) {
    printf("[CRT] __crt_atexit -- skipped\n");
    ctx->gpr[3] = 0;
}
'''
    with open('generated/ppu_stubs.c', 'w') as f:
        f.write(stub_content)
    print(f"  Generated ppu_stubs.c ({len(CRT_OVERRIDES)} overrides)")


def main():
    global TOC_VALUE
    parser = argparse.ArgumentParser(description='Post-lift processing for Tokyo Jungle')
    parser.add_argument('--toc', default=f'0x{TOC_VALUE:08X}',
                        help='TOC value (default: 0x00359220)')
    args = parser.parse_args()

    TOC_VALUE = int(args.toc, 16)

    print("=== Tokyo Jungle Post-Lift Processing ===\n")

    print("[1/6] Renaming source to .cpp...")
    src_path = rename_source_to_cpp()

    print("[2/6] Patching ppu_recomp.cpp...")
    patch_recomp(src_path)

    print("[3/6] Regenerating header...")
    regen_header(src_path)

    print("[4/6] Generating dispatch table...")
    gen_dispatch_table(src_path)

    print("[5/6] Generating stubs...")
    gen_stubs()

    print("[6/6] Generating missing-symbol stubs...")
    gen_missing_stubs(src_path)

    print("\n=== Done! Run: cmake --build build --config Release ===")


if __name__ == '__main__':
    main()
