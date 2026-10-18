// Isolated, one-frame renderer replay. Never executes guest instructions.
#include <cstdio>
#include <cerrno>
#include <cstdlib>
#include <cstring>
#include <stdexcept>
#include <vector>
#include <memory>
#include "vm.h"
#include "guest_world_rendezvous.h"
#include "../../ps3recomp/libs/video/cellGcmSys.h"
#include "../../ps3recomp/libs/video/rsx_commands.h"
#include "../../ps3recomp/libs/video/rsx_d3d12_backend.h"
#ifdef _WIN32
#include <windows.h>
#endif

extern "C" int cellGcmFifoSubchannelMethod(u32, u32, u32);
extern "C" int cellGcmReplayBeginFrame(u32, CellGcmFlipTicket*);

namespace {
struct Reader {
    std::vector<u8> data;
    size_t pos = 0;
    const u8* take(size_t n) {
        if (n > data.size() - pos) throw std::runtime_error("truncated fixture");
        const u8* p = data.data() + pos; pos += n; return p;
    }
    u32 word() { const u8* p = take(4); return p[0] | (u32)p[1]<<8 |
        (u32)p[2]<<16 | (u32)p[3]<<24; }
};
struct Block { u32 ea, bytes; const u8* data; };
struct Command { u32 method, value; std::vector<u32> blocks; };
struct RestoreTiming {
    u64 ticks, bytes, block_refs;
    u32 block_commands;
    bool valid;
};
struct ReplayResult {
    CellGcmFlipTicket ticket;
    u32 methods, draws, depth_writes;
    RestoreTiming restore;
    std::unique_ptr<rsx_state> state;
};

u32 reference_repeat_count() {
    const char* value=getenv("TJ_RSX_REFERENCE_REPEAT");
    if (!value) return 1;
    for (const char* p=value; *p; ++p)
        if (*p<'0' || *p>'9')
            throw std::runtime_error("invalid reference replay repeat count");
    errno=0; char* end=nullptr; unsigned long parsed=strtoul(value,&end,10);
    if (!*value || *end || errno==ERANGE || !parsed || parsed>120)
        throw std::runtime_error("invalid reference replay repeat count");
    return (u32)parsed;
}

bool reference_copy_timing_enabled(u32 repeats) {
    const char* value=repeats>1 ? getenv("TJ_RSX_REFERENCE_COPY_TIMING") : nullptr;
    return value && !strcmp(value,"1");
}

#ifdef _WIN32
bool reference_elapsed_us(LARGE_INTEGER start, LARGE_INTEGER end, LARGE_INTEGER frequency,
                          u64* elapsed) {
    if (!elapsed || frequency.QuadPart<=0 || end.QuadPart<=start.QuadPart) return false;
    *elapsed=(u64)(end.QuadPart-start.QuadPart)*1000000ull/(u64)frequency.QuadPart;
    return true;
}
#endif

bool initial_setter(u32 m) {
    // Initial registers are state, not a request to repeat old draws/clears,
    // reports, semaphore writes or inline payloads. Program bytes load below.
    if (m < 0x200 || m >= 0x2000) return false;
    if ((m >= 0xB80 && m < 0xC00) || (m >= 0x1F00)) return false;
    switch (m) {
    case 0x1808: case 0x1814: case 0x1818: case 0x1824:
    case 0x1D6C: case 0x1D70: case 0x1D74: case 0x1D78:
    case 0x1D84: case 0x1D94: return false;
    default: return true;
    }
}
}

int tj_rsx_reference_main(const char* path) {
    bool gpu = false, vm = false, registered = false;
    try {
        Reader reader;
        std::unique_ptr<FILE, decltype(&fclose)> file(fopen(path, "rb"), fclose);
        if (!file) throw std::runtime_error("cannot open fixture");
        fseek(file.get(), 0, SEEK_END);
        long size = ftell(file.get());
        if (size < 20 || size > 256 * 1024 * 1024)
            throw std::runtime_error("fixture size outside limit");
        rewind(file.get()); reader.data.resize((size_t)size);
        if (fread(reader.data.data(), 1, reader.data.size(), file.get()) != reader.data.size())
            throw std::runtime_error("fixture read failed");
        if (memcmp(reader.take(8), "TJRSX001", 8))
            throw std::runtime_error("wrong fixture version");
        const u32 blocks_count = reader.word(), commands_count = reader.word();
        const u32 flip = reader.word();
        if (blocks_count > 8192 || commands_count > 1000000 || flip >= 8)
            throw std::runtime_error("fixture count outside limit");
        std::vector<u32> initial(16384);
        for (u32& v : initial) v = reader.word();
        const u8* program = reader.take(544 * 16);
        u32 display[8][4], tiles[15][4];
        for (auto& d : display) for (u32& v : d) v = reader.word();
        u32 display_count = reader.word();
        for (auto& t : tiles) for (u32& v : t) v = reader.word();
        if (!display_count || display_count > 8 || flip >= display_count)
            throw std::runtime_error("invalid display selection");
        for (u32 i=0; i<display_count; ++i) {
            const auto& d=display[i];
            if (!d[0] || !d[1] || d[0]>4096 || d[1]>4096 ||
                d[2]<d[0]*4 || (u64)d[3]+(u64)d[2]*d[1]>0x10000000)
                throw std::runtime_error("invalid display extent");
        }
        std::vector<Block> blocks; blocks.reserve(blocks_count);
        for (u32 i=0; i<blocks_count; ++i) {
            u32 ea=reader.word(), bytes=reader.word(); u64 end=(u64)ea+bytes;
            if (!((ea>=0xC0000000 && end<=0xD0000000) ||
                  (ea>=0x10000000 && end<=0x18000000)))
                throw std::runtime_error("memory block outside replay VM");
            blocks.push_back({ea,bytes,reader.take(bytes)});
        }
        std::vector<Command> commands; commands.reserve(commands_count);
        for (u32 i=0; i<commands_count; ++i) {
            Command c{reader.word(),reader.word(),{}}; u32 n=reader.word();
            if (c.method>=0x10000 || (c.method&3) || n>blocks_count)
                throw std::runtime_error("invalid replay method/reference count");
            for (u32 j=0; j<n; ++j) {
                u32 id=reader.word();
                if (id>=blocks_count) throw std::runtime_error("unknown memory block");
                c.blocks.push_back(id);
            }
            commands.push_back(std::move(c));
        }
        if (reader.pos != reader.data.size() || commands.empty() ||
            commands.back().method!=0xE944 || commands.back().value!=flip)
            throw std::runtime_error("incomplete frame or trailing bytes");
        u32 draw_limit=0;
        if (const char* value=getenv("TJ_RSX_REFERENCE_DRAW_LIMIT")) {
            char* end=nullptr; unsigned long parsed=strtoul(value,&end,10);
            if (!*value || *end || !parsed || parsed>commands_count)
                throw std::runtime_error("invalid replay draw limit");
            draw_limit=(u32)parsed;
        }
        const u32 repeats=reference_repeat_count();
        const bool copy_timing=reference_copy_timing_enabled(repeats);
        // Validate the complete fixture before reserving memory or creating GPU state.
        if (vm_init()!=0) throw std::runtime_error("VM init failed"); vm=true;
        if (!guest_world_register_current("rsx-reference"))
            throw std::runtime_error("replay owner registration failed");
        registered=true;
        if (vm_commit(0,0x10000)!=0 || vm_commit(0x03000000,0x10000)!=0 ||
            vm_commit(0x10000000,0x08000000)!=0 || vm_commit(0xC0000000,0x10000000)!=0)
            throw std::runtime_error("VM commit failed");
        if (cellGcmInit(0x10000,0x08000000,0x10000000)!=0)
            throw std::runtime_error("GCM init failed");
        for (u32 i=0; i<display_count; ++i) {
            auto& d=display[i];
            if (cellGcmSetDisplayBuffer(i,d[3],d[2],d[0],d[1])!=0)
                throw std::runtime_error("display setup failed");
        }
        for (u32 i=0; i<15; ++i) {
            auto& t=tiles[i]; if (!t[2]) continue;
            u32 offset=t[0]&0x7FFF0000, limit=t[1]&0x7FFF0000;
            if (limit<offset || (u64)limit+0x10000>0x10000000)
                throw std::runtime_error("invalid tile extent");
            // RRC stores logical memory; native resources do not use RSX compression.
            if (cellGcmSetTileInfo((u8)i,0,offset,limit-offset+0x10000,t[2],0,0,0)!=0 ||
                cellGcmBindTile((u8)i)!=0)
                throw std::runtime_error("tile setup failed");
        }
        _putenv_s("TJ_BACKGROUND_TEST_WINDOW","1");
        // Prefix copies can leave fewer than the game's usual 99 final draws.
        _putenv_s("TJ_COMPOSE_CAPTURE_SHORT_FRAME","1");
        if (rsx_d3d12_backend_init(display[flip][0],display[flip][1],"Tokyo Jungle reference frame")!=0)
            throw std::runtime_error("D3D12 init failed");
        gpu=true; rsx_d3d12_arm_survival_trace();
        auto dispatch_once = [&](bool time_block_copies) -> ReplayResult {
            ReplayResult result{};
            result.restore.valid=true;
            result.state=std::make_unique<rsx_state>();
            rsx_state_init(result.state.get());
            for (u32 i=0; i<initial.size(); ++i)
                if (initial_setter(i*4)) rsx_process_method(result.state.get(),i*4,initial[i]);
            memcpy(result.state->vp_ucode,program,544*16);
            result.state->vp_ucode_bytes=544*16; result.state->vp_dirty=1;
            if (!cellGcmReplayBeginFrame(flip,&result.ticket))
                throw std::runtime_error("replay frame ownership failed");
            for (const auto& c : commands) {
                ++result.methods;
                const bool time_blocks=time_block_copies && !c.blocks.empty();
                if (time_blocks) ++result.restore.block_commands;
#ifdef _WIN32
                LARGE_INTEGER copy_start{}, copy_end{};
                const bool copy_start_valid=!time_blocks ||
                    (QueryPerformanceCounter(&copy_start) != 0 && copy_start.QuadPart>0);
#endif
                for (u32 id : c.blocks) { const auto& b=blocks[id];
                    if (time_blocks) {
                        ++result.restore.block_refs;
                        result.restore.bytes+=b.bytes;
                    }
                    memcpy(vm_base+b.ea,b.data,b.bytes); }
#ifdef _WIN32
                if (time_blocks) {
                    const bool copy_end_valid=QueryPerformanceCounter(&copy_end) != 0 &&
                        copy_end.QuadPart>0;
                    if (!copy_start_valid || !copy_end_valid ||
                        copy_end.QuadPart<copy_start.QuadPart)
                        result.restore.valid=false;
                    else
                        result.restore.ticks+=(u64)(copy_end.QuadPart-copy_start.QuadPart);
                }
#endif
                if (c.method==0xE944) break; // present once below, no guest flip callback
                u32 subch=c.method>>13, method=c.method&0x1FFC;
                if (method==0 || subch!=0) {
                    if (cellGcmFifoSubchannelMethod(subch,method,c.value)!=0)
                        throw std::runtime_error("2D replay method failed");
                } else {
                    rsx_process_method(result.state.get(),method,c.value);
                    if (method==0x1808 && c.value) {
                        ++result.draws;
                        if (result.state->depth_test_enable && result.state->depth_mask)
                            ++result.depth_writes;
                    }
                }
                // A diagnostic prefix ends only after a complete draw. This does
                // not alter or skip any method inside the selected prefix.
                if (draw_limit && result.draws>=draw_limit && c.method==0x1808 && !c.value)
                    break;
            }
            return result;
        };
        auto present_once = [&](const ReplayResult& result) {
            rsx_d3d12_backend_present_buffer(result.ticket.generation,result.ticket.buffer_id);
            if (rsx_d3d12_backend_fifo_faulted())
                throw std::runtime_error("renderer FIFO fault");
        };
        ReplayResult result{};
        if (repeats==1) {
            result=dispatch_once(false); // Preserve the existing one-frame diagnostic route.
            present_once(result);
        } else {
#ifdef _WIN32
            LARGE_INTEGER frequency{};
            const bool frequency_valid=QueryPerformanceFrequency(&frequency) && frequency.QuadPart>0;
            for (u32 repeat=0; repeat<repeats; ++repeat) {
                LARGE_INTEGER marks[4]{};
                bool clock_valid[4] = {
                    QueryPerformanceCounter(&marks[0]) != 0 && marks[0].QuadPart>0,
                    false, false, false
                };
                ReplayResult current=dispatch_once(copy_timing);
                clock_valid[1]=QueryPerformanceCounter(&marks[1]) != 0 && marks[1].QuadPart>0;
                present_once(current);
                clock_valid[2]=QueryPerformanceCounter(&marks[2]) != 0 && marks[2].QuadPart>0;
                if (rsx_d3d12_backend_finish_reference_frame()!=0 ||
                    rsx_d3d12_backend_fifo_faulted())
                    throw std::runtime_error("reference replay completion failed");
                if (!cellGcm_complete_flip_ticket(&current.ticket))
                    throw std::runtime_error("reference replay ticket completion failed");
                clock_valid[3]=QueryPerformanceCounter(&marks[3]) != 0 && marks[3].QuadPart>0;
                if (repeat && (current.methods!=result.methods || current.draws!=result.draws ||
                               current.depth_writes!=result.depth_writes))
                    throw std::runtime_error("reference replay count changed");
                result=std::move(current);
                u64 prep_us=0, present_us=0, finish_us=0, total_us=0;
                u64 restore_us=0;
                const bool timing_valid=frequency_valid && clock_valid[0] && clock_valid[1] &&
                    clock_valid[2] && clock_valid[3] &&
                    reference_elapsed_us(marks[0],marks[1],frequency,&prep_us) &&
                    reference_elapsed_us(marks[1],marks[2],frequency,&present_us) &&
                    reference_elapsed_us(marks[2],marks[3],frequency,&finish_us) &&
                    reference_elapsed_us(marks[0],marks[3],frequency,&total_us);
                const bool restore_valid=copy_timing && frequency_valid && result.restore.valid;
                if (restore_valid)
                    restore_us=result.restore.ticks*1000000ull/(u64)frequency.QuadPart;
                if (timing_valid) {
                    printf("[TJ-REFERENCE] repeat=%u/%u phase=%s hostelapsed_valid=1 prep_us=%llu present_call_us=%llu finish_us=%llu total_us=%llu methods=%u draws=%u tested_depth_writes=%u",
                           repeat+1,repeats,repeat ? "warm" : "cold",
                           (unsigned long long)prep_us,(unsigned long long)present_us,
                           (unsigned long long)finish_us,(unsigned long long)total_us,
                           result.methods,result.draws,result.depth_writes);
                } else {
                    printf("[TJ-REFERENCE] repeat=%u/%u phase=%s hostelapsed_valid=0 prep_us=NA present_call_us=NA finish_us=NA total_us=NA methods=%u draws=%u tested_depth_writes=%u",
                           repeat+1,repeats,repeat ? "warm" : "cold",
                           result.methods,result.draws,result.depth_writes);
                }
                if (copy_timing) {
                    if (restore_valid)
                        printf(" restore_valid=1 restore_us=%llu restore_bytes=%llu block_refs=%llu block_commands=%u",
                               (unsigned long long)restore_us,
                               (unsigned long long)result.restore.bytes,
                               (unsigned long long)result.restore.block_refs,
                               result.restore.block_commands);
                    else
                        printf(" restore_valid=0 restore_us=NA restore_bytes=%llu block_refs=%llu block_commands=%u",
                               (unsigned long long)result.restore.bytes,
                               (unsigned long long)result.restore.block_refs,
                               result.restore.block_commands);
                }
                printf("\n");
            }
#else
            throw std::runtime_error("reference replay repeat requires Windows QPC");
#endif
        }
        rsx_d3d12_backend_shutdown(); gpu=false;
        if (!guest_world_unregister_current())
            throw std::runtime_error("replay owner cleanup failed");
        registered=false;
        vm_shutdown(); vm=false;
        printf("[TJ-REFERENCE] %s methods=%u draws=%u tested_depth_writes=%u gameplay=false\n",
               draw_limit ? "diagnostic-prefix" : "complete",result.methods,result.draws,result.depth_writes);
        return 0;
    } catch (const std::exception& e) {
        fprintf(stderr,"[TJ-REFERENCE] FAILED: %s\n",e.what());
        if (gpu) rsx_d3d12_backend_shutdown();
        if (registered) guest_world_unregister_current();
        if (vm) vm_shutdown();
        return 1;
    }
}
