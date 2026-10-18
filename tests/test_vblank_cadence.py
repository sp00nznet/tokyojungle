"""Check the production rational VBlank deadline helper and ticker integration."""
from pathlib import Path
import shutil
import subprocess
import tempfile

from test_fifo_present_boundary import function

ROOT = Path(__file__).resolve().parents[1]


def main():
    present_path = ROOT / "src/tj_present.cpp"
    header_path = ROOT / "src/tj_vblank_cadence.h"
    present = present_path.read_text(encoding="utf-8")
    thread = function(present, "static DWORD WINAPI tj_present_thread(")
    drain = function(present, "static void tj_flip_timeline_drain(void)")
    clock = function(present, "static uint64_t tj_vblank_clock_now(")

    assert '#include "tj_vblank_cadence.h"' in present
    assert "QueryPerformanceFrequency(&vblank_qpc_frequency)" in thread
    assert "QueryPerformanceCounter(&sample)" in clock
    assert "*frequency = 1000ULL" in clock
    assert "GetTickCount64 fallback" in clock
    assert "tj_vblank_cadence_rearm_after_clock_switch" in clock
    assert "while (tj_vblank_cadence_due(&vblank_cadence, now) && fired < 240)" in thread
    assert "tj_vblank_cadence_resync(&vblank_cadence, now)" in thread
    assert "Sleep(1);" in thread
    assert "tj_fifo_kick_wait_once(" in thread
    assert thread.index("uint64_t before_wait = tj_vblank_clock_now(") < thread.index(
        "tj_fifo_kick_wait_once(") < thread.index("uint64_t now = tj_vblank_clock_now(")
    assert "wait_timeout_ms = tj_vblank_cadence_due(" in thread
    assert "next_tick += 16" not in thread

    assert "tj_flip_timeline_drain();" in thread
    disabled_drain = drain.split("if (!t.enabled)", 1)[1].split("return;", 1)[0]
    assert disabled_drain.count("cellGcm_rsx_process_fifo();") == 1, \
        "default-off timeline wrapper must dispatch exactly one FIFO drain"

    ordered = [
        thread.index("cellGcmTickVBlank();"),
        thread.index("tj_present_flip(1, &awaiting_vblank)"),
        thread.index("tj_flip_timeline_drain();"),
        thread.index("tj_vblank_cadence_advance(&vblank_cadence);"),
    ]
    assert ordered == sorted(ordered), "tick / flip / FIFO / deadline order changed"

    fixture = r"""#include <stdint.h>
#include <stdio.h>
#include "tj_vblank_cadence.h"
static unsigned checks, failures;
#define CHECK(test, message) do { ++checks; if (!(test)) { ++failures; fprintf(stderr, "FAIL: %s\n", message); } } while (0)
static void check_frequency(uint64_t frequency, uint64_t cycles, uint64_t epoch) {
    TjVBlankCadence cadence;
    tj_vblank_cadence_init(&cadence, epoch, frequency);
    CHECK(tj_vblank_cadence_due(&cadence, epoch), "first tick was not immediately due");
    CHECK(!tj_vblank_cadence_due(&cadence, epoch - 1), "tick was due before its first deadline");
    const uint64_t whole = frequency / 60;
    uint64_t prior = cadence.deadline;
    for (uint64_t n = 1; n <= cycles; ++n) {
        tj_vblank_cadence_advance(&cadence);
        uint64_t step = cadence.deadline - prior;
        CHECK(step == whole || step == whole + 1, "period was not floor/ceil of frequency/60");
        prior = cadence.deadline;
    }
    uint64_t expected_delta = cycles * whole + (cycles * (frequency % 60)) / 60;
    CHECK(cadence.deadline - epoch == expected_delta,
          "rational remainder accumulated phase drift");
}
int main(void) {
    const uint64_t large_epoch = UINT64_C(0x7FFFFFFFF0000000);
    check_frequency(1000, 60000, large_epoch);       /* TickCount fallback */
    check_frequency(10000000, 60000, large_epoch);   /* common non-divisible QPC rate */
    check_frequency(24000000, 60000, large_epoch);   /* divisible QPC rate */
    check_frequency(3579545, 60000, large_epoch);    /* non-divisible synthetic QPC rate */

    /* A 10-second QPC schedule must emit 600 deadlines despite poll jitter. */
    TjVBlankCadence jittered;
    const uint64_t jitter_epoch = UINT64_C(0x7000000000000000);
    const uint64_t jitter_frequency = 10000000;
    const uint64_t jitter_end = jitter_epoch + 10 * jitter_frequency;
    const uint64_t poll_steps[] = {10000, 150000, 47000, 119000, 31000, 83000, 141000};
    tj_vblank_cadence_init(&jittered, jitter_epoch, jitter_frequency);
    uint64_t poll_now = jitter_epoch;
    unsigned jittered_ticks = 0;
    unsigned poll_index = 0;
    for (;;) {
        while (tj_vblank_cadence_due(&jittered, poll_now) &&
               jittered.deadline < jitter_end) {
            ++jittered_ticks;
            tj_vblank_cadence_advance(&jittered);
        }
        if (poll_now == jitter_end) break;
        uint64_t step = poll_steps[poll_index++ %
            (sizeof(poll_steps) / sizeof(poll_steps[0]))];
        poll_now = step > jitter_end - poll_now ? jitter_end : poll_now + step;
    }
    CHECK(jittered_ticks == 600,
          "jittered polls changed the 60 Hz tick count over ten seconds");

    TjVBlankCadence cadence;
    tj_vblank_cadence_init(&cadence, 5000, 6000);
    uint64_t overdue_now = 5000 + 1000000;
    unsigned fired = 0;
    while (tj_vblank_cadence_due(&cadence, overdue_now) && fired < 240) {
        tj_vblank_cadence_advance(&cadence);
        ++fired;
    }
    CHECK(fired == 240 && tj_vblank_cadence_due(&cadence, overdue_now),
          "overdue cap no longer leaves debt for resync");
    tj_vblank_cadence_resync(&cadence, overdue_now);
    CHECK(tj_vblank_cadence_due(&cadence, overdue_now),
          "cap resync no longer matches immediate next-loop due semantics");
    tj_vblank_cadence_advance(&cadence);
    CHECK(!tj_vblank_cadence_due(&cadence, overdue_now),
          "cap resync did not restart the rational phase");

    tj_vblank_cadence_rearm_after_clock_switch(&cadence, 9000, 1000);
    CHECK(!tj_vblank_cadence_due(&cadence, 9000),
          "clock switch invented an immediate extra tick");
    CHECK(tj_vblank_cadence_due(&cadence, cadence.deadline),
          "fallback clock switch did not arm one fallback period");
    tj_vblank_cadence_init(&cadence, 100, 10);
    CHECK(cadence.whole_step == 16 && cadence.step_remainder == 40,
          "invalid clock rate did not use the millisecond fallback");

    if (failures) return 1;
    printf("PASS: VBlank cadence %u/%u checks\n", checks, checks);
    return 0;
}
"""
    compiler = shutil.which("clang++") or r"C:\Program Files\LLVM\bin\clang++.exe"
    with tempfile.TemporaryDirectory(prefix="tj-vblank-cadence-") as temp:
        source = Path(temp) / "test.cpp"
        executable = Path(temp) / "test.exe"
        source.write_text(fixture, encoding="utf-8")
        subprocess.run([
            compiler, "-std=c++17", "-O2", "-I", str(header_path.parent),
            str(source), "-o", str(executable),
        ], check=True)
        subprocess.run([str(executable)], check=True)


if __name__ == "__main__":
    main()
