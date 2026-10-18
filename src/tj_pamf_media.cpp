#include "tj_pamf_media.h"
#include "tj_pamf_timing.h"
#include "tj_pamf_audio.h"

#include <algorithm>
#include <array>
#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <limits>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

#ifdef _WIN32
#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#include <windows.h>
#endif

namespace {

constexpr size_t kFrameRingSize = 3;
constexpr size_t kAudioRingSize = 8;
constexpr uint32_t kAudioSamplesPerBlock = 256;
constexpr uint32_t kAudioChannels = 2;
constexpr uint32_t kAudioBytesPerSample = 4;
constexpr size_t kAudioBlockBytes =
    (size_t)kAudioSamplesPerBlock * kAudioChannels * kAudioBytesPerSample;
constexpr uint64_t kPamfSectorSize = 2048;
constexpr size_t kSequenceScanBytes = 1024 * 1024;

uint16_t read_be16(const uint8_t* p)
{
    return (uint16_t)((uint16_t)p[0] << 8 | p[1]);
}

uint32_t read_be32(const uint8_t* p)
{
    return (uint32_t)p[0] << 24 | (uint32_t)p[1] << 16 |
           (uint32_t)p[2] << 8 | p[3];
}

bool frame_rate_from_mpeg_code(uint8_t code, uint32_t& num, uint32_t& den)
{
    switch (code) {
    case 1: num = 24000; den = 1001; return true;
    case 2: num = 24;    den = 1;    return true;
    case 3: num = 25;    den = 1;    return true;
    case 4: num = 30000; den = 1001; return true;
    case 5: num = 30;    den = 1;    return true;
    case 6: num = 50;    den = 1;    return true;
    case 7: num = 60000; den = 1001; return true;
    case 8: num = 60;    den = 1;    return true;
    default: return false;
    }
}

bool parse_pamf(const char* path, TjPamfMediaInfo& info, std::string& error)
{
    std::ifstream file(path, std::ios::binary | std::ios::ate);
    if (!file) {
        error = "cannot open PAMF";
        return false;
    }

    const std::streamoff file_size_signed = file.tellg();
    if (file_size_signed < 0x70) {
        error = "PAMF header is truncated";
        return false;
    }
    const uint64_t file_size = (uint64_t)file_size_signed;
    file.seekg(0, std::ios::beg);

    std::array<uint8_t, 0x70> header{};
    file.read(reinterpret_cast<char*>(header.data()), header.size());
    if ((size_t)file.gcount() != header.size() ||
        std::memcmp(header.data(), "PAMF", 4) != 0) {
        error = "not a PAMF container";
        return false;
    }
    if (std::memcmp(header.data() + 4, "0040", 4) != 0 &&
        std::memcmp(header.data() + 4, "0041", 4) != 0) {
        error = "unsupported PAMF version";
        return false;
    }

    const uint64_t header_sectors = read_be32(header.data() + 0x08);
    const uint64_t data_sectors = read_be32(header.data() + 0x0c);
    if (!header_sectors || header_sectors > UINT64_MAX / kPamfSectorSize ||
        data_sectors > UINT64_MAX / kPamfSectorSize) {
        error = "invalid PAMF sector counts";
        return false;
    }

    info.data_offset = header_sectors * kPamfSectorSize;
    info.data_size = data_sectors * kPamfSectorSize;
    if (info.data_offset >= file_size) {
        error = "PAMF stream offset is outside the file";
        return false;
    }
    info.data_size = std::min(info.data_size, file_size - info.data_offset);

    /* Packed PAMF sequence info begins at 0x50. PTS units are 90 kHz. */
    const uint64_t start_high = read_be16(header.data() + 0x56);
    const uint64_t start_low = read_be32(header.data() + 0x58);
    const uint64_t end_high = read_be16(header.data() + 0x5c);
    const uint64_t end_low = read_be32(header.data() + 0x5e);
    info.presentation_start_90k = start_high << 32 | start_low;
    info.presentation_end_90k = end_high << 32 | end_low;
    info.mux_rate_bound = read_be32(header.data() + 0x62) & 0x003fffffu;
    info.stream_count = read_be32(header.data() + 0x6a);

    const uint64_t scan_size64 =
        std::min<uint64_t>(info.data_size, kSequenceScanBytes);
    std::vector<uint8_t> scan((size_t)scan_size64);
    file.clear();
    file.seekg((std::streamoff)info.data_offset, std::ios::beg);
    file.read(reinterpret_cast<char*>(scan.data()),
              (std::streamsize)scan.size());
    scan.resize((size_t)std::max<std::streamsize>(file.gcount(), 0));

    info.audio_start_90k = info.presentation_start_90k;
    for (size_t i = 0; i + 9 < scan.size(); ++i) {
        if (scan[i] || scan[i + 1] || scan[i + 2] != 1 || scan[i + 3] != 0xBD)
            continue;
        const size_t size = read_be16(scan.data() + i + 4);
        if (i + 6 + size > scan.size()) continue;
        TjPamfAc3Packet packet;
        if (tj_pamf_ac3_packet(scan.data() + i + 6, size, packet) &&
            packet.size >= 2 && packet.data[0] == 0x0B && packet.data[1] == 0x77) {
            info.ac3_substream = packet.substream;
            if (packet.has_pts) info.audio_start_90k = packet.pts;
            break;
        }
    }

    bool found_sequence = false;
    for (size_t i = 0; i + 7 < scan.size(); ++i) {
        if (scan[i] != 0 || scan[i + 1] != 0 || scan[i + 2] != 1 ||
            scan[i + 3] != 0xb3) {
            continue;
        }
        const uint32_t width = (uint32_t)scan[i + 4] << 4 |
                               scan[i + 5] >> 4;
        const uint32_t height = (uint32_t)(scan[i + 5] & 0x0f) << 8 |
                                scan[i + 6];
        uint32_t rate_num = 0;
        uint32_t rate_den = 1;
        if (!width || !height || width > 8192 || height > 8192 ||
            !frame_rate_from_mpeg_code(scan[i + 7] & 0x0f,
                                       rate_num, rate_den)) {
            continue;
        }
        info.width = width;
        info.height = height;
        info.frame_rate_num = rate_num;
        info.frame_rate_den = rate_den;
        found_sequence = true;
        break;
    }
    if (!found_sequence) {
        error = "MPEG-2 sequence header not found";
        return false;
    }

    const uint64_t pixels = (uint64_t)info.width * info.height;
    if (pixels > std::numeric_limits<size_t>::max() / 4) {
        error = "decoded frame is too large";
        return false;
    }
    return true;
}

#ifdef _WIN32

std::wstring utf8_to_wide(const char* text)
{
    if (!text || !*text) return {};
    int count = MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, text, -1,
                                    nullptr, 0);
    UINT code_page = CP_UTF8;
    DWORD flags = MB_ERR_INVALID_CHARS;
    if (!count) {
        code_page = CP_ACP;
        flags = 0;
        count = MultiByteToWideChar(code_page, flags, text, -1, nullptr, 0);
    }
    if (!count) return {};
    std::wstring result((size_t)count, L'\0');
    MultiByteToWideChar(code_page, flags, text, -1, result.data(), count);
    result.resize((size_t)count - 1);
    return result;
}

bool regular_file(const std::wstring& path)
{
    const DWORD attrs = GetFileAttributesW(path.c_str());
    return attrs != INVALID_FILE_ATTRIBUTES &&
           !(attrs & FILE_ATTRIBUTE_DIRECTORY);
}

bool find_ffmpeg(std::wstring& path)
{
    std::array<wchar_t, 32768> module{};
    const DWORD module_len = GetModuleFileNameW(nullptr, module.data(),
                                                (DWORD)module.size());
    if (module_len && module_len < module.size()) {
        std::wstring beside(module.data(), module_len);
        const size_t slash = beside.find_last_of(L"\\/");
        if (slash != std::wstring::npos) beside.resize(slash + 1);
        else beside.clear();
        beside += L"ffmpeg.exe";
        if (regular_file(beside)) {
            path = std::move(beside);
            return true;
        }
    }

    std::array<wchar_t, 32768> found{};
    const DWORD found_len = SearchPathW(nullptr, L"ffmpeg.exe", nullptr,
                                        (DWORD)found.size(), found.data(),
                                        nullptr);
    if (found_len && found_len < found.size() && regular_file(found.data())) {
        path.assign(found.data(), found_len);
        return true;
    }
    return false;
}

std::wstring quote_arg(const std::wstring& arg)
{
    if (arg.find_first_of(L" \t\"") == std::wstring::npos) return arg;
    std::wstring result = L"\"";
    size_t backslashes = 0;
    for (wchar_t ch : arg) {
        if (ch == L'\\') {
            ++backslashes;
        } else if (ch == L'\"') {
            result.append(backslashes * 2 + 1, L'\\');
            result.push_back(ch);
            backslashes = 0;
        } else {
            result.append(backslashes, L'\\');
            backslashes = 0;
            result.push_back(ch);
        }
    }
    result.append(backslashes * 2, L'\\');
    result.push_back(L'\"');
    return result;
}

struct MediaState {
    std::mutex mutex;
    std::condition_variable space_available;
    std::condition_variable audio_space_available;
    std::array<std::vector<uint8_t>, kFrameRingSize> frames;
    std::array<uint64_t, kFrameRingSize> pts{};
    std::array<uint32_t, kFrameRingSize> indices{};
    size_t head = 0;
    size_t tail = 0;
    size_t count = 0;
    size_t frame_bytes = 0;
    std::array<std::vector<uint8_t>, kAudioRingSize> audio_frames;
    std::array<uint64_t, kAudioRingSize> audio_pts{};
    std::array<uint32_t, kAudioRingSize> audio_tags{};
    size_t audio_head = 0;
    size_t audio_tail = 0;
    size_t audio_count = 0;
    uint64_t audio_sample_index = 0;
    std::string source_path;
    std::wstring ffmpeg_path;
    TjPamfMediaInfo info{};
    std::chrono::steady_clock::time_point playback_epoch{};
    std::chrono::steady_clock::time_point pause_started{};
    std::thread worker;
    std::thread audio_worker;
    std::thread audio_input_worker;
    HANDLE process = nullptr;
    HANDLE stdout_read = nullptr;
    HANDLE audio_process = nullptr;
    HANDLE audio_stdout_read = nullptr;
    std::atomic<bool> prepared{false};
    std::atomic<bool> active{false};
    std::atomic<bool> stop_requested{false};
    std::atomic<bool> eof{false};
    std::atomic<bool> decoder_failed{false};
    std::atomic<bool> audio_enabled{false};
    std::atomic<bool> audio_eof{true};
    std::atomic<bool> audio_failed{false};
    bool paused = false;
};

MediaState g_media;

/* Caller holds g_media.mutex. playback_epoch already excludes paused time. */
bool playback_at_presentation_endpoint_locked()
{
    const bool playback_started =
        g_media.playback_epoch.time_since_epoch().count() != 0;
    uint64_t elapsed_90k = 0;
    if (playback_started) {
        const uint64_t elapsed_us = (uint64_t)
            std::chrono::duration_cast<std::chrono::microseconds>(
                std::chrono::steady_clock::now() -
                g_media.playback_epoch).count();
        elapsed_90k = elapsed_us * 9 / 100;
    }
    return tj_pamf_presentation_endpoint_reached(
        playback_started, g_media.paused,
        g_media.info.presentation_start_90k,
        g_media.info.presentation_end_90k, elapsed_90k);
}

bool read_exact_frame(HANDLE pipe, uint8_t* dst, size_t bytes,
                      const std::atomic<bool>& stop_requested)
{
    size_t done = 0;
    while (done < bytes && !stop_requested.load(std::memory_order_acquire)) {
        const DWORD request = (DWORD)std::min<size_t>(bytes - done, 1u << 20);
        DWORD received = 0;
        if (!ReadFile(pipe, dst + done, request, &received, nullptr) ||
            received == 0) {
            return false;
        }
        done += received;
    }
    return done == bytes;
}

bool launch_ffmpeg(const std::wstring& command, HANDLE& stdout_read,
                   HANDLE& process, HANDLE* stdin_write = nullptr)
{
    SECURITY_ATTRIBUTES inherit{};
    inherit.nLength = sizeof(inherit);
    inherit.bInheritHandle = TRUE;

    HANDLE stdout_write = nullptr;
    if (!CreatePipe(&stdout_read, &stdout_write, &inherit, 1u << 20) ||
        !SetHandleInformation(stdout_read, HANDLE_FLAG_INHERIT, 0)) {
        if (stdout_write) CloseHandle(stdout_write);
        if (stdout_read) CloseHandle(stdout_read);
        stdout_read = nullptr;
        return false;
    }

    HANDLE null_handle = CreateFileW(
        L"NUL", GENERIC_READ | GENERIC_WRITE,
        FILE_SHARE_READ | FILE_SHARE_WRITE, &inherit, OPEN_EXISTING,
        FILE_ATTRIBUTE_NORMAL, nullptr);
    if (null_handle == INVALID_HANDLE_VALUE) {
        CloseHandle(stdout_write);
        CloseHandle(stdout_read);
        stdout_read = nullptr;
        return false;
    }

    HANDLE input_read = nullptr, input_write = nullptr;
    if (stdin_write &&
        (!CreatePipe(&input_read, &input_write, &inherit, 1u << 16) ||
         !SetHandleInformation(input_write, HANDLE_FLAG_INHERIT, 0))) {
        if (input_read) CloseHandle(input_read);
        if (input_write) CloseHandle(input_write);
        CloseHandle(null_handle);
        CloseHandle(stdout_write);
        CloseHandle(stdout_read);
        stdout_read = nullptr;
        return false;
    }
    STARTUPINFOW startup{};
    startup.cb = sizeof(startup);
    startup.dwFlags = STARTF_USESTDHANDLES;
    startup.hStdInput = input_read ? input_read : null_handle;
    startup.hStdOutput = stdout_write;
    startup.hStdError = null_handle;
    PROCESS_INFORMATION process_info{};
    const BOOL created = CreateProcessW(
        nullptr, const_cast<wchar_t*>(command.data()), nullptr, nullptr, TRUE,
        CREATE_NO_WINDOW, nullptr, nullptr, &startup, &process_info);
    const DWORD create_error = created ? ERROR_SUCCESS : GetLastError();
    CloseHandle(null_handle);
    CloseHandle(stdout_write);
    if (input_read) CloseHandle(input_read);
    if (!created) {
        if (input_write) CloseHandle(input_write);
        CloseHandle(stdout_read);
        stdout_read = nullptr;
        std::fprintf(stderr, "[PAMF] ffmpeg start failed (%lu)\n",
                     create_error);
        return false;
    }
    CloseHandle(process_info.hThread);
    process = process_info.hProcess;
    if (stdin_write) *stdin_write = input_write;
    return true;
}

void decoder_worker()
{
    uint32_t index = 0;
    for (;;) {
        size_t slot = 0;
        {
            std::unique_lock<std::mutex> lock(g_media.mutex);
            for (;;) {
                if (g_media.stop_requested.load(std::memory_order_acquire) ||
                    g_media.count < kFrameRingSize) {
                    break;
                }

                /* Once playback has started, keep decoding at media time even
                 * when guest GetFrame polls more slowly than the video rate.
                 * Only discard the oldest frame after it is due; newer queued
                 * frames remain available for ordered newest-due delivery. */
                if (g_media.paused ||
                    !g_media.playback_epoch.time_since_epoch().count()) {
                    g_media.space_available.wait(lock);
                    continue;
                }

                const uint64_t elapsed_us = (uint64_t)
                    std::chrono::duration_cast<std::chrono::microseconds>(
                        std::chrono::steady_clock::now() -
                        g_media.playback_epoch).count();
                const uint64_t elapsed_90k = elapsed_us * 9 / 100;
                const size_t stale_count = tj_pamf_stale_frame_count(
                        g_media.pts.data(), kFrameRingSize, g_media.head,
                        g_media.count,
                        g_media.info.presentation_start_90k,
                        elapsed_90k);
                if (stale_count) {
                    g_media.head = (g_media.head + stale_count) %
                        kFrameRingSize;
                    g_media.count -= stale_count;
                    continue;
                }

                /* The head is the one displayable due frame, or all frames
                 * are future. Wait until the second queued frame is due so a
                 * stale predecessor can be replaced without stealing it. */
                const size_t second_slot =
                    (g_media.head + 1) % kFrameRingSize;
                const uint64_t due_90k =
                    g_media.pts[second_slot] -
                    g_media.info.presentation_start_90k;
                const auto due_time = g_media.playback_epoch +
                    std::chrono::duration_cast<std::chrono::steady_clock::duration>(
                        std::chrono::duration<uint64_t, std::ratio<1, 90000>>(
                            due_90k));
                g_media.space_available.wait_until(lock, due_time);
            }
            if (g_media.stop_requested.load(std::memory_order_acquire)) break;
            slot = g_media.tail;
        }

        if (!read_exact_frame(g_media.stdout_read,
                              g_media.frames[slot].data(),
                              g_media.frame_bytes,
                              g_media.stop_requested)) {
            break;
        }

        const uint64_t frame_delta =
            (uint64_t)index * 90000ull * g_media.info.frame_rate_den /
            g_media.info.frame_rate_num;
        {
            std::lock_guard<std::mutex> lock(g_media.mutex);
            g_media.pts[slot] = g_media.info.presentation_start_90k +
                                frame_delta;
            g_media.indices[slot] = index;
            g_media.tail = (g_media.tail + 1) % kFrameRingSize;
            ++g_media.count;
        }
        ++index;
    }

    if (!g_media.stop_requested.load(std::memory_order_acquire)) {
        DWORD exit_code = STILL_ACTIVE;
        if (!GetExitCodeProcess(g_media.process, &exit_code) ||
            (exit_code != STILL_ACTIVE && exit_code != 0)) {
            g_media.decoder_failed.store(true, std::memory_order_release);
        }
        g_media.eof.store(true, std::memory_order_release);
    }
}

void audio_input_worker(HANDLE input)
{
    std::ifstream file(g_media.source_path, std::ios::binary);
    std::string error;
    const auto cancelled = [] {
        return g_media.stop_requested.load(std::memory_order_acquire);
    };
    const auto emit = [&](const uint8_t* data, size_t size) {
        size_t done = 0;
        while (done < size && !cancelled()) {
            DWORD written = 0;
            if (!WriteFile(input, data + done, DWORD(size - done), &written, nullptr) || !written)
                return false;
            done += written;
        }
        return done == size;
    };
    if (!tj_pamf_demux_ac3(file, g_media.info.data_offset, g_media.info.data_size,
                           uint8_t(g_media.info.ac3_substream), emit, cancelled, error) &&
        !cancelled()) {
        g_media.audio_failed.store(true, std::memory_order_release);
        std::fprintf(stderr, "[PAMF] AC3 demux failed: %s\n", error.c_str());
    }
    CloseHandle(input); // EOF to decoder; this feeder exclusively owns the write end.
}

void audio_decoder_worker()
{
    uint32_t tag = 0;
    for (;;) {
        size_t slot = 0;
        {
            std::unique_lock<std::mutex> lock(g_media.mutex);
            g_media.audio_space_available.wait(lock, [] {
                return g_media.stop_requested.load(std::memory_order_acquire) ||
                       g_media.audio_count < kAudioRingSize;
            });
            if (g_media.stop_requested.load(std::memory_order_acquire)) break;
            slot = g_media.audio_tail;
        }

        if (!read_exact_frame(g_media.audio_stdout_read,
                              g_media.audio_frames[slot].data(),
                              kAudioBlockBytes, g_media.stop_requested)) {
            break;
        }

        {
            std::lock_guard<std::mutex> lock(g_media.mutex);
            g_media.audio_pts[slot] =
                g_media.info.audio_start_90k +
                g_media.audio_sample_index * 90000ull / 48000ull;
            g_media.audio_tags[slot] = tag;
            g_media.audio_tail = (g_media.audio_tail + 1) % kAudioRingSize;
            ++g_media.audio_count;
            g_media.audio_sample_index += kAudioSamplesPerBlock;
        }
        ++tag;
        if (tag == 1)
            std::fprintf(stderr, "[PAMF] decoded first audio block (f32be 48k stereo)\n");
    }

    if (!g_media.stop_requested.load(std::memory_order_acquire)) {
        DWORD exit_code = STILL_ACTIVE;
        if (!GetExitCodeProcess(g_media.audio_process, &exit_code) ||
            (exit_code != STILL_ACTIVE && exit_code != 0)) {
            g_media.audio_failed.store(true, std::memory_order_release);
        }
        g_media.audio_eof.store(true, std::memory_order_release);
    }
}

void stop_decoder()
{
    g_media.stop_requested.store(true, std::memory_order_release);
    g_media.space_available.notify_all();
    g_media.audio_space_available.notify_all();

    if (g_media.process) {
        DWORD exit_code = 0;
        if (GetExitCodeProcess(g_media.process, &exit_code) &&
            exit_code == STILL_ACTIVE) {
            TerminateProcess(g_media.process, 0);
        }
    }
    if (g_media.audio_process) {
        DWORD exit_code = 0;
        if (GetExitCodeProcess(g_media.audio_process, &exit_code) &&
            exit_code == STILL_ACTIVE) {
            TerminateProcess(g_media.audio_process, 0);
        }
    }
    if (g_media.worker.joinable()) g_media.worker.join();
    if (g_media.audio_worker.joinable()) g_media.audio_worker.join();
    if (g_media.audio_input_worker.joinable()) g_media.audio_input_worker.join();
    if (g_media.stdout_read) {
        CloseHandle(g_media.stdout_read);
        g_media.stdout_read = nullptr;
    }
    if (g_media.process) {
        WaitForSingleObject(g_media.process, 2000);
        CloseHandle(g_media.process);
        g_media.process = nullptr;
    }
    if (g_media.audio_stdout_read) {
        CloseHandle(g_media.audio_stdout_read);
        g_media.audio_stdout_read = nullptr;
    }
    if (g_media.audio_process) {
        WaitForSingleObject(g_media.audio_process, 2000);
        CloseHandle(g_media.audio_process);
        g_media.audio_process = nullptr;
    }

    {
        std::lock_guard<std::mutex> lock(g_media.mutex);
        g_media.head = 0;
        g_media.tail = 0;
        g_media.count = 0;
        g_media.audio_head = 0;
        g_media.audio_tail = 0;
        g_media.audio_count = 0;
        g_media.audio_sample_index = 0;
        g_media.paused = false;
    }
    g_media.active.store(false, std::memory_order_release);
    g_media.audio_enabled.store(false, std::memory_order_release);
    g_media.audio_eof.store(true, std::memory_order_release);
}

#else

struct MediaState {
    TjPamfMediaInfo info{};
    std::string source_path;
    std::atomic<bool> prepared{false};
};

MediaState g_media;
void stop_decoder() {}

#endif

} // namespace

bool tj_pamf_prepare(const char* host_path, TjPamfMediaInfo* out_info)
{
    static const bool cleanup_registered = [] {
        std::atexit(tj_pamf_close);
        return true;
    }();
    (void)cleanup_registered;
    tj_pamf_close();
    if (!host_path || !*host_path) return false;

    TjPamfMediaInfo info{};
    std::string error;
    if (!parse_pamf(host_path, info, error)) {
        std::fprintf(stderr, "[PAMF] prepare failed: %s (%s)\n",
                     error.c_str(), host_path);
        return false;
    }

#ifdef _WIN32
    std::wstring ffmpeg;
    if (!find_ffmpeg(ffmpeg)) {
        std::fprintf(stderr,
                     "[PAMF] decoder unavailable: ffmpeg.exe not beside recomp or on PATH\n");
        return false;
    }
    g_media.ffmpeg_path = std::move(ffmpeg);
    g_media.frame_bytes = (size_t)info.width * info.height * 4;
    for (auto& frame : g_media.frames) frame.resize(g_media.frame_bytes);
    for (auto& frame : g_media.audio_frames) frame.resize(kAudioBlockBytes);
#else
    std::fprintf(stderr, "[PAMF] decoder unavailable on this platform\n");
    return false;
#endif

    g_media.source_path = host_path;
    g_media.info = info;
    g_media.prepared.store(true, std::memory_order_release);
    if (out_info) *out_info = info;
    std::fprintf(stderr,
                 "[PAMF] prepared %ux%u %u/%u fps PTS=%llu..%llu data=0x%llX+0x%llX\n",
                 info.width, info.height, info.frame_rate_num,
                 info.frame_rate_den,
                 (unsigned long long)info.presentation_start_90k,
                 (unsigned long long)info.presentation_end_90k,
                 (unsigned long long)info.data_offset,
                 (unsigned long long)info.data_size);
    return true;
}

bool tj_pamf_start()
{
#ifndef _WIN32
    return false;
#else
    if (!g_media.prepared.load(std::memory_order_acquire)) return false;
    stop_decoder();

    const std::wstring source = utf8_to_wide(g_media.source_path.c_str());
    if (source.empty()) {
        return false;
    }

    std::wstring command = quote_arg(g_media.ffmpeg_path) +
        L" -v error -nostdin -f mpeg -skip_initial_bytes " +
        std::to_wstring(g_media.info.data_offset) + L" -i " +
        quote_arg(source) +
        L" -map 0:v:0 -an -sn -f rawvideo -pix_fmt argb -";

    if (!launch_ffmpeg(command, g_media.stdout_read, g_media.process)) {
        return false;
    }

    {
        std::lock_guard<std::mutex> lock(g_media.mutex);
        g_media.head = 0;
        g_media.tail = 0;
        g_media.count = 0;
        g_media.audio_head = 0;
        g_media.audio_tail = 0;
        g_media.audio_count = 0;
        g_media.audio_sample_index = 0;
        g_media.playback_epoch = {};
        g_media.pause_started = {};
        g_media.paused = false;
    }
    g_media.stop_requested.store(false, std::memory_order_release);
    g_media.eof.store(false, std::memory_order_release);
    g_media.decoder_failed.store(false, std::memory_order_release);
    g_media.audio_failed.store(false, std::memory_order_release);
    g_media.audio_enabled.store(false, std::memory_order_release);
    g_media.audio_eof.store(true, std::memory_order_release);
    g_media.active.store(true, std::memory_order_release);

    const bool sony_ac3 = g_media.info.ac3_substream >= 0;
    const std::wstring audio_input = sony_ac3 ? L" -f ac3 -i pipe:0" :
        L" -f mpeg -skip_initial_bytes " + std::to_wstring(g_media.info.data_offset) +
        L" -i " + quote_arg(source);
    const std::wstring audio_command = quote_arg(g_media.ffmpeg_path) +
        L" -v error -nostdin" + audio_input +
        L" -map 0:a:0 -vn -sn -acodec pcm_f32be -f f32be -ar 48000 -ac 2 -";
    HANDLE audio_stdin_write = nullptr;
    if (launch_ffmpeg(audio_command, g_media.audio_stdout_read,
                      g_media.audio_process, sony_ac3 ? &audio_stdin_write : nullptr)) {
        g_media.audio_enabled.store(true, std::memory_order_release);
        g_media.audio_eof.store(false, std::memory_order_release);
        g_media.audio_worker = std::thread(audio_decoder_worker);
        if (sony_ac3)
            g_media.audio_input_worker = std::thread(audio_input_worker, audio_stdin_write);
        std::fprintf(stderr, "[PAMF] audio decoder launched (%s -> f32be 48k stereo)\n",
                     sony_ac3 ? "PAMF AC3" : "MPEG audio");
    } else {
        /* Audio is optional. A video-only PAMF must remain playable. */
        g_media.audio_enabled.store(false, std::memory_order_release);
        g_media.audio_eof.store(true, std::memory_order_release);
        g_media.audio_failed.store(true, std::memory_order_release);
        std::fprintf(stderr, "[PAMF] audio decoder unavailable; continuing video-only\n");
    }
    g_media.worker = std::thread(decoder_worker);
    std::fprintf(stderr, "[PAMF] decoder started\n");
    return true;
#endif
}

void tj_pamf_stop()
{
    stop_decoder();
}

void tj_pamf_close()
{
    stop_decoder();
    g_media.prepared.store(false, std::memory_order_release);
    g_media.source_path.clear();
#ifdef _WIN32
    g_media.ffmpeg_path.clear();
    g_media.frame_bytes = 0;
    for (auto& frame : g_media.frames) frame.clear();
    for (auto& frame : g_media.audio_frames) frame.clear();
#endif
    g_media.info = {};
}

void tj_pamf_set_paused(bool paused)
{
#ifdef _WIN32
    if (!tj_pamf_active()) return;
    const auto now = std::chrono::steady_clock::now();
    {
        std::lock_guard<std::mutex> lock(g_media.mutex);
        if (paused == g_media.paused) return;
        if (paused) {
            g_media.pause_started = now;
        } else if (g_media.pause_started.time_since_epoch().count() &&
                   g_media.playback_epoch.time_since_epoch().count()) {
            g_media.playback_epoch += now - g_media.pause_started;
            g_media.pause_started = {};
        } else {
            g_media.pause_started = {};
        }
        g_media.paused = paused;
    }
    g_media.space_available.notify_all();
#else
    (void)paused;
#endif
}

bool tj_pamf_prepared()
{
    return g_media.prepared.load(std::memory_order_acquire);
}

bool tj_pamf_active()
{
#ifdef _WIN32
    return g_media.active.load(std::memory_order_acquire);
#else
    return false;
#endif
}

bool tj_pamf_eof()
{
#ifndef _WIN32
    return true;
#else
    std::lock_guard<std::mutex> lock(g_media.mutex);
    if (playback_at_presentation_endpoint_locked()) return true;
    const bool video_done = g_media.eof.load(std::memory_order_acquire) &&
                            g_media.count == 0;
    const bool audio_done =
        !g_media.audio_enabled.load(std::memory_order_acquire) ||
        (g_media.audio_eof.load(std::memory_order_acquire) &&
         g_media.audio_count == 0);
    return video_done && audio_done;
#endif
}

bool tj_pamf_get_info(TjPamfMediaInfo* out_info)
{
    if (!out_info || !tj_pamf_prepared()) return false;
    *out_info = g_media.info;
    return true;
}

bool tj_pamf_pop_argb(void* dst, size_t dst_size, uint64_t* pts_90k,
                      uint32_t* frame_index)
{
#ifndef _WIN32
    (void)dst;
    (void)dst_size;
    (void)pts_90k;
    (void)frame_index;
    return false;
#else
    if (!dst || dst_size < g_media.frame_bytes || !tj_pamf_active())
        return false;

    std::unique_lock<std::mutex> lock(g_media.mutex);
    if (playback_at_presentation_endpoint_locked()) return false;
    if (!g_media.count || g_media.paused) return false;

    if (!g_media.playback_epoch.time_since_epoch().count())
        g_media.playback_epoch = std::chrono::steady_clock::now();
    const uint64_t elapsed_us = (uint64_t)
        std::chrono::duration_cast<std::chrono::microseconds>(
            std::chrono::steady_clock::now() - g_media.playback_epoch).count();
    const uint64_t elapsed_90k = elapsed_us * 9 / 100;
    const size_t due_count = tj_pamf_due_frame_count(
        g_media.pts.data(), kFrameRingSize, g_media.head, g_media.count,
        g_media.info.presentation_start_90k, elapsed_90k);
    if (!due_count) return false;

    const size_t slot = (g_media.head + due_count - 1) % kFrameRingSize;
    std::memcpy(dst, g_media.frames[slot].data(), g_media.frame_bytes);
    if (pts_90k) *pts_90k = g_media.pts[slot];
    if (frame_index) *frame_index = g_media.indices[slot];
    g_media.head = (slot + 1) % kFrameRingSize;
    g_media.count -= due_count;
    lock.unlock();
    g_media.space_available.notify_one();
    return true;
#endif
}

bool tj_pamf_pop_audio_f32be(void* dst, size_t dst_size,
                             uint32_t requested_samples,
                             uint64_t* pts_90k, uint32_t* tag)
{
#ifndef _WIN32
    (void)dst;
    (void)dst_size;
    (void)requested_samples;
    (void)pts_90k;
    (void)tag;
    return false;
#else
    if (!dst || requested_samples != kAudioSamplesPerBlock ||
        dst_size < kAudioBlockBytes || !tj_pamf_active() ||
        !g_media.audio_enabled.load(std::memory_order_acquire)) {
        return false;
    }

    std::unique_lock<std::mutex> lock(g_media.mutex);
    if (playback_at_presentation_endpoint_locked()) return false;
    if (!g_media.audio_count || g_media.paused) return false;

    const size_t slot = g_media.audio_head;
    if (!g_media.playback_epoch.time_since_epoch().count())
        g_media.playback_epoch = std::chrono::steady_clock::now();
    const uint64_t elapsed_us = (uint64_t)
        std::chrono::duration_cast<std::chrono::microseconds>(
            std::chrono::steady_clock::now() - g_media.playback_epoch).count();
    const uint64_t elapsed_90k = elapsed_us * 9 / 100;
    const uint64_t due_90k = g_media.audio_pts[slot] -
        g_media.info.presentation_start_90k;
    if (elapsed_90k < due_90k) return false;

    std::memcpy(dst, g_media.audio_frames[slot].data(), kAudioBlockBytes);
    if (pts_90k) *pts_90k = g_media.audio_pts[slot];
    if (tag) *tag = g_media.audio_tags[slot];
    g_media.audio_head = (g_media.audio_head + 1) % kAudioRingSize;
    --g_media.audio_count;
    lock.unlock();
    g_media.audio_space_available.notify_one();
    return true;
#endif
}
