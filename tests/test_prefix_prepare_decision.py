"""Exercise the extracted first-failing-admission diagnostic helper."""
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT.parent / "ps3recomp" / "libs" / "video" / "rsx_d3d12_backend.c"


def extract_function(source, signature):
    start = source.find(signature)
    while start >= 0:
        brace = source.find("{", start)
        semi = source.find(";", start)
        if brace >= 0 and (semi < 0 or brace < semi):
            depth, end = 1, brace + 1
            while depth:
                depth += (source[end] == "{") - (source[end] == "}")
                end += 1
            return source[start:end]
        start = source.find(signature, start + len(signature))
    raise AssertionError(f"production function missing: {signature}")


def test_prefix_prepare_decision():
    backend = BACKEND.read_text(encoding="utf-8")
    enum = re.search(r"enum\s*\{\s*TJ_PREPARE_ACCEPTED,.*?\};", backend, re.S)
    assert enum, "production decision enum missing"
    helper = extract_function(backend, "static int d3d12_prefix_prepare_decision(u32 reason, int result)")

    source = r'''
#include <cstdarg>
#include <cstdio>
#include <cstdint>
#include <string>
#include <algorithm>
using u32=uint32_t; using u64=uint64_t;
static int cost_enabled,prepare_enabled,survival_armed;
static u64 fake_tick;
static std::string captured;
static int tj_prefix_cost_enabled(){return cost_enabled;}
static int d3d12_prefix_prepare_enabled(){return prepare_enabled;}
static int rsx_survival_trace_is_armed(){return survival_armed;}
static u64 GetTickCount64(){return fake_tick;}
static int fixture_fprintf(FILE*,const char* format,...){
    char buffer[2048]; va_list args; va_start(args,format);
    const int n=vsnprintf(buffer,sizeof(buffer),format,args); va_end(args);
    if(n>0)captured.append(buffer,(size_t)std::min(n,(int)sizeof(buffer)-1));
    return n;
}
#define fprintf fixture_fprintf
ENUM
HELPER
#undef fprintf
static int checks,failures;
static void check(bool ok,const char* label){if(!ok){++failures;fprintf(stderr,"FAIL %s\n",label);}++checks;}
int main(){
    cost_enabled=0;prepare_enabled=1;survival_armed=1;fake_tick=10;
    check(d3d12_prefix_prepare_decision(TJ_PREPARE_ACCEPTED,17)==17&&captured.empty(),
          "disabled PrefixCost preserves result without reporting");
    cost_enabled=1;prepare_enabled=0;
    check(d3d12_prefix_prepare_decision(TJ_PREPARE_ACTIVE,-4)==-4&&captured.empty(),
          "disabled prepare gate preserves result without reporting");
    prepare_enabled=1;survival_armed=0;
    check(d3d12_prefix_prepare_decision(TJ_PREPARE_TICKET,29)==29&&captured.empty(),
          "unarmed Survival gate preserves result without reporting");
    survival_armed=1;
    check(d3d12_prefix_prepare_decision(TJ_PREPARE_DECISION_COUNT,31)==31&&captured.empty(),
          "out-of-range reason preserves result without counting");

    fake_tick=1000;
    check(d3d12_prefix_prepare_decision(TJ_PREPARE_ACCEPTED,1)==1&&captured.empty(),
          "first enabled event seeds interval without premature report");
    check(d3d12_prefix_prepare_decision(TJ_PREPARE_NO_PRIOR,0)==0&&captured.empty(),
          "second event retains rejection result before deadline");
    fake_tick=1999;
    check(d3d12_prefix_prepare_decision(TJ_PREPARE_LABEL_BATCH,23)==23&&captured.empty(),
          "subsecond event retains result and does not report");
    fake_tick=2000;
    check(d3d12_prefix_prepare_decision(TJ_PREPARE_EPOCH,-9)==-9,
          "reporting event retains original result");
    const std::string first =
        "[TJ-PREFIX-PREPARE-ADMISSION] t_ms=2000 calls=4 accepted=1 no_prior=1 invalid_state=0 active=0 label_batch=1 epoch=1 writeback=0 empty=0 observer=0 slot_busy=0 ticket=0 mirror=0 record=0 snapshot_fault=0 fragment_gpu=0 fragment_missing=0 vertex_gpu=0 vertex_missing=0 cache_capacity=0\n";
    check(captured==first,"first interval reports exact cumulative reasons and call sum");

    fake_tick=2050;
    check(d3d12_prefix_prepare_decision(TJ_PREPARE_CACHE_CAPACITY,0)==0,
          "next interval records capacity rejection");
    fake_tick=2999;
    check(d3d12_prefix_prepare_decision(TJ_PREPARE_TICKET,0)==0&&captured==first,
          "second interval waits for full one-second threshold");
    fake_tick=3000;
    check(d3d12_prefix_prepare_decision(TJ_PREPARE_RECORD,0)==0,
          "second threshold event retains rejection result");
    const std::string expected = first +
        "[TJ-PREFIX-PREPARE-ADMISSION] t_ms=3000 calls=7 accepted=1 no_prior=1 invalid_state=0 active=0 label_batch=1 epoch=1 writeback=0 empty=0 observer=0 slot_busy=0 ticket=1 mirror=0 record=1 snapshot_fault=0 fragment_gpu=0 fragment_missing=0 vertex_gpu=0 vertex_missing=0 cache_capacity=1\n";
    check(captured==expected,"second report is cumulative and reason increments sum to calls");
    printf("Prefix prepare decision: %d checks, %d failures\n",checks,failures);
    return failures?1:0;
}
'''.replace("ENUM", enum.group()).replace("HELPER", helper)

    compiler = shutil.which("clang++") or r"C:\Program Files\LLVM\bin\clang++.exe"
    with tempfile.TemporaryDirectory(prefix="tj-prefix-decision-") as directory:
        cpp, exe = Path(directory) / "decision.cpp", Path(directory) / "decision.exe"
        cpp.write_text(source, encoding="utf-8")
        subprocess.run([compiler, "-std=c++17", "-D_CRT_SECURE_NO_WARNINGS",
                        "-Wno-unused-function", str(cpp), "-o", str(exe)], check=True)
        result = subprocess.run([str(exe)], check=False, capture_output=True, text=True)
        assert result.returncode == 0, result.stdout + result.stderr
        print(result.stdout.strip())


if __name__ == "__main__":
    test_prefix_prepare_decision()
