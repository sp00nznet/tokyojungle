#!/usr/bin/env python3
"""Post-lift patches for the Tokyo Jungle recompilation.

Run once after the lifter, before building. Idempotent: re-running on an
already-patched tree is a no-op. The generated tree is regenerate-able, so
patches belong here rather than being hand-edited into it.

Currently patches: export the function table.

The lifter emits

    static const func_entry function_table[] = { ... { 0, NULL, NULL } };

which is private to that translation unit. ps3recomp's ppu_loader.cpp resolves
an indirect branch by scanning `function_table[]` / `function_table_count` to
map a host return address back to a guest address, so it needs both as external
symbols. Drop the `static` and emit the count after the sentinel.
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RECOMP = os.path.join(ROOT, "generated", "ppu_recomp.cpp")

LOCAL_TYPEDEF = ("typedef struct { uint64_t addr; void (*func)(ppu_context*); "
                 "const char* name; } func_entry;")
DECL_STATIC = "static const func_entry function_table[] = {"
DECL_EXPORT = "const func_entry function_table[] = {"
COUNT_MARK = "function_table_count"


def patch_function_table(path):
    with open(path, "r", encoding="utf-8", errors="surrogateescape") as f:
        text = f.read()

    if COUNT_MARK in text:
        if LOCAL_TYPEDEF in text:            # partially patched by an older run
            text = text.replace(LOCAL_TYPEDEF + chr(10), "", 1)
            with open(path, "w", encoding="utf-8", errors="surrogateescape") as f:
                f.write(text)
            return "typedef removed (table already exported)"
        return "already patched"
    if DECL_STATIC not in text:
        return "MISSING: no 'static const func_entry function_table[]' found"

    text = text.replace(DECL_STATIC, DECL_EXPORT, 1)

    # The lifter also emits its own anonymous typedef for the entry type. The
    # complete type has to be visible to ppu_loader.cpp, so it lives in
    # src/recomp_bridge.h (which the generated header includes) -- leaving this
    # copy in place makes two distinct types with one name in this TU.
    text = text.replace(LOCAL_TYPEDEF + chr(10), "", 1)

    # Close out the table, then define the count from its own size. The table is
    # sentinel-terminated ({0, NULL, NULL}) and the loader wants the count
    # WITHOUT the sentinel, matching what other ps3recomp titles emit.
    start = text.index(DECL_EXPORT)
    end = text.index("\n};", start) + len("\n};")
    text = (text[:end] + "\n\n"
            "/* post_lift.py: exported for ps3recomp's ppu_loader.cpp. */\n"
            "const uint64_t function_table_count =\n"
            "    sizeof(function_table) / sizeof(function_table[0]) - 1;\n"
            + text[end:])

    with open(path, "w", encoding="utf-8", errors="surrogateescape") as f:
        f.write(text)
    return "patched"


def main():
    if not os.path.exists(RECOMP):
        print(f"post_lift: {RECOMP} not found -- run the lifter first",
              file=sys.stderr)
        return 1
    status = patch_function_table(RECOMP)
    print(f"post_lift: function_table export -> {status}")
    return 0 if not status.startswith("MISSING") else 1


if __name__ == "__main__":
    sys.exit(main())
