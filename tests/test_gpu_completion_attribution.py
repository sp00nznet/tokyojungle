"""Check production per-reason totals and report-window reset without a GPU."""
from pathlib import Path
import shutil
import subprocess
import tempfile
from test_fifo_present_boundary import function


def main():
    video = Path(__file__).resolve().parents[2] / "ps3recomp/libs/video"
    backend = (video / "rsx_d3d12_backend.c").read_text(encoding="utf-8")
    header = (video / "rsx_d3d12_backend.h").read_text(encoding="utf-8")
    enum_start = header.index("typedef enum RsxD3D12CompletionReason")
    enum_end = header.index("} RsxD3D12CompletionReason;", enum_start) + len("} RsxD3D12CompletionReason;")
    types = []
    for name in ("TJGcm2dBackendPerf", "TJGcm2dBackendPerfSample"):
        end = backend.index("} " + name + ";") + len(name) + 3
        start = backend.rfind("typedef struct {", 0, end)
        types.append(backend[start:end])
    source = r'''
#include <cstdint>
#include <cstdio>
#include <cstring>
using u32=uint32_t; using u64=uint64_t;
using ULONGLONG=uint64_t; using LONGLONG=int64_t;
ENUM
TYPES
static TJGcm2dBackendPerf s_tj_gcm2d_backend_perf, reported;
static u64 now_ms=100, ticks=100;
static unsigned reports, checks, failures;
static u64 GetTickCount64(){return now_ms;}
static LONGLONG tj_gcm2d_backend_perf_now(){return (LONGLONG)ticks;}
static void tj_gcm2d_backend_perf_log(const TJGcm2dBackendPerf* sample,u64){
    ++reports; reported=*sample;
}
PRODUCTION
static void check(bool ok,const char* label){
    ++checks;if(!ok){++failures;std::fprintf(stderr,"FAIL: %s\n",label);}
}
int main(){
    TJGcm2dBackendPerfSample sample={};
    tj_gcm2d_backend_perf_finish(nullptr);
    tj_gcm2d_backend_perf_finish(&sample);
    check(!s_tj_gcm2d_backend_perf.prefixes,"inactive sample stays inert");
    sample.start=1;sample.completion_reason=RSX_COMPLETION_ACQUIRE;
    sample.draws=3;sample.fence_ticks=17;sample.gpu_elapsed_us=19;
    sample.gpu_samples=1;sample.gpu_calibrated_samples=1;
    sample.gpu_queue_us=23;sample.gpu_completion_wake_us=29;
    tj_gcm2d_backend_perf_finish(&sample);
    sample.completion_reason=RSX_COMPLETION_BACKEND_RELEASE;
    sample.draws=5;sample.fence_ticks=31;sample.gpu_elapsed_us=37;
    sample.gpu_queue_us=41;sample.gpu_completion_wake_us=43;
    tj_gcm2d_backend_perf_finish(&sample);
    const auto& p=s_tj_gcm2d_backend_perf;
    check(p.completion_prefixes[RSX_COMPLETION_ACQUIRE]==1&&
          p.completion_draws[RSX_COMPLETION_ACQUIRE]==3&&
          p.completion_prefix_fence_ticks[RSX_COMPLETION_ACQUIRE]==17,
          "acquire prefix retains entry reason and CPU totals");
    check(p.completion_gpu_us[RSX_COMPLETION_ACQUIRE]==19&&
          p.completion_gpu_samples[RSX_COMPLETION_ACQUIRE]==1&&
          p.completion_calibrated_samples[RSX_COMPLETION_ACQUIRE]==1&&
          p.completion_queue_us[RSX_COMPLETION_ACQUIRE]==23&&
          p.completion_wake_us[RSX_COMPLETION_ACQUIRE]==29,"acquire GPU totals");
    check(p.completion_draws[RSX_COMPLETION_BACKEND_RELEASE]==5&&
          p.completion_prefix_fence_ticks[RSX_COMPLETION_BACKEND_RELEASE]==31&&
          p.completion_gpu_us[RSX_COMPLETION_BACKEND_RELEASE]==37&&
          p.completion_queue_us[RSX_COMPLETION_BACKEND_RELEASE]==41&&
          p.completion_wake_us[RSX_COMPLETION_BACKEND_RELEASE]==43,
          "backend release remains separate");
    check(p.prefixes==2&&p.draw_total==8&&p.fence_ticks==48&&
          p.gpu_elapsed_us==56,"reason totals partition existing aggregate");
    sample.completion_reason=(RsxD3D12CompletionReason)-1;
    sample.draws=0;sample.fence_ticks=0;sample.gpu_elapsed_us=0;
    tj_gcm2d_backend_perf_finish(&sample);
    check(p.completion_prefixes[RSX_COMPLETION_OTHER]==1,"invalid reason is bounded");
    now_ms=1100;
    sample.completion_reason=RSX_COMPLETION_ACQUIRE;
    tj_gcm2d_backend_perf_finish(&sample);
    check(reports==1&&reported.prefixes==4&&
          reported.completion_prefixes[RSX_COMPLETION_ACQUIRE]==2&&
          reported.completion_prefixes[RSX_COMPLETION_BACKEND_RELEASE]==1,
          "report snapshots the complete window");
    check(!p.prefixes&&!p.completion_prefixes[RSX_COMPLETION_ACQUIRE]&&
          p.report_start_ms==1100,"new report window clears per-reason totals");
    std::printf("GPU completion attribution: %u checks, %u failures\n",checks,failures);
    return failures?1:0;
}
'''.replace("ENUM", header[enum_start:enum_end]).replace("TYPES", "\n".join(types))
    source = source.replace("PRODUCTION", function(backend, "static void tj_gcm2d_backend_perf_add(") +
                            function(backend, "static void tj_gcm2d_backend_perf_finish("))
    compiler = shutil.which("clang++") or shutil.which("g++") or r"C:\Program Files\LLVM\bin\clang++.exe"
    with tempfile.TemporaryDirectory(prefix="tj-completion-attribution-") as directory:
        cpp, exe = Path(directory) / "attribution.cpp", Path(directory) / "attribution.exe"
        cpp.write_text(source, encoding="utf-8")
        subprocess.run([compiler,"-std=c++17","-O2",str(cpp),"-o",str(exe)],check=True)
        return subprocess.run([str(exe)],timeout=10).returncode


if __name__ == "__main__":
    raise SystemExit(main())
