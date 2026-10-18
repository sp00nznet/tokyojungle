#define GUEST_WORLD_RENDEZVOUS_TESTING 1
#include "guest_world_rendezvous.h"

#include <windows.h>
#include <stdio.h>

static volatile LONG g_reservation_depth;
static volatile LONG g_reservation_lock_calls;
static volatile LONG g_reservation_unlock_calls;
void spu_reservation_lock(void) { InterlockedIncrement(&g_reservation_lock_calls); InterlockedIncrement(&g_reservation_depth); }
void spu_reservation_unlock(void) { InterlockedIncrement(&g_reservation_unlock_calls); InterlockedDecrement(&g_reservation_depth); }

static int require(int condition, const char* message)
{
    if (condition) return 1;
    fprintf(stderr, "guest_world_rendezvous: %s\n", message);
    return 0;
}

typedef struct Worker {
    HANDLE ready;
    volatile LONG run;
    volatile LONG ticks;
    int registered;
} Worker;

static DWORD WINAPI worker_proc(LPVOID p)
{
    Worker* w = (Worker*)p;
    w->registered = guest_world_register_current("test-worker");
    SetEvent(w->ready);
    while (InterlockedCompareExchange(&w->run, 0, 0)) {
        InterlockedIncrement(&w->ticks);
        Sleep(1);
    }
    if (w->registered) guest_world_unregister_current();
    return 0;
}

static HANDLE worker_start(Worker* w)
{
    ZeroMemory(w, sizeof(*w));
    w->ready = CreateEventA(NULL, TRUE, FALSE, NULL);
    w->run = 1;
    HANDLE h = CreateThread(NULL, 0, worker_proc, w, 0, NULL);
    if (!h || WaitForSingleObject(w->ready, 2000) != WAIT_OBJECT_0) return NULL;
    return h;
}

static void worker_stop(Worker* w, HANDLE h)
{
    InterlockedExchange(&w->run, 0);
    WaitForSingleObject(h, 2000);
    CloseHandle(h);
    CloseHandle(w->ready);
}

static DWORD g_fake_suspend_result = 0;
static DWORD g_fake_resume_result = 0;
static LONG g_fake_suspend_calls;
static LONG g_fake_resume_calls;
static LONG g_fake_suspend_fail_on;
static LONG g_fake_resume_fail_on;
static LONG g_fake_live;
static unsigned long fake_suspend(void* handle)
{
    (void)handle;
    if (InterlockedCompareExchange(&g_reservation_depth, 0, 0) != 1) return (unsigned long)-1;
    LONG call = InterlockedIncrement(&g_fake_suspend_calls);
    if (g_fake_suspend_fail_on && call == g_fake_suspend_fail_on) return (unsigned long)-1;
    return g_fake_suspend_result;
}
static unsigned long fake_resume(void* handle)
{
    (void)handle;
    if (InterlockedCompareExchange(&g_reservation_depth, 0, 0) != 0) return (unsigned long)-1;
    LONG call = InterlockedIncrement(&g_fake_resume_calls);
    if (g_fake_resume_fail_on && call == g_fake_resume_fail_on) return (unsigned long)-1;
    return g_fake_resume_result;
}
static int fake_live(void* handle) { (void)handle; return InterlockedCompareExchange(&g_fake_live, 0, 0); }

typedef struct BlockedRegister {
    HANDLE start;
    HANDLE done;
    int registered;
    int wrong_end;
} BlockedRegister;

static DWORD WINAPI blocked_register_proc(LPVOID p)
{
    BlockedRegister* b = (BlockedRegister*)p;
    WaitForSingleObject(b->start, INFINITE);
    b->wrong_end = guest_world_exclusive_end();
    b->registered = guest_world_register_current("blocked-register");
    if (b->registered) guest_world_unregister_current();
    SetEvent(b->done);
    return 0;
}

static int test_freeze_and_preserve_suspend(void)
{
    Worker w, w2; HANDLE h, h2;
    guest_world_test_reset();
    if (!require(guest_world_register_current("test-main"), "main register")) return 0;
    h = worker_start(&w);
    h2 = worker_start(&w2);
    if (!require(h && h2 && w.registered && w2.registered, "two worker register")) return 0;
    Sleep(10);
    if (!require(guest_world_exclusive_begin(), "exclusive begin")) return 0;
    LONG frozen = w.ticks, frozen2 = w2.ticks;
    Sleep(30);
    if (!require(w.ticks == frozen && w2.ticks == frozen2, "writer freezes workers")) return 0;
    if (!require(guest_world_exclusive_end(), "exclusive end")) return 0;
    for (int i = 0; i < 50 && w.ticks == frozen; ++i) Sleep(2);
    if (!require(w.ticks > frozen, "worker resumed")) return 0;

    SuspendThread(h);
    LONG before = w.ticks;
    if (!require(guest_world_exclusive_begin(), "pre-suspended begin")) return 0;
    if (!require(guest_world_exclusive_end(), "pre-suspended end")) return 0;
    Sleep(20);
    if (!require(w.ticks == before, "prior suspend count preserved")) return 0;
    ResumeThread(h);
    worker_stop(&w, h); worker_stop(&w2, h2);
    if (!require(guest_world_registered_count() == 1, "worker unregistered")) return 0;
    return require(guest_world_unregister_current(), "main unregister");
}

static int test_injection_and_ownership(void)
{
    Worker w, w2; HANDLE h, h2; DWORD code = 0;
    guest_world_test_reset();
    if (!require(guest_world_register_current("test-main"), "main register injection")) return 0;
    h = worker_start(&w);
    h2 = worker_start(&w2);
    if (!require(h && h2 && w.registered && w2.registered, "workers register injection")) return 0;
    g_fake_suspend_result = 0;
    g_fake_resume_result = 0;
    g_fake_suspend_calls = g_fake_resume_calls = 0;
    g_fake_suspend_fail_on = 2; g_fake_resume_fail_on = 0; g_fake_live = 1;
    guest_world_test_set_hooks(fake_suspend, fake_resume, fake_live);
    if (!require(!guest_world_exclusive_begin(), "suspend injection rejects")) return 0;
    if (!require(guest_world_is_poisoned(), "suspend failure poisons")) return 0;
    if (!require(g_fake_suspend_calls == 2 && g_fake_resume_calls == 1 &&
                 g_reservation_depth == 0 && g_reservation_lock_calls == g_reservation_unlock_calls,
                 "reservation unlocks after failed begin")) return 0;
    guest_world_test_set_hooks(NULL, NULL, NULL);
    worker_stop(&w, h); worker_stop(&w2, h2);
    guest_world_unregister_current();

    guest_world_test_reset();
    if (!require(guest_world_register_current("test-main"), "main register resume injection")) return 0;
    h = worker_start(&w);
    h2 = worker_start(&w2);
    if (!require(h && h2 && w.registered && w2.registered, "workers register resume injection")) return 0;
    g_fake_suspend_result = 0;
    g_fake_resume_result = (DWORD)-1;
    g_fake_suspend_calls = g_fake_resume_calls = 0;
    g_fake_suspend_fail_on = 0; g_fake_resume_fail_on = 1; g_fake_live = 1;
    guest_world_test_set_hooks(fake_suspend, fake_resume, fake_live);
    if (!require(guest_world_exclusive_begin(), "resume injection begin")) return 0;
    if (!require(!guest_world_exclusive_end() && guest_world_is_poisoned(),
                 "resume injection poisons after attempting end")) return 0;
    if (!require(g_fake_suspend_calls == 2 && g_fake_resume_calls == 2 &&
                 g_reservation_depth == 0, "resume is after reservation unlock")) return 0;
    guest_world_test_set_hooks(NULL, NULL, NULL);
    worker_stop(&w, h); worker_stop(&w2, h2);
    guest_world_unregister_current();

    guest_world_test_reset();
    if (!require(guest_world_register_current("test-main"), "main register owner")) return 0;
    h = worker_start(&w);
    if (!require(h && w.registered, "worker register owner")) return 0;
    BlockedRegister b = {};
    b.start = CreateEventA(NULL, TRUE, FALSE, NULL);
    b.done = CreateEventA(NULL, TRUE, FALSE, NULL);
    HANDLE wrong = CreateThread(NULL, 0, blocked_register_proc, &b, 0, NULL);
    if (!require(guest_world_exclusive_begin(), "owner begin")) return 0;
    SetEvent(b.start);
    if (!require(WaitForSingleObject(b.done, 30) == WAIT_TIMEOUT,
                 "registration blocks during exclusive")) return 0;
    if (!require(!guest_world_exclusive_begin(), "nested begin rejected")) return 0;
    if (!require(guest_world_exclusive_end(), "owner end after wrong owner")) return 0;
    WaitForSingleObject(wrong, 2000); GetExitCodeThread(wrong, &code); CloseHandle(wrong);
    CloseHandle(b.start); CloseHandle(b.done);
    if (!require(code == 0 && b.registered && !b.wrong_end,
                 "wrong owner rejected while owner active")) return 0;
    worker_stop(&w, h);
    return require(guest_world_unregister_current() && guest_world_registered_count() == 0,
                   "shutdown count zero");
}

static int test_dead_handle_liveness(void)
{
    Worker w; HANDLE h;
    guest_world_test_reset();
    if (!require(guest_world_register_current("dead-main"), "dead main register")) return 0;
    h = worker_start(&w);
    if (!require(h && w.registered, "dead worker register")) return 0;
    g_fake_suspend_result = g_fake_resume_result = 0;
    g_fake_suspend_calls = g_fake_resume_calls = 0;
    g_fake_suspend_fail_on = g_fake_resume_fail_on = 0; g_fake_live = 0;
    guest_world_test_set_hooks(fake_suspend, fake_resume, fake_live);
    if (!require(!guest_world_exclusive_begin() && guest_world_is_poisoned(),
                 "dead registered handle poisons before suspend")) return 0;
    guest_world_test_set_hooks(NULL, NULL, NULL);
    worker_stop(&w, h);
    guest_world_unregister_current();
    return 1;
}

static int test_capacity_and_concurrent_lifetime(void)
{
    Worker a, b; HANDLE ha, hb;
    guest_world_test_reset();
    guest_world_test_set_capacity_limit(1);
    if (!require(guest_world_register_current("capacity-main"), "capacity main")) return 0;
    ha = worker_start(&a);
    if (!require(ha && !a.registered && guest_world_is_poisoned(), "capacity rejects second registration")) return 0;
    worker_stop(&a, ha);
    guest_world_unregister_current();

    guest_world_test_reset();
    if (!require(guest_world_register_current("duplicate-main"), "duplicate main")) return 0;
    if (!require(!guest_world_register_current("duplicate-main") && guest_world_is_poisoned(),
                 "duplicate registration poisons")) return 0;
    guest_world_unregister_current();

    guest_world_test_reset();
    if (!require(guest_world_register_current("concurrent-main"), "concurrent main")) return 0;
    ha = worker_start(&a); hb = worker_start(&b);
    if (!require(ha && hb && a.registered && b.registered, "concurrent registrations")) return 0;
    worker_stop(&a, ha); worker_stop(&b, hb);
    return require(guest_world_registered_count() == 1 && guest_world_unregister_current() &&
                   guest_world_registered_count() == 0, "concurrent unregister count");
}

int main(void)
{
    int ok = test_freeze_and_preserve_suspend() &&
             test_injection_and_ownership() &&
             test_capacity_and_concurrent_lifetime() &&
             test_dead_handle_liveness();
    guest_world_test_reset();
    return ok ? 0 : 1;
}
