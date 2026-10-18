"""Exercise actual SPU wait-entry recovery ownership without launching the game.

A published descriptor and parked workers do not authorize execution before the
guest graphics callback. Recovery must remain an explicitly enabled experiment.
"""
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

from test_fifo_present_boundary import function

ROOT = Path(__file__).resolve().parents[1]
SPU = ROOT.parent / "ps3recomp/runtime/spu"


def main():
    source = (SPU / "spu_workload.c").read_text(encoding="utf-8")
    header = (SPU / "spu_workload.h").read_text(encoding="utf-8")
    state = re.search(
        r"typedef struct SpuSignalTaskset \{.*?\} SpuSignalTaskset;", source, re.S
    ).group()
    policy = header[header.index("static inline uint16_t spu_task_signal_merge_pending"):
                    header.index("void spu_taskset_register_signal_state")]
    bodies = "\n".join(function(source, signature) for signature in (
        "static void signal_pending_add_locked(",
        "static int spu_taskset_retry_signal_generation_locked(",
        "static int spu_taskset_retry_current_generation_locked(",
    ))
    fixture = r'''
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
typedef long long LONGLONG;
typedef struct { LONGLONG QuadPart; } LARGE_INTEGER;
static int task_perf_enabled(void) { return 0; }
static void QueryPerformanceCounter(LARGE_INTEGER* counter) { counter->QuadPart=1; }
POLICY
STATE
static SpuSignalTaskset state;
static uint32_t completion;
static unsigned char guest[128];
static int recovery_enabled, diagnostics_enabled, bumps, lock_depth;
static int checks, failures;
static char* fixture_getenv(const char* name) {
    if (!strcmp(name,"TJ_SPU_COMPLETION_WAIT_RECOVERY"))
        return recovery_enabled ? "1" : NULL;
    if (!strcmp(name,"TJ_SPU_COMPLETION_WAIT_TRACE"))
        return diagnostics_enabled ? "1" : NULL;
    return NULL;
}
#define getenv fixture_getenv
static void spu_reservation_lock(void) { ++lock_depth; }
static void spu_reservation_unlock(void) { --lock_depth; }
static uint32_t vm_read32(uint32_t ea) {
    if (ea != state.generation_completion_ea) abort();
    return completion;
}
#define CSTS_SIGNALLED 0x30u
static int spurs_bitset_test(uint32_t ea, uint32_t task) {
    if (ea != state.ea+CSTS_SIGNALLED || task >= 128) abort();
    return guest[task];
}
static void spurs_bitset_set(uint32_t ea, uint32_t task) {
    if (ea != state.ea+CSTS_SIGNALLED || task >= 128) abort();
    guest[task]=1;
}
static void spu_reservation_bump_locked(uint32_t ea) {
    if (ea != state.ea || lock_depth != 1) abort();
    ++bumps;
}
BODIES
#define CHECK(condition,label) do { ++checks; if (!(condition)) { \
    ++failures; fprintf(stderr,"FAIL: %s\n",label); } } while (0)
static void reset(void) {
    memset(&state,0,sizeof(state));
    memset(guest,0,sizeof(guest));
    completion=0; bumps=0; lock_depth=0;
    recovery_enabled=diagnostics_enabled=0;
    state.ea=0x06E8C780;
    state.generation=24;
    state.generation_owner_ea=0x00A96AA0;
    state.generation_completion_ea=0x00A96ACC;
    state.generation_count=2;
    state.generation_expected_image_id=15;
    state.generation_create_ca9c_substitution_debt=1;
    for (unsigned i=0;i<2;++i) {
        state.generation_task_ids[i]=i;
        state.waiter_active[i]=1;
        state.waiter_image_id[i]=15;
        state.delivery_epoch[i]=state.generation_delivery_epoch[i]=23;
        state.consume_epoch[i]=state.generation_consume_epoch[i]=23;
    }
}
static void check_untouched(const char* label) {
    CHECK(!state.pending[0] && !state.pending[1] && !guest[0] && !guest[1],label);
    CHECK(!state.ca9c_substitution_debt[0] && !state.ca9c_substitution_debt[1],
          "unrequested wake must not create callback substitution debt");
    CHECK(!bumps && !state.retry_delivered_generation,
          "unrequested wake must not publish or claim a generation");
    CHECK(state.delivery_epoch[0]==23 && state.delivery_epoch[1]==23,
          "unrequested wake must not advance delivery epochs");
    CHECK(lock_depth==0,"reservation lock balanced");
}
int main(void) {
    reset();
    CHECK(spu_taskset_retry_current_generation_locked(&state,state.ea)!=1,
          "default WAIT_SIGNAL synthesized a wake before the graphics callback");
    check_untouched("default WAIT_SIGNAL must stay parked without a real signal");

    reset(); diagnostics_enabled=1;
    CHECK(spu_taskset_retry_current_generation_locked(&state,state.ea)!=1,
          "read-only completion tracing enabled synthetic recovery");
    check_untouched("diagnostics must not deliver signals");

    reset(); recovery_enabled=1;
    CHECK(spu_taskset_retry_current_generation_locked(&state,state.ea)==1,
          "explicit recovery no longer executes its guarded experiment");
    CHECK(state.pending[0]==1 && state.pending[1]==1 && guest[0] && guest[1],
          "explicit recovery did not publish the complete task group");
    CHECK(state.ca9c_substitution_debt[0]==1 && state.ca9c_substitution_debt[1]==1,
          "explicit recovery lost existing exact-callback substitution accounting");
    CHECK(state.delivery_epoch[0]==24 && state.delivery_epoch[1]==24 && bumps==1,
          "explicit recovery did not publish exactly once");
    CHECK(state.retry_delivered_generation==24 && lock_depth==0,
          "explicit recovery generation or reservation ownership incorrect");
    CHECK(spu_taskset_retry_current_generation_locked(&state,state.ea)!=1 && bumps==1,
          "same generation recovered twice");

    reset(); recovery_enabled=1; completion=1;
    CHECK(spu_taskset_retry_current_generation_locked(&state,state.ea)!=1,
          "completed work was recovered again");
    check_untouched("completed work must not receive a synthetic signal");

    reset(); recovery_enabled=1; state.waiter_active[1]=0;
    CHECK(spu_taskset_retry_current_generation_locked(&state,state.ea)!=1,
          "recovery ran before all managed workers parked");
    check_untouched("partial waiter group must not receive a synthetic signal");

    reset(); recovery_enabled=1; state.delivery_epoch[0]=24;
    CHECK(spu_taskset_retry_current_generation_locked(&state,state.ea)!=1,
          "recovery duplicated a real signal already delivered for this generation");
    CHECK(!state.pending[0] && !state.pending[1] && !bumps,
          "real delivery was overwritten by recovery");

    reset(); recovery_enabled=1;
    CHECK(spu_taskset_retry_current_generation_locked(NULL,state.ea)!=1,
          "null taskset accepted");
    CHECK(spu_taskset_retry_current_generation_locked(&state,state.ea+128)!=1,
          "different taskset accepted");
    printf("SPU wait-entry recovery: %d/%d checks pass\n",checks-failures,checks);
    return failures ? 1 : 0;
}
'''.replace("POLICY", policy).replace("STATE", state).replace("BODIES", bodies)
    compiler = shutil.which("clang") or "C:/Program Files/LLVM/bin/clang.exe"
    if not Path(compiler).is_file():
        raise SystemExit("clang is required")
    with tempfile.TemporaryDirectory(prefix="tj-spu-wait-recovery-") as directory:
        src = Path(directory) / "fixture.c"
        exe = Path(directory) / "fixture.exe"
        src.write_text(fixture, encoding="utf-8")
        subprocess.run([compiler, "-std=c11", "-O0", str(src), "-o", str(exe)], check=True)
        subprocess.run([str(exe)], check=True)


if __name__ == "__main__":
    main()
