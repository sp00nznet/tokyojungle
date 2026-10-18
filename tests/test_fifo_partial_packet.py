"""Run both production FIFO walkers against incrementally published packets.

This is a host-only decoder regression test, not a renderer or gameplay test.
"""
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

from test_fifo_present_boundary import function

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT.parent / "ps3recomp/libs/video/cellGcmSys.c"


def main():
    gcm = SOURCE.read_text(encoding="utf-8")
    commands = SOURCE.with_name("rsx_commands.c").read_text(encoding="utf-8")
    release_begin = commands.index("{ static u32 s_sem_off = 0;")
    release_end = commands.index("    if (method == NV4097_SET_CONTEXT_DMA_COLOR_B", release_begin)
    backend_label = commands[release_begin:release_end].replace("0x03000000u", "GCM_LABEL_GUEST_BASE")
    packet = re.search(r"typedef struct GcmFifoPacket \{.*?\} GcmFifoPacket;", gcm, re.S)
    packet_type = packet.group() if packet else (
        "typedef struct { u32 raw, header_ea, header_get, next_index, remaining; } GcmFifoPacket;"
    )
    perf_type = re.search(r"typedef struct TjFifoPerfCall \{.*?\} TjFifoPerfCall;", gcm, re.S).group()
    object_enum = re.search(
        r"typedef enum GcmFifoObjectClass \{.*?\} GcmFifoObjectClass;", gcm, re.S
    ).group()
    gcm2d_state = re.search(
        r"static struct \{\n\s*u32 src_dma;.*?\} s_gcm2d;", gcm, re.S
    ).group()
    for signature in (
        "static void gcm_rsx_process_fifo_profiled(void)",
        "void cellGcm_rsx_process_fifo(void)",
    ):
        if "rsx_d3d12_backend_flush_texture_labels();" not in function(gcm, signature):
            raise AssertionError(f"{signature} lost its end-of-drain label flush")
    fixture = r'''
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
typedef uint32_t u32; typedef uint64_t u64;
typedef uint8_t u8; typedef uint16_t u16; typedef int32_t s32;
typedef struct {
    u32 pending_indexed_valid, pending_indexed_primitive;
    u32 pending_indexed_first, pending_indexed_count;
    u32 depth_mask;
} rsx_state;
typedef struct {
    void* userdata;
    void (*draw_indexed)(void*,u32,u32,u32);
    int (*defer_texture_label)(void*,u32,u32);
} rsx_backend;
static const rsx_backend* s_backend;
#define GCM_CONTROL_GUEST_ADDR 0x20u
#define RSX_CONTEXT_DMA_MAIN 0xfeed0001u
#define RSX_CONTEXT_DMA_LOCAL 0xfeed0000u
#define FRAME_PROF_FIFO_DISPATCH 1
#define EA_BASE 0x100000u
#define GCM_LABEL_GUEST_BASE 0x700u
#define GCM_LABEL_STRIDE 0x10u
#define CELL_GCM_MAX_LABEL_COUNT 256u
OBJECT_ENUM
PACKET_TYPE
PERF_TYPE
enum { TJ_FIFO_EXIT_CAUGHT_UP, TJ_FIFO_EXIT_FLIP, TJ_FIFO_EXIT_IDLE,
       TJ_FIFO_EXIT_EA, TJ_FIFO_EXIT_BUDGET, TJ_FIFO_EXIT_NOT_INITIALIZED };
static GcmFifoPacket s_fifo_packet;
static u32 s_fifo_semaphore_offset, s_fifo_semaphore_dma;
static u64 s_fifo_semaphore_wait_since_ns;
static u32 memory[4096], published[4096], s_fifo_getoff, s_fifo_calloff;
static u32 s_gcm_context_ea=0x80, g_rsx_last_reference, g_gcm_fifo_drained_ea;
static GcmFifoObjectClass s_gcm_fifo_object_class[8];
static u32 missing_io;
static u32 s_gcm_fifo_object_handle[8];
static u8 s_gcm_fifo_object_trusted[8];
GCM2D_STATE
static int s_ref_pub_lock, mode, checks, failures, case_id;
static unsigned calls, unpublished_reads, prefix_calls, refs, interrupts, flips;
static unsigned draw_flushes;
static unsigned pulse_calls, pulse_partial;
static u32 pulse_get[8], pulse_published_get[8], pulse_refs[8], pulse_interrupts[8];
static unsigned context_resets;
static rsx_state* last_state;
static unsigned gpu_draws, prefix_draws_before;
static u32 gpu_first, gpu_count, gpu_depth_mask;
static int prefix_fails;
static int batch_enabled, batch_armed;
static unsigned batch_count;
static u32 batch_offsets[8], batch_values[8];
static u32 prefix_label_before, prefix_label_offset;
static u32 methods[64], values[64], callback_get, callback_value;
static u32 observer_label_ea, observer_label_value;
static unsigned m2mf_observer_calls, scaled_observer_calls, inline_observer_calls;
static unsigned observer_order_failures;
static int watch_guest_writes;
static TjFifoPerfCall perf;
static void tj_fifo_perf_group_add(u32 subch,u32 cls,u32 first,u32 last,u32 count,u64 ns)
{ (void)subch;(void)cls;(void)first;(void)last;(void)count;(void)ns; }
static void note_observer_order(void) {
    if (batch_count || memory[observer_label_ea/4] != observer_label_value)
        ++observer_order_failures;
}
static u32 vm_read32(u32 ea) {
    if (ea>=EA_BASE) {
        ea-=EA_BASE;
        if (!published[ea/4]) ++unpublished_reads;
    }
    return memory[ea/4];
}
static void vm_write32(u32 ea,u32 value) {
    if (watch_guest_writes && ea != observer_label_ea) {
        ++inline_observer_calls; note_observer_order();
    }
    memory[ea/4]=value;
}
static void vm_write16(u32 ea,u16 value) {
    if (watch_guest_writes && ea != observer_label_ea) {
        ++inline_observer_calls; note_observer_order();
    }
    memory[ea/4]=value;
}
u32 ppu_vm_size=0x10000u;
int cellGcmResolveLocatedChecked(int local,u32 offset,u32* out) {
    (void)local; *out=0x2000u+offset; return *out<ppu_vm_size;
}
static int gcm_m2mf_buffer_notify(u32 notify) {
    (void)notify; ++m2mf_observer_calls; note_observer_order(); return 0;
}
static int gcm_scaled_image_in(u32 point) {
    (void)point; ++scaled_observer_calls; note_observer_order(); return 0;
}
static u32 gcm_io2ea(u32 io) {
    return io>=0x100 && io<0x600 && io!=missing_io ? EA_BASE+io : 0;
}
static void record(u32 method,u32 data) {
    if (calls<64) { methods[calls]=method; values[calls]=data; }
    ++calls;
}
static void rsx_state_init(rsx_state* state) { memset(state,0,sizeof(*state)); }
static void rsx_flush_pending_draws_impl(rsx_state* state);
static int rsx_process_method(rsx_state* state,u32 method,u32 data) {
    last_state=state;
    if (method!=0x1824) rsx_flush_pending_draws_impl(state);
    record(method,data); if (method==0x50) g_rsx_last_reference=data;
    if (method==0xa70) state->depth_mask=data;
    if (method==0x1824) {
        state->pending_indexed_valid=1; state->pending_indexed_primitive=5;
        state->pending_indexed_first=data & 0xffffffu;
        state->pending_indexed_count=(data>>24)+1;
    }
BACKEND_LABEL
    return 0;
}
static int rsx_d3d12_backend_fifo_faulted(void) { return 0; }
static void rsx_d3d12_backend_pulse_fence_wait_service(void) {
    if (s_fifo_packet.remaining) ++pulse_partial;
    if (pulse_calls < 8) {
        pulse_get[pulse_calls] = s_fifo_getoff;
        pulse_published_get[pulse_calls] = memory[(GCM_CONTROL_GUEST_ADDR+4)/4];
        pulse_refs[pulse_calls] = refs;
        pulse_interrupts[pulse_calls] = interrupts;
    }
    ++pulse_calls;
}
static void rsx_d3d12_backend_trace_texture_read_prefix(void) {}
static int rsx_d3d12_backend_complete_fifo_prefix(void) {
    ++prefix_calls; prefix_label_before=vm_read32(GCM_LABEL_GUEST_BASE+prefix_label_offset);
    prefix_draws_before=gpu_draws;
    if (prefix_fails) return -1;
    for (unsigned i=0;i<batch_count;++i)
        vm_write32(GCM_LABEL_GUEST_BASE+batch_offsets[i],batch_values[i]);
    batch_count=0;
    return 0;
}
static int rsx_d3d12_backend_prepare_texture_label(void) {
    /* Completion attribution must keep these real decoder boundaries intact. */
    if (batch_enabled) { batch_armed=1; return 0; }
    return rsx_d3d12_backend_complete_fifo_prefix();
}
static int rsx_d3d12_backend_flush_texture_labels(void) {
    return batch_count ? rsx_d3d12_backend_complete_fifo_prefix() : 0;
}
typedef enum {
    RSX_COMPLETION_OTHER, RSX_COMPLETION_BACKEND_RELEASE, RSX_COMPLETION_ACQUIRE
} RsxD3D12CompletionReason;
static int last_flush_reason, last_prefix_reason;
static int rsx_d3d12_backend_flush_texture_labels_at(RsxD3D12CompletionReason reason) {
    last_flush_reason=reason; return rsx_d3d12_backend_flush_texture_labels();
}
static int rsx_d3d12_backend_complete_fifo_prefix_at(int reason) {
    last_prefix_reason=reason; return rsx_d3d12_backend_complete_fifo_prefix();
}
static int defer_label(void* unused,u32 offset,u32 value) {
    (void)unused;
    if(!batch_armed)return 0;
    batch_armed=0;batch_offsets[batch_count]=offset;batch_values[batch_count++]=value;
    return 1;
}
static void draw_indexed(void* unused,u32 primitive,u32 first,u32 count) {
    (void)unused; (void)primitive; ++gpu_draws; gpu_first=first; gpu_count=count;
    gpu_depth_mask=last_state->depth_mask;
}
static const rsx_backend backend={NULL,draw_indexed,defer_label};
FLUSH_INDEXED
FLUSH_DRAWS
static void rsx_flush_pending_draws(rsx_state* state) {
    ++draw_flushes; rsx_flush_pending_draws_impl(state);
}
static void rsx_state_reset(rsx_state* state) {
    ++context_resets; rsx_flush_pending_draws(state); state->depth_mask=1;
}
OBJECT_HELPERS
CONTEXT_RESET
#define cellGcmFifoSubchannelMethod cellGcmFifoSubchannelMethod_production
PRODUCTION_SUBCHANNEL
#undef cellGcmFifoSubchannelMethod
static int cellGcmFifoSubchannelMethod(u32 subch,u32 method,u32 data) {
    record(method,data);
    return cellGcmFifoSubchannelMethod_production(subch,method,data);
}
static void cellGcmQueueUserInterrupt(u32 data,u32 edge) {
    ++interrupts; callback_value=data; callback_get=vm_read32(GCM_CONTROL_GUEST_ADDR+4);
    if (callback_get!=edge) ++failures;
}
static void gcm_ref_push_at(u32 data,u32 edge) {
    ++refs; callback_value=data; callback_get=vm_read32(GCM_CONTROL_GUEST_ADDR+4);
    if (callback_get!=edge) ++failures;
}
static void cellGcmSetFlipCommand_source(u32 buffer,int source) { (void)buffer; (void)source; ++flips; }
static void gcm_ref_publish_one(void) {}
static void AcquireSRWLockExclusive(int* lock) { (void)lock; }
static void ReleaseSRWLockExclusive(int* lock) { (void)lock; }
static void tj_geom_fifo_trace_outcome(u32 a,u32 b,u32 c) { (void)a; (void)b; (void)c; }
static void tj_geom_fifo_trace_relevant_header(u32 a,u32 b,u32 c) { (void)a; (void)b; (void)c; }
static void tj_geom_fifo_trace_call(u32 a,u32 b,u32 c) { (void)a; (void)b; (void)c; }
static void tj_depth_pass_trace(rsx_state* s,u32 a,u32 b,u32 c,u32 d) { (void)s; (void)a; (void)b; (void)c; (void)d; }
unsigned long long ps3_ms_now(void) { return 0; }
static u64 get_timestamp_ns(void) { static u64 tick; return ++tick; }
METADATA
SEMAPHORE
FAST
PROFILED
#define CHECK(test,message) do { ++checks; if (!(test)) { ++failures; fprintf(stderr,"FAIL %s case%d: %s\n",mode?"profiled":"fast",case_id,message); } } while (0)
static void setup(int which) {
    case_id=which; memset(memory,0,sizeof(memory)); memset(published,0,sizeof(published));
    memset(&s_fifo_packet,0,sizeof(s_fifo_packet));
    gcm_fifo_objects_reset();
    s_fifo_getoff=0x100; s_fifo_calloff=missing_io=0;
    s_fifo_semaphore_offset=0; s_fifo_semaphore_dma=0x66606660u;
    s_fifo_semaphore_wait_since_ns=0;
    calls=unpublished_reads=prefix_calls=refs=interrupts=flips=0;
    pulse_calls=pulse_partial=0;
    draw_flushes=0; prefix_fails=0; prefix_label_before=0; prefix_label_offset=0x20;
    context_resets=0;
    batch_enabled=batch_armed=0;batch_count=0;
    last_flush_reason=last_prefix_reason=-1;
    gpu_draws=prefix_draws_before=gpu_first=gpu_count=0;
    callback_get=callback_value=g_rsx_last_reference=0;
    observer_label_ea=GCM_LABEL_GUEST_BASE+0x20; observer_label_value=0;
    m2mf_observer_calls=scaled_observer_calls=inline_observer_calls=0;
    observer_order_failures=0; watch_guest_writes=0;
    for (unsigned i=0;i<8;++i) s_gcm_fifo_object_class[i]=GCM_FIFO_OBJECT_NV4097;
}
static u32 header(u32 method,u32 count,u32 subch,int ni) {
    return method|(count<<18)|(subch<<13)|(ni?0x40000000u:0);
}
static void word(u32 io,u32 data) { memory[io/4]=data; }
static void publish(u32 begin,u32 end) { for (u32 io=begin;io<end;io+=4) published[io/4]=1; }
static void drain(u32 put) {
    memory[GCM_CONTROL_GUEST_ADDR/4]=put; memset(&perf,0,sizeof(perf));
    if (mode) gcm_rsx_process_fifo_unlocked_profiled(&perf);
    else gcm_rsx_process_fifo_unlocked_fast();
    CHECK(memory[(GCM_CONTROL_GUEST_ADDR+4)/4]==s_fifo_getoff,"GET publication disagrees with walker");
    CHECK(!pulse_partial,"deadline pulse ran inside a partial method packet");
}
static int dispatch_with_boundary(rsx_state* state,u32 subch,u32 method,u32 data) {
    int result=gcm_fifo_semaphore_method(state,subch,method,data);
    if(result!=0)return result;
    return cellGcmFifoSubchannelMethod(subch,method,data);
}
static void cases(void) {
    setup(1); word(0x100,header(0xa70,3,0,0));
    word(0x104,1); word(0x108,2); word(0x10c,3);
    publish(0x100,0x104); drain(0x104);
    CHECK(!calls && !unpublished_reads && s_fifo_getoff==0x104,"header-only PUT consumed unpublished payload");
    CHECK(pulse_calls==1 && pulse_get[0]==0x100,"initial packet boundary did not pulse once");
    if (mode) CHECK(perf.headers==1 && perf.words==1 && !perf.payloads,"header-only accounting wrong");
    publish(0x104,0x108); drain(0x108);
    CHECK(calls==1 && methods[0]==0xa70 && values[0]==1 && !unpublished_reads,"first payload lost, duplicated, or overread");
    if (mode) CHECK(!perf.headers && perf.words==1 && perf.payloads==1,"resume counted another header");
    /* A producer may reuse the already-consumed header. Resume must retain it. */
    word(0x100,0x20000100); publish(0x108,0x110); drain(0x110);
    CHECK(calls==3 && methods[1]==0xa74 && methods[2]==0xa78 && values[2]==3,"incrementing continuation lost method index/header");
    CHECK(pulse_calls==1,"resuming a split packet introduced an internal pulse");
    CHECK(s_fifo_getoff==0x110 && !unpublished_reads,"complete packet advanced beyond PUT");
    if (mode) CHECK(!perf.headers && perf.payloads==2 && perf.method_calls==2 &&
        perf.max_group_getoff==0x100 && perf.max_group_header_ea==EA_BASE+0x100 &&
        perf.max_group_first_method==0xa74 && perf.max_group_last_method==0xa78,"resume profiling lost original group or consumed range");

    setup(2); word(0x100,header(0x1800,3,0,1));
    word(0x104,0x20000104); word(0x108,0x00000202); word(0x10c,0x00020000);
    publish(0x100,0x108); drain(0x108);
    publish(0x108,0x10c); drain(0x10c);
    publish(0x10c,0x110); drain(0x110);
    CHECK(calls==3 && methods[0]==0x1800 && methods[1]==0x1800 && methods[2]==0x1800,"non-incrementing partial packet changed methods");
    CHECK(values[0]==0x20000104 && values[1]==0x202 && values[2]==0x20000 && !s_fifo_calloff,"payload control bits executed as control words");
    CHECK(!unpublished_reads && s_fifo_getoff==0x110,"NI packet read beyond PUT");

    setup(3); word(0x100,0x202); word(0x104,0); /* CALL and return-side NOP */
    word(0x200,header(0xa70,2,0,0)); word(0x204,7); word(0x208,8); word(0x20c,0x20000);
    publish(0x100,0x108); publish(0x200,0x208); drain(0x208);
    CHECK(calls==1 && s_fifo_getoff==0x208 && s_fifo_calloff==0x104,"partial CALL packet lost saved return");
    publish(0x208,0x210); drain(0x108);
    CHECK(calls==2 && values[1]==8 && s_fifo_getoff==0x108 && !s_fifo_calloff,"CALL resume/RET failed across non-monotonic offsets");
    CHECK(!unpublished_reads,"CALL packet consumed unpublished memory");

    setup(4); word(0x100,header(0xa70,3,0,0));
    word(0x104,9); word(0x108,10); word(0x10c,11); publish(0x100,0x110);
    missing_io=0x108; drain(0x110);
    CHECK(calls==1 && s_fifo_getoff==0x108,"mapping hole discarded remaining payload");
    if (mode) CHECK(perf.payload_ea_failures==1 && perf.exit_reason==TJ_FIFO_EXIT_EA,"payload mapping failure not reported");
    missing_io=0; drain(0x110);
    CHECK(calls==3 && values[1]==10 && values[2]==11 && methods[2]==0xa78,"remapped payload failed to resume exactly once");

    setup(5); word(0x100,header(0x50,2,0,0)); word(0x104,0x1234); word(0x108,0x5678);
    publish(0x100,0x108); drain(0x108);
    CHECK(calls==1 && refs==1 && prefix_calls==1 && callback_get==0x108 && callback_value==0x1234,"published reference blocked or edge/order changed");
    publish(0x108,0x10c); drain(0x10c);
    CHECK(calls==2 && methods[1]==0x54 && refs==1 && prefix_calls==1,"reference repeated during resume");

    setup(6); word(0x100,header(0xb00,2,7,0)); word(0x104,0x77); word(0x108,0x88);
    publish(0x100,0x108); drain(0x108);
    CHECK(interrupts==1 && !calls && prefix_calls==1 && callback_get==0x108 && callback_value==0x77,"USER_CMD did not complete/publish before callback");
    publish(0x108,0x10c); drain(0x10c);
    CHECK(interrupts==1 && calls==1 && methods[0]==0xb04 && values[0]==0x88,"USER_CMD continuation duplicated interrupt");

    setup(7); word(0x100,0x20000200); word(0x200,0); word(0x204,header(0,1,2,0));
    word(0x208,0x1234); word(0x20c,header(0x308,1,2,0)); word(0x210,0x55);
    word(0x214,0xfead0001); word(0x218,header(0xa70,1,0,0)); word(0x21c,1);
    publish(0x100,0x104); publish(0x200,0x220); drain(0x220);
    CHECK(flips==1 && calls==1 && methods[0]==0x308 &&
          s_gcm_fifo_object_class[2]==GCM_FIFO_OBJECT_NV4097 &&
          s_gcm_fifo_object_handle[2]==0x1234 && s_fifo_getoff==0x218,
          "JUMP/NOP/unknown binding/subchannel/flip behavior changed");
    drain(0x220);
    CHECK(calls==2 && methods[1]==0xa70 && values[1]==1 && s_fifo_getoff==0x220,"drain after flip failed");

    setup(8); word(0x100,header(0x64,2,0,0)); word(0x104,0x20); word(0x108,0x12345678);
    word(0x10c,header(0xa70,1,0,0)); word(0x110,1); word(0x114,0xfead0001); word(0x118,0);
    vm_write32(GCM_LABEL_GUEST_BASE+0x20,0x55); publish(0x100,0x11c); drain(0x11c);
    CHECK(s_fifo_getoff==0x108 && s_fifo_packet.remaining==1 && s_fifo_packet.next_index==1,
          "unsatisfied acquire consumed its payload or lost packet position");
    CHECK(!calls && !flips && vm_read32(GCM_LABEL_GUEST_BASE+0x20)==0x55,
          "acquire ran later work or wrote its own expected label");
    CHECK(prefix_calls==1 && draw_flushes>=1,"acquire did not submit preceding pending draws");
    if (mode) CHECK(perf.payloads==1 && perf.words==2 && perf.exit_reason==TJ_FIFO_EXIT_IDLE,
                    "parked acquire counted as consumed work");
    drain(0x11c);
    CHECK(s_fifo_getoff==0x108 && !calls && !flips,"repeated acquire skipped unresolved wait");
    if (mode) CHECK(!perf.payloads && !perf.words,"acquire retry counted another payload");
    word(0x100,0x20000100); /* Consumed header reuse must not change the parked method. */
    vm_write32(GCM_LABEL_GUEST_BASE+0x20,0x12345678); drain(0x11c);
    CHECK(calls==1 && methods[0]==0xa70 && values[0]==1 && flips==1 && s_fifo_getoff==0x118,
          "real producer release did not resume at acquire then depth/flip exactly once");
    drain(0x11c); CHECK(s_fifo_getoff==0x11c && calls==1 && flips==1,"post-acquire tail repeated work");

    setup(9); word(0x100,header(0x60,4,0,0)); word(0x104,0x66616661); word(0x108,0x20);
    word(0x10c,0x01020304); word(0x110,0xA1B2C3D4);
    word(0x114,header(0xa70,1,0,0)); word(0x118,1);
    vm_write32(GCM_LABEL_GUEST_BASE+0x20,0x01020304); publish(0x100,0x11c); drain(0x11c);
    CHECK(vm_read32(GCM_LABEL_GUEST_BASE+0x20)==0xA1B2C3D4 && prefix_calls==1 &&
          prefix_label_before==0x01020304 && draw_flushes>=1,"frontend release missing, byte-swapped, or published before completion");
    CHECK(calls==1 && methods[0]==0xa70 && s_fifo_getoff==0x11c,"semaphore methods leaked into graphics dispatcher");

    setup(10); word(0x100,header(0x60,4,0,0)); word(0x104,0x66606660); word(0x108,0x20);
    word(0x10c,7); word(0x110,9); vm_write32(GCM_LABEL_GUEST_BASE+0x20,7);
    publish(0x100,0x114); prefix_fails=1; drain(0x114);
    CHECK(s_fifo_getoff==0x110 && s_fifo_packet.remaining==1 && vm_read32(GCM_LABEL_GUEST_BASE+0x20)==7,
          "failed GPU completion published release or consumed its payload");
    prefix_fails=0; drain(0x114);
    CHECK(s_fifo_getoff==0x114 && vm_read32(GCM_LABEL_GUEST_BASE+0x20)==9 && prefix_calls==2,
          "release retry failed to complete and publish exactly once");

    setup(11); word(0x100,0x202); word(0x104,0);
    word(0x200,header(0x64,2,0,0)); word(0x204,0x20); word(0x208,5); word(0x20c,0x20000);
    publish(0x100,0x108); publish(0x200,0x210); drain(0x108);
    CHECK(s_fifo_getoff==0x208 && s_fifo_calloff==0x104 && !calls,"parked CALL acquire lost saved return");
    vm_write32(GCM_LABEL_GUEST_BASE+0x20,5); drain(0x108);
    CHECK(s_fifo_getoff==0x108 && !s_fifo_calloff && !unpublished_reads,"CALL acquire/RET failed after producer release");

    for (unsigned bad=0;bad<3;++bad) {
        setup(12+(int)bad); word(0x100,header(0x60,4,0,0));
        word(0x104,bad==2 ? 0x56616660 : 0x66606660);
        word(0x108,bad==0 ? 3 : (bad==1 ? 0x1000 : 0x20));
        word(0x10c,0); word(0x110,99); publish(0x100,0x114); drain(0x114);
        CHECK(s_fifo_getoff==0x10c && s_fifo_packet.remaining==2 && !calls && !prefix_calls,
              "invalid label offset or device DMA context consumed acquire/release");
        CHECK(vm_read32(GCM_LABEL_GUEST_BASE+0x20)==0,"invalid semaphore changed a valid label");
    }

    setup(15); word(0x100,header(0x64,1,0,0)); word(0x104,0xffc);
    word(0x108,header(0x6c,1,0,0)); word(0x10c,0x1234); publish(0x100,0x110); drain(0x110);
    CHECK(vm_read32(GCM_LABEL_GUEST_BASE+0xffc)==0x1234 && s_fifo_getoff==0x110,
          "last aligned word inside label window rejected");

    setup(16); word(0x100,header(0x64,1,0,0)); word(0x104,0x10);
    word(0x108,header(0x6c,1,0,0)); word(0x10c,3); publish(0x100,0x110); drain(0x110);
    CHECK(vm_read32(GCM_LABEL_GUEST_BASE+0x10)==3 && !prefix_calls,
          "reserved flip label incorrectly required whole GPU prefix completion");

    setup(17); word(0x100,header(0x64,2,2,0)); word(0x104,0x20); word(0x108,5);
    publish(0x100,0x10c); drain(0x10c);
    CHECK(calls==2 && methods[0]==0x64 && methods[1]==0x68 && s_fifo_getoff==0x10c,
          "frontend semaphore handling swallowed another subchannel's methods");

    setup(18); word(0x100,header(0x1d6c,1,0,0)); word(0x104,0x20);
    word(0x108,header(0x1824,1,0,0)); word(0x10c,0x03000010);
    word(0x110,header(0x1d70,1,0,0)); word(0x114,0xA133C311);
    word(0x118,header(0xa70,1,0,0)); word(0x11c,1);
    vm_write32(GCM_LABEL_GUEST_BASE+0x20,7); publish(0x100,0x120); drain(0x120);
    CHECK(prefix_calls==1 && prefix_draws_before==1 && gpu_draws==1 && gpu_first==16 && gpu_count==4,
          "backend release did not complete its actual deferred indexed span first");
    CHECK(prefix_label_before==7 && vm_read32(GCM_LABEL_GUEST_BASE+0x20)==0xA111C333 &&
          calls==4 && methods[3]==0xa70 && s_fifo_getoff==0x120,
          "backend release published early, changed byte swap, or lost following state");

    setup(19); word(0x100,header(0x1d6c,1,0,0)); word(0x104,0x20);
    word(0x108,header(0x1824,1,0,0)); word(0x10c,0x03000010);
    word(0x110,header(0x1d70,1,0,0)); word(0x114,0x00010000);
    vm_write32(GCM_LABEL_GUEST_BASE+0x20,7); publish(0x100,0x118); prefix_fails=1; drain(0x118);
    CHECK(s_fifo_getoff==0x114 && s_fifo_packet.remaining==1 && calls==2 &&
          vm_read32(GCM_LABEL_GUEST_BASE+0x20)==7 && gpu_draws==1,
          "failed backend fence published label, consumed payload, or lost pending draw");
    prefix_fails=0; drain(0x118);
    CHECK(s_fifo_getoff==0x118 && calls==3 && prefix_calls==2 && gpu_draws==1 &&
          vm_read32(GCM_LABEL_GUEST_BASE+0x20)==1,
          "backend fence retry duplicated draw or failed exact payload resume");

    setup(20); s_gcm_fifo_object_class[2]=GCM_FIFO_OBJECT_UNKNOWN;
    word(0x100,header(0x1d70,1,2,0)); word(0x104,0x55); publish(0x100,0x108); drain(0x108);
    CHECK(calls==1 && methods[0]==0x1d70 && !prefix_calls,
          "backend fence handling intercepted another object class");

    setup(21); word(0x100,header(0x1824,1,0,0)); word(0x104,0x03000010);
    word(0x108,header(0xb00,1,7,0)); word(0x10c,0x77); publish(0x100,0x110); drain(0x110);
    CHECK(interrupts==1 && prefix_calls==1 && prefix_draws_before==1 && gpu_draws==1 && callback_get==0x110,
          "USER_CMD callback ran before its actual deferred indexed span completed");

    setup(22); word(0x100,header(0x1824,1,0,0)); word(0x104,0x03000010);
    word(0x108,header(0xb00,1,7,0)); word(0x10c,0x77); publish(0x100,0x110);
    prefix_fails=1; drain(0x110);
    CHECK(!interrupts && s_fifo_getoff==0x10c && s_fifo_packet.remaining==1 && gpu_draws==1,
          "failed USER_CMD completion published callback or lost its unconsumed payload/draw");
    if (mode) CHECK(perf.payloads==1 && perf.exit_reason==TJ_FIFO_EXIT_IDLE,
                    "failed USER_CMD completion counted consumed payload");
    prefix_fails=0; drain(0x110);
    CHECK(interrupts==1 && prefix_calls==2 && gpu_draws==1 && s_fifo_getoff==0x110,
          "USER_CMD retry duplicated draw/callback or failed to resume");

    setup(23); word(0x100,header(0x50,1,0,0)); word(0x104,0x1234); publish(0x100,0x108);
    prefix_fails=1; drain(0x108);
    CHECK(!refs && s_fifo_getoff==0x104 && s_fifo_packet.remaining==1,
          "failed reference completion published fence or consumed payload");
    if (mode) CHECK(!perf.payloads && perf.exit_reason==TJ_FIFO_EXIT_IDLE,
                    "failed reference completion counted consumed payload");
    prefix_fails=0; drain(0x108);
    CHECK(refs==1 && prefix_calls==2 && s_fifo_getoff==0x108 && callback_get==0x108,
          "reference retry failed to publish its exact completion edge");

    setup(24); word(0x100,header(0xa70,1,0,0)); word(0x104,0);
    word(0x108,header(0x1824,1,0,0)); word(0x10c,0x03000010);
    word(0x110,0xfead0001); word(0x114,header(0xa74,1,0,0)); word(0x118,1);
    s_fifo_semaphore_offset=0x40; s_fifo_semaphore_dma=0x66606660u;
    vm_write32(GCM_LABEL_GUEST_BASE+0x10,0x55);
    publish(0x100,0x11c); drain(0x11c);
    CHECK(flips==1 && context_resets==1 && s_fifo_getoff==0x114 && calls==2,
          "flip must reset once and stop before next-frame methods");
    CHECK(gpu_draws==1 && gpu_depth_mask==0 && gpu_first==16 && gpu_count==4,
          "flip must submit last indexed span with pre-reset state");
    CHECK(last_state->depth_mask==1 && s_fifo_semaphore_offset==0x10 &&
          s_fifo_semaphore_dma==0x66616661u && vm_read32(GCM_LABEL_GUEST_BASE+0x10)==0x55,
          "flip reset must restore registers without writing guest labels");
    drain(0x11c);
    CHECK(calls==3 && methods[2]==0xa74 && last_state->depth_mask==1 && context_resets==1 && gpu_draws==1,
          "next frame must inherit reset state without repeated draw/reset");

    /* Original image17 tail replay emits 00041D6C 00000800 00041D74 03707800.
     * Unlike BACK_END_WRITE, TEXTURE_READ releases must not swap bytes 0/2. */
    setup(25); prefix_label_offset=0x800;
    word(0x100,header(0x1824,1,0,0)); word(0x104,0x03000010);
    word(0x108,0x00041d6c); word(0x10c,0x800);
    word(0x110,0x00041d74); word(0x114,0x03707800);
    word(0x118,header(0xa70,1,0,0)); word(0x11c,1);
    vm_write32(GCM_LABEL_GUEST_BASE+0x800,0x03b00000);
    publish(0x100,0x120); drain(0x120);
    CHECK(prefix_calls==1 && prefix_draws_before==1 && gpu_draws==1 && gpu_count==4,
          "texture release did not complete its preceding indexed span");
    CHECK(prefix_label_before==0x03b00000 && vm_read32(GCM_LABEL_GUEST_BASE+0x800)==0x03707800 &&
          calls==4 && methods[3]==0xa70 && s_fifo_getoff==0x120,
          "original geometry ring release missing, early, byte-swapped, or lost following state");

    setup(26); word(0x100,header(0x1d6c,1,0,0)); word(0x104,0x20);
    word(0x108,header(0x1824,1,0,0)); word(0x10c,0x03000010);
    word(0x110,header(0x1d74,1,0,0)); word(0x114,0xa133c311);
    vm_write32(GCM_LABEL_GUEST_BASE+0x20,7);
    publish(0x100,0x114); drain(0x114);
    CHECK(s_fifo_getoff==0x114 && s_fifo_packet.remaining==1 && !prefix_calls &&
          vm_read32(GCM_LABEL_GUEST_BASE+0x20)==7 && !unpublished_reads,
          "partial texture release consumed an unpublished payload");
    publish(0x114,0x118); prefix_fails=1; drain(0x118);
    CHECK(s_fifo_getoff==0x114 && s_fifo_packet.remaining==1 && calls==2 &&
          gpu_draws==1 && vm_read32(GCM_LABEL_GUEST_BASE+0x20)==7,
          "failed texture fence published label or consumed payload");
    prefix_fails=0; drain(0x118);
    CHECK(s_fifo_getoff==0x118 && calls==3 && prefix_calls==2 && gpu_draws==1 &&
          vm_read32(GCM_LABEL_GUEST_BASE+0x20)==0xa133c311,
          "texture fence retry duplicated draw or failed exact unswapped publication");

    setup(27); s_gcm_fifo_object_class[2]=GCM_FIFO_OBJECT_UNKNOWN;
    word(0x100,header(0x1d74,1,2,0)); word(0x104,0x55); publish(0x100,0x108); drain(0x108);
    CHECK(calls==1 && methods[0]==0x1d74 && !prefix_calls,
          "texture fence intercepted another object class");

    setup(28); word(0x100,header(0x1d6c,1,0,0)); word(0x104,0x800);
    word(0x108,header(0x1d70,1,0,0)); word(0x10c,0xa133c311);
    word(0x110,header(0x1d6c,1,0,0)); word(0x114,0x810);
    word(0x118,header(0x1d74,1,0,0)); word(0x11c,0xa133c311);
    publish(0x100,0x120); drain(0x120);
    CHECK(prefix_calls==2 && vm_read32(GCM_LABEL_GUEST_BASE+0x800)==0xa111c333 &&
          vm_read32(GCM_LABEL_GUEST_BASE+0x810)==0xa133c311,
          "backend/texture release byte order or worker label isolation changed");
}
static int queue_pending_label(rsx_state* state,u32 old_value,u32 value) {
    vm_write32(GCM_LABEL_GUEST_BASE+0x20,old_value);
    rsx_process_method(state,0x1D6C,0x20);
    int result=gcm_fifo_semaphore_method(state,0,0x1D74,value);
    if(result==0)rsx_process_method(state,0x1D74,value);
    observer_label_ea=GCM_LABEL_GUEST_BASE+0x20; observer_label_value=value;
    return result;
}
static void metadata_dispatch_cases(void) {
    static const struct {u32 handle,method;} setters[]={
        {0x31337303u,0x0180},{0x31337303u,0x0184},{0x31337303u,0x0188},
        {0x31337303u,0x030C},{0x31337303u,0x0310},{0x31337303u,0x0314},
        {0x31337303u,0x0318},{0x31337303u,0x031C},{0x31337303u,0x0320},
        {0x31337303u,0x0324},
        {0x3137AF00u,0x0184},{0x3137AF00u,0x0198},{0x3137AF00u,0x02FC},
        {0x3137AF00u,0x0300},{0x3137AF00u,0x0304},{0x3137AF00u,0x0308},
        {0x3137AF00u,0x030C},{0x3137AF00u,0x0310},{0x3137AF00u,0x0314},
        {0x3137AF00u,0x0318},{0x3137AF00u,0x031C},{0x3137AF00u,0x0400},
        {0x3137AF00u,0x0404},{0x3137AF00u,0x0408},
        {0x313371C3u,0x0184},{0x313371C3u,0x0188},{0x313371C3u,0x0300},
        {0x313371C3u,0x0304},{0x313371C3u,0x030C},
        {0x31337A73u,0x0180},{0x31337A73u,0x0184},{0x31337A73u,0x0300},
        {0x31337A73u,0x0304},
        {0x31337808u,0x0304},{0x31337808u,0x0308},{0x31337808u,0x030C},
    };
    for(unsigned i=0;i<sizeof(setters)/sizeof(setters[0]);++i){
        setup(200+(int)i); batch_enabled=1; rsx_state state={0};
        CHECK(queue_pending_label(&state,7,8)==0,"could not queue label for setter case");
        CHECK(dispatch_with_boundary(&state,1,0,setters[i].handle)==0&&
              s_gcm_fifo_object_handle[1]==setters[i].handle&&batch_count==1&&prefix_calls==0,
              "known object bind forced a completion");
        CHECK(dispatch_with_boundary(&state,1,setters[i].method,0x12345678u)==0&&
              batch_count==1&&prefix_calls==0&&vm_read32(observer_label_ea)==7,
              "whitelisted production setter retired a queued label");
    }

    setup(240); batch_enabled=1; {rsx_state state={0};
        CHECK(queue_pending_label(&state,7,8)==0,"M2MF label setup failed");
        CHECK(dispatch_with_boundary(&state,1,0,0x31337303u)==0&&
              dispatch_with_boundary(&state,1,0x0184,0x1234u)==0&&batch_count==1,
              "M2MF setup setter failed to cross batch");
        CHECK(dispatch_with_boundary(&state,1,0x0328,0)==0&&m2mf_observer_calls==1&&
              prefix_calls==1&&observer_order_failures==0&&vm_read32(observer_label_ea)==8,
              "M2MF observer ran before checked label publication");
    }

    setup(241); batch_enabled=1; {rsx_state state={0};
        CHECK(queue_pending_label(&state,8,9)==0,"scaled-image label setup failed");
        CHECK(dispatch_with_boundary(&state,1,0,0x3137AF00u)==0&&
              dispatch_with_boundary(&state,1,0x0400,0x11223344u)==0&&batch_count==1,
              "scaled-image setup setter failed to cross batch");
        CHECK(dispatch_with_boundary(&state,1,0x040C,0)==0&&scaled_observer_calls==1&&
              prefix_calls==1&&observer_order_failures==0&&vm_read32(observer_label_ea)==9,
              "scaled-image observer ran before checked label publication");
    }

    setup(242); batch_enabled=1; {rsx_state state={0};
        CHECK(queue_pending_label(&state,9,10)==0,"inline-write label setup failed");
        CHECK(dispatch_with_boundary(&state,1,0,0x31337808u)==0&&
              dispatch_with_boundary(&state,1,0x0304,0)==0&&
              dispatch_with_boundary(&state,1,0x0308,1)==0&&batch_count==1,
              "inline-write setup setters failed to cross batch");
        s_gcm2d.dst_dma=RSX_CONTEXT_DMA_MAIN; s_gcm2d.dst_offset=0;
        s_gcm2d.pitch=256u<<16; s_gcm2d.color_fmt=4;
        watch_guest_writes=1;
        CHECK(dispatch_with_boundary(&state,1,0x0400,0x00FF0000u)==0&&
              inline_observer_calls==1&&prefix_calls==1&&observer_order_failures==0&&
              vm_read32(observer_label_ea)==10,
              "inline NV308A write preceded checked label publication");
        watch_guest_writes=0;
    }

    setup(243); batch_enabled=1; {rsx_state state={0};
        CHECK(queue_pending_label(&state,11,12)==0,"unknown-method label setup failed");
        CHECK(dispatch_with_boundary(&state,1,0,0x31337303u)==0&&batch_count==1,
              "known bind failed before unknown method test");
        unsigned before=calls;
        CHECK(dispatch_with_boundary(&state,1,0x0330,0)==0&&prefix_calls==1&&
              calls==before+1&&vm_read32(observer_label_ea)==12,
              "unknown method did not retire before dispatch");
    }

    setup(244); batch_enabled=1; {rsx_state state={0};
        CHECK(queue_pending_label(&state,13,14)==0,"failed-observer label setup failed");
        CHECK(dispatch_with_boundary(&state,1,0,0x31337303u)==0,"observer bind failed");
        prefix_fails=1; unsigned before=calls;
        CHECK(dispatch_with_boundary(&state,1,0x0328,0)<0&&calls==before&&
              !m2mf_observer_calls&&batch_count==1&&vm_read32(observer_label_ea)==13,
              "failed checked completion consumed observer or published label");
        prefix_fails=0;
        CHECK(dispatch_with_boundary(&state,1,0x0328,0)==0&&m2mf_observer_calls==1&&
              batch_count==0&&vm_read32(observer_label_ea)==14&&observer_order_failures==0,
              "observer retry did not resume after checked publication");
    }

    setup(245); batch_enabled=1; {rsx_state state={0};
        CHECK(queue_pending_label(&state,15,16)==0,"drain label setup failed");
        drain(0x100);
        CHECK(batch_count==1&&vm_read32(observer_label_ea)==15,
              "empty unlocked drain consumed a pending label unexpectedly");
        CHECK(rsx_d3d12_backend_flush_texture_labels()==0&&batch_count==0&&
              vm_read32(observer_label_ea)==16,
              "end-of-drain checked publication failed");
    }

    setup(246); batch_enabled=1; {rsx_state state={0};
        CHECK(dispatch_with_boundary(&state,1,0,0x31337303u)==0,
              "walker observer bind failed");
        CHECK(queue_pending_label(&state,17,18)==0,
              "walker observer label setup failed");
        unsigned before=calls; prefix_fails=1;
        word(0x100,header(0x0328,1,1,0)); word(0x104,0);
        publish(0x100,0x108); drain(0x108);
        CHECK(s_fifo_getoff==0x104&&s_fifo_packet.remaining==1&&calls==before&&
              !m2mf_observer_calls&&batch_count==1&&vm_read32(observer_label_ea)==17,
              "failed observer fence consumed FIFO payload or published its label");
        prefix_fails=0; drain(0x108);
        CHECK(s_fifo_getoff==0x108&&!s_fifo_packet.remaining&&calls==before+1&&
              m2mf_observer_calls==1&&batch_count==0&&vm_read32(observer_label_ea)==18&&
              observer_order_failures==0,
              "observer FIFO retry did not resume after checked label publication");
    }

    for (unsigned i=0;i<2;++i) {
        const u32 unknown_handle=i?0x1234u:0u;
        setup(247+(int)i); batch_enabled=1; rsx_state state={0};
        CHECK(dispatch_with_boundary(&state,1,0,0x31337303u)==0&&
              dispatch_with_boundary(&state,1,0,unknown_handle)==0&&
              s_gcm_fifo_object_class[1]==GCM_FIFO_OBJECT_NV0039&&
              s_gcm_fifo_object_handle[1]==unknown_handle&&!s_gcm_fifo_object_trusted[1],
              "unknown bind did not preserve compatibility class while clearing trust");
        CHECK(queue_pending_label(&state,20+i,30+i)==0&&batch_count==1,
              "unknown-bind setter label setup failed");
        CHECK(dispatch_with_boundary(&state,1,0x0184,0x12345678u)==0&&
              !batch_count&&prefix_calls==1&&vm_read32(observer_label_ea)==30+i,
              "whitelisted setter crossed a batch after unknown bind");
    }

    setup(249); batch_enabled=1; {rsx_state state={0};
        CHECK(dispatch_with_boundary(&state,1,0,0)==0&&!s_gcm_fifo_object_trusted[1],
              "zero bind did not clear object trust");
        CHECK(queue_pending_label(&state,40,41)==0&&batch_count==1,
              "known-rebind label setup failed");
        CHECK(dispatch_with_boundary(&state,1,0,0x31337303u)==0&&
              s_gcm_fifo_object_trusted[1]&&batch_count==1&&prefix_calls==0,
              "known rebind did not restore trust across an eligible batch");
        CHECK(dispatch_with_boundary(&state,1,0x0184,0x12345678u)==0&&
              batch_count==1&&prefix_calls==0&&vm_read32(observer_label_ea)==40,
              "known rebind failed to restore metadata crossing");
        CHECK(rsx_d3d12_backend_flush_texture_labels()==0&&batch_count==0&&
              vm_read32(observer_label_ea)==41,"known-rebind batch failed final checked publication");
    }

    setup(250);
    for(unsigned i=0;i<8;++i)
        CHECK(s_gcm_fifo_object_trusted[i],"object lifecycle reset must trust default bindings");
}
static void pulse_boundary_cases(void) {
    setup(300);
    word(0x100,header(0xa70,3,0,0));
    word(0x104,1); word(0x108,2); word(0x10c,3);
    word(0x110,header(0xa7c,1,0,0)); word(0x114,4);
    publish(0x100,0x118); drain(0x118);
    CHECK(pulse_calls==2 && pulse_get[0]==0x100 && pulse_get[1]==0x110 &&
          calls==4 && values[3]==4 && !unpublished_reads && !prefix_calls,
          "complete-packet pulses changed dispatch or inserted a prefix flush");

    setup(301);
    word(0x100,header(0x50,1,0,0)); word(0x104,17);
    word(0x108,header(0xb00,1,7,0)); word(0x10c,99);
    word(0x110,header(0xa70,1,0,0)); word(0x114,4);
    publish(0x100,0x118); drain(0x118);
    CHECK(pulse_calls==3 && pulse_get[1]==0x108 && pulse_get[2]==0x110 &&
          pulse_published_get[1]==0x108 && pulse_refs[1]==1 && !pulse_interrupts[1] &&
          pulse_published_get[2]==0x110 && pulse_refs[2]==1 && pulse_interrupts[2]==1 &&
          refs==1 && interrupts==1 && prefix_calls==2,
          "pulse overtook checked completion, edge GET, reference, or user interrupt");
}
int main(void) {
    s_backend=&backend;
    for (mode=0;mode<2;++mode) {
        int before_checks=checks, before_failures=failures; cases();
        pulse_boundary_cases();
        metadata_dispatch_cases();
        for(int edge=0;edge<6;++edge){
            setup(100+edge);batch_enabled=1;
            rsx_state state={0};
            vm_write32(GCM_LABEL_GUEST_BASE+0x20,7);
            rsx_process_method(&state,0x1D6C,0x20);
            CHECK(gcm_fifo_semaphore_method(&state,1,0x1D74,8)==0,"deferred prepare failed");
            rsx_process_method(&state,0x1D74,8);
            CHECK(batch_count==1&&prefix_calls==0&&vm_read32(GCM_LABEL_GUEST_BASE+0x20)==7,
                  "real label dispatch bypassed deferred callback");
            s_fifo_semaphore_offset=0x20;
            if(edge==0){
                CHECK(gcm_fifo_semaphore_method(&state,0,0x68,7)==2&&prefix_calls==2,
                      "acquire matched stale value before deferred release");
            }else if(edge==1){
                prefix_fails=1;
                s_gcm_fifo_object_class[0]=GCM_FIFO_OBJECT_UNKNOWN; /* early generic boundary */
                CHECK(gcm_fifo_semaphore_method(&state,0,0x68,7)<0&&batch_count==1&&
                      vm_read32(GCM_LABEL_GUEST_BASE+0x20)==7,"failed boundary published or crossed label");
            }else if(edge==2){
                s_gcm_fifo_object_class[2]=GCM_FIFO_OBJECT_NV3089;
                observer_label_value=8;
                CHECK(gcm_fifo_semaphore_method(&state,2,0x040C,0)==0&&prefix_calls==1&&
                      vm_read32(observer_label_ea)==8,"scaled-image observer crossed pending label");
                CHECK(cellGcmFifoSubchannelMethod(2,0x040C,0)==0&&
                      scaled_observer_calls==1&&observer_order_failures==0,
                      "scaled-image observer ran before label publication");
            }else if(edge==3){
                CHECK(gcm_fifo_semaphore_method(&state,1,0,0x1234)==0&&prefix_calls==1,
                      "unknown object rebind crossed pending labels");
                CHECK(cellGcmFifoSubchannelMethod(1,0,0x1234)==0&&
                      s_gcm_fifo_object_class[1]==GCM_FIFO_OBJECT_NV4097&&
                      s_gcm_fifo_object_handle[1]==0x1234,
                      "unknown rebind did not preserve the compatibility class");
            }else if(edge==4){
                CHECK(gcm_fifo_semaphore_method(&state,1,0x1D70,0x10000)==0&&
                      vm_read32(GCM_LABEL_GUEST_BASE+0x20)==8,"backend label overtook texture label");
                rsx_process_method(&state,0x1D70,0x10000);
                CHECK(vm_read32(GCM_LABEL_GUEST_BASE+0x20)==1,"backend byte order changed in batch mode");
            }else{
                s_gcm_fifo_object_class[0]=GCM_FIFO_OBJECT_UNKNOWN;
                CHECK(gcm_fifo_semaphore_method(&state,0,0x6c,9)==1&&
                      prefix_calls==2&&vm_read32(GCM_LABEL_GUEST_BASE+0x20)==9,
                      "frontend release crossed deferred label");
            }
            if(edge==4)
                CHECK(last_prefix_reason==RSX_COMPLETION_BACKEND_RELEASE,
                      "NV4097 backend release attribution lost");
            else
                CHECK(last_flush_reason==(edge<2?RSX_COMPLETION_ACQUIRE:
                      edge==5?RSX_COMPLETION_BACKEND_RELEASE:RSX_COMPLETION_OTHER),
                      "deferred completion attributed to wrong boundary");
            if(edge!=1)CHECK(!batch_count,"boundary stranded deferred label");
        }
        printf("%s: %d checks, %d failures\n",mode?"profiled":"fast",checks-before_checks,failures-before_failures);
    }
    return failures ? 1 : 0;
}
'''
    replacements = {
        "PACKET_TYPE": packet_type,
        "PERF_TYPE": perf_type,
        "OBJECT_ENUM": object_enum,
        "GCM2D_STATE": gcm2d_state,
        "OBJECT_HELPERS": "\n".join([
            function(gcm, "static GcmFifoObjectClass gcm_fifo_object_class_from_handle("),
            function(gcm, "static void gcm_fifo_bind_object("),
            function(gcm, "static void gcm_fifo_objects_reset(void)\n{"),
        ]),
        "BACKEND_LABEL": backend_label,
        "PRODUCTION_SUBCHANNEL": function(gcm, "int cellGcmFifoSubchannelMethod("),
        "METADATA": function(gcm, "static int gcm_fifo_method_is_metadata(")
            if "static int gcm_fifo_method_is_metadata(" in gcm else "",
        "CONTEXT_RESET": function(gcm, "static void gcm_fifo_context_reset(")
            if "static void gcm_fifo_context_reset(" in gcm else "",
        "FLUSH_INDEXED": function(commands, "static void rsx_flush_pending_indexed("),
        "FLUSH_DRAWS": function(commands, "void rsx_flush_pending_draws(").replace(
            "rsx_flush_pending_draws(", "rsx_flush_pending_draws_impl(", 1),
        "SEMAPHORE": function(gcm, "static int gcm_fifo_semaphore_method(")
            if "static int gcm_fifo_semaphore_method(" in gcm else "",
        "FAST": function(gcm, "static void gcm_rsx_process_fifo_unlocked_fast(void)\n{"),
        "PROFILED": function(gcm, "static void gcm_rsx_process_fifo_unlocked_profiled(TjFifoPerfCall* perf_call)\n{"),
    }
    for marker, value in replacements.items():
        fixture = fixture.replace("\n" + marker + "\n", "\n" + value + "\n")
    compiler = shutil.which("clang") or shutil.which("gcc") or r"C:\Program Files\LLVM\bin\clang.exe"
    with tempfile.TemporaryDirectory(prefix="tj-fifo-partial-") as directory:
        source = Path(directory) / "partial.c"
        executable = Path(directory) / "partial.exe"
        source.write_text(fixture, encoding="utf-8")
        subprocess.run([compiler, "-std=c11", "-D_CRT_SECURE_NO_WARNINGS",
                        str(source), "-o", str(executable)], check=True)
        return subprocess.run([str(executable)], timeout=10).returncode


if __name__ == "__main__":
    raise SystemExit(main())
