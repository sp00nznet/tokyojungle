"""Exercise the actual opt-in FIFO kick setup and wait helper without a game."""
from pathlib import Path
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src/tj_present.cpp"
CADENCE = ROOT / "src/tj_vblank_cadence.h"
LAUNCHER = ROOT.parent.parent / "launch_tokyojungle.ps1"


def function(source, signature):
    start = source.index(signature)
    brace = source.index("{", start)
    depth = 0
    for index in range(brace, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[start:index + 1]
    raise AssertionError(f"unterminated function: {signature}")


def main():
    present = SOURCE.read_text(encoding="utf-8")
    helper_start = present.index("/* TJ_FIFO_KICK_WAIT=1")
    helper_end = present.index("static uint64_t tj_vblank_clock_now(", helper_start)
    helpers = present[helper_start:helper_end]
    clock = function(present, "static uint64_t tj_vblank_clock_now(")
    cadence = CADENCE.read_text(encoding="utf-8")
    thread = function(present, "static DWORD WINAPI tj_present_thread(")
    start = function(present, 'extern "C" void tj_start_present_thread(')
    stop = function(present, 'extern "C" void tj_stop_present_thread(')

    assert 'getenv("TJ_FIFO_KICK_WAIT")' in helpers
    assert 'strcmp(setting, "1") == 0' in helpers
    assert "WaitForSingleObject(event, timeout_ms)" in helpers
    assert helpers.count("if (timeout_ms) Sleep(1);") == 3
    assert "DWORD error = result == WAIT_FAILED ? GetLastError() : 0;" in helpers
    assert "CloseHandle" not in helpers
    assert start.index('if (getenv("TJ_NO_PRESENT"))') < start.index("if (g_tj_present_thread) return;")
    assert start.index("if (g_tj_present_thread) return;") < start.index("tj_fifo_kick_wait_event(&fifo_kick_requested)")
    assert start.index("tj_fifo_kick_wait_event(&fifo_kick_requested)") < start.index("tj_start_gcm_interrupt_thread()")
    assert start.index("tj_start_gcm_interrupt_thread()") < start.index("CreateThread(")
    assert "tj_present_thread, fifo_kick_event" in start
    assert "fifo_kick_event != NULL, fifo_kick_requested" in start
    assert "tj_fifo_kick_wait_once(" in thread
    assert "if (fifo_kick_wait_requested && perf_enabled)" in thread
    assert "CloseHandle(fifo_kick_event)" not in stop
    pacing = thread.index("tj_fifo_kick_wait_once(")
    first_clock = thread.index("tj_vblank_clock_now(")
    assert first_clock < pacing
    assert thread.index("wait_timeout_ms") < pacing
    assert pacing < thread.index("uint64_t now = tj_vblank_clock_now(")
    for unchanged in ("cellGcmTickVBlank();", "tj_present_flip(1, &awaiting_vblank)",
                      "tj_flip_timeline_drain();", "tj_vblank_cadence_advance(&vblank_cadence);"):
        assert unchanged in thread
    assert not any(name in helpers for name in (
        "QueryPerformanceCounter", "tj_vblank_cadence_", "cellGcmTickVBlank",
        "tj_flip_timeline", "tj_present_flip"))
    assert "QueryPerformanceCounter(&sample)" in clock
    assert "tj_vblank_cadence_rearm_after_clock_switch" in clock
    assert "*frequency = 1000ULL;" in clock

    launcher = LAUNCHER.read_text(encoding="utf-8")
    assert "[switch]$FifoKickWait" in launcher
    assert "'TJ_FIFO_KICK_WAIT'" in launcher
    assert "$info.EnvironmentVariables['TJ_FIFO_KICK_WAIT'] = '1'" in launcher
    assert "FifoKickWait = $info.EnvironmentVariables.ContainsKey('TJ_FIFO_KICK_WAIT')" in launcher

    fixture = r'''
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdarg.h>
typedef void* HANDLE;
typedef unsigned long DWORD;
#define WAIT_OBJECT_0 0UL
#define WAIT_TIMEOUT 258UL
#define WAIT_FAILED 0xffffffffUL
static const char* setting;
static HANDLE fake_event=(HANDLE)0x1234;
static unsigned env_calls, event_calls, wait_calls, error_calls, sleep_calls, log_calls, flush_calls, close_calls;
static unsigned qpc_calls, tick_calls;
static DWORD wait_result, last_error, wait_timeout;
static int qpc_result;
static long long qpc_value;
static uint64_t tick_value;
static int bad_timeout;
static char order[64], log_text[256];
static unsigned order_size;
static void push(char value) { if(order_size+1<sizeof(order)){order[order_size++]=value;order[order_size]=0;} }
static char* fixture_getenv(const char* name) { ++env_calls; return !strcmp(name,"TJ_FIFO_KICK_WAIT")?(char*)setting:NULL; }
static void* cellGcm_fifo_kick_event(void) { ++event_calls; return fake_event; }
static DWORD fixture_wait(HANDLE event,DWORD timeout) { ++wait_calls;push('W');wait_timeout=timeout;if(event!=fake_event||timeout>1)bad_timeout=1;return wait_result; }
static DWORD fixture_get_last_error(void) { ++error_calls;push('E');return last_error; }
static void fixture_sleep(DWORD ms) { ++sleep_calls;push('S');if(ms!=1)bad_timeout=1; }
typedef struct { long long QuadPart; } LARGE_INTEGER;
static int fixture_qpc(LARGE_INTEGER* value) { ++qpc_calls; value->QuadPart=qpc_value; return qpc_result; }
static uint64_t fixture_tick(void) { ++tick_calls; return tick_value; }
static int fixture_fprintf(void* stream,const char* format,...) { (void)stream;++log_calls;push('L');va_list args;va_start(args,format);vsnprintf(log_text,sizeof(log_text),format,args);va_end(args);return 0; }
static int fixture_fflush(void* stream) { (void)stream;++flush_calls;push('F');return 0; }
static int fixture_close(HANDLE event) { (void)event;++close_calls;return 1; }
#define getenv fixture_getenv
#define WaitForSingleObject fixture_wait
#define GetLastError fixture_get_last_error
#define Sleep fixture_sleep
#define QueryPerformanceCounter fixture_qpc
#define GetTickCount64 fixture_tick
#define fprintf fixture_fprintf
#define fflush fixture_fflush
#undef stderr
#define stderr ((void*)0)
CADENCE
HELPERS
CLOCK
static unsigned checks, failures;
#define CHECK(test,message) do { ++checks; if(!(test)){++failures;fprintf(stderr,"FAIL: %s\n",message);} } while(0)
static void reset_io(void) { wait_calls=error_calls=sleep_calls=log_calls=flush_calls=close_calls=0;wait_timeout=99;bad_timeout=0;order_size=0;order[0]=0;log_text[0]=0; }
int main(void) {
    int requested=0,active=0,logged=0;
    const char* off_values[]={NULL,"","0","true","01"};
    for(unsigned i=0;i<sizeof(off_values)/sizeof(off_values[0]);++i){
        setting=off_values[i];requested=1;fake_event=(HANDLE)0x1234;
        HANDLE event=(HANDLE)tj_fifo_kick_wait_event(&requested);
        CHECK(!requested && !event,"OFF or non-exact env value requested event creation");
    }
    CHECK(event_calls==0,"OFF path created the shared event");
    uint64_t wakes=0,timeouts=0,fallbacks=0;
    active=0;reset_io();
    tj_fifo_kick_wait_once(NULL,&active,&logged,&wakes,&timeouts,&fallbacks,1);
    CHECK(wait_calls==0 && sleep_calls==1 && wakes==0 && timeouts==0 && fallbacks==0,
          "OFF/null path did not preserve Sleep(1) without waiting");
    reset_io();
    tj_fifo_kick_wait_once(NULL,&active,&logged,&wakes,&timeouts,&fallbacks,0);
    CHECK(wait_calls==0 && sleep_calls==0,"overdue OFF/null path slept");

    setting="1";requested=0;fake_event=NULL;
    HANDLE event=(HANDLE)tj_fifo_kick_wait_event(&requested);
    CHECK(requested && !event && event_calls==1,"event-creation failure was not reported as requested but unavailable");
    active=0;reset_io();
    tj_fifo_kick_wait_once(event,&active,&logged,&wakes,&timeouts,&fallbacks,1);
    CHECK(wait_calls==0 && sleep_calls==1,"creation failure attempted a wait");

    fake_event=(HANDLE)0x1234;event=(HANDLE)tj_fifo_kick_wait_event(&requested);
    CHECK(requested && event==fake_event && event_calls==2,"exact opt-in did not return shared event");
    active=1;logged=0;wakes=timeouts=fallbacks=0;reset_io();wait_result=WAIT_OBJECT_0;
    tj_fifo_kick_wait_once(event,&active,&logged,&wakes,&timeouts,&fallbacks,1);
    CHECK(wait_calls==1 && wait_timeout==1 && wakes==1 && timeouts==0 && fallbacks==0 && active,
          "wake was not counted or changed active state");
    active=1;reset_io();wait_result=WAIT_TIMEOUT;
    tj_fifo_kick_wait_once(event,&active,&logged,&wakes,&timeouts,&fallbacks,1);
    CHECK(wait_calls==1 && wait_timeout==1 && timeouts==1 && active,"timeout did not resume with active wait");
    active=1;reset_io();wait_result=WAIT_TIMEOUT;
    tj_fifo_kick_wait_once(event,&active,&logged,&wakes,&timeouts,&fallbacks,0);
    CHECK(wait_calls==1 && wait_timeout==0 && !sleep_calls && timeouts==2 && active,
          "overdue event timeout slept or disabled the wait");
    active=1;reset_io();wait_result=WAIT_OBJECT_0;
    tj_fifo_kick_wait_once(event,&active,&logged,&wakes,&timeouts,&fallbacks,0);
    CHECK(wait_calls==1 && wait_timeout==0 && !sleep_calls && wakes==2 && active,
          "overdue event poll did not consume a ready token without sleep");

    active=1;logged=0;reset_io();wait_result=WAIT_FAILED;last_error=0x1234;
    tj_fifo_kick_wait_once(event,&active,&logged,&wakes,&timeouts,&fallbacks,1);
    CHECK(!active && logged && fallbacks==1 && error_calls==1,"WAIT_FAILED did not disable and fall back");
    CHECK(!strcmp(order,"WELFS"),"GetLastError was not captured immediately before logging/fallback");
    CHECK(strstr(log_text,"error=4660")!=NULL,"captured wait error was not logged");
    tj_fifo_kick_wait_once(event,&active,&logged,&wakes,&timeouts,&fallbacks,1);
    CHECK(wait_calls==1 && log_calls==1 && fallbacks==2 && sleep_calls==2,
          "disabled wait retried or logged repeatedly instead of sleeping");

    active=1;logged=0;reset_io();wait_result=77;
    tj_fifo_kick_wait_once(event,&active,&logged,&wakes,&timeouts,&fallbacks,1);
    CHECK(!active && error_calls==0 && fallbacks==3 && log_calls==1,
          "unexpected wait result did not disable with one fallback log");
    CHECK(strstr(log_text,"result=77 error=0")!=NULL,"unexpected result was mislabeled as WAIT_FAILED");
    tj_fifo_kick_wait_once(event,&active,&logged,&wakes,&timeouts,&fallbacks,1);
    CHECK(wait_calls==1 && log_calls==1 && fallbacks==4,"unexpected result was retried or relogged");

    active=1;reset_io();wait_result=WAIT_OBJECT_0;
    tj_fifo_kick_wait_once(event,&active,&logged,NULL,NULL,NULL,1);
    CHECK(active && wait_calls==1,"trace-disabled wait counter pointers were not optional");
    active=1;logged=0;reset_io();wait_result=WAIT_FAILED;last_error=7;
    tj_fifo_kick_wait_once(event,&active,&logged,&wakes,&timeouts,&fallbacks,0);
    CHECK(!active && logged && wait_calls==1 && wait_timeout==0 && !sleep_calls,
          "overdue failed event poll slept after disabling");
    qpc_calls=tick_calls=0;qpc_result=1;qpc_value=12345;tick_value=700;
    { TjVBlankCadence cadence; bool qpc_active=true; uint64_t frequency=10000000;
      tj_vblank_cadence_init(&cadence,100,frequency);
      CHECK(tj_vblank_clock_now(&cadence,&qpc_active,&frequency)==12345 && qpc_active &&
            frequency==10000000 && qpc_calls==1 && !tick_calls,
            "QPC clock sample changed active-domain state");
      qpc_result=0; tick_value=700;
      CHECK(tj_vblank_clock_now(&cadence,&qpc_active,&frequency)==700 && !qpc_active &&
            frequency==1000 && cadence.deadline==716 && tick_calls==1,
            "failed QPC did not rearm the TickCount cadence");
      tick_value=701;
      CHECK(tj_vblank_clock_now(&cadence,&qpc_active,&frequency)==701 && qpc_calls==2 &&
            tick_calls==2,"fallback clock re-probed QPC or mixed domains"); }
    { TjVBlankCadence cadence;
      tj_vblank_cadence_init(&cadence,100,1000);
      CHECK(tj_vblank_cadence_due(&cadence,100) && cadence.deadline==100,
            "overdue check advanced the cadence before its tick");
      tj_vblank_cadence_advance(&cadence);
      CHECK(!tj_vblank_cadence_due(&cadence,100),
            "not-due check did not restore the one-millisecond wait path"); }
    CHECK(close_calls==0 && !bad_timeout,"helper changed handle ownership or wait timeout");
    if(failures){printf("FIFO kick wait: %u checks, %u failures\n",checks,failures);return 1;}
    printf("FIFO kick wait: %u native checks passed\n",checks);return 0;
}
'''.replace("CADENCE", cadence).replace("HELPERS", helpers).replace("CLOCK", clock)

    compiler = shutil.which("clang++") or r"C:\Program Files\LLVM\bin\clang++.exe"
    with tempfile.TemporaryDirectory(prefix="tj-fifo-kick-wait-") as folder:
        source = Path(folder) / "fixture.cpp"
        executable = Path(folder) / "fixture.exe"
        source.write_text(fixture, encoding="utf-8")
        subprocess.run([compiler, "-std=c++17", "-O2", "-D_CRT_SECURE_NO_WARNINGS",
                        str(source), "-o", str(executable)], check=True)
        result = subprocess.run([str(executable)], capture_output=True, text=True,
                                timeout=15, check=False)
        if result.returncode:
            raise AssertionError(f"fixture failed:\n{result.stdout}\n{result.stderr}")
        print(result.stdout.strip())


if __name__ == "__main__":
    main()
