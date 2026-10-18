#pragma once

#include <cstddef>
#include <cstdint>

struct TjPamfMediaInfo {
    uint32_t width = 0;
    uint32_t height = 0;
    uint32_t frame_rate_num = 0;
    uint32_t frame_rate_den = 1;
    uint64_t presentation_start_90k = 0;
    uint64_t presentation_end_90k = 0;
    uint32_t mux_rate_bound = 0;
    uint32_t stream_count = 0;
    uint64_t data_offset = 0;
    uint64_t data_size = 0;
    int32_t ac3_substream = -1;
    uint64_t audio_start_90k = 0;
};

/* Read and validate a PAMF container, locate ffmpeg.exe beside the recomp or
 * on PATH, and retain the source for a later start. Preparing never launches
 * a process. A new prepare replaces the previous source. */
bool tj_pamf_prepare(const char* host_path, TjPamfMediaInfo* out_info);

/* Start/stop the decoder owned by this bridge. The decoder writes ARGB frames
 * into a bounded host ring; only the guest-thread GetFrame HLE copies them to
 * emulated memory. stop keeps the prepared source restartable, close does not. */
bool tj_pamf_start();
void tj_pamf_stop();
void tj_pamf_close();
void tj_pamf_set_paused(bool paused);

bool tj_pamf_prepared();
bool tj_pamf_active();
bool tj_pamf_eof();
bool tj_pamf_get_info(TjPamfMediaInfo* out_info);

/* Returns one frame only when its presentation time is due. */
bool tj_pamf_pop_argb(void* dst, size_t dst_size, uint64_t* pts_90k,
                      uint32_t* frame_index);

/* Sound frames are interleaved big-endian float32, stereo, 48 kHz. A sound
 * pop is all-or-nothing: the requested block must be available and due. */
bool tj_pamf_pop_audio_f32be(void* dst, size_t dst_size,
                             uint32_t requested_samples,
                             uint64_t* pts_90k, uint32_t* tag);
