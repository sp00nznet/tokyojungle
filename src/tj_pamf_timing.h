#pragma once

#include <cstddef>
#include <cstdint>

/* The presentation interval is [start, end) in 90 kHz ticks. */
inline bool tj_pamf_presentation_endpoint_reached(
    bool playback_started, bool paused, uint64_t presentation_start_90k,
    uint64_t presentation_end_90k, uint64_t elapsed_90k)
{
    return playback_started && !paused &&
           presentation_end_90k > presentation_start_90k &&
           elapsed_90k >= presentation_end_90k - presentation_start_90k;
}

/* Number of queued frames whose presentation time has elapsed. */
inline size_t tj_pamf_due_frame_count(const uint64_t* pts, size_t ring_size,
                                      size_t head, size_t count,
                                      uint64_t presentation_start_90k,
                                      uint64_t elapsed_90k)
{
    if (!pts || !ring_size) return 0;

    size_t due = 0;
    while (due < count) {
        const uint64_t frame_pts = pts[(head + due) % ring_size];
        if (frame_pts > presentation_start_90k &&
            elapsed_90k < frame_pts - presentation_start_90k) {
            break;
        }
        ++due;
    }
    return due;
}

/* A producer may evict only older due frames, retaining the newest one for
 * the guest consumer. */
inline size_t tj_pamf_stale_frame_count(const uint64_t* pts, size_t ring_size,
                                        size_t head, size_t count,
                                        uint64_t presentation_start_90k,
                                        uint64_t elapsed_90k)
{
    const size_t due = tj_pamf_due_frame_count(
        pts, ring_size, head, count, presentation_start_90k, elapsed_90k);
    return due ? due - 1 : 0;
}
