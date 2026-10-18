"""Check actual vertex readers preserve values without per-attribute getenv scans."""
from pathlib import Path
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
VIDEO = ROOT.parent / "ps3recomp/libs/video"


def function(text, name):
    start = text.index(name)
    start = text.rfind("\n", 0, start) + 1
    brace = text.index("{", start)
    depth = 1
    end = brace + 1
    while depth:
        depth += (text[end] == "{") - (text[end] == "}")
        end += 1
    return text[start:end]


def main():
    backend = (VIDEO / "rsx_d3d12_backend.c").read_text()
    names = ["static u32 rd_beu32(", "static float rd_bef(",
             "static float rd_half_be("]
    prefix = r'''
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <windows.h>
#include "rsx_commands.h"
typedef struct { float v[4]; } VPSlot;
static unsigned char bytes[4096];
uint8_t* vm_base = bytes;
static const char* option;
static volatile LONG lookups;
static char* counted_getenv(const char* name) {
    if (strcmp(name, "VP_NOFREQ")) abort();
    InterlockedIncrement(&lookups);
    return (char*)option;
}
#define getenv counted_getenv
static int cellGcmResolveLocatedChecked(int local, u32 off, u32* ea) {
    (void)local;
    if (off >= sizeof(bytes)) return 0;
    *ea = off; return 1;
}
static int tj_indexed_diag_guest_span(u32 ea, u64 size) {
    return ea <= sizeof(bytes) && size <= sizeof(bytes)-ea;
}
'''
    if "static int vp_frequency_override_disabled(" in backend:
        names.append("static int vp_frequency_override_disabled(")
    if "static void decode_vp_vertex(" in backend:
        names.append("static void decode_vp_vertex(")
        prefix += "\n#define VP_VERT_STRIDE 256u\n"
    names += ["static void read_vp_vertex(", "static int tj_indexed_diag_read_attr("]
    source = prefix + "\n".join(function(backend, name) for name in names) + r'''
static DWORD WINAPI exercise(void* ignored) {
    (void)ignored;
    rsx_state state = {0};
    VPSlot output[16];
    unsigned failures = 0;
    state.vertex_attrib_input_mask = 3;
    state.vertex_attribs[0].enabled = state.vertex_attribs[1].enabled = 1;
    state.vertex_attribs[0].stride = state.vertex_attribs[1].stride = 4;
    state.vertex_attribs[0].type = state.vertex_attribs[1].type = 2;
    state.vertex_attribs[0].size = state.vertex_attribs[1].size = 1;
    state.vertex_attribs[1].offset = 0x80000000u; /* checked MAIN path */
    state.frequency_divider_op = 2; /* attr0 divide; attr1 modulo */
    for (unsigned frequency = 0; frequency < 8; ++frequency) {
        state.vertex_attribs[0].frequency = state.vertex_attribs[1].frequency = frequency;
        for (unsigned repeat = 0; repeat < 16; ++repeat) {
            for (unsigned vi = 0; vi < 64; ++vi) {
                read_vp_vertex(&state, vi, output);
                for (unsigned attr = 0; attr < 2; ++attr) {
                    unsigned ei = frequency > 1 && !option
                        ? (attr ? vi % frequency : vi / frequency) : vi;
                    u32 actual_ei = ~0u, actual_ea = ~0u;
                    float diagnostic[4];
                    failures += output[attr].v[0] != (float)ei;
                    failures += !tj_indexed_diag_read_attr(&state, attr, vi,
                                                         &actual_ei, &actual_ea, diagnostic);
                    failures += actual_ei != ei || actual_ea != ei*4;
                    failures += memcmp(diagnostic, output[attr].v, sizeof(diagnostic)) != 0;
                }
            }
        }
    }
    /* Unavailable sources retain current attributes rather than dereferencing. */
    state.vertex_data4f[0][0] = 99;
    state.vertex_attribs[0].offset = sizeof(bytes);
    read_vp_vertex(&state, 0, output);
    failures += output[0].v[0] != 99;
    return failures;
}
int main(int argc, char** argv) {
    option = argc > 1 ? argv[1] : NULL;
    for (u32 i = 0; i < 64; ++i) {
        float f = (float)i; u32 bits; memcpy(&bits, &f, 4);
        bytes[i*4] = bits >> 24; bytes[i*4+1] = bits >> 16;
        bytes[i*4+2] = bits >> 8; bytes[i*4+3] = bits;
    }
    unsigned failures = exercise(NULL);
    HANDLE threads[4];
    for (unsigned i = 0; i < 4; ++i) {
        threads[i] = CreateThread(NULL, 0, exercise, NULL, 0, NULL);
        if (!threads[i]) return 2;
    }
    WaitForMultipleObjects(4, threads, TRUE, INFINITE);
    for (unsigned i = 0; i < 4; ++i) {
        DWORD result; GetExitCodeThread(threads[i], &result);
        failures += result; CloseHandle(threads[i]);
    }
    printf("Vertex values: failures=%u; option lookups=%ld, expected=5 (one per thread)\n",
           failures, lookups);
    return failures || lookups != 5;
}
'''
    clang = shutil.which("clang") or r"C:\Program Files\LLVM\bin\clang.exe"
    with tempfile.TemporaryDirectory(prefix="tj-vertex-options-") as temp:
        c, exe = Path(temp) / "vertex.c", Path(temp) / "vertex.exe"
        c.write_text(source)
        subprocess.run([clang, "-O2", "-std=c11", "-D_CRT_SECURE_NO_WARNINGS",
                        "-I", str(VIDEO), "-I", str(VIDEO.parent.parent / "include"),
                        str(c), "-o", str(exe)], check=True)
        result = 0
        for flags in ([], ["1"], ["0"], [""]):
            run = subprocess.run([str(exe), *flags], capture_output=True, text=True)
            print(f"Option {flags!r}: {run.stdout.strip()}")
            if run.stderr:
                print(run.stderr)
            result |= run.returncode
        return result


if __name__ == "__main__":
    raise SystemExit(main())
