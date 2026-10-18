#include "tj_pamf_timing.h"

#include <cstdio>
#include <cstdint>

namespace {

bool check_due_count(const char* name, size_t actual, size_t expected)
{
    if (actual == expected) return true;
    std::fprintf(stderr, "%s: expected %zu due frames, got %zu\n", name,
                 expected, actual);
    return false;
}

bool check_endpoint(const char* name, bool actual, bool expected)
{
    if (actual == expected) return true;
    std::fprintf(stderr, "%s: expected endpoint=%d, got %d\n", name,
                 expected, actual);
    return false;
}

} // namespace

int main()
{
    constexpr uint64_t start = 900000;
    constexpr uint64_t end = start + 90000;
    const uint64_t pts[] = {start, start + 3003, start + 6006};
    int failures = 0;

    failures += !check_due_count("empty/null", 
        tj_pamf_due_frame_count(nullptr, 0, 0, 0, start, 0), 0);
    failures += !check_due_count("empty", 
        tj_pamf_due_frame_count(pts, 3, 0, 0, start, 0), 0);
    failures += !check_due_count("first-frame-only", 
        tj_pamf_due_frame_count(pts, 3, 0, 3, start, 3002), 1);
    failures += !check_due_count("all-three-due", 
        tj_pamf_due_frame_count(pts, 3, 0, 3, start, 6006), 3);

    const uint64_t future_pts[] = {
        start + 3003, start + 6006, start + 9009};
    failures += !check_due_count("stale-due0", 
        tj_pamf_stale_frame_count(future_pts, 3, 0, 3, start, 0), 0);
    failures += !check_due_count("stale-due1", 
        tj_pamf_stale_frame_count(pts, 3, 0, 3, start, 0), 0);
    failures += !check_due_count("stale-due2", 
        tj_pamf_stale_frame_count(pts, 3, 0, 3, start, 3003), 1);
    failures += !check_due_count("stale-due3", 
        tj_pamf_stale_frame_count(pts, 3, 0, 3, start, 6006), 2);

    const uint64_t wrapped_pts[] = {
        start + 9009, start + 12012, start + 6006};
    failures += !check_due_count("wrapped-head", 
        tj_pamf_due_frame_count(wrapped_pts, 3, 2, 3, start, 11000), 2);
    failures += !check_due_count("wrapped-future-retained", 
        tj_pamf_stale_frame_count(wrapped_pts, 3, 2, 3, start, 9008), 0);
    failures += !check_due_count("wrapped-stale-only", 
        tj_pamf_stale_frame_count(wrapped_pts, 3, 2, 3, start, 9009), 1);

    failures += !check_endpoint("endpoint-not-started",
        tj_pamf_presentation_endpoint_reached(false, false, start, end, 90000),
        false);
    failures += !check_endpoint("endpoint-paused",
        tj_pamf_presentation_endpoint_reached(true, true, start, end, 90000),
        false);
    failures += !check_endpoint("endpoint-invalid-range",
        tj_pamf_presentation_endpoint_reached(true, false, end, start, 0),
        false);
    failures += !check_endpoint("endpoint-before",
        tj_pamf_presentation_endpoint_reached(true, false, start, end, 89999),
        false);
    failures += !check_endpoint("endpoint-exact",
        tj_pamf_presentation_endpoint_reached(true, false, start, end, 90000),
        true);
    failures += !check_endpoint("endpoint-after",
        tj_pamf_presentation_endpoint_reached(true, false, start, end, 90001),
        true);
    return failures ? 1 : 0;
}
