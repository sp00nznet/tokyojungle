"""Native proof of launch-only dump flag presence and independent TLS caches."""
from pathlib import Path
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT.parent / "ps3recomp/libs/video/rsx_d3d12_backend.c"


def function(text, signature):
    start = text.index(signature)
    brace = text.index("{", start)
    depth, end = 1, brace + 1
    while depth:
        depth += (text[end] == "{") - (text[end] == "}")
        end += 1
    return text[start:end]


def main():
    source = BACKEND.read_text()
    for signature, required in (
            ("static u32 upload_quads_vp(", "vp_vertex_dump_enabled()"),
            ("static u32 upload_quads_vp_indexed(", "vp_vertex_dump_enabled()"),
            ("static u32 upload_tris_vp(", "tj_vertex_dump_enabled()")):
        body = function(source, signature)
        assert required in body
        assert 'getenv("VTX_DUMP")' not in body
        assert 'getenv("TJ_VTX_DUMP")' not in body
    assert "vp_vertex_dump_enabled() && state->surface_color_offset[0] == 0xCC0000" in source
    c = r'''
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <windows.h>
static const char* options[2];
static volatile LONG lookups[2];
static char* counted_getenv(const char* name) {
    int slot;
    if (!strcmp(name, "VTX_DUMP")) slot = 0;
    else if (!strcmp(name, "TJ_VTX_DUMP")) slot = 1;
    else abort();
    InterlockedIncrement(&lookups[slot]);
    return (char*)options[slot];
}
#define getenv counted_getenv
'''
    c += "\n".join(function(source, signature) for signature in (
        "static int vp_vertex_dump_enabled(", "static int tj_vertex_dump_enabled("))
    c += r'''
static DWORD WINAPI exercise(void* ignored) {
    (void)ignored;
    DWORD failures = 0;
    for (unsigned i = 0; i < 1000; ++i) {
        /* Presence, including empty and "0", retains the old getenv branch. */
        failures += vp_vertex_dump_enabled() != (options[0] != NULL);
        failures += tj_vertex_dump_enabled() != (options[1] != NULL);
        failures += tj_vertex_dump_enabled() != (options[1] != NULL);
        failures += vp_vertex_dump_enabled() != (options[0] != NULL);
    }
    return failures;
}
int main(int argc, char** argv) {
    if (argc != 3) return 2;
    options[0] = strcmp(argv[1], "ABSENT") ? argv[1] : NULL;
    options[1] = strcmp(argv[2], "ABSENT") ? argv[2] : NULL;
    unsigned failures = exercise(NULL);
    HANDLE threads[4];
    for (unsigned i = 0; i < 4; ++i) {
        threads[i] = CreateThread(NULL, 0, exercise, NULL, 0, NULL);
        if (!threads[i]) return 2;
    }
    if (WaitForMultipleObjects(4, threads, TRUE, 10000) != WAIT_OBJECT_0) return 3;
    for (unsigned i = 0; i < 4; ++i) {
        DWORD result;
        if (!GetExitCodeThread(threads[i], &result)) return 4;
        failures += result;
        CloseHandle(threads[i]);
    }
    failures += lookups[0] != 5 || lookups[1] != 5;
    printf("dump cache: failures=%u, VTX_DUMP lookups=%ld TJ_VTX_DUMP lookups=%ld (5 threads)\n",
           failures, lookups[0], lookups[1]);
    return failures != 0;
}
'''
    clang = shutil.which("clang") or r"C:\Program Files\LLVM\bin\clang.exe"
    with tempfile.TemporaryDirectory(prefix="tj-dump-cache-") as temp:
        source_path, exe = Path(temp) / "cache.c", Path(temp) / "cache.exe"
        source_path.write_text(c)
        subprocess.run([clang, "-O2", "-std=c17", "-D_CRT_SECURE_NO_WARNINGS",
                        str(source_path), "-o", str(exe)], check=True)
        for options in (("ABSENT", "ABSENT"), ("1", "ABSENT"), ("ABSENT", "0"),
                        ("", "1"), ("0", "")):
            result = subprocess.run([str(exe), *options], capture_output=True, text=True)
            print(f"Presence {options!r}: {result.stdout.strip()}")
            if result.returncode:
                print(result.stderr)
                return result.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
