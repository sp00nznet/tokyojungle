#ifndef TJ_VBLANK_CADENCE_H
#define TJ_VBLANK_CADENCE_H

#include <stdint.h>

typedef struct TjVBlankCadence {
    uint64_t deadline;
    uint64_t whole_step;
    uint32_t step_remainder;
    uint32_t remainder_accumulator;
} TjVBlankCadence;

/* Deadlines after N advances are start + floor(N * frequency / 60). */
static inline void tj_vblank_cadence_init(TjVBlankCadence* cadence,
                                         uint64_t now,
                                         uint64_t frequency)
{
    if (frequency < 60) frequency = 1000;
    cadence->deadline = now;
    cadence->whole_step = frequency / 60;
    cadence->step_remainder = (uint32_t)(frequency % 60);
    cadence->remainder_accumulator = 0;
}

static inline int tj_vblank_cadence_due(const TjVBlankCadence* cadence,
                                        uint64_t now)
{
    return (int64_t)(now - cadence->deadline) >= 0;
}

static inline void tj_vblank_cadence_advance(TjVBlankCadence* cadence)
{
    cadence->deadline += cadence->whole_step;
    cadence->remainder_accumulator += cadence->step_remainder;
    if (cadence->remainder_accumulator >= 60) {
        ++cadence->deadline;
        cadence->remainder_accumulator -= 60;
    }
}

/* Match the old `next_tick = now` cap recovery and restart its phase. */
static inline void tj_vblank_cadence_resync(TjVBlankCadence* cadence,
                                            uint64_t now)
{
    cadence->deadline = now;
    cadence->remainder_accumulator = 0;
}

/* A runtime source failure changes units. Rebase and wait one period before
 * resuming so the fallback does not invent an immediate extra VBlank. */
static inline void tj_vblank_cadence_rearm_after_clock_switch(
    TjVBlankCadence* cadence, uint64_t now, uint64_t frequency)
{
    tj_vblank_cadence_init(cadence, now, frequency);
    tj_vblank_cadence_advance(cadence);
}

#endif
