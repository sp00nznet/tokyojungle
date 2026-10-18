#include "rsx_boundary_policy.h"

#include <stdint.h>
#include <stdio.h>

typedef struct rsx_boundary_case {
    const char* name;
    uint64_t consumed_generation;
    uint64_t submitted_generation;
    int have_display_work;
    rsx_boundary_decision expected;
} rsx_boundary_case;

static int check_incoming_clear_starts_next_frame(void)
{
    uint64_t submitted_generation = 40u;

    /* Prior accumulated display work closes first. Runtime resets records,
     * then records incoming clear as first work of next frame. */
    if (rsx_boundary_decide(41u, submitted_generation, 1) !=
        RSX_BOUNDARY_PRESENT)
        return 0;
    submitted_generation = rsx_boundary_submitted_generation_after_submit(
        41u, submitted_generation);

    /* Same generation must coalesce; incoming clear cannot re-present itself. */
    return rsx_boundary_decide(41u, submitted_generation, 1) ==
        RSX_BOUNDARY_COALESCE;
}

int main(void)
{
    static const rsx_boundary_case cases[] = {
        { "advanced display work", 53u, 52u, 1, RSX_BOUNDARY_PRESENT },
        { "same generation display work", 53u, 53u, 1, RSX_BOUNDARY_COALESCE },
        { "advanced offscreen work", 54u, 53u, 0, RSX_BOUNDARY_ACCUMULATE },
        { "legacy display work", 0u, 53u, 1, RSX_BOUNDARY_PRESENT },
        { "legacy offscreen work", 0u, 53u, 0, RSX_BOUNDARY_ACCUMULATE },
    };
    int failed = 0;

    if (!rsx_boundary_should_arm_survival_profile(
            10u, 0u, 0x0F510000u, 0x003A0000u, 5120u)) {
        fputs("FAIL: exact Survival tile did not arm\n", stderr);
        failed = 1;
    }
    if (rsx_boundary_should_arm_survival_profile(
            9u, 0u, 0x0F510000u, 0x003A0000u, 5120u) ||
        rsx_boundary_should_arm_survival_profile(
            10u, 1u, 0x0F510000u, 0x003A0000u, 5120u) ||
        rsx_boundary_should_arm_survival_profile(
            10u, 0u, 0x0F500000u, 0x003A0000u, 5120u) ||
        rsx_boundary_should_arm_survival_profile(
            10u, 0u, 0x0F510000u, 0x00390000u, 5120u) ||
        rsx_boundary_should_arm_survival_profile(
            10u, 0u, 0x0F510000u, 0x003A0000u, 5119u)) {
        fputs("FAIL: near-miss Survival tile armed\n", stderr);
        failed = 1;
    }

    for (size_t i = 0; i < sizeof(cases) / sizeof(cases[0]); ++i) {
        const rsx_boundary_decision actual = rsx_boundary_decide(
            cases[i].consumed_generation,
            cases[i].submitted_generation,
            cases[i].have_display_work);
        if (actual != cases[i].expected) {
            fprintf(stderr, "FAIL: %s actual=%d expected=%d\n",
                    cases[i].name,
                    actual, cases[i].expected);
            failed = 1;
        }
    }

    if (!check_incoming_clear_starts_next_frame()) {
        fputs("FAIL: incoming display clear did not stay in next frame\n", stderr);
        failed = 1;
    }

    if (failed)
        return 1;

    puts("PASS: RSX boundary generation policy");
    return 0;
}
