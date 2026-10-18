"""Compile the presenter-owned flip timeline helpers with a fake QPC clock."""
from pathlib import Path
import os
import re
import shutil
import subprocess
import tempfile

from test_fifo_present_boundary import function

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src/tj_present.cpp"


def production_helpers(text):
    start = text.index("/* Timestamp-only flip ring.")
    end = text.index("static int tj_present_flip(", start)
    return text[start:end]


def fields(line):
    return dict(re.findall(r"([A-Za-z_][A-Za-z0-9_]*)=([^ ]*)", line))


def run_case(exe, mode, enabled):
    env = dict(os.environ)
    env.pop("TJ_FLIP_TIMELINE", None)
    if enabled:
        env["TJ_FLIP_TIMELINE"] = "1"
    result = subprocess.run([str(exe), mode], env=env, capture_output=True,
                            text=True, check=False)
    if result.returncode:
        raise AssertionError(f"{mode} failed ({result.returncode}):\n"
                             f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}")
    lines = [line for line in result.stderr.splitlines()
             if line.startswith("[TJ-FLIP-TIMELINE]")]
    return result.stdout, lines


def main():
    source_text = SOURCE.read_text(encoding="utf-8")
    helpers = production_helpers(source_text)
    presenter = function(source_text, "static int tj_present_flip(int vblank,")
    fixture = r'''
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
typedef struct { long long QuadPart; } LARGE_INTEGER;
struct tj_gcm_flip_ticket { uint64_t generation; uint32_t buffer_id, source; };
enum { TJ_GCM_FLIP_PUBLICATION_QPC_OFF=0, TJ_GCM_FLIP_PUBLICATION_QPC_VALID=1,
       TJ_GCM_FLIP_PUBLICATION_QPC_FAILED=2 };
struct tj_gcm_flip_publication_timing { uint64_t publication_qpc; uint32_t publication_qpc_state; };
static uint64_t fake_qpc=1000, fake_ms=123;
static uint32_t fake_requests=0;
static int qpc_ok=1, qpc_calls=0, ms_calls=0, drain_calls=0;
static int drain_request_delta=0;
static int complete_inside_drain=0;
static uint32_t drain_tail_request_delta=0;
static struct tj_gcm_flip_ticket drain_completion_ticket={};
static struct tj_gcm_flip_ticket fake_pending={};
static int fake_has_pending=0,fake_vsync=1,fake_completion_result=1;
static unsigned fake_presents=0,fake_completion_calls=0;
static void tj_flip_timeline_complete(const tj_gcm_flip_ticket& ticket);
static int QueryPerformanceCounter(LARGE_INTEGER* now) {
    ++qpc_calls;
    if (!qpc_ok) return 0;
    now->QuadPart=(long long)++fake_qpc;
    return 1;
}
static uint64_t GetTickCount64(void) { ++ms_calls; return fake_ms; }
static uint32_t cellGcm_flip_request_count(void) { return fake_requests; }
static void cellGcm_rsx_process_fifo(void) {
    ++drain_calls;
    fake_qpc += 80;
    fake_requests += (uint32_t)drain_request_delta;
    if (complete_inside_drain) {
        tj_flip_timeline_complete(drain_completion_ticket);
        fake_qpc += 120;
        fake_requests += drain_tail_request_delta;
    }
}
static int cellGcm_flip_uses_vsync(void) { return fake_vsync; }
static int cellGcm_take_flip_ticket(tj_gcm_flip_ticket* out) {
    if (!fake_has_pending) return 0;
    *out=fake_pending; fake_has_pending=0; return 1;
}
static int cellGcm_take_flip_ticket_timed(tj_gcm_flip_ticket* out,
                                          tj_gcm_flip_publication_timing* timing) {
    if (timing) *timing={};
    return cellGcm_take_flip_ticket(out);
}
static void rsx_d3d12_backend_present_buffer(uint64_t generation,uint32_t buffer) {
    (void)generation; (void)buffer; ++fake_presents;
}
static int cellGcm_complete_flip_ticket(const tj_gcm_flip_ticket* ticket) {
    (void)ticket; ++fake_completion_calls; return fake_completion_result;
}
static int message_dump_calls=0;
static void rsx_d3d12_backend_dump_message_pump_trace(void) {
    ++message_dump_calls;
}
HELPERS
FLIP
#define CHECK(c,m) do { if (!(c)) { fprintf(stderr,"FAIL: %s\n",m); return 1; } } while (0)
static void dump(void) {
    const int before=message_dump_calls;
    const int expected=s_tj_flip_timeline.enabled && !s_tj_flip_timeline.dumped;
    tj_flip_timeline_dump(); tj_flip_timeline_dump();
    if (message_dump_calls-before!=expected) {
        fprintf(stderr,"FAIL: backend message diagnostic dump not exactly once\n");
        exit(1);
    }
}
int main(int argc,char** argv) {
    if (argc!=2) return 2;
    const char* mode=argv[1];
    if (!strcmp(mode,"off")) {
        tj_flip_timeline_init(1,1000000);
        tj_flip_timeline_tick(1,900);
        tj_gcm_flip_ticket t={1,2,1};
        tj_flip_timeline_take(t); tj_flip_timeline_present_return();
        tj_flip_timeline_complete(t); tj_flip_timeline_drain(); dump();
        fake_vsync=0; fake_completion_result=0;
        fake_pending={2,1,0}; fake_has_pending=1;
        tj_gcm_flip_ticket awaiting={};
        CHECK(tj_present_flip(0,&awaiting)==1 && fake_completion_calls==1 &&
              s_tj_flip_timeline.association_misses==0,
              "default-off failed completion touched timeline association state");
        CHECK(!s_tj_flip_timeline.enabled && qpc_calls==0 && ms_calls==0,
              "default-off made an extra clock read");
        puts("PASS: default-off timeline has no clock reads"); return 0;
    }
    if (!strcmp(mode,"present_fail_awaiting") || !strcmp(mode,"present_fail_immediate")) {
        tj_flip_timeline_init(1,1000000);
        fake_vsync=!strcmp(mode,"present_fail_awaiting");
        fake_completion_result=0;
        fake_pending={41,2,1}; fake_has_pending=1;
        tj_gcm_flip_ticket awaiting={};
        CHECK(tj_present_flip(0,&awaiting)==1 && fake_presents==1,
              "failed-completion frame did not reach the production presenter");
        if (fake_vsync) {
            CHECK(awaiting.generation==41 && fake_completion_calls==0,
                  "VSync completion ran before its VBlank");
            CHECK(tj_present_flip(1,&awaiting)==0 && fake_completion_calls==1 &&
                  !awaiting.generation,
                  "failed awaiting-VBlank completion changed presenter control flow");
        } else {
            CHECK(!awaiting.generation && fake_completion_calls==1,
                  "failed immediate completion changed presenter control flow");
        }
        CHECK(s_tj_flip_timeline.takes==1 && s_tj_flip_timeline.completions==0 &&
              s_tj_flip_timeline.association_misses==1 &&
              s_tj_flip_timeline.active.generation==0 &&
              s_tj_flip_timeline.ring[0].seq==0,
              "failed GCM completion became a completed row or retained a stale active record");

        fake_completion_result=1;
        fake_pending={42,1,0}; fake_has_pending=1;
        CHECK(tj_present_flip(fake_vsync ? 1 : 0,&awaiting)==1 &&
              fake_completion_calls==2,
              "valid frame after failed completion did not complete");
        CHECK(s_tj_flip_timeline.takes==2 && s_tj_flip_timeline.completions==1 &&
              s_tj_flip_timeline.association_misses==1 &&
              s_tj_flip_timeline.ring[0].seq==1 &&
              s_tj_flip_timeline.ring[0].generation==42 &&
              !s_tj_flip_timeline.ring[0].drain_time_valid &&
              !s_tj_flip_timeline.ring[0].request_delta_valid,
              "next valid frame did not recover with uncertain intervals flagged");
        dump();
        puts("PASS: rejected completion is omitted and next valid ticket recovers"); return 0;
    }
    if (!strcmp(mode,"init_failure")) {
        tj_flip_timeline_init(0,0);
        tj_flip_timeline_tick(0,900);
        tj_gcm_flip_ticket t={17,3,1};
        tj_flip_timeline_take(t); tj_flip_timeline_present_return();
        tj_flip_timeline_complete(t); dump();
        CHECK(s_tj_flip_timeline.enabled && !s_tj_flip_timeline.qpc_init_ok &&
              !s_tj_flip_timeline.qpc_available && s_tj_flip_timeline.frequency==0,
              "fallback clock was reported as QPC");
        CHECK(qpc_calls==0 && ms_calls==1,"QPC init failure sampled the counter or missed host anchor");
        puts("PASS: unavailable QPC is explicit and produces invalid timing fields"); return 0;
    }
    if (!strcmp(mode,"runtime_failure")) {
        tj_flip_timeline_init(1,1000000);
        CHECK(s_tj_flip_timeline.qpc_init_ok && s_tj_flip_timeline.qpc_available &&
              s_tj_flip_timeline.anchor!=0,"QPC anchor was not captured");
        qpc_ok=0;
        CHECK(tj_flip_timeline_now("take")==0 && !s_tj_flip_timeline.qpc_available &&
              s_tj_flip_timeline.qpc_failures==1 &&
              !strcmp(s_tj_flip_timeline.qpc_failure_stage,"take"),
              "runtime QPC failure was not contained and labeled");
        CHECK(tj_flip_timeline_now("after_failure")==0 && qpc_calls==3,
              "QPC was queried again after becoming unavailable");
        dump();
        puts("PASS: runtime QPC failure disables further stamps"); return 0;
    }
    if (!strcmp(mode,"restart")) {
        tj_flip_timeline_init(1,1000000);
        tj_gcm_flip_ticket t={5,1,1};
        tj_flip_timeline_take(t); tj_flip_timeline_present_return();
        tj_flip_timeline_complete(t);
        CHECK(s_tj_flip_timeline.completions==1 && s_tj_flip_timeline.takes==1,
              "initial trace state did not record its ticket");
        tj_flip_timeline_init(1,2000000);
        CHECK(s_tj_flip_timeline.enabled && s_tj_flip_timeline.frequency==2000000 &&
              s_tj_flip_timeline.takes==0 && s_tj_flip_timeline.completions==0 &&
              s_tj_flip_timeline.association_misses==0 && s_tj_flip_timeline.ring[0].seq==0,
              "presenter restart retained prior trace state");
        dump();
        CHECK(qpc_calls==7 && ms_calls==2,"restart anchor accounting is wrong");
        puts("PASS: enabled presenter restart clears the timeline state"); return 0;
    }
    if (!strcmp(mode,"association")) {
        tj_flip_timeline_init(1,1000000);
        fake_requests=UINT32_MAX-1u; drain_request_delta=3;
        tj_flip_timeline_tick(1,900);
        tj_gcm_flip_ticket t={77,4,1};
        tj_flip_timeline_take(t); tj_flip_timeline_present_return();
        tj_flip_timeline_drain();
        tj_flip_timeline_tick(1,1900);
        tj_flip_timeline_complete(t);
        tj_gcm_flip_ticket t2={79,5,0}, wrong2={80,5,0};
        tj_flip_timeline_take(t2); tj_flip_timeline_present_return();
        tj_flip_timeline_complete(wrong2); // mismatch must leave the active generation pending
        CHECK(s_tj_flip_timeline.completions==1 && s_tj_flip_timeline.active.generation==79,
              "wrong generation consumed the active ticket");
        tj_flip_timeline_complete(t2); dump();
        CHECK(s_tj_flip_timeline.takes==2 && s_tj_flip_timeline.completions==2 &&
              s_tj_flip_timeline.association_misses==1,
              "completion did not require the exact ticket generation");
        const TjFlipTimelineRecord& r=s_tj_flip_timeline.ring[0];
        CHECK(r.generation==77 && r.source==1 && r.buffer==4 &&
              r.take_qpc_valid && r.present_return_qpc_valid && r.complete_qpc_valid,
              "completed row lost ticket identity or QPC stamps");
        CHECK(r.present_tick_context && r.present_tick_valid &&
              r.present_tick_due_qpc==900 && r.present_tick_actual_qpc!=0 &&
              r.complete_tick_context && r.complete_tick_valid &&
              r.complete_tick_due_qpc==1900 && r.complete_tick_actual_qpc!=0,
              "present/completion tick association is wrong");
        CHECK(r.drain_time_valid && r.drain_calls_since_prev_complete==1 &&
              r.drain_ns_since_prev_complete==81000 && r.request_delta_valid &&
              r.request_delta_during_drain==3,
              "drain duration or wrapped request-counter delta is wrong");
        const TjFlipTimelineRecord& r2=s_tj_flip_timeline.ring[1];
        CHECK(r2.generation==79 && !r2.drain_time_valid && !r2.request_delta_valid,
              "association uncertainty did not invalidate the affected interval");
        puts("PASS: exact generation association, QPC stamps, and wrapped drain delta"); return 0;
    }
    if (!strcmp(mode,"drain_split")) {
        tj_flip_timeline_init(1,1000000);
        fake_requests=UINT32_MAX-1u; drain_request_delta=3;
        drain_tail_request_delta=5;
        drain_completion_ticket={88,4,1};
        tj_flip_timeline_take(drain_completion_ticket);
        tj_flip_timeline_present_return();
        complete_inside_drain=1;
        tj_flip_timeline_drain();
        complete_inside_drain=0;
        tj_gcm_flip_ticket after={89,5,0};
        tj_flip_timeline_take(after); tj_flip_timeline_present_return();
        tj_flip_timeline_complete(after); dump();
        CHECK(s_tj_flip_timeline.completions==2 &&
              s_tj_flip_timeline.association_misses==0,
              "a completion inside FIFO drain lost ticket association");
        const TjFlipTimelineRecord& first=s_tj_flip_timeline.ring[0];
        const TjFlipTimelineRecord& second=s_tj_flip_timeline.ring[1];
        CHECK(first.generation==88 && first.drain_time_valid &&
              first.drain_ns_since_prev_complete==81000 &&
              first.request_delta_valid && first.request_delta_during_drain==3 &&
              first.drain_calls_since_prev_complete==1 && first.drain_interval_split,
              "pre-completion drain segment was not attributed exactly once");
        CHECK(second.generation==89 && second.drain_time_valid &&
              second.drain_ns_since_prev_complete==121000 &&
              second.request_delta_valid && second.request_delta_during_drain==5 &&
              second.drain_calls_since_prev_complete==0 && second.drain_interval_split,
              "post-completion drain segment was doubled or lost");
        CHECK(first.drain_ns_since_prev_complete+second.drain_ns_since_prev_complete==202000 &&
              first.request_delta_during_drain+second.request_delta_during_drain==8,
              "split drain duration or request delta did not conserve the full drain");
        puts("PASS: completion splits active drain duration/request attribution without double count"); return 0;
    }
    if (!strcmp(mode,"publication")) {
        tj_flip_timeline_init(1,1000000);
        tj_gcm_flip_ticket t={701,2,1};
        tj_gcm_flip_publication_timing valid={1000,TJ_GCM_FLIP_PUBLICATION_QPC_VALID};
        tj_flip_timeline_take(t,&valid); tj_flip_timeline_present_return();
        tj_flip_timeline_complete(t);
        const TjFlipTimelineRecord& good=s_tj_flip_timeline.ring[0];
        CHECK(good.publication_qpc==1000 && good.publication_qpc_state==1 &&
              good.publish_to_take_valid && good.publish_to_take_ticks>0,
              "valid publication stamp was not copied before take");
        tj_gcm_flip_ticket failed={702,2,1};
        tj_gcm_flip_publication_timing bad={0,TJ_GCM_FLIP_PUBLICATION_QPC_FAILED};
        tj_flip_timeline_take(failed,&bad); tj_flip_timeline_present_return();
        tj_flip_timeline_complete(failed);
        const TjFlipTimelineRecord& badrow=s_tj_flip_timeline.ring[1];
        CHECK(badrow.publication_qpc==0 && badrow.publication_qpc_state==2 &&
              !badrow.publish_to_take_valid && !badrow.publish_to_take_ticks,
              "failed publication stamp produced an interval");
        tj_gcm_flip_ticket late={703,2,1};
        tj_gcm_flip_publication_timing future={UINT64_MAX,TJ_GCM_FLIP_PUBLICATION_QPC_VALID};
        tj_flip_timeline_take(late,&future); tj_flip_timeline_present_return();
        tj_flip_timeline_complete(late);
        const TjFlipTimelineRecord& laterow=s_tj_flip_timeline.ring[2];
        CHECK(laterow.publication_qpc==UINT64_MAX && !laterow.publish_to_take_valid &&
              !laterow.publish_to_take_ticks,"publication after take wrapped an interval");
        tj_gcm_flip_ticket zero={704,2,1};
        tj_gcm_flip_publication_timing malformed={0,TJ_GCM_FLIP_PUBLICATION_QPC_VALID};
        tj_flip_timeline_take(zero,&malformed); tj_flip_timeline_present_return();
        tj_flip_timeline_complete(zero); dump();
        const TjFlipTimelineRecord& zerorow=s_tj_flip_timeline.ring[3];
        CHECK(zerorow.publication_qpc_state==1 && !zerorow.publish_to_take_valid &&
              !zerorow.publish_to_take_ticks,"zero publication timestamp became valid latency");
        puts("PASS: publication-to-take states reject failed and reversed stamps"); return 0;
    }
    if (!strcmp(mode,"overflow")) {
        tj_flip_timeline_init(1,1000000);
        for (uint64_t i=1;i<=1025;i++) {
            tj_gcm_flip_ticket t={1000+i,(uint32_t)(i%3),(uint32_t)(i%2)};
            tj_flip_timeline_take(t); tj_flip_timeline_present_return();
            tj_flip_timeline_complete(t);
        }
        dump();
        CHECK(s_tj_flip_timeline.takes==1025 && s_tj_flip_timeline.completions==1025,
              "take/completion counts did not advance");
        puts("PASS: 1024-slot wrap retains newest completed tickets"); return 0;
    }
    if (!strcmp(mode,"pending")) {
        tj_flip_timeline_init(1,1000000);
        tj_gcm_flip_ticket t={9001,1,1};
        tj_flip_timeline_take(t); dump();
        CHECK(s_tj_flip_timeline.takes==1 && s_tj_flip_timeline.completions==0,
              "unfinished ticket was counted as a completion");
        puts("PASS: pending take is excluded from completed ring rows"); return 0;
    }
    return 2;
}
'''.replace("HELPERS", helpers).replace("\nFLIP\n", "\n" + presenter + "\n")

    compiler = shutil.which("clang++") or "C:/Program Files/LLVM/bin/clang++.exe"
    with tempfile.TemporaryDirectory(prefix="tj-flip-timeline-") as temp:
        source = Path(temp) / "timeline.cpp"
        exe = Path(temp) / "timeline.exe"
        source.write_text(fixture, encoding="utf-8")
        subprocess.run([compiler, "-std=c++17", "-O2", "-D_CRT_SECURE_NO_WARNINGS",
                        str(source), "-o", str(exe)], check=True)

        stdout, lines = run_case(exe, "off", False)
        assert not lines and "PASS:" in stdout

        for mode in ("present_fail_awaiting", "present_fail_immediate"):
            stdout, lines = run_case(exe, mode, True)
            assert len(lines) == 2, (mode, lines)
            header, row = map(fields, lines)
            assert (header["takes"], header["completions"], header["records"],
                    header["association_misses"]) == ("2", "1", "1", "1")
            assert row["generation"] == "42"
            assert row["drain_time_valid"] == "0" and row["request_delta_valid"] == "0"
            assert "PASS:" in stdout

        stdout, lines = run_case(exe, "init_failure", True)
        assert len(lines) == 2, lines
        header, row = map(fields, lines)
        assert (header["enabled"], header["qpc_init_ok"], header["qpc_available"],
                header["qpc_frequency"], header["records"]) == ("1", "0", "0", "0", "1")
        assert (row["generation"], row["take_qpc_valid"], row["present_return_qpc_valid"],
                row["complete_qpc_valid"], row["present_tick_context"],
                row["present_tick_valid"], row["complete_tick_context"],
                row["complete_tick_valid"]) == ("17", "0", "0", "0", "1", "0", "1", "0")
        assert "PASS:" in stdout

        stdout, lines = run_case(exe, "runtime_failure", True)
        assert len(lines) == 1, lines
        header = fields(lines[0])
        assert (header["qpc_init_ok"], header["qpc_available"], header["qpc_failures"],
                header["qpc_failure_stage"]) == ("1", "0", "1", "take")
        assert "PASS:" in stdout

        stdout, lines = run_case(exe, "restart", True)
        assert len(lines) == 1, lines
        header = fields(lines[0])
        assert (header["takes"], header["completions"], header["records"],
                header["qpc_frequency"]) == ("0", "0", "0", "2000000")
        assert "PASS:" in stdout

        stdout, lines = run_case(exe, "association", True)
        assert len(lines) == 3, lines
        header, row, row2 = map(fields, lines)
        assert (header["takes"], header["completions"], header["records"],
                header["association_misses"]) == ("2", "2", "2", "1")
        assert (row["generation"], row["request_delta_during_drain"],
                row["drain_calls_since_prev_complete"], row["drain_ns_since_prev_complete"],
                row["drain_time_valid"], row["interval_kind"]) == (
                    "77", "3", "1", "81000", "1", "first")
        assert (row2["generation"], row2["drain_time_valid"],
                row2["request_delta_valid"], row2["interval_kind"]) == (
                    "79", "0", "0", "steady")
        assert "PASS:" in stdout

        stdout, lines = run_case(exe, "drain_split", True)
        assert len(lines) == 3, lines
        header, split, tail = map(fields, lines)
        assert header["drain_calls_semantics"] == "starts_in_interval"
        assert (split["generation"], split["drain_ns_since_prev_complete"],
                split["drain_calls_since_prev_complete"],
                split["request_delta_during_drain"], split["drain_interval_split"]) == (
                    "88", "81000", "1", "3", "1")
        assert (tail["generation"], tail["drain_ns_since_prev_complete"],
                tail["drain_calls_since_prev_complete"],
                tail["request_delta_during_drain"], tail["drain_interval_split"]) == (
                    "89", "121000", "0", "5", "1")
        assert "PASS:" in stdout

        stdout, lines = run_case(exe, "publication", True)
        assert len(lines) == 5, lines
        _, good, failed, late, zero = map(fields, lines)
        assert (good["publication_qpc_state"], good["publish_to_take_valid"],
                failed["publication_qpc_state"], failed["publish_to_take_valid"],
                late["publish_to_take_valid"], zero["publish_to_take_valid"]) == ("1", "1", "2", "0", "0", "0")
        assert "PASS:" in stdout

        stdout, lines = run_case(exe, "overflow", True)
        assert len(lines) == 1025, len(lines)
        header = fields(lines[0])
        rows = [fields(line) for line in lines[1:]]
        assert (header["takes"], header["completions"], header["records"],
                header["ring_overwrites"], header["association_misses"]) == (
                    "1025", "1025", "1024", "1", "0")
        assert len(rows) == 1024 and (rows[0]["seq"], rows[0]["generation"],
                                      rows[-1]["seq"], rows[-1]["generation"]) == (
                                          "2", "1002", "1025", "2025")
        assert int(header["qpc_anchor_after"]) > int(header["qpc_anchor"])
        assert header["host_ms_uncertainty_ms"] == "32"
        assert all(row["present_tick_context"] == "0" and row["complete_tick_context"] == "0"
                   for row in rows), "off-VBlank rows must declare zero tick context"
        assert rows[0]["interval_kind"] == "steady"
        assert "PASS:" in stdout

        stdout, lines = run_case(exe, "pending", True)
        assert len(lines) == 1 and fields(lines[0])["takes"] == "1" and \
            fields(lines[0])["completions"] == "0" and fields(lines[0])["records"] == "0"
        assert "PASS:" in stdout

    print("PASS: default-off, rejected immediate/VBlank completions, valid-frame recovery, QPC init/runtime failure, enabled restart reset, exact ticket association, split-drain attribution, wrapped request delta, 1024 wrap, and completed-only rows")


if __name__ == "__main__":
    main()
