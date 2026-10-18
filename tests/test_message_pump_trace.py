"""Compile the actual Win32 pump and WndProc trace against bounded stubs."""
from pathlib import Path
import os
import re
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT.parent / "ps3recomp/libs/video/rsx_d3d12_backend.c"
PRESENTER = ROOT / "src/tj_present.cpp"


def function(source, signature):
    start = source.index(signature)
    brace = source.index("{", start)
    depth = 1
    end = brace + 1
    while depth:
        depth += (source[end] == "{") - (source[end] == "}")
        end += 1
    return source[start:end]


def production_parts():
    backend = BACKEND.read_text(encoding="utf-8")
    start = backend.index("/* One retained slow pump, enabled only by the flip-timeline diagnostic. */")
    end = backend.index("static HWND create_window(", start)
    helpers = backend[start:end]
    pump = function(backend, "int rsx_d3d12_backend_pump_messages(void)")
    host = PRESENTER.read_text(encoding="utf-8")
    dump = function(host, "static void tj_flip_timeline_dump(void)")
    export_poll = function(host, "static void tj_flip_timeline_export_poll(void)")
    thread = function(host, "static DWORD WINAPI tj_present_thread(")
    assert dump.index("rsx_d3d12_backend_dump_message_pump_trace();") < \
        dump.index("for (uint64_t i = 0; i < records; ++i)")
    assert "tj_flip_timeline_dump();" in export_poll
    assert "tj_flip_timeline_dump();" in thread
    return helpers, pump


def fixture_source():
    helpers, pump = production_parts()
    prefix = r'''
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <deque>
#include <string>
using u8 = uint8_t; using u16 = uint16_t; using u32 = uint32_t;
using UINT = unsigned int; using DWORD = uint32_t;
using WPARAM = uintptr_t; using LPARAM = intptr_t; using LRESULT = intptr_t;
using HWND = void*; using BOOL = int;
struct LARGE_INTEGER { long long QuadPart; };
struct MSG { HWND hwnd; UINT message; WPARAM wParam; LPARAM lParam; };
#define CALLBACK
#define PM_REMOVE 1
#define WM_APP 0x8000
#define WM_DESTROY 0x0002
#define WM_SIZE 0x0005
#define WM_CLOSE 0x0010
#define WM_QUIT 0x0012
#define WM_MOVE 0x0003
#define WM_KEYDOWN 0x0100
#define WM_SYSCOMMAND 0x0112
#define SC_SIZE 0xF000
#define SC_MOVE 0xF010
static struct { HWND hwnd; int window_closed; } s_d3d = {};
static int QueryPerformanceFrequency(LARGE_INTEGER*);
static int QueryPerformanceCounter(LARGE_INTEGER*);
static BOOL PeekMessageA(MSG*, HWND, UINT, UINT, UINT);
static BOOL TranslateMessage(const MSG*);
static LRESULT DispatchMessageA(const MSG*);
static LRESULT CALLBACK d3d12_wndproc(HWND, UINT, WPARAM, LPARAM);
static BOOL DestroyWindow(HWND);
static void PostQuitMessage(int);
static LRESULT DefWindowProcA(HWND, UINT, WPARAM, LPARAM);
static void cellPadDebugPulse(uint16_t, uint32_t);
static void cellPadDebugAnalogPulse(uint8_t, uint8_t, uint32_t);
'''
    support = r'''
static std::deque<MSG> queue;
static std::string order;
static uint64_t fake_qpc = 100000;
static long long last_qpc_return = 0;
static uint64_t fake_frequency = 1000;
static uint64_t slow_ticks_current = 0;
static int frequency_calls, qpc_calls, qpc_fail_on, qpc_back_on, frequency_fail;
static int peek_calls, translate_calls, dispatch_calls, pulse_calls, analog_calls;
static int sent_pending, sent_nested, nested_seen;
static UINT sent_message;
static WPARAM sent_wparam;

static int QueryPerformanceFrequency(LARGE_INTEGER* frequency) {
    ++frequency_calls;
    if (frequency_fail) return 0;
    frequency->QuadPart = (long long)fake_frequency;
    return 1;
}
static int QueryPerformanceCounter(LARGE_INTEGER* counter) {
    ++qpc_calls;
    if (qpc_fail_on && qpc_calls == qpc_fail_on) return 0;
    if (qpc_back_on && qpc_calls == qpc_back_on) {
        counter->QuadPart = last_qpc_return - 1;
        return 1;
    }
    counter->QuadPart = (long long)++fake_qpc;
    last_qpc_return = counter->QuadPart;
    return 1;
}
static BOOL PeekMessageA(MSG* message, HWND, UINT, UINT, UINT) {
    ++peek_calls; order.push_back('P');
    if (sent_pending) {
        sent_pending = 0;
        d3d12_wndproc(NULL, sent_message, sent_wparam, 0);
    }
    if (queue.empty()) return 0;
    *message = queue.front(); queue.pop_front();
    return 1;
}
static BOOL TranslateMessage(const MSG*) {
    ++translate_calls; order.push_back('T'); return 1;
}
static LRESULT DispatchMessageA(const MSG* message) {
    ++dispatch_calls; order.push_back('D');
    return d3d12_wndproc(message->hwnd, message->message,
                         message->wParam, message->lParam);
}
static BOOL DestroyWindow(HWND) {
    d3d12_wndproc(NULL, WM_DESTROY, 0, 0); return 1;
}
static void PostQuitMessage(int) {}
static LRESULT DefWindowProcA(HWND hwnd, UINT message, WPARAM, LPARAM) {
    if (message == WM_SYSCOMMAND) {
        fake_qpc += slow_ticks_current;
        if (sent_nested) {
            ++nested_seen;
            d3d12_wndproc(hwnd, WM_SIZE, 0, 0);
        }
    }
    return 0;
}
static void cellPadDebugPulse(uint16_t, uint32_t) { ++pulse_calls; }
static void cellPadDebugAnalogPulse(uint8_t, uint8_t, uint32_t) { ++analog_calls; }
static void enqueue(UINT message, WPARAM wparam=0) {
    MSG row = {}; row.message=message; row.wParam=wparam; queue.push_back(row);
}
static void reset_api(void) {
    queue.clear(); order.clear(); fake_qpc=100000; last_qpc_return=0;
    slow_ticks_current=0; frequency_calls=qpc_calls=qpc_fail_on=qpc_back_on=0;
    frequency_fail=peek_calls=translate_calls=dispatch_calls=pulse_calls=0;
    analog_calls=sent_pending=sent_nested=nested_seen=0;
    sent_message=0; sent_wparam=0;
}
#define CHECK(c,m) do { if (!(c)) { fprintf(stderr,"FAIL: %s\n",m); return 1; } } while (0)
'''
    main = r'''
int main(int argc, char** argv) {
    if (argc != 2) return 2;
    const char* mode=argv[1]; reset_api();
    if (!strcmp(mode,"off")) {
        enqueue(WM_APP+0x51); enqueue(WM_QUIT); enqueue(WM_APP+0x52);
        int result=rsx_d3d12_backend_pump_messages();
        CHECK(result==-1 && order=="PTDP" && translate_calls==1 &&
              dispatch_calls==1 && pulse_calls==1 && queue.size()==1,
              "default-off changed Peek/Translate/Dispatch/WM_QUIT behavior");
        CHECK(frequency_calls==0 && qpc_calls==0,
              "default-off called a QPC API");
        rsx_d3d12_backend_dump_message_pump_trace();
        puts("PASS: default-off exact message order and zero QPC calls"); return 0;
    }
    if (!strcmp(mode,"empty")) {
        CHECK(rsx_d3d12_backend_pump_messages()==0 && order=="P" &&
              translate_calls==0 && dispatch_calls==0,
              "empty queue changed its normal result/order");
        CHECK(!s_tj_message_pump_trace.best_valid &&
              s_tj_message_pump_trace.qualifying_count==0 &&
              s_tj_message_pump_trace.invalid_pumps==0,
              "empty queue produced an invalid or slow event");
    } else if (!strcmp(mode,"sent")) {
        enqueue(WM_APP+0x52); sent_pending=1;
        sent_message=WM_SYSCOMMAND; sent_wparam=SC_SIZE; slow_ticks_current=150;
        CHECK(rsx_d3d12_backend_pump_messages()==0 && order=="PTDP" &&
              analog_calls==1 && dispatch_calls==1,
              "sent-message probe changed posted message processing");
        CHECK(s_tj_message_pump_trace.best_valid &&
              s_tj_message_pump_trace.best.handler_valid &&
              s_tj_message_pump_trace.best.handler_phase==TJ_MESSAGE_PUMP_PEEK &&
              s_tj_message_pump_trace.best.handler_message==WM_SYSCOMMAND &&
              s_tj_message_pump_trace.best.handler_wparam==SC_SIZE,
              "Peek attributed the returned posted message instead of sent WndProc");
    } else if (!strcmp(mode,"dispatch")) {
        enqueue(WM_SYSCOMMAND, SC_SIZE); slow_ticks_current=170; sent_nested=1;
        CHECK(rsx_d3d12_backend_pump_messages()==0 && order=="PTDP" &&
              nested_seen==1 && dispatch_calls==1,
              "nested move/size callback changed dispatch order");
        CHECK(s_tj_message_pump_trace.best.handler_valid &&
              s_tj_message_pump_trace.best.handler_phase==TJ_MESSAGE_PUMP_DISPATCH &&
              s_tj_message_pump_trace.best.handler_message==WM_SYSCOMMAND &&
              s_tj_message_pump_trace.best.handler_wparam==SC_SIZE,
              "nested WndProc displaced the top-level system-command attribution");
    } else if (!strcmp(mode,"flood")) {
        for (unsigned i=0;i<80;++i) enqueue(WM_APP+0x33);
        CHECK(rsx_d3d12_backend_pump_messages()==0 &&
              translate_calls==80 && dispatch_calls==80,
              "fast message flood changed normal delivery count");
        CHECK(s_tj_message_pump_trace.best_valid &&
              s_tj_message_pump_trace.best.message_count==80 &&
              !s_tj_message_pump_trace.best.handler_valid &&
              s_tj_message_pump_trace.best.handler_message==0,
              "message flood was falsely attributed to a fast handler");
    } else if (!strcmp(mode,"quit")) {
        enqueue(WM_QUIT); enqueue(WM_APP+0x51);
        CHECK(rsx_d3d12_backend_pump_messages()==-1 && order=="P" &&
              translate_calls==0 && dispatch_calls==0 && queue.size()==1,
              "WM_QUIT no longer exits before Translate/Dispatch");
    } else if (!strcmp(mode,"qpcfail")) {
        enqueue(WM_APP+0x51); qpc_fail_on=2;
        CHECK(rsx_d3d12_backend_pump_messages()==0 && pulse_calls==1 &&
              order=="PTDP" && !s_tj_message_pump_trace.best_valid &&
              s_tj_message_pump_trace.invalid_pumps==1 &&
              s_tj_message_pump_trace.qpc_failures==1,
              "QPC failure affected delivery or lacked invalid accounting");
    } else if (!strcmp(mode,"qpcback")) {
        enqueue(WM_APP+0x51); qpc_back_on=3;
        CHECK(rsx_d3d12_backend_pump_messages()==0 && pulse_calls==1 &&
              order=="PTDP" && !s_tj_message_pump_trace.best_valid &&
              s_tj_message_pump_trace.invalid_pumps==1 &&
              s_tj_message_pump_trace.qpc_failures==1,
              "backward QPC affected delivery or lacked invalid accounting");
    } else if (!strcmp(mode,"freqfail")) {
        enqueue(WM_APP+0x51); frequency_fail=1;
        CHECK(rsx_d3d12_backend_pump_messages()==0 && pulse_calls==1 &&
              order=="PTDP" && qpc_calls==0 &&
              s_tj_message_pump_trace.invalid_pumps==1 &&
              s_tj_message_pump_trace.qpc_failures==1,
              "QPF failure affected delivery or lacked invalid accounting");
    } else if (!strcmp(mode,"retain")) {
        const uint64_t delays[]={150,230,180};
        const WPARAM ids[]={SC_MOVE,SC_SIZE,SC_MOVE};
        for (unsigned i=0;i<3;++i) {
            queue.clear(); order.clear(); slow_ticks_current=delays[i];
            enqueue(WM_SYSCOMMAND,ids[i]);
            CHECK(rsx_d3d12_backend_pump_messages()==0,
                  "retention case changed pump return");
        }
        CHECK(s_tj_message_pump_trace.best_valid &&
              s_tj_message_pump_trace.qualifying_count==3 &&
              s_tj_message_pump_trace.omitted_count==2 &&
              s_tj_message_pump_trace.best.handler_wparam==SC_SIZE &&
              s_tj_message_pump_trace.best.end_qpc>
                  s_tj_message_pump_trace.best.begin_qpc,
              "largest interval/raw bounds/lost count were not retained");
    } else if (!strcmp(mode,"history")) {
        enqueue(WM_SYSCOMMAND,SC_SIZE); slow_ticks_current=150;
        CHECK(rsx_d3d12_backend_pump_messages()==0 &&
              s_tj_message_pump_trace.best_valid,
              "initial valid slow pump was not retained");
        uint64_t old_begin=s_tj_message_pump_trace.best.begin_qpc;
        uint64_t old_end=s_tj_message_pump_trace.best.end_qpc;
        queue.clear(); order.clear(); slow_ticks_current=0;
        enqueue(WM_APP+0x51); qpc_fail_on=qpc_calls+1;
        CHECK(rsx_d3d12_backend_pump_messages()==0 && pulse_calls==1 &&
              order=="PTDP" && s_tj_message_pump_trace.best_valid &&
              s_tj_message_pump_trace.best.begin_qpc==old_begin &&
              s_tj_message_pump_trace.best.end_qpc==old_end &&
              s_tj_message_pump_trace.qpc_failures==1 &&
              s_tj_message_pump_trace.invalid_pumps==1,
              "later clock failure damaged the earlier valid raw record");
    } else return 2;
    rsx_d3d12_backend_dump_message_pump_trace();
    puts("PASS: traced pump semantics and bounded attribution"); return 0;
}
'''
    return prefix + helpers + "\n" + support + "\n" + pump + "\n" + main


def run_case(exe, mode, opt_in):
    env = dict(os.environ)
    env.pop("TJ_FLIP_TIMELINE", None)
    if opt_in is not None:
        env["TJ_FLIP_TIMELINE"] = opt_in
    result = subprocess.run([str(exe), mode], env=env, capture_output=True,
                            text=True, timeout=15, check=False)
    if result.returncode:
        raise AssertionError(f"{mode} failed ({result.returncode}):\n"
                             f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}")
    lines = [line for line in result.stderr.splitlines()
             if line.startswith("[TJ-FLIP-TIMELINE] schema=2 event=message_pump")]
    return result.stdout, [dict(re.findall(r"([A-Za-z_][A-Za-z0-9_]*)=([^ ]*)", line))
                           for line in lines]


def main():
    fixture = fixture_source()
    compiler = shutil.which("clang++") or r"C:\Program Files\LLVM\bin\clang++.exe"
    with tempfile.TemporaryDirectory(prefix="tj-message-pump-trace-") as temp:
        temp = Path(temp)
        source = temp / "fixture.cpp"
        executable = temp / "fixture.exe"
        source.write_text(fixture, encoding="utf-8")
        subprocess.run([compiler, "-std=c++17", "-O2", "-D_CRT_SECURE_NO_WARNINGS",
                        str(source), "-o", str(executable)], check=True)
        out, events = run_case(executable, "off", None)
        assert not events and "PASS:" in out
        out, events = run_case(executable, "off", "01")
        assert not events and "PASS:" in out
        for mode in ("empty", "sent", "dispatch", "flood", "quit",
                     "qpcfail", "qpcback", "freqfail", "retain", "history"):
            out, events = run_case(executable, mode, "1")
            assert len(events) == 1, (mode, events)
            assert "PASS:" in out
            event = events[0]
            if mode == "empty":
                assert event["retained"] == "0" and \
                    event["removed_messages"] == "0"
            elif mode == "sent":
                assert event["handler_phase"] == "peek" and \
                    event["handler_name"] == "WM_SYSCOMMAND" and \
                    event["handler_msg"] == "0x0112" and \
                    event["handler_wparam"] == "0xF000"
            elif mode == "dispatch":
                assert event["handler_phase"] == "dispatch" and \
                    event["handler_name"] == "WM_SYSCOMMAND" and \
                    event["handler_msg"] == "0x0112" and \
                    event["handler_wparam"] == "0xF000"
            elif mode == "flood":
                assert event["removed_messages"] == "80" and \
                    int(event["elapsed_ticks"]) > 100 and \
                    event["handler_valid"] == "0" and \
                    event["handler_phase"] == "unknown"
            elif mode in ("qpcfail", "qpcback", "freqfail"):
                assert event["retained"] == "0" and \
                    event["qpc_failures"] == "1" and \
                    event["invalid_pumps"] == "1"
            elif mode == "retain":
                assert event["qualifying_count"] == "3" and \
                    event["omitted_count"] == "2" and \
                    event["handler_wparam"] == "0xF000" and \
                    int(event["end_qpc"]) > int(event["begin_qpc"]) and \
                    int(event["elapsed_ticks"]) == \
                    int(event["end_qpc"]) - int(event["begin_qpc"])
            elif mode == "history":
                assert event["retained"] == "1" and \
                    event["record_qpc_valid"] == "1" and \
                    event["qpc_available"] == "0" and \
                    event["qpc_failures"] == "1" and \
                    event["invalid_pumps"] == "1" and \
                    event["qualifying_count"] == "1" and \
                    int(event["end_qpc"]) > int(event["begin_qpc"])
        print("PASS: exact opt-in, actual pump/WndProc attribution, queue and quit semantics, bounded retention, QPC invalidation")


if __name__ == "__main__":
    main()
