"""Keep bounded WaitFlip polling from fabricating a completed flip.

This source-extracted test proves host status behavior only. It does not model
the PS3 command-stream semaphore semantics or demonstrate an FPS effect.
"""
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

from test_fifo_present_boundary import function

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT.parent / "ps3recomp/libs/video/cellGcmSys.c"
SIGNATURE = "void cellGcmSetWaitFlip(void)"
DONE_STORE = "flip_status_store(CELL_GCM_FLIP_STATUS_DONE);"


def prepare_helper(body, name):
    body = body.replace(SIGNATURE, f"void {name}(void)", 1)
    declaration = "__declspec(dllimport) void __stdcall Sleep(unsigned long);"
    assert body.count(declaration) == 1, "WaitFlip Sleep seam changed"
    body = body.replace(declaration, "void test_sleep(unsigned long);", 1)
    assert body.count("Sleep(1);") == 1, "WaitFlip must retain one Sleep(1) poll"
    return body.replace("Sleep(1);", "test_sleep(1);", 1)


def main():
    source = SOURCE.read_text(encoding="utf-8")
    actual = function(source, SIGNATURE)
    code = re.sub(r"/\*.*?\*/|//[^\n]*", "", actual, flags=re.S)
    compact = " ".join(code.split())
    assert "for (int i = 0; i < 64 && flip_status_load() == CELL_GCM_FLIP_STATUS_WAITING; i++) Sleep(1);" in compact, \
        "WaitFlip's existing 64-poll bound or Sleep(1) cadence changed"
    assert DONE_STORE not in code, "WaitFlip must not publish DONE itself"
    assert "cellGcm_complete_flip_ticket(" not in code
    assert "cellGcmTickFlip(" not in code
    assert "gcm_fifo_" not in code and "vm_write" not in code, \
        "WaitFlip unexpectedly acquired FIFO or guest-memory behavior"

    # Reconstruct the former implementation by appending only its unconditional
    # final store. The production helper above must be the post-fix source.
    old_body = actual[:-1] + f"    {DONE_STORE}\n}}"
    assert DONE_STORE in old_body
    production_helper = prepare_helper(actual, "production_wait_flip")
    old_helper = prepare_helper(old_body, "old_wait_flip")

    fixture = r'''#include <stdint.h>
#include <stdio.h>
typedef uint32_t u32;
enum { CELL_GCM_FLIP_STATUS_DONE=0, CELL_GCM_FLIP_STATUS_WAITING=1 };
static u32 status;
static unsigned sleeps, complete_on_sleep;
static unsigned helper_done_stores, presenter_done_stores;
static int presenter_store_scope, publish_waiting_on_load;
static int failures, checks;
static u32 flip_status_load(void);
static void flip_status_store(u32 value);
void test_sleep(unsigned long milliseconds);
PRODUCTION_HELPER
OLD_HELPER
static void reset(u32 initial) {
    status=initial; sleeps=complete_on_sleep=0;
    helper_done_stores=presenter_done_stores=0;
    presenter_store_scope=publish_waiting_on_load=0;
}
static void complete_from_presenter(void) {
    presenter_store_scope=1;
    flip_status_store(CELL_GCM_FLIP_STATUS_DONE);
    presenter_store_scope=0;
}
static u32 flip_status_load(void) {
    u32 observed=status;
    if (publish_waiting_on_load) {
        publish_waiting_on_load=0;
        status=CELL_GCM_FLIP_STATUS_WAITING;
    }
    return observed;
}
static void flip_status_store(u32 value) {
    if (value==CELL_GCM_FLIP_STATUS_DONE) {
        if (presenter_store_scope) ++presenter_done_stores;
        else ++helper_done_stores;
    }
    status=value;
}
void test_sleep(unsigned long milliseconds) {
    if (milliseconds!=1) ++failures;
    ++sleeps;
    if (complete_on_sleep && sleeps==complete_on_sleep)
        complete_from_presenter();
}
#define CHECK(condition,message) do { \
    ++checks; \
    if (!(condition)) { ++failures; fprintf(stderr,"FAIL: %s\n",message); } \
} while (0)
int main(void) {
    const unsigned completion_points[]={1,63,64};
    reset(CELL_GCM_FLIP_STATUS_DONE);
    production_wait_flip();
    CHECK(status==CELL_GCM_FLIP_STATUS_DONE && sleeps==0 &&
          helper_done_stores==0 && presenter_done_stores==0,
          "already-complete entry changed status or slept");

    for (unsigned i=0;i<sizeof(completion_points)/sizeof(completion_points[0]);++i) {
        unsigned at=completion_points[i];
        reset(CELL_GCM_FLIP_STATUS_WAITING);
        complete_on_sleep=at;
        production_wait_flip();
        CHECK(status==CELL_GCM_FLIP_STATUS_DONE && sleeps==at &&
              helper_done_stores==0 && presenter_done_stores==1,
              "real completion at sleep boundary was not preserved as owner-published DONE");
    }

    reset(CELL_GCM_FLIP_STATUS_WAITING);
    production_wait_flip();
    CHECK(status==CELL_GCM_FLIP_STATUS_WAITING && sleeps==64 &&
          helper_done_stores==0 && presenter_done_stores==0,
          "64-poll timeout fabricated DONE");

    reset(CELL_GCM_FLIP_STATUS_WAITING);
    production_wait_flip();
    CHECK(status==CELL_GCM_FLIP_STATUS_WAITING && helper_done_stores==0,
          "delayed-completion case returned with fabricated DONE");
    complete_from_presenter();
    CHECK(status==CELL_GCM_FLIP_STATUS_DONE && helper_done_stores==0 &&
          presenter_done_stores==1,
          "exact presenter completion owner could not publish DONE after return");

    reset(CELL_GCM_FLIP_STATUS_DONE);
    publish_waiting_on_load=1;
    production_wait_flip();
    CHECK(status==CELL_GCM_FLIP_STATUS_WAITING && sleeps==0 &&
          helper_done_stores==0,
          "new WAITING request was clobbered after terminal DONE observation");

    /* The reconstructed old helper must reproduce both false transitions. */
    reset(CELL_GCM_FLIP_STATUS_WAITING);
    old_wait_flip();
    CHECK(status==CELL_GCM_FLIP_STATUS_DONE && sleeps==64 &&
          helper_done_stores==1,
          "old 64-poll timeout no longer reproduces fabricated DONE");

    reset(CELL_GCM_FLIP_STATUS_DONE);
    publish_waiting_on_load=1;
    old_wait_flip();
    CHECK(status==CELL_GCM_FLIP_STATUS_DONE && sleeps==0 &&
          helper_done_stores==1,
          "old final store no longer reproduces the new-request clobber race");

    printf("WaitFlip bounded status: %d/%d checks passed\n",checks-failures,checks);
    return failures ? 1 : 0;
}
'''.replace("PRODUCTION_HELPER", production_helper).replace("OLD_HELPER", old_helper)

    compiler = shutil.which("clang") or "C:/Program Files/LLVM/bin/clang.exe"
    with tempfile.TemporaryDirectory(prefix="tj-wait-flip-status-") as temp:
        path = Path(temp) / "test.c"
        path.write_text(fixture, encoding="utf-8")
        exe = Path(temp) / "test.exe"
        subprocess.run([compiler, "-std=c11", "-O2", "-D_CRT_SECURE_NO_WARNINGS",
                        str(path), "-o", str(exe)], check=True)
        subprocess.run([str(exe)], check=True)


if __name__ == "__main__":
    main()
