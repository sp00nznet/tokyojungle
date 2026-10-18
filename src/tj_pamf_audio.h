#pragma once

#include <array>
#include <cstddef>
#include <cstdint>
#include <istream>
#include <string>

struct TjPamfAc3Packet {
    const uint8_t* data = nullptr;
    size_t size = 0;
    uint8_t substream = 0;
    uint64_t pts = 0;
    bool has_pts = false;
};

/* Sony's private_stream_1 AC3 IDs30..37 overlap DVD subtitle IDs. The four
 * private bytes prefix every PES fragment, including fragments inside a frame. */
inline bool tj_pamf_ac3_packet(const uint8_t* pes, size_t size,
                              TjPamfAc3Packet& packet)
{
    if (size < 3 || (pes[0] & 0xC0) != 0x80) return false;
    const size_t payload = 3 + pes[2];
    if (payload + 4 >= size || pes[payload] < 0x30 || pes[payload] > 0x37)
        return false;
    packet = {};
    packet.substream = pes[payload];
    packet.data = pes + payload + 4;
    packet.size = size - payload - 4;
    if ((pes[1] & 0x80) && pes[2] >= 5 &&
        (pes[3] & 1) && (pes[5] & 1) && (pes[7] & 1)) {
        packet.pts = uint64_t((pes[3] >> 1) & 7) << 30 |
                     uint64_t(pes[4]) << 22 | uint64_t(pes[5] >> 1) << 15 |
                     uint64_t(pes[6]) << 7 | (pes[7] >> 1);
        packet.has_pts = true;
    }
    return true;
}

/* Bounded program-stream demux. Never searches compressed payload for packet
 * boundaries, combines different private tracks, or buffers the entire movie. */
template<class Emit, class Cancelled>
bool tj_pamf_demux_ac3(std::istream& file, uint64_t offset, uint64_t bytes,
                       uint8_t substream, Emit emit, Cancelled cancelled,
                       std::string& error)
{
    if (substream < 0x30 || substream > 0x37) {
        error = "invalid AC3 substream";
        return false;
    }
    file.clear();
    file.seekg(static_cast<std::streamoff>(offset), std::ios::beg);
    if (!file) { error = "cannot seek PAMF audio"; return false; }
    std::array<uint8_t, 65535> packet{};
    const auto read = [&](uint8_t* dst, size_t count) {
        if (count > bytes) return false;
        file.read(reinterpret_cast<char*>(dst), static_cast<std::streamsize>(count));
        if (size_t(file.gcount()) != count) return false;
        bytes -= count;
        return true;
    };
    while (bytes && !cancelled()) {
        uint8_t prefix[4];
        if (!read(prefix, 4) || prefix[0] || prefix[1] || prefix[2] != 1) {
            error = "invalid PAMF packet boundary"; return false;
        }
        if (prefix[3] == 0xB9) return true;
        if (prefix[3] == 0xBA) {
            if (!read(packet.data(), 10) || (packet[0] & 0xC0) != 0x40 ||
                !read(packet.data(), packet[9] & 7)) {
                error = "truncated MPEG-2 pack"; return false;
            }
            continue;
        }
        uint8_t length[2];
        if (!read(length, 2)) { error = "missing PES length"; return false; }
        const size_t size = size_t(length[0]) << 8 | length[1];
        if (!size || !read(packet.data(), size)) {
            error = "invalid or truncated PES length"; return false;
        }
        if (prefix[3] != 0xBD) continue;
        TjPamfAc3Packet audio;
        if (tj_pamf_ac3_packet(packet.data(), size, audio) &&
            audio.substream == substream && !emit(audio.data, audio.size)) {
            error = "AC3 input consumer stopped"; return false;
        }
    }
    return !cancelled();
}
