"""FPS-only reporting must not enable detailed renderer timing."""
from pathlib import Path
import os
import shutil
import subprocess
import tempfile
from test_fifo_present_boundary import function

ROOT=Path(__file__).resolve().parents[1]


def main():
    backend=(ROOT.parent/"ps3recomp/libs/video/rsx_d3d12_backend.c").read_text(encoding="utf-8")
    funcs="\n".join(function(backend,sig) for sig in (
        "static int tj_perf_trace_enabled(","static int tj_fps_trace_enabled("))
    assert "if (tj_fps_trace_enabled() && rsx_survival_trace_is_armed())" in backend
    source=r'''
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
static unsigned timer_inits;
static struct { long long QuadPart; } s_tj_perf_frequency;
static int QueryPerformanceFrequency(void* p) { (void)p;++timer_inits;return 1; }
FUNCTIONS
int main(int argc,char** argv) {
    if(argc!=3)return 2;
    int fps=atoi(argv[1]),perf=atoi(argv[2]);
    for(int i=0;i<3;i++)if(tj_fps_trace_enabled()!=fps || tj_perf_trace_enabled()!=perf)return 1;
    if(timer_inits!=(unsigned)perf)return 1;
    printf("FPS=%d detailed=%d timer_initializations=%u PASS\n",fps,perf,timer_inits);
    return 0;
}
'''.replace("FUNCTIONS",funcs)
    clang=shutil.which("clang") or r"C:\Program Files\LLVM\bin\clang.exe"
    with tempfile.TemporaryDirectory(prefix="tj-fps-mode-") as temp:
        c,exe=Path(temp)/"mode.c",Path(temp)/"mode.exe"
        c.write_text(source,encoding="utf-8")
        subprocess.run([clang,"-std=c11","-O2","-D_CRT_SECURE_NO_WARNINGS",
                        str(c),"-o",str(exe)],check=True)
        for fps,perf,blob,expected_fps,expected_perf in (
            ("0","0","0",0,0),("1","0","0",1,0),
            ("0","1","0",1,1),("1","1","0",1,1),
            ("0","0","1",0,1),("","","",0,0)):
            env=dict(os.environ,TJ_FPS_TRACE=fps,TJ_PERF_TRACE=perf,TJ_FP_BLOB_TRACE=blob)
            subprocess.run([str(exe),str(expected_fps),str(expected_perf)],env=env,check=True)


if __name__=="__main__":
    main()
