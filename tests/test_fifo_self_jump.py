"""Exercise the actual fast/profiled FIFO jump branches without launching the game."""
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
VIDEO = ROOT.parent / "ps3recomp/libs/video"


def jump_branch(source, walker):
    definition = re.search(r"static void " + walker + r"\([^)]*\)\s*\{", source)
    if not definition:
        raise RuntimeError("FIFO walker definition missing")
    start = definition.start()
    match = re.search(r"if \(type == 1\)\s*\{", source[start:])
    if not match:
        raise RuntimeError("FIFO jump branch missing")
    start += match.start()
    pos = source.index("{", start) + 1
    depth = 1
    while depth:
        depth += (source[pos] == "{") - (source[pos] == "}")
        pos += 1
    return source[start:pos]


def main():
    compiler = shutil.which("clang") or r"C:\Program Files\LLVM\bin\clang.exe"
    if not Path(compiler).is_file():
        raise RuntimeError("clang is required")
    source = (VIDEO / "cellGcmSys.c").read_text(encoding="utf-8")
    with tempfile.TemporaryDirectory(prefix="tj-fifo-self-jump-") as temp:
        failures = 0
        for walker in ("gcm_rsx_process_fifo_unlocked_fast",
                       "gcm_rsx_process_fifo_unlocked_profiled"):
            fixture = r'''
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
typedef uint32_t u32;
static u32 s_fifo_getoff, s_fifo_calloff;
typedef struct { unsigned controls, exit_reason; } TjFifoPerfCall;
#define TJ_FIFO_EXIT_IDLE 7
static void jump(u32 w, u32 put) {
    TjFifoPerfCall perf={0}, *perf_call=&perf;
    u32 type=w>>29;
    for (unsigned step=0;step<1;++step) {
BRANCH
    }
}
static int checks, failures;
#define CHECK(c,msg) do { ++checks; if (!(c)) { ++failures; fprintf(stderr,"FAIL: %s\n",msg); } } while(0)
int main(void) {
    s_fifo_getoff=0x800; s_fifo_calloff=0x104;
    jump(0x20000800,0x120);
    CHECK(s_fifo_getoff==0x800,"called self-jump skipped pending producer commands");
    CHECK(s_fifo_calloff==0x104,"called self-jump discarded saved return address");
    /* Producer still pending on another drain. Nothing is consumed. */
    jump(0x20000800,0x120);
    CHECK(s_fifo_getoff==0x800 && s_fifo_calloff==0x104,"repeated idle drain changed FIFO state");
    /* Producer patches the parked word to the now-complete command chain. */
    jump(0x20000900,0x120);
    CHECK(s_fifo_getoff==0x900 && s_fifo_calloff==0x104,"patched jump lost CALL ownership");
    s_fifo_getoff=0x800; s_fifo_calloff=0;
    jump(0x20000800,0x120);
    CHECK(s_fifo_getoff==0x800 && s_fifo_calloff==0,"root self-jump advanced");
    s_fifo_getoff=0x800; s_fifo_calloff=0x120;
    jump(0x20000800,0x120);
    CHECK(s_fifo_getoff==0x800 && s_fifo_calloff==0x120,"CALL return equal PUT changed");
    printf("%d checks, %d failures\n",checks,failures);
    return failures ? 1 : 0;
}
'''.replace("BRANCH", jump_branch(source, walker))
            test = Path(temp) / (walker + ".c")
            exe = test.with_suffix(".exe")
            test.write_text(fixture, encoding="utf-8")
            subprocess.run([compiler, "-std=c11", "-D_CRT_SECURE_NO_WARNINGS",
                            str(test), "-o", str(exe)], check=True)
            print(walker, flush=True)
            failures += subprocess.run([str(exe)]).returncode != 0
    return int(failures != 0)


if __name__ == "__main__":
    raise SystemExit(main())
