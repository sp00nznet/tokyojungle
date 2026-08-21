#!/usr/bin/env python3
"""Post-lift patches for the Tokyo Jungle recompilation.

Run after the lifter, before building. Idempotent: re-running on an
already-patched tree is a no-op. The generated tree is regenerate-able, so
patches belong here rather than being hand-edited into it.

Currently patches: the PS3 libc/CRT init chain -> tj_crt_skip (see
src/tj_crt_overrides.cpp for why each one is skipped).

The lifter emits each function as

    void func_XXXXXXXX(ppu_context* ctx) {
        ...body...
    }                                        <- a lone "}" at column 0

so we replace the whole span [signature line .. next lone "}"] with a
one-line redirect. This is robust to the body's own nested braces (which are
indented). Same shape as rubberducky/tools/post_lift.py.
"""
import glob
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RECOMP_GLOB = os.path.join(ROOT, "generated", "ppu_recomp_*.cpp")

# guest addr -> human name. The real bodies abort() or wander into kernel
# structures the HLE runtime does not model.
CRT_SKIPS = {
    # Empty: with the import table fully resolved (see src/import_resolver.h)
    # the real CRT init chain runs. It only abort()ed because
    # sys_ppu_thread_create "returned" a stale nonzero r3 -- the call had been
    # dropped by the lifter. Re-add an address here if its real body proves
    # too far ahead of the runtime.
}

# The __sys_init_* entries at 0x0025Fxxx that the old generated/ppu_stubs.c
# also stubbed are .stub import thunks, not functions -- find_functions no
# longer emits them and src/import_resolver.h owns that range instead.

SIG = re.compile(r"void func_([0-9A-F]{8})\(ppu_context\* ctx\) \{\s*$")
MARK = "/* tj-crt-skip"
HEAP_MARK = "/* tj-heap"

# guest addr -> (host redirect symbol, human name). The libc heap wrappers:
# the mspace they front is never initialised, so all of them return 0 and the
# game aborts with "exception: bad allocation" before graphics init. See
# src/tj_crt_overrides.cpp for how each was identified from the binary.
HEAP_PATCHES = {
    "0024DDC0": ("tj_heap_malloc",   "malloc"),
    "0024DD94": ("tj_heap_free",     "free"),
    "0024DCBC": ("tj_heap_free",     "free (realloc-internal)"),
    "0024DCEC": ("tj_heap_memalign", "memalign"),
    "0024DD24": ("tj_heap_realloc",  "realloc"),
}


def patch_file(path):
    with open(path, "r", encoding="utf-8", errors="surrogateescape") as f:
        lines = f.readlines()

    out, i, patched = [], 0, set()
    while i < len(lines):
        m = SIG.match(lines[i])
        addr = m.group(1) if m else None
        if addr in HEAP_PATCHES:
            sym, name = HEAP_PATCHES[addr]
            if HEAP_MARK in (lines[i + 1] if i + 1 < len(lines) else ""):
                patched.add(addr)                    # already patched
                out.append(lines[i]); i += 1; continue
            j = i + 1
            while j < len(lines) and lines[j].rstrip(chr(10)) != "}":
                j += 1
            out.append(lines[i])
            out.append("    " + HEAP_MARK + ": " + name + " -> " + sym + " */" + chr(10))
            out.append("    extern void " + sym + "(ppu_context*);" + chr(10))
            out.append("    " + sym + "(ctx);" + chr(10))
            out.append("}" + chr(10))
            patched.add(addr)
            i = j + 1
            continue
        if addr in CRT_SKIPS:
            if MARK in (lines[i + 1] if i + 1 < len(lines) else ""):
                patched.add(addr)                       # already patched
                out.append(lines[i]); i += 1; continue
            j = i + 1
            while j < len(lines) and lines[j].rstrip("\n") != "}":
                j += 1
            name = CRT_SKIPS[addr]
            out.append(lines[i])
            out.append('    %s: %s */\n' % (MARK, name))
            out.append('    extern void tj_crt_skip(ppu_context*, const char*);\n')
            out.append('    tj_crt_skip(ctx, "%s");\n' % name)
            out.append("}\n")
            patched.add(addr)
            i = j + 1
            continue
        out.append(lines[i]); i += 1

    if patched:
        with open(path, "w", encoding="utf-8", errors="surrogateescape") as f:
            f.writelines(out)
    return patched


def main():
    files = sorted(glob.glob(RECOMP_GLOB))
    if not files:
        print("post_lift: no %s -- run the lifter first" % RECOMP_GLOB,
              file=sys.stderr)
        return 1

    done = set()
    for path in files:
        done |= patch_file(path)

    want = set(CRT_SKIPS) | set(HEAP_PATCHES)
    print("post_lift: %d/%d redirects (%d CRT skips, %d heap)"
          % (len(done), len(want), len(CRT_SKIPS), len(HEAP_PATCHES)))
    missing = sorted(want - done)
    if missing:
        print("  WARNING: not found (a re-lift may have moved them): %s"
              % ", ".join(missing), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
