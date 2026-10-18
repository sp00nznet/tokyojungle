"""Exercise the opt-in pending-flip notifier extracted from cellGcmSys.c."""
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

from test_fifo_present_boundary import function

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT.parent / "ps3recomp/libs/video/cellGcmSys.c"


def main():
    source = SOURCE.read_text(encoding="utf-8")
    publication = re.search(
        r"typedef struct GcmFlipPublication \{.*?\} GcmFlipPublication;",
        source,
        re.S,
    ).group()
    event_state = re.search(
        r"static HANDLE s_gcm_kick_ev = NULL;\s*"
        r"static _Atomic\(void\*\) s_gcm_flip_kick_ev = ATOMIC_VAR_INIT\(NULL\);",
        source,
    ).group()
    signatures = [
        "static void gcm_flip_publication_lock(void)",
        "static void gcm_flip_publication_unlock(void)",
        "static void gcm_flip_publish(u32 buffer_id, int pending, u32 source)",
        "void* cellGcm_fifo_kick_event(void)",
        "static void gcm_flip_kick_presenter(void)",
    ]
    bodies = []
    for signature in signatures:
        match = re.search(re.escape(signature) + r"\s*\{", source)
        assert match, f"missing source function: {signature}"
        bodies.append(function(source[match.start():], signature))

    fixture = r'''
#include <stdatomic.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
typedef uint32_t u32;
typedef uint64_t u64;
typedef void* HANDLE;
typedef struct { u64 generation; u32 buffer_id, source; } CellGcmFlipTicket;
enum { CELL_GCM_FLIP_PUBLICATION_QPC_OFF = 0, CELL_GCM_FLIP_PUBLICATION_QPC_VALID = 1,
       CELL_GCM_FLIP_PUBLICATION_QPC_FAILED = 2 };
typedef struct { u64 publication_qpc; u32 publication_qpc_state; } CellGcmFlipPublicationTiming;
#define FALSE 0
#define CELL_GCM_FLIP_STATUS_DONE 0u
#define CELL_GCM_FLIP_STATUS_WAITING 1u
INSERT_PUBLICATION_STRUCT
static GcmFlipPublication s_flip_publication = { ATOMIC_FLAG_INIT, 0 };
static atomic_uint s_current_display_buffer_id;
static atomic_uint s_flip_request_count;
static atomic_uint s_flip_status;
EVENT_STATE
static const char* env_value;
static HANDLE create_result = (HANDLE)0x1234;
static HANDLE expected_event = (HANDLE)0x1234;
static unsigned create_calls, set_calls, set_failures, getenv_calls;
static unsigned lock_was_held, incoherent_on_signal;
static unsigned signal_state, expected_buffer;
static char* fixture_getenv(const char* name) {
    ++getenv_calls;
    return strcmp(name, "TJ_FIFO_KICK_WAIT") == 0 ? (char*)env_value : NULL;
}
static HANDLE fixture_create_event(void* attrs, int manual, int initial, const char* name) {
    (void)attrs; (void)manual; (void)initial; (void)name;
    ++create_calls;
    return create_result;
}
static int fixture_set_event(HANDLE event) {
    ++set_calls;
    if (event != expected_event) ++incoherent_on_signal;
    if (atomic_flag_test_and_set_explicit(&s_flip_publication.lock, memory_order_acquire)) {
        ++lock_was_held;
    } else {
        atomic_flag_clear_explicit(&s_flip_publication.lock, memory_order_release);
    }
    if (!s_flip_publication.pending_valid ||
        s_flip_publication.pending.buffer_id != expected_buffer ||
        atomic_load_explicit(&s_current_display_buffer_id, memory_order_acquire) != expected_buffer ||
        atomic_load_explicit(&s_flip_status, memory_order_acquire) != CELL_GCM_FLIP_STATUS_WAITING ||
        atomic_load_explicit(&s_flip_request_count, memory_order_acquire) == 0)
        ++incoherent_on_signal;
    if (set_failures) return 0;
    signal_state = 1; /* Auto-reset events coalesce repeated SetEvent calls. */
    return 1;
}
#define getenv fixture_getenv
#define CreateEventA fixture_create_event
#define SetEvent fixture_set_event
static u32 flip_status_load(void) { return atomic_load_explicit(&s_flip_status, memory_order_acquire); }
static void flip_status_store(u32 value) { atomic_store_explicit(&s_flip_status, value, memory_order_release); }
static void gcm_flip_kick_presenter(void);
static CellGcmFlipPublicationTiming gcm_flip_publication_timestamp_locked(int pending) {
    (void)pending;
    return (CellGcmFlipPublicationTiming){ 0, CELL_GCM_FLIP_PUBLICATION_QPC_OFF };
}
BODIES
#define CHECK(test, message) do { ++checks; if (!(test)) { ++failures; fprintf(stderr, "FAIL: %s\n", message); } } while (0)
static unsigned checks, failures;
static void reset_state(void) {
    atomic_flag_clear_explicit(&s_flip_publication.lock, memory_order_release);
    s_flip_publication.next_generation = 0;
    memset(&s_flip_publication.pending, 0, sizeof(s_flip_publication.pending));
    memset(&s_flip_publication.consumed, 0, sizeof(s_flip_publication.consumed));
    memset(&s_flip_publication.pending_timing, 0, sizeof(s_flip_publication.pending_timing));
    memset(&s_flip_publication.consumed_timing, 0, sizeof(s_flip_publication.consumed_timing));
    s_flip_publication.pending_valid = 0;
    s_flip_publication.completed_generation = 0;
    s_flip_publication.last_publication_qpc = 0;
    atomic_store(&s_current_display_buffer_id, 0);
    atomic_store(&s_flip_request_count, 0);
    atomic_store(&s_flip_status, CELL_GCM_FLIP_STATUS_DONE);
    s_gcm_kick_ev = NULL;
    atomic_store(&s_gcm_flip_kick_ev, NULL);
    create_calls = set_calls = set_failures = getenv_calls = 0;
    lock_was_held = incoherent_on_signal = signal_state = 0;
    expected_buffer = 0;
    create_result = expected_event;
}
static void setup_opt_in(void) {
    env_value = "1";
    CHECK(cellGcm_fifo_kick_event() == expected_event, "opt-in getter did not return shared event");
    CHECK(atomic_load_explicit(&s_gcm_flip_kick_ev, memory_order_acquire) == expected_event,
          "exact opt-in did not publish notifier handle");
}
int main(void) {
    const char* off_values[] = { NULL, "", "0", "true", "01", "1 " };
    for (unsigned i = 0; i < sizeof(off_values) / sizeof(off_values[0]); ++i) {
        reset_state();
        env_value = off_values[i];
        (void)cellGcm_fifo_kick_event();
        CHECK(atomic_load_explicit(&s_gcm_flip_kick_ev, memory_order_acquire) == NULL,
              "non-exact environment value enabled flip notifier");
        expected_buffer = 9;
        gcm_flip_publish(expected_buffer, 1, 2);
        CHECK(set_calls == 0, "default-off publication signaled shared event");
    }

    reset_state();
    expected_buffer = 3;
    gcm_flip_publish(expected_buffer, 1, 2);
    CHECK(set_calls == 0 && create_calls == 0,
          "publication created or signaled an event without opt-in initialization");

    reset_state();
    setup_opt_in();
    expected_buffer = 4;
    gcm_flip_publish(expected_buffer, 1, 2);
    CHECK(set_calls == 1 && signal_state == 1, "pending publication did not signal once");
    CHECK(lock_was_held == 0 && incoherent_on_signal == 0,
          "notification ran under lock or before coherent pending state was visible");

    reset_state();
    setup_opt_in();
    gcm_flip_publish(5, 0, 2);
    CHECK(set_calls == 0 && !s_flip_publication.pending_valid &&
          s_flip_publication.consumed.buffer_id == 5,
          "prepare flip signaled or lost immediate consumed state");

    reset_state();
    setup_opt_in();
    set_failures = 1;
    expected_buffer = 6;
    gcm_flip_publish(expected_buffer, 1, 2);
    CHECK(set_calls == 1 && s_flip_publication.pending_valid &&
          flip_status_load() == CELL_GCM_FLIP_STATUS_WAITING &&
          s_flip_publication.pending.buffer_id == expected_buffer,
          "failed SetEvent fabricated completion or discarded pending flip");

    reset_state();
    create_result = NULL;
    env_value = "1";
    CHECK(cellGcm_fifo_kick_event() == NULL, "event creation failure returned a handle");
    CHECK(atomic_load_explicit(&s_gcm_flip_kick_ev, memory_order_acquire) == NULL,
          "event creation failure published a notifier handle");
    expected_buffer = 7;
    gcm_flip_publish(expected_buffer, 1, 2);
    CHECK(atomic_load_explicit(&s_gcm_flip_kick_ev, memory_order_acquire) == NULL &&
          set_calls == 0 && s_flip_publication.pending_valid,
          "missing shared handle attempted signaling or lost pending flip");

    reset_state();
    setup_opt_in();
    expected_buffer = 8;
    gcm_flip_publish(expected_buffer, 1, 2);
    expected_buffer = 9;
    gcm_flip_publish(expected_buffer, 1, 2);
    CHECK(set_calls == 2 && signal_state == 1 &&
          s_flip_publication.pending.buffer_id == expected_buffer &&
          atomic_load(&s_flip_request_count) == 2,
          "coalesced event changed one-deep latest pending publication");
    CHECK(lock_was_held == 0 && incoherent_on_signal == 0,
          "coalesced notification observed locked or partial publication");

    if (failures) {
        printf("FIFO flip kick: %u checks, %u failures\n", checks - failures, checks);
        return 1;
    }
    printf("FIFO flip kick: %u native checks passed\n", checks);
    return 0;
}
'''.replace("INSERT_PUBLICATION_STRUCT", publication).replace("EVENT_STATE", event_state).replace("BODIES", "\n".join(bodies))

    compiler = shutil.which("clang") or r"C:\Program Files\LLVM\bin\clang.exe"
    with tempfile.TemporaryDirectory(prefix="tj-fifo-flip-kick-") as folder:
        c_file = Path(folder) / "fixture.c"
        executable = Path(folder) / "fixture.exe"
        c_file.write_text(fixture, encoding="utf-8")
        subprocess.run([compiler, "-std=c11", "-O2", "-D_CRT_SECURE_NO_WARNINGS",
                        str(c_file), "-o", str(executable)], check=True)
        result = subprocess.run([str(executable)], capture_output=True, text=True,
                                timeout=15, check=False)
        if result.returncode:
            raise AssertionError(f"fixture failed:\n{result.stdout}\n{result.stderr}")
        print(result.stdout.strip())


if __name__ == "__main__":
    main()
