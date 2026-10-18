"""Exercise actual constant dispatch, including disabled diagnostic lookup cost."""
from pathlib import Path
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT.parent / "ps3recomp"


def main():
    source = r'''
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
static unsigned seq_lookups;
static int enabled;
static char* counted_getenv(const char* name) {
    if(!strcmp(name,"SEQ_DBG")){++seq_lookups;return enabled?"1":NULL;}
    return NULL;
}
#define getenv counted_getenv
#include "rsx_commands.c"
#undef getenv
void vm_write32(uint32_t address,uint32_t value){(void)address;(void)value;}
int main(int argc,char** argv) {
    (void)argv; enabled=argc>1;
    static rsx_state state;
    rsx_state_init(&state);
    unsigned failures=0,words=0;
    for(unsigned round=0;round<20;round++) {
        state.vertex_constants_dirty=0;
        /* Match the game's 16-word packets; higher method addresses also
         * contain separately decoded registers and are not this fixture. */
        for(unsigned base=0;base<RSX_MAX_VERTEX_CONSTANTS;base+=4) {
            rsx_process_method(&state,NV4097_SET_TRANSFORM_CONSTANT_LOAD,base);
            for(unsigned lane=0;lane<16;lane++) {
                uint32_t raw=0x3f000000u+(round<<16)+base*4+lane;
                rsx_process_method(&state,NV4097_SET_TRANSFORM_CONSTANT+lane*4,raw);
                uint32_t actual=0;
                memcpy(&actual,&state.vertex_constants[base+lane/4][lane%4],4);
                failures+=actual!=raw; ++words;
            }
        }
        failures+=!state.vertex_constants_dirty || state.vertex_constants_lo!=0 ||
                  state.vertex_constants_hi!=RSX_MAX_VERTEX_CONSTANTS-1;
    }
    unsigned expected=20;
    failures+=seq_lookups!=expected;
    printf("Constant dispatch: enabled=%d words=%u sequence-lookups=%u expected=%u failures=%u\n",
           enabled,words,seq_lookups,expected,failures);
    return failures?1:0;
}
'''
    clang = shutil.which("clang") or r"C:\Program Files\LLVM\bin\clang.exe"
    with tempfile.TemporaryDirectory(prefix="tj-constant-diag-") as temp:
        c, exe = Path(temp)/"constants.c", Path(temp)/"constants.exe"
        c.write_text(source, encoding="utf-8")
        subprocess.run([clang, "-O2", "-std=c11", "-D_CRT_SECURE_NO_WARNINGS", "-I",
                        str(RUNTIME/"include"), "-I", str(RUNTIME/"libs/video"),
                        str(c), "-o", str(exe)], check=True)
        result = 0
        for flags in ([], ["enabled"]):
            run = subprocess.run([str(exe), *flags], capture_output=True, text=True)
            print(run.stdout.strip())
            result |= run.returncode
            if flags and run.stderr.count("[SEQ] upload c256.x=") != 20:
                raise AssertionError("enabled sequence diagnostic changed")
            if not flags and run.stderr:
                raise AssertionError("disabled diagnostic emitted output")
        return result


if __name__ == "__main__":
    raise SystemExit(main())
