#ifndef TJ_VBLANK_SOURCE_H
#define TJ_VBLANK_SOURCE_H

#include <stdint.h>
#include "tj_vblank_cadence.h"

/* Opt-in display-locked VBlank (TJ_VBLANK_SOURCE=display). Presenter-thread
 * state only. While `following`, every tick the backend's display waiter
 * published fires one emulated VBlank; otherwise the rational QPC cadence
 * fires exactly as before. With `display` clear each helper reduces to the
 * plain cadence call, so the default path is unchanged.
 *
 * `now`, `last_tick` and the waiter's tick stamps share the presenter's QPC
 * domain; the display source is abandoned if the clock leaves QPC. */
typedef struct TjVBlankSource {
    int display;            /* display waiter running for this session */
    int following;          /* display ticks drive VBlank; else QPC cadence */
    uint64_t consumed;      /* display ticks fired or skipped */
    uint64_t resume_mark;   /* published count when the watchdog fell back */
    uint64_t stale_after;   /* watchdog: 50 ms in clock units */
    uint64_t armed_at;      /* clock when display following (re)started */
    uint64_t last_tick;     /* newest published tick stamp, 0 = none */
    uint64_t last_qpc_fire; /* clock when the last QPC VBlank actually fired */
    uint64_t fired_display, fired_qpc, fallbacks, resumes, dropped;
} TjVBlankSource;

static inline void tj_vblank_source_init(TjVBlankSource* s, int display,
                                         uint64_t now, uint64_t frequency)
{
    s->display = s->following = display ? 1 : 0;
    s->consumed = s->resume_mark = 0;
    s->stale_after = frequency / 20u;          /* 50 ms */
    s->armed_at = now;
    s->last_tick = 0;
    s->last_qpc_fire = now;
    s->fired_display = s->fired_qpc = 0;
    s->fallbacks = s->resumes = s->dropped = 0;
}

/* Watchdog and resume; `published`/`last_tick` come from the display waiter.
 * Returns 1 when `following` changed. */
static inline int tj_vblank_source_watch(TjVBlankSource* s,
                                         TjVBlankCadence* cadence,
                                         uint64_t now, uint64_t published,
                                         uint64_t last_tick)
{
    if (!s->display) return 0;
    s->last_tick = last_tick;
    if (s->following) {
        uint64_t newest = s->armed_at;
        if (last_tick && (int64_t)(last_tick - newest) > 0) newest = last_tick;
        if ((int64_t)(now - newest) <= (int64_t)s->stale_after) return 0;
        /* No display tick for 50 ms (display asleep, unusable output, waiter
         * failing). Rebase the QPC cadence on now: one VBlank is due at once
         * -- it is overdue -- then the normal 60 Hz deadlines. Unfired
         * display ticks are dropped, never replayed as a burst. */
        s->following = 0;
        ++s->fallbacks;
        s->dropped += published - s->consumed;
        s->consumed = s->resume_mark = published;
        tj_vblank_cadence_resync(cadence, now);
        return 1;
    }
    /* Resume after four ticks, the newest fresh, and only once no QPC
     * deadline is overdue: a QPC backlog first fires through the bounded
     * loop. The QPC cadence kept the guest current, so ticks published
     * meanwhile are skipped. The newest one still fires when it arrived at
     * least half a period after the last QPC VBlank actually fired; otherwise
     * the next display tick is the first. Never more than one. */
    if (published - s->resume_mark >= 4u && last_tick &&
        (int64_t)(now - last_tick) <= (int64_t)s->stale_after &&
        !tj_vblank_cadence_due(cadence, now)) {
        const int fire_newest =
            (int64_t)(last_tick - s->last_qpc_fire) >=
            (int64_t)(cadence->whole_step / 2u);
        s->following = 1;
        ++s->resumes;
        s->consumed = fire_newest ? published - 1u : published;
        s->armed_at = now;
        return 1;
    }
    return 0;
}

static inline int tj_vblank_source_due(const TjVBlankSource* s,
                                       const TjVBlankCadence* cadence,
                                       uint64_t now, uint64_t published)
{
    if (s->following) return (int64_t)(published - s->consumed) > 0;
    return tj_vblank_cadence_due(cadence, now);
}

/* Due time of the VBlank about to fire (flip timeline / trace). */
static inline uint64_t tj_vblank_source_stamp(const TjVBlankSource* s,
                                              const TjVBlankCadence* cadence)
{
    return s->following ? s->last_tick : cadence->deadline;
}

/* `now`: the clock the due check that fired this VBlank used. */
static inline void tj_vblank_source_advance(TjVBlankSource* s,
                                            TjVBlankCadence* cadence,
                                            uint64_t now)
{
    if (s->following) {
        ++s->consumed;
        ++s->fired_display;
        return;
    }
    tj_vblank_cadence_advance(cadence);
    s->last_qpc_fire = now;
    ++s->fired_qpc;
}

/* The bounded catch-up loop fell far behind: drop the rest like the QPC
 * resync does. */
static inline void tj_vblank_source_resync(TjVBlankSource* s,
                                           TjVBlankCadence* cadence,
                                           uint64_t now, uint64_t published)
{
    if (s->following) {
        s->dropped += published - s->consumed;
        s->consumed = published;
        return;
    }
    tj_vblank_cadence_resync(cadence, now);
}

/* The clock left QPC: the watchdog has no time base. QPC fallback (or its
 * millisecond clock) for the rest of the session; the clock switch already
 * rearmed the cadence one period out. */
static inline void tj_vblank_source_abandon(TjVBlankSource* s,
                                            uint64_t published)
{
    if (!s->display) return;
    if (s->following) s->dropped += published - s->consumed;
    s->display = s->following = 0;
    s->consumed = published;
}

#endif
