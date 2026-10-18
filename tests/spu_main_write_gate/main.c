#include "spu_main_write_gate.h"

#include <stdint.h>
#include <stdio.h>

#ifdef _WIN32
#include <windows.h>

typedef struct writer_args {
    uint32_t ea;
    uint32_t size;
    HANDLE entered;
    HANDLE release;
    volatile LONG *write_target;
    LONG write_value;
    uint64_t result;
} writer_args;

typedef struct gate_args {
    uint32_t ea;
    uint32_t size;
    HANDLE began;
    HANDLE release;
    uint64_t token;
    int end_result;
} gate_args;

static DWORD WINAPI writer_thread(void *opaque)
{
    writer_args *args = (writer_args *)opaque;
    const uint64_t token = spu_main_write_gate_writer_enter(args->ea, args->size);
    args->result = token;
    if (token && args->write_target)
        InterlockedExchange(args->write_target, args->write_value);
    SetEvent(args->entered);
    if (token)
        WaitForSingleObject(args->release, INFINITE);
    spu_main_write_gate_writer_exit(token);
    return token == 0;
}

static DWORD WINAPI gate_thread(void *opaque)
{
    gate_args *args = (gate_args *)opaque;
    args->token = spu_main_write_gate_begin(args->ea, args->size);
    SetEvent(args->began);
    if (args->token)
        WaitForSingleObject(args->release, INFINITE);
    args->end_result = args->token ? spu_main_write_gate_end(args->token) : 0;
    return args->token && !args->end_result;
}

static int check(int condition, const char *what)
{
    if (condition)
        return 1;
    fprintf(stderr, "spu_main_write_gate: %s\n", what);
    return 0;
}

static int wait_gate_state(int expected, DWORD timeout_ms)
{
    const DWORD deadline = GetTickCount() + timeout_ms;
    do {
        if (spu_main_write_gate_test_state() == expected)
            return 1;
        Sleep(1);
    } while ((LONG)(GetTickCount() - deadline) < 0);
    return 0;
}

static int release_and_join(HANDLE release, HANDLE thread)
{
    DWORD result;
    SetEvent(release);
    result = WaitForSingleObject(thread, 2000);
    CloseHandle(thread);
    return result == WAIT_OBJECT_0;
}

int main(void)
{
    volatile LONG queued_write = 0;
    writer_args before = { 0x1080u, 0x20u, CreateEventA(NULL, TRUE, FALSE, NULL),
                           CreateEventA(NULL, TRUE, FALSE, NULL), NULL, 0, 0 };
    /* [0x10FF, 0x1100) overlaps one byte. [0x1100, 0x1101) is adjacent. */
    writer_args overlap = { 0x10FFu, 1u, CreateEventA(NULL, TRUE, FALSE, NULL),
                            CreateEventA(NULL, TRUE, FALSE, NULL),
                            &queued_write, 0x0A, 0 };
    writer_args adjacent = { 0x1100u, 1u, CreateEventA(NULL, TRUE, FALSE, NULL),
                             CreateEventA(NULL, TRUE, FALSE, NULL), NULL, 0, 0 };
    gate_args gate = { 0x1000u, 0x100u,
                       CreateEventA(NULL, TRUE, FALSE, NULL),
                       CreateEventA(NULL, TRUE, FALSE, NULL), 0, 0 };
    gate_args next_gate = { 0x1100u, 1u,
                            CreateEventA(NULL, TRUE, FALSE, NULL),
                            CreateEventA(NULL, TRUE, FALSE, NULL), 0, 0 };
    HANDLE before_thread = NULL, gate_owner = NULL, overlap_thread = NULL,
           adjacent_thread = NULL, next_gate_owner = NULL;
    uint64_t stale_token, fresh_token;
    int ok = check(spu_main_write_gate_begin(0, 0) == 0, "zero range accepted") &&
             check(spu_main_write_gate_begin(UINT32_MAX, 2) == 0,
                   "overflow range accepted") &&
             check(spu_main_write_gate_end(1) == 0, "end without begin accepted");

    before_thread = CreateThread(NULL, 0, writer_thread, &before, 0, NULL);
    ok &= check(before_thread != NULL, "pre-existing writer creation");
    ok &= check(WaitForSingleObject(before.entered, 1000) == WAIT_OBJECT_0,
                "pre-existing writer did not enter");
    /* The writer is now registered before the publishing thread exists. */
    gate_owner = CreateThread(NULL, 0, gate_thread, &gate, 0, NULL);
    ok &= check(gate_owner != NULL, "gate owner creation");
    ok &= check(wait_gate_state(1, 1000),
                "gate was not published while waiting for pre-existing PUT");

    overlap_thread = CreateThread(NULL, 0, writer_thread, &overlap, 0, NULL);
    adjacent_thread = CreateThread(NULL, 0, writer_thread, &adjacent, 0, NULL);
    ok &= check(overlap_thread != NULL && adjacent_thread != NULL, "writer creation");
    ok &= check(WaitForSingleObject(overlap.entered, 100) == WAIT_TIMEOUT,
                "one-byte overlapping PUT passed active gate");
    ok &= check(WaitForSingleObject(adjacent.entered, 1000) == WAIT_OBJECT_0,
                "adjacent half-open PUT blocked by gate");

    ok &= check(release_and_join(before.release, before_thread),
                "pre-existing writer did not leave");
    before_thread = NULL;
    ok &= check(WaitForSingleObject(gate.began, 1000) == WAIT_OBJECT_0 &&
                gate.token != 0 && wait_gate_state(2, 1000),
                "begin did not finish after writer drain");
    ok &= check(WaitForSingleObject(overlap.entered, 100) == WAIT_TIMEOUT &&
                InterlockedCompareExchange(&queued_write, 0, 0) == 0,
                "overlap wrote before owner released gate");
    ok &= check(spu_main_write_gate_end(gate.token) == 0, "wrong owner ended gate");

    SetEvent(gate.release);
    ok &= check(WaitForSingleObject(gate_owner, 2000) == WAIT_OBJECT_0 &&
                gate.end_result == 1, "owner failed to end gate");
    CloseHandle(gate_owner); gate_owner = NULL;
    ok &= check(WaitForSingleObject(overlap.entered, 1000) == WAIT_OBJECT_0 &&
                InterlockedCompareExchange(&queued_write, 0, 0) == 0x0A,
                "queued PUT did not write after gate release");
    ok &= check(release_and_join(overlap.release, overlap_thread),
                "overlap writer did not leave");
    overlap_thread = NULL;

    /* This writer was disjoint from gate A, but it is still in flight. Gate B
     * covers its range and must drain the retained writer token before begin
     * returns. This catches lost starts across gate generations. */
    next_gate_owner = CreateThread(NULL, 0, gate_thread, &next_gate, 0, NULL);
    ok &= check(next_gate_owner != NULL, "next gate owner creation");
    ok &= check(wait_gate_state(1, 1000) &&
                WaitForSingleObject(next_gate.began, 0) == WAIT_TIMEOUT,
                "next gate did not drain prior disjoint writer");
    ok &= check(release_and_join(adjacent.release, adjacent_thread),
                "adjacent writer did not leave");
    adjacent_thread = NULL;
    ok &= check(WaitForSingleObject(next_gate.began, 1000) == WAIT_OBJECT_0 &&
                next_gate.token != 0 && wait_gate_state(2, 1000),
                "next gate did not finish after disjoint writer left");
    SetEvent(next_gate.release);
    ok &= check(WaitForSingleObject(next_gate_owner, 2000) == WAIT_OBJECT_0 &&
                next_gate.end_result == 1, "next gate owner failed to end gate");
    CloseHandle(next_gate_owner); next_gate_owner = NULL;
    ok &= check(before.result != 0 && overlap.result != 0 && adjacent.result != 0,
                "valid writer was not retained until exit");

    stale_token = spu_main_write_gate_begin(0x1000u, 0x100u);
    ok &= check(stale_token != 0, "valid begin failed");
    ok &= check(spu_main_write_gate_begin(0x1000u, 0x100u) == 0,
                "nested begin accepted");
    ok &= check(spu_main_write_gate_end(stale_token) == 1, "valid end failed");
    fresh_token = spu_main_write_gate_begin(0x1000u, 0x100u);
    ok &= check(fresh_token != 0 && fresh_token != stale_token,
                "new gate did not get a new token");
    ok &= check(spu_main_write_gate_end(stale_token) == 0,
                "stale token cleared newer gate");
    ok &= check(spu_main_write_gate_test_state() == 2 &&
                spu_main_write_gate_end(fresh_token) == 1,
                "fresh same-owner token failed to release gate");

    if (before_thread) release_and_join(before.release, before_thread);
    if (overlap_thread) release_and_join(overlap.release, overlap_thread);
    if (adjacent_thread) release_and_join(adjacent.release, adjacent_thread);
    if (gate_owner) { SetEvent(gate.release); WaitForSingleObject(gate_owner, 2000); CloseHandle(gate_owner); }
    if (next_gate_owner) { SetEvent(next_gate.release); WaitForSingleObject(next_gate_owner, 2000); CloseHandle(next_gate_owner); }
    CloseHandle(before.entered); CloseHandle(before.release);
    CloseHandle(overlap.entered); CloseHandle(overlap.release);
    CloseHandle(adjacent.entered); CloseHandle(adjacent.release);
    CloseHandle(gate.began); CloseHandle(gate.release);
    CloseHandle(next_gate.began); CloseHandle(next_gate.release);
    return ok ? 0 : 1;
}
#else
int main(void) { return 0; }
#endif
