"""Actual FIFO/syscall dispatch and sampled launch readers: values and lookup cost."""
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

from test_constant_diagnostic_cost import RUNTIME
from test_fifo_present_boundary import function


def main():
    video = RUNTIME / "libs/video"
    backend = (video / "rsx_d3d12_backend.c").read_text()
    loader = (RUNTIME / "runtime/ppu/ppu_loader.cpp").read_text()
    register = (RUNTIME / "runtime/syscalls/lv2_register.c").read_text()
    groups = [
        (backend, "static void vp_record_cb(", ["NO_ALPHATEST", "VP_FIXPROJ"]),
        (backend, "static u32 current_rt_off(", ["RT_OFFDBG"]),
        (backend, "static void render_frame(", ["TJ_TEX_IDENTITY", "RTT_VIEWRT", "RTT_DUMP"]),
        (backend, "static void vp_attrs_dbg(", ["VP_ATTRS"]),
        (backend, "static void d3d12_bind_texture(", ["MOVIE_BIND"]),
        (loader, 'extern "C" void lv2_syscall(', ["YDKJ_SCTRACE", "YDKJ_BLOCKTRACE"]),
    ]
    readers = []
    options = []
    for source, name, flags in groups:
        body = function(source, name)
        for flag in flags:
            pattern = (r"static (?:_Thread_local|thread_local) int (\w+) = -1;\s*"
                       r'if \(\1 < 0\) \1 = getenv\("' + flag + r'"\) != NULL;')
            match = re.search(pattern, body)
            assert match, (name, flag)
            # Guard against caching an unused copy while the actual branch still scans.
            remaining = body[:match.start()] + body[match.end():]
            assert remaining.count(match[1]) > 0, (name, "unused cache")
            if flag not in ("RTT_VIEWRT", "RTT_DUMP"):
                assert f'getenv("{flag}")' not in remaining
            code = match[0]
            readers.append(f'extern "C" int read_{len(options)}(void) {{ {code} return {match[1]}; }}')
            options.append(flag)
    frame = function(backend, "static void render_frame(")
    assert 'rtt_viewrt_enabled ? getenv("RTT_VIEWRT") : NULL' in frame
    assert 'int viewrt = rtt_viewrt_enabled;' in frame
    assert 'if (rtt_dump_enabled && s_d3d.draw_count > 0)' in frame
    assert frame.count('if (tex_identity_enabled &&') == 7
    options += ["LOAD_DBG", "MVPDBG", "YDKJ_GFXSCAN"]
    preamble = r'''
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <windows.h>
'''
    c = preamble + 'static const char* names[] = {' + ','.join(f'"{s}"' for s in options) + '};\n'
    c += '\n'.join(f'int read_{i}(void);' for i in range(len(readers)))
    c += r'''
static const char* value;
static unsigned counts[13];
char* counted_getenv(const char* name) {
    for (unsigned i=0; i<13; ++i) if (!strcmp(name,names[i])) {
        ++counts[i]; return (char*)value;
    }
    return NULL;
}
#define getenv counted_getenv
#include "rsx_commands.c"
void vm_write32(uint32_t a,uint32_t v) {(void)a;(void)v;}
typedef struct { uint64_t gpr[32],lr; } ppu_context;
typedef int64_t (*lv2_syscall_fn)(ppu_context*);
#define LV2_SYSCALL_MAX 1024
static struct { lv2_syscall_fn handlers[1024]; } g_lv2_syscalls;
static int64_t lv2_syscall_unimplemented(ppu_context* c) {(void)c;return -1;}
static unsigned dispatches;
static int64_t handler(ppu_context* c) {++dispatches;return -(int64_t)c->gpr[4];}
'''
    c += function(register, "int lv2_try_syscall(")
    c += r'''
static DWORD WINAPI exercise(void* ignored) {
    (void)ignored;
    rsx_state* state=(rsx_state*)calloc(1,sizeof(rsx_state));
    if (!state) return 1;
    rsx_state_init(state);
    unsigned failures=0;
    for (unsigned repeat=0; repeat<1024; ++repeat) {
        failures+=rsx_process_method(state,NV4097_SET_TRANSFORM_CONSTANT_LOAD,repeat)!=0;
        failures+=state->transform_constant_load!=repeat;
        failures+=rsx_process_method(state,NV4097_DRAW_ARRAYS,0)!=0;
        ppu_context ctx={0};ctx.gpr[11]=141;ctx.gpr[4]=3;
        failures+=lv2_try_syscall(&ctx)!=1 || ctx.gpr[3]!=UINT64_C(0xfffffffffffffffd);
READERS
    }
    ppu_context ctx={0};ctx.gpr[3]=123;
    const unsigned missing[]={1024,0,1};
    for(unsigned i=0;i<3;++i){ctx.gpr[11]=missing[i];failures+=lv2_try_syscall(&ctx)!=0 || ctx.gpr[3]!=123;}
    free(state);return failures;
}
int main(int argc,char** argv) {
    value=argc>1?argv[1]:NULL;
    g_lv2_syscalls.handlers[1]=lv2_syscall_unimplemented;
    g_lv2_syscalls.handlers[141]=handler;
    unsigned failures=exercise(NULL);
    /* Sequential fresh threads verify TLS without racing pre-existing debug counters. */
    for(unsigned i=0;i<4;++i){
        HANDLE t=CreateThread(NULL,0,exercise,NULL,0,NULL);if(!t)return 2;
        WaitForSingleObject(t,INFINITE);DWORD result;GetExitCodeThread(t,&result);
        CloseHandle(t);failures+=result;
    }
    for(unsigned i=0;i<13;++i) failures+=counts[i]!=5;
    failures+=dispatches!=5120;
    printf("Hot launch options: flags=13 threads=5 dispatches=%u failures=%u\n",dispatches,failures);
    return failures?1:0;
}
'''
    c = c.replace("READERS", '\n'.join(f'        failures+=read_{i}()!=(value!=NULL);' for i in range(len(readers))))
    cpp = preamble + '''
extern "C" char* counted_getenv(const char*);
#define getenv counted_getenv
#define _Thread_local thread_local
''' + '\n'.join(readers)
    clang = shutil.which("clang") or r"C:\Program Files\LLVM\bin\clang.exe"
    with tempfile.TemporaryDirectory(prefix="tj-hot-options-") as folder:
        temp = Path(folder)
        (temp/"dispatch.c").write_text(c)
        (temp/"readers.cpp").write_text(cpp)
        for src in ("dispatch.c", "readers.cpp"):
            subprocess.run([clang, "-O2", "-D_CRT_SECURE_NO_WARNINGS", "-I", str(video),
                            "-I", str(RUNTIME/"include"), "-c", str(temp/src),
                            "-o", str(temp/(src+".obj"))], check=True)
        exe=temp/"options.exe"
        subprocess.run([clang, str(temp/"dispatch.c.obj"), str(temp/"readers.cpp.obj"), "-o", str(exe)], check=True)
        for flags in ([], ["1"], ["0"], [""]):
            run=subprocess.run([str(exe), *flags], capture_output=True, text=True, timeout=20)
            print(repr(flags), run.stdout.strip().splitlines()[-1])
            if run.returncode:
                raise AssertionError(run.stderr)


if __name__ == "__main__":
    main()
