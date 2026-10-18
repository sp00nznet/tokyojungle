/* GPU interrupts must not re-enter the game main thread while it holds a
 * non-recursive guest mutex. This single worker owns all GCM callbacks; the
 * present thread only publishes completed interrupts and wakes it. */
#include <windows.h>
#include <cstdint>
#include <cstdio>
#include <cstdlib>

#include "guest_world_rendezvous.h"

extern "C" void ppu_gcm_pump(void);
extern "C" void cellGcmSetHostInterruptDispatcher(uint32_t host_thread_id,
                                                void (*wake)(void));

static SRWLOCK s_wake_lock = SRWLOCK_INIT;
static HANDLE s_wake_event, s_ready_event, s_interrupt_thread;
static volatile LONG s_stop, s_ready;

static void tj_gcm_interrupt_wake()
{
    /* An in-flight producer may have loaded the old wake hook just before
     * unregister. Protect the handle through SetEvent and final close. */
    AcquireSRWLockShared(&s_wake_lock);
    if (s_wake_event) SetEvent(s_wake_event);
    ReleaseSRWLockShared(&s_wake_lock);
}

static DWORD WINAPI tj_gcm_interrupt_worker(LPVOID)
{
    if (!guest_world_register_current("tj-gcm-interrupt")) {
        SetEvent(s_ready_event);
        return 1;
    }
    cellGcmSetHostInterruptDispatcher(GetCurrentThreadId(), tj_gcm_interrupt_wake);
    InterlockedExchange(&s_ready, 1);
    SetEvent(s_ready_event);
    while (!InterlockedCompareExchange(&s_stop, 0, 0)) {
        /* Drain before waiting. An event raised between the drain and wait
         * remains signaled, so neither startup nor a concurrent edge is lost. */
        try {
            ppu_gcm_pump();
        } catch (...) {
            std::fputs("[TJ-GCM-INTERRUPT] fatal: guest callback escaped worker\n", stderr);
            std::abort();
        }
        if (InterlockedCompareExchange(&s_stop, 0, 0)) break;
        if (WaitForSingleObject(s_wake_event, INFINITE) != WAIT_OBJECT_0) {
            std::fputs("[TJ-GCM-INTERRUPT] fatal: interrupt wait failed\n", stderr);
            std::abort();
        }
    }
    cellGcmSetHostInterruptDispatcher(0, nullptr);
    guest_world_unregister_current();
    return 0;
}

extern "C" int tj_stop_gcm_interrupt_thread(void)
{
    if (s_interrupt_thread) {
        InterlockedExchange(&s_stop, 1);
        tj_gcm_interrupt_wake();
        if (WaitForSingleObject(s_interrupt_thread, 5000) != WAIT_OBJECT_0) {
            /* Do not free resources still owned by a live callback. */
            std::fputs("[TJ-GCM-INTERRUPT] shutdown timed out; worker handles retained\n", stderr);
            return 0;
        }
        CloseHandle(s_interrupt_thread);
        s_interrupt_thread = nullptr;
    }
    AcquireSRWLockExclusive(&s_wake_lock);
    if (s_wake_event) CloseHandle(s_wake_event);
    s_wake_event = nullptr;
    ReleaseSRWLockExclusive(&s_wake_lock);
    if (s_ready_event) CloseHandle(s_ready_event);
    s_ready_event = nullptr;
    return 1;
}

extern "C" int tj_start_gcm_interrupt_thread(void)
{
    if (s_interrupt_thread)
        return WaitForSingleObject(s_interrupt_thread, 0) == WAIT_TIMEOUT &&
               InterlockedCompareExchange(&s_ready, 0, 0) &&
               !InterlockedCompareExchange(&s_stop, 0, 0);
    InterlockedExchange(&s_stop, 0);
    InterlockedExchange(&s_ready, 0);
    AcquireSRWLockExclusive(&s_wake_lock);
    s_wake_event = CreateEventW(nullptr, FALSE, FALSE, nullptr);
    ReleaseSRWLockExclusive(&s_wake_lock);
    s_ready_event = CreateEventW(nullptr, TRUE, FALSE, nullptr);
    if (!s_wake_event || !s_ready_event) {
        tj_stop_gcm_interrupt_thread();
        return 0;
    }
    s_interrupt_thread = CreateThread(nullptr, 4u * 1024u * 1024u,
                                      tj_gcm_interrupt_worker, nullptr, 0, nullptr);
    if (!s_interrupt_thread ||
        WaitForSingleObject(s_ready_event, 5000) != WAIT_OBJECT_0 ||
        !InterlockedCompareExchange(&s_ready, 0, 0)) {
        tj_stop_gcm_interrupt_thread();
        return 0;
    }
    return 1;
}
