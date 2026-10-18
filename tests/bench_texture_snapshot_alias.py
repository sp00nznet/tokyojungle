"""Bounded native cost probe for same-address hints versus cross-address aliases."""
from pathlib import Path
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile

from bench_texture_snapshot_hash import snapshot_hint_helpers

ROOT = Path(__file__).resolve().parents[1]
VIDEO = ROOT.parent / "ps3recomp/libs/video"
BACKEND = VIDEO / "rsx_d3d12_backend.c"
HASH_IMPL = VIDEO / "rsx_stream_hash.c"
SIZES = (16 * 1024, 256 * 1024, 1024 * 1024)
HINT_COLLISION_START_OPS = {16 * 1024: 16000, 256 * 1024: 960, 1024 * 1024: 160}
HINT_COLLISION_MAX_OPS = {16 * 1024: 131072, 256 * 1024: 131072, 1024 * 1024: 131072}
START_OPS = {16 * 1024: 250, 256 * 1024: 30, 1024 * 1024: 10}
START_OPS_SAME = {16 * 1024: 64000, 256 * 1024: 32000, 1024 * 1024: 16000}
MAX_OPS = {
    "same": {16 * 1024: 128000, 256 * 1024: 64000, 1024 * 1024: 32000},
    "alias-off": {16 * 1024: 16000, 256 * 1024: 1000, 1024 * 1024: 250},
    "alias-on": {16 * 1024: 128000, 256 * 1024: 64000, 1024 * 1024: 32000},
}
MIN_SAMPLE_MS = 10.0


def function(source, signature):
    start = source.find(signature)
    while start >= 0:
        brace = source.find("{", start)
        semi = source.find(";", start)
        if brace >= 0 and (semi < 0 or brace < semi):
            depth, end = 1, brace + 1
            while depth:
                depth += (source[end] == "{") - (source[end] == "}")
                end += 1
            return source[start:end]
        start = source.find(signature, start + len(signature))
    raise ValueError(f"definition not found: {signature}")


def make_source(backend):
    structure = re.search(r"typedef struct VPTexSnapshot \{.*?\} VPTexSnapshot;", backend, re.S)
    churn = re.search(r"typedef struct VPTexChurnCounters \{.*?\} VPTexChurnCounters;", backend, re.S)
    assert structure and churn
    pool_start = backend.index("typedef enum VPTexSnapshotPoolEvictReason {")
    pool_end = backend.index("static void vp_texture_churn_record_new_snapshot(", pool_start)
    pool_helpers = backend[pool_start:pool_end]
    capture = function(backend, "static int vp_capture_tex_snapshot_impl(")
    retarget = function(backend, "static int vp_texture_snapshot_hint_retarget(")
    trace_enabled = function(backend, "static int vp_texture_churn_trace_enabled(")
    record_new = function(backend, "static void vp_texture_churn_record_new_snapshot(")
    record_reject = function(backend, "static void vp_texture_churn_record_budget_reject(")
    record_trim = function(backend, "static void vp_texture_churn_record_trim(")
    hash_function = function(backend, "static u64 vp_tex_source_hash(")
    hash_marker = "    int perf = tj_perf_trace_enabled();"
    assert hash_function.count(hash_marker) == 1
    hash_function = hash_function.replace(
        hash_marker,
        "    ++bench_hash_calls; bench_hash_bytes += (u64)row_bytes * rows;\n" + hash_marker,
    )
    hash_function = hash_function.replace(
        "{\n",
        "{\n    if (force_hash_collision) { ++bench_hash_calls; bench_hash_bytes += (u64)row_bytes * rows; return 0xA11A5u; }\n",
        1,
    )
    copy_call = "memcpy(snapshot->data, source, bytes);"
    assert capture.count(copy_call) == 1
    capture = capture.replace(
        copy_call,
        "++bench_copy_calls; bench_copy_bytes += bytes; memcpy(snapshot->data, source, bytes);",
    )
    capture = capture.replace("(VPTexSnapshot*)calloc(1, sizeof(*snapshot))", "bench_snapshot_struct_alloc()")
    capture = capture.replace("(u8*)malloc(bytes)", "bench_snapshot_data_alloc(bytes)")
    capture = capture.replace("free(snapshot);", "bench_snapshot_struct_free(snapshot);")
    discard = function(backend, "static void vp_texture_snapshot_discard(")
    discard = discard.replace("free(snapshot->data);", "bench_snapshot_data_free(snapshot->data);")
    discard = discard.replace("free(snapshot);", "bench_snapshot_struct_free(snapshot);")
    byte_equal = function(backend, "static int vp_snapshot_bytes_equal(")
    byte_equal = byte_equal.replace(")\n{", ")\n{\n    ++bench_equal_calls; bench_equal_bytes += bytes;", 1)

    source = r'''
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <windows.h>
typedef uint8_t u8; typedef uint32_t u32; typedef uint64_t u64;
static u64 bench_hash_calls,bench_hash_bytes,bench_equal_calls,bench_equal_bytes;
static u64 bench_copy_calls,bench_copy_bytes,bench_get_write_watch_calls;
static u64 bench_struct_allocs,bench_data_allocs,bench_struct_frees,bench_data_frees;
static int force_hash_collision;
static UINT WINAPI bench_get_write_watch(DWORD flags,PVOID base,SIZE_T size,
    PVOID* addresses,ULONG_PTR* count,LPDWORD granularity)
{ ++bench_get_write_watch_calls; return GetWriteWatch(flags,base,size,addresses,count,granularity); }
#define GetWriteWatch bench_get_write_watch
#include "rsx_texture_write_watch.h"
#undef GetWriteWatch
#include "rsx_bytes_equal.h"
#include "rsx_stream_hash.h"
#define VP_TEX_SNAPSHOT_BUCKETS 1024u
#define VP_TEX_SNAPSHOT_HINTS 256u
#define VP_TEX_SNAPSHOT_HINT_WAYS 4u
#define VP_TEX_SNAPSHOT_MAX_BYTES (128ull*1024ull*1024ull)
#define VP_TEX_SNAPSHOT_SINGLE_MAX_BYTES (32ull*1024ull*1024ull)
#define VP_TEX_SNAPSHOT_RETAIN_MAX_BYTES (64ull*1024ull*1024ull)
#define VP_TEX_SNAPSHOT_RETAIN_MAX_ENTRIES 256u
#define VP_TEX_SNAPSHOT_HASH_REUSE_MIN_BYTES (16u*1024u)
#define VP_TEX_SLOTS 96
STRUCTURE
typedef struct { VPTexSnapshot* source_snapshot; int used; } VPTexSlot;
static struct {
    u64 vp_tex_snapshot_bytes;
    VPTexSnapshot* vp_tex_snapshot_buckets[VP_TEX_SNAPSHOT_BUCKETS];
    VPTexSnapshot* vp_tex_snapshot_hints[VP_TEX_SNAPSHOT_HINTS];
    VPTexSnapshot* vp_tex_snapshot_pool[VP_TEX_SNAPSHOT_RETAIN_MAX_ENTRIES];
    u32 vp_tex_snapshot_pool_head,vp_tex_snapshot_pool_count;
    u64 vp_tex_snapshot_pool_bytes;
    int vp_tex_snapshot_pool_enabled;
    u32 vp_tex_snapshot_pool_suppress;
    VPTexSlot vp_tex[VP_TEX_SLOTS];
} s_d3d;
CHURN_STRUCT
static VPTexChurnCounters s_vp_texture_churn;
static int s_vp_texture_churn_enabled=1;
static int s_vp_texture_hint_retarget_config;
typedef struct {u64 tex_hash_ticks,tex_hash_calls,tex_hash_bytes;} PerfPhase;
static PerfPhase s_tj_perf_phase;
static u8* memory; u8* vm_base; u32 ppu_vm_size;
static RsxTextureWriteWatch s_vp_write_watch;
static u64 s_vp_watch_hits,s_vp_watch_bytes,s_vp_watch_mismatches;
static int tj_perf_trace_enabled(void){return 0;}
static LONGLONG tj_perf_now(void){return 0;}
static void* bench_snapshot_struct_alloc(void)
{void*p=calloc(1,sizeof(VPTexSnapshot));if(p)++bench_struct_allocs;return p;}
static u8* bench_snapshot_data_alloc(u32 bytes)
{u8*p=(u8*)malloc(bytes);if(p)++bench_data_allocs;return p;}
static void bench_snapshot_struct_free(void*p){if(p)++bench_struct_frees;free(p);}
static void bench_snapshot_data_free(void*p){if(p)++bench_data_frees;free(p);}
static int d3d12_prefix_owner_texture_reserved(u32 slot){(void)slot;return 0;}
static int vp_tex_source_span(u32 w,u32 h,u32 d,u32 fmt,u32 pitch,int cube,
    u32 dimension,u32 mips,int border,u32* bytes,u64* face_stride)
{(void)fmt;(void)pitch;(void)cube;(void)dimension;(void)mips;(void)border;(void)face_stride;
 if(!bytes||!w||!h||!d)return -1;*bytes=w*h*d;return 0;}
static int tj_gcm2d_backend_perf_enabled(void){return 0;}
static u64 tj_gcm2d_backend_perf_now(void){return 0;}
static u64 tj_gcm2d_backend_perf_us(u64 ticks){return ticks;}
static void vp_texture_snapshot_release(VPTexSnapshot** owner);
static void vp_texture_cache_trim_sources(u32 bytes);
static void vp_texture_snapshot_pool_evict_oldest(u32 reason);
static int vp_texture_snapshot_pool_admit(VPTexSnapshot* snapshot);
static void vp_texture_snapshot_pool_reclaim_for_budget(u32 bytes);
static void vp_texture_snapshot_pool_drain(u32 reason);
HASH
HINT
RETARGET_HELPER
TRACE_ENABLED
POOL
RECORD_NEW
RECORD_REJECT
RECORD_TRIM
static int vp_texture_write_watch_mode(void);
static u64 vp_texture_write_stamp(const u8* base,u64 span,u32 off,u32 bytes,
                                  RsxTextureWriteStampHint* hint);
BYTE_EQUAL
DISCARD
RELEASE
SET_SOURCE
TRIM
WATCH_MODE
STAMP
CAPTURE
static VPTexSnapshot* capture(u32 off,u32 bytes)
{VPTexSnapshot*out=NULL;if(vp_capture_tex_snapshot_impl(off,bytes,1,1,0,bytes,0,2,1,0,&out)||!out){
 fprintf(stderr,"capture failed off=%u bytes=%u\n",off,bytes);exit(3);}return out;}
static void fill(u8* dst,u32 bytes)
{for(u32 i=0;i<bytes;++i)dst[i]=(u8)(i*37u+(i>>5)+0x39u);}
static double elapsed_ms(LARGE_INTEGER begin,LARGE_INTEGER end,LARGE_INTEGER freq)
{return (double)(end.QuadPart-begin.QuadPart)*1000.0/(double)freq.QuadPart;}
static void reset_probe_counts(void)
{
    bench_hash_calls=bench_hash_bytes=bench_equal_calls=bench_equal_bytes=0;
    bench_copy_calls=bench_copy_bytes=bench_get_write_watch_calls=0;
    bench_struct_allocs=bench_data_allocs=bench_struct_frees=bench_data_frees=0;
    InterlockedExchange64(&s_vp_texture_churn.new_snapshot_count,0);
    InterlockedExchange64(&s_vp_texture_churn.new_snapshot_bytes,0);
    InterlockedExchange64(&s_vp_texture_churn.snapshot_pool_hint_reuses,0);
    InterlockedExchange64(&s_vp_texture_churn.snapshot_pool_hash_reuses,0);
    InterlockedExchange64(&s_vp_texture_churn.snapshot_pool_hash_same_off_reuses,0);
    InterlockedExchange64(&s_vp_texture_churn.snapshot_pool_hash_other_off_reuses,0);
    InterlockedExchange64(&s_vp_texture_churn.snapshot_pool_hint_retargets,0);
    s_vp_watch_hits=s_vp_watch_bytes=s_vp_watch_mismatches=0;
}
static int setup_memory(u32 bytes,u32 aoff,u32 boff)
{
    const u32 vm_size=4u*1024u*1024u;
    memory=(u8*)VirtualAlloc(NULL,vm_size,MEM_RESERVE|MEM_COMMIT|MEM_WRITE_WATCH,PAGE_READWRITE);
    if(!memory){fprintf(stderr,"MEM_WRITE_WATCH allocation failed\n");return 0;}
    vm_base=memory;ppu_vm_size=vm_size;
    fill(memory+aoff,bytes);memcpy(memory+boff,memory+aoff,bytes);
    if(ResetWriteWatch(memory,vm_size)!=ERROR_SUCCESS){fprintf(stderr,"ResetWriteWatch failed\n");return 0;}
    vp_texture_snapshot_pool_init();
    if(!s_d3d.vp_tex_snapshot_pool_enabled){fprintf(stderr,"retention pool did not enable\n");return 0;}
    return 1;
}
static int cleanup_memory(void)
{
    vp_texture_snapshot_pool_drain(VP_TEX_SNAPSHOT_POOL_EVICT_SHUTDOWN);
    const int clean=!s_d3d.vp_tex_snapshot_pool_count &&
        !s_d3d.vp_tex_snapshot_pool_bytes && !s_d3d.vp_tex_snapshot_bytes;
    int indexes_clean=1;
    for(u32 i=0;i<VP_TEX_SNAPSHOT_HINTS;++i)
        if(s_d3d.vp_tex_snapshot_hints[i])indexes_clean=0;
    for(u32 i=0;i<VP_TEX_SNAPSHOT_BUCKETS;++i)
        if(s_d3d.vp_tex_snapshot_buckets[i])indexes_clean=0;
    rsx_texture_write_watch_destroy(&s_vp_write_watch);
    if(memory)VirtualFree(memory,0,MEM_RELEASE);
    return clean && indexes_clean;
}
static int verify_alias(u32 bytes,int watch_mode,int absent_stamp)
{
    const u32 aoff=65536u,boff=2u*1024u*1024u;
    if(!setup_memory(bytes,aoff,boff))return 10;
    const int retarget=vp_texture_snapshot_hint_retarget_enabled();
    const u32 ahint=vp_texture_snapshot_hint_index(aoff,bytes);
    const u32 bhint=vp_texture_snapshot_hint_index(boff,bytes);
    if(ahint==bhint){fprintf(stderr,"test offsets collide in hint index\n");return 11;}
    VPTexSnapshot* warm=capture(aoff,bytes);
    vp_texture_snapshot_release(&warm);
    if(s_d3d.vp_tex_snapshot_pool_count!=1)return 12;
    VPTexSnapshot* owner=s_d3d.vp_tex_snapshot_pool[s_d3d.vp_tex_snapshot_pool_head];
    if(!owner || owner->refs!=1 || s_d3d.vp_tex_snapshot_hints[ahint]!=owner)return 13;
    if(absent_stamp)rsx_texture_write_watch_fail(&s_vp_write_watch,ERROR_INVALID_DATA);
    reset_probe_counts();
    for(u32 i=0;i<3;++i){
        VPTexSnapshot* hit=capture(boff,bytes);
        if(hit!=owner){fprintf(stderr,"alias lookup returned a different snapshot\n");return 14;}
        vp_texture_snapshot_release(&hit);
        if(owner->refs!=1){fprintf(stderr,"pool owner reference count drifted\n");return 14;}
        if(i==0){
            const int old_cleared=s_d3d.vp_tex_snapshot_hints[ahint]!=owner;
            const int new_points=s_d3d.vp_tex_snapshot_hints[bhint]==owner;
            if(old_cleared!=retarget || new_points!=retarget ||
               owner->source_off!=(retarget?boff:aoff)){
                fprintf(stderr,"hint transition mismatch old_clear=%d new=%d source=%u\n",
                        old_cleared,new_points,owner->source_off);return 15;
            }
        }
    }
    const u64 expected_hash=retarget?1u:3u;
    const u64 expected_equal=(absent_stamp || !retarget || watch_mode==2)?3u:1u;
    const u64 expected_watch=absent_stamp?0u:3u;
    if(bench_hash_calls!=expected_hash || bench_hash_bytes!=expected_hash*bytes ||
       bench_equal_calls!=expected_equal || bench_equal_bytes!=expected_equal*bytes ||
       bench_get_write_watch_calls!=expected_watch || bench_copy_calls ||
       s_vp_texture_churn.snapshot_pool_hash_reuses!=expected_hash ||
       s_vp_texture_churn.snapshot_pool_hash_other_off_reuses!=expected_hash ||
       s_vp_texture_churn.snapshot_pool_hint_reuses!=(retarget?2u:0u) ||
       s_vp_texture_churn.snapshot_pool_hint_retargets!=(retarget?1u:0u) ||
       (watch_mode==2 && !absent_stamp &&
        (s_vp_write_watch.state!=1 || s_vp_watch_mismatches!=0)) ||
       (absent_stamp && s_vp_write_watch.state>=0)){
        fprintf(stderr,"alias verify hash=%llu equal=%llu watch=%llu poolhash=%llu hints=%llu retargets=%llu state=%d mismatch=%llu\n",
          (unsigned long long)bench_hash_calls,(unsigned long long)bench_equal_calls,
          (unsigned long long)bench_get_write_watch_calls,
          (unsigned long long)s_vp_texture_churn.snapshot_pool_hash_reuses,
          (unsigned long long)s_vp_texture_churn.snapshot_pool_hint_reuses,
          (unsigned long long)s_vp_texture_churn.snapshot_pool_hint_retargets,
          s_vp_write_watch.state,(unsigned long long)s_vp_watch_mismatches);return 16;
    }
    const u8 old_byte=owner->data[bytes/2u];
    memory[boff+bytes/2u]^=0x80u;
    reset_probe_counts();
    VPTexSnapshot* changed=capture(boff,bytes);
    if(changed==owner || changed->data[bytes/2u]!=memory[boff+bytes/2u] ||
       owner->data[bytes/2u]!=old_byte || owner->data[bytes/2u]==memory[boff+bytes/2u] ||
       bench_hash_calls!=((watch_mode==2 || absent_stamp)?2u:1u) || bench_copy_calls!=1 ||
       bench_copy_bytes!=bytes || s_vp_texture_churn.new_snapshot_count!=1 ||
       s_vp_texture_churn.new_snapshot_bytes!=bytes ||
       (watch_mode==2 && (s_vp_write_watch.state!=1 || s_vp_watch_mismatches!=0))){
        fprintf(stderr,"changed alias mismatch hash=%llu eq=%llu copy=%llu copy_bytes=%llu new=%llu new_bytes=%llu old=%u guest=%u snap=%u\n",
          (unsigned long long)bench_hash_calls,(unsigned long long)bench_equal_calls,
          (unsigned long long)bench_copy_calls,(unsigned long long)bench_copy_bytes,
          (unsigned long long)s_vp_texture_churn.new_snapshot_count,
          (unsigned long long)s_vp_texture_churn.new_snapshot_bytes,
          old_byte,memory[boff+bytes/2u],changed?changed->data[bytes/2u]:0);return 17;
    }
    vp_texture_snapshot_release(&changed);
    if(!cleanup_memory()){fprintf(stderr,"pool drain left retained bytes\n");return 18;}
    printf("{\"scenario\":\"verify-alias\",\"watch_mode\":%d,\"retarget\":%s,\"absent_stamp\":%s,\"result\":\"pass\"}\n",
           watch_mode,retarget?"true":"false",absent_stamp?"true":"false");
    return 0;
}
static int verify_collision(u32 bytes)
{
    const u32 aoff=65536u,boff=2u*1024u*1024u;
    if(!setup_memory(bytes,aoff,boff))return 20;
    if(vp_texture_snapshot_hint_index(aoff,bytes)==vp_texture_snapshot_hint_index(boff,bytes)){
        fprintf(stderr,"full-hash collision case also collides in the hint bucket\n");return 19;
    }
    force_hash_collision=1;
    memory[boff+bytes/2u]^=0x40u;
    VPTexSnapshot* old=capture(aoff,bytes);vp_texture_snapshot_release(&old);
    VPTexSnapshot* pooled=s_d3d.vp_tex_snapshot_pool[s_d3d.vp_tex_snapshot_pool_head];
    reset_probe_counts();
    VPTexSnapshot* different=capture(boff,bytes);
    const u32 ahint=vp_texture_snapshot_hint_index(aoff,bytes);
    const u32 bhint=vp_texture_snapshot_hint_index(boff,bytes);
    if(different==pooled || different->hash!=pooled->hash || different->data[bytes/2u]==pooled->data[bytes/2u] ||
       pooled->source_off!=aoff || s_d3d.vp_tex_snapshot_hints[ahint]!=pooled ||
       s_d3d.vp_tex_snapshot_hints[bhint]!=different || bench_hash_calls!=1 ||
       bench_equal_calls!=1 || bench_copy_calls!=1 || bench_copy_bytes!=bytes ||
       s_vp_texture_churn.new_snapshot_count!=1 || s_vp_texture_churn.new_snapshot_bytes!=bytes){
        fprintf(stderr,"forced full-hash collision was not rejected by byte comparison\n");return 21;
    }
    vp_texture_snapshot_release(&different);
    if(!cleanup_memory()){fprintf(stderr,"collision cleanup left pooled bytes\n");return 22;}
    puts("{\"scenario\":\"forced-full-hash-collision\",\"result\":\"pass\"}");
    return 0;
}
#ifdef TEST_LINKED_HINTS
static int hint_bucket_check_fail(int code,const char* message)
{fprintf(stderr,"hint bucket check %d failed: %s\n",code,message);return code;}
static u32 hint_bucket_count(u32 hint)
{
    u32 count=0;
    for(VPTexSnapshot* node=s_d3d.vp_tex_snapshot_hints[hint];node&&count<16u;
        node=node->hint_next)++count;
    return count;
}
static u32 hint_key_count(u32 hint,u32 off,u32 bytes)
{
    u32 count=0;
    for(VPTexSnapshot* node=s_d3d.vp_tex_snapshot_hints[hint];node&&count<16u;
        node=node->hint_next)
        if(node->source_off==off&&node->bytes==bytes)++count;
    return count;
}
static int find_hint_collision_offsets(u32 bytes,u32 needed,u32* offsets)
{
    const u32 limit=4u*1024u*1024u;
    offsets[0]=65536u;
    const u32 target=vp_texture_snapshot_hint_index(offsets[0],bytes);
    u32 found=1;
    for(u32 off=offsets[0]+bytes;(u64)off+bytes<=limit;off+=bytes){
        if(vp_texture_snapshot_hint_index(off,bytes)==target){
            offsets[found++]=off;
            if(found==needed)return 1;
        }
    }
    return 0;
}
static void fill_salted(u32 off,u32 bytes,u32 salt)
{for(u32 i=0;i<bytes;++i)memory[off+i]=(u8)(i*37u+(i>>5)+0x39u+salt*73u);}
static int verify_hint_bucket(void)
{
    const u32 bytes=64u;u32 offsets[5];
    if(!find_hint_collision_offsets(bytes,5u,offsets))return hint_bucket_check_fail(50,"five same-index nonoverlapping sources were not found");
    if(!setup_memory(bytes,offsets[0],offsets[1]))return 51;
    const u32 hint=vp_texture_snapshot_hint_index(offsets[0],bytes);
    for(u32 i=0;i<5u;++i){
        if(vp_texture_snapshot_hint_index(offsets[i],bytes)!=hint)return hint_bucket_check_fail(52,"fixture offsets do not share an index");
        fill_salted(offsets[i],bytes,i+1u);
    }
    if(ResetWriteWatch(memory,4u*1024u*1024u)!=ERROR_SUCCESS)return 53;
    VPTexSnapshot* owners[5]={0};
    for(u32 i=0;i<4u;++i){
        owners[i]=capture(offsets[i],bytes);
        if(!owners[i]||owners[i]->source_off!=offsets[i]||owners[i]->bytes!=bytes)
            return hint_bucket_check_fail(54,"source key changed during warm capture");
        for(u32 j=0;j<i;++j)
            if(owners[j]==owners[i]||owners[j]->hash==owners[i]->hash||
               !memcmp(owners[j]->data,owners[i]->data,bytes))
                return hint_bucket_check_fail(55,"distinct contents collapsed to one snapshot");
        vp_texture_snapshot_release(&owners[i]);
        if(!owners[i]){
            /* Keep the immutable pool identity after dropping the temporary owner. */
            owners[i]=s_d3d.vp_tex_snapshot_hints[hint];
            while(owners[i]&&owners[i]->source_off!=offsets[i])owners[i]=owners[i]->hint_next;
        }
    }
    if(s_d3d.vp_tex_snapshot_pool_count!=4u||hint_bucket_count(hint)!=4u)
        return hint_bucket_check_fail(56,"four warmed keys did not fit one bucket");
    if(s_d3d.vp_tex_snapshot_hints[hint]!=owners[3]||owners[3]->hint_next!=owners[2]||
       owners[2]->hint_next!=owners[1]||owners[1]->hint_next!=owners[0]||
       owners[0]->hint_next||vp_texture_snapshot_hint_find(offsets[2],bytes)!=owners[2])
        return hint_bucket_check_fail(57,"four-way insertion or full-key lookup order is wrong");

    reset_probe_counts();
    const int watch_mode=vp_texture_write_watch_mode();
    for(u32 i=0;i<4u;++i){
        const u32 off=(i&1u)?offsets[1]:offsets[0];
        VPTexSnapshot* hit=capture(off,bytes);
        if(hit!=(i&1u?owners[1]:owners[0]))return hint_bucket_check_fail(58,"alternating colliding keys returned the wrong snapshot");
        vp_texture_snapshot_release(&hit);
    }
    if(bench_hash_calls||bench_copy_calls||bench_get_write_watch_calls!=(watch_mode?4u:0u)||
       bench_equal_calls!=(watch_mode==1?0u:4u)||
       s_vp_texture_churn.snapshot_pool_hint_reuses!=4u||
       s_vp_texture_churn.snapshot_pool_hash_reuses||s_vp_watch_mismatches)
        return hint_bucket_check_fail(59,"alternating keys did not use stamp-guarded exact hints");

    if(!vp_texture_snapshot_hint_remove(owners[3])||s_d3d.vp_tex_snapshot_hints[hint]!=owners[2]||
       owners[3]->hint_next)return hint_bucket_check_fail(60,"head removal left a stale link");
    if(!vp_texture_snapshot_hint_upsert(owners[3])||s_d3d.vp_tex_snapshot_hints[hint]!=owners[3]||
       owners[3]->hint_next!=owners[2])return hint_bucket_check_fail(61,"head reinsertion failed");
    if(!vp_texture_snapshot_hint_remove(owners[2])||owners[3]->hint_next!=owners[1]||
       owners[2]->hint_next)return hint_bucket_check_fail(62,"middle removal left a stale link");
    if(!vp_texture_snapshot_hint_upsert(owners[2])||s_d3d.vp_tex_snapshot_hints[hint]!=owners[2]||
       hint_bucket_count(hint)!=4u)return hint_bucket_check_fail(63,"middle reinsertion duplicated or lost a key");
    if(!vp_texture_snapshot_hint_remove(owners[0])||hint_key_count(hint,offsets[0],bytes)||
       owners[0]->hint_next)return hint_bucket_check_fail(64,"tail removal left a stale link");
    if(!vp_texture_snapshot_hint_upsert(owners[0])||hint_bucket_count(hint)!=4u)
        return hint_bucket_check_fail(65,"tail reinsertion failed");

    VPTexSnapshot* tail=s_d3d.vp_tex_snapshot_hints[hint];
    while(tail->hint_next)tail=tail->hint_next;
    VPTexSnapshot* evicted=tail;
    VPTexSnapshot* fifth=capture(offsets[4],bytes);
    if(fifth->source_off!=offsets[4])return hint_bucket_check_fail(66,"fifth key source identity changed");
    owners[4]=fifth;vp_texture_snapshot_release(&fifth);
    if(hint_bucket_count(hint)!=4u||evicted->hint_next||
       vp_texture_snapshot_hint_find(evicted->source_off,bytes)||
       hint_key_count(hint,offsets[4],bytes)!=1u||s_d3d.vp_tex_snapshot_pool_count!=5u)
        return hint_bucket_check_fail(67,"fifth key did not evict exactly the tail hint");

    reset_probe_counts();
    VPTexSnapshot* evicted_hit=capture(evicted->source_off,bytes);
    if(evicted_hit!=evicted||bench_hash_calls!=1u||bench_equal_calls!=1u||
       bench_get_write_watch_calls!=(watch_mode?1u:0u)||bench_copy_calls||
       s_vp_texture_churn.snapshot_pool_hash_reuses!=1u)
        return hint_bucket_check_fail(68,"evicted hint did not fall back to exact hash/content reuse");
    vp_texture_snapshot_release(&evicted_hit);

    const u32 mutate_off=offsets[4],mutate_at=bytes/2u;
    const u8 original=memory[mutate_off+mutate_at];
    memory[mutate_off+mutate_at]^=0x80u;reset_probe_counts();
    VPTexSnapshot* changed=capture(mutate_off,bytes);
    VPTexSnapshot* changed_owner=changed;
    if(changed==owners[4]||changed->data[mutate_at]!=memory[mutate_off+mutate_at]||
       owners[4]->data[mutate_at]!=original||vp_texture_snapshot_hint_find(mutate_off,bytes)!=changed||
       hint_key_count(hint,mutate_off,bytes)!=1u||bench_copy_calls!=1u)
        return hint_bucket_check_fail(69,"mutation reused stale immutable content or duplicated its source key");
    vp_texture_snapshot_release(&changed);
    memory[mutate_off+mutate_at]=original;reset_probe_counts();
    VPTexSnapshot* restored=capture(mutate_off,bytes);
    if(restored!=owners[4]||restored->data[mutate_at]!=original||
       changed_owner->data[mutate_at]==original||changed_owner->hint_next||
       vp_texture_snapshot_hint_find(mutate_off,bytes)!=owners[4]||
       hint_key_count(hint,mutate_off,bytes)!=1u)
        return hint_bucket_check_fail(70,"content reversion did not replace the same-key hint safely");
    vp_texture_snapshot_release(&restored);

    rsx_texture_write_watch_fail(&s_vp_write_watch,ERROR_INVALID_DATA);
    reset_probe_counts();
    VPTexSnapshot* failed_stamp=capture(mutate_off,bytes);
    if(failed_stamp!=owners[4]||bench_hash_calls||bench_equal_calls!=1u||
       bench_get_write_watch_calls||bench_copy_calls||s_vp_write_watch.state>=0)
        return hint_bucket_check_fail(71,"zero failed stamp skipped exact byte verification");
    vp_texture_snapshot_release(&failed_stamp);

    if(!vp_texture_snapshot_hint_remove(owners[0])||owners[0]->hint_next)
        return hint_bucket_check_fail(72,"oldest snapshot could not be detached before pool eviction");
    vp_texture_snapshot_pool_evict_oldest(VP_TEX_SNAPSHOT_POOL_EVICT_CAP);
    if(s_d3d.vp_tex_snapshot_pool_count!=5u||
       vp_texture_snapshot_hint_find(offsets[0],bytes)||hint_bucket_count(hint)>4u)
        return hint_bucket_check_fail(73,"evicting a previously unlinked owner corrupted the bucket");
    const u64 new_before=s_vp_texture_churn.new_snapshot_count;
    VPTexSnapshot* fresh=capture(offsets[0],bytes);
    if(s_vp_texture_churn.new_snapshot_count!=new_before+1u||
       memcmp(fresh->data,memory+offsets[0],bytes))
        return hint_bucket_check_fail(74,"evicted source did not recapture without stale lookup");
    vp_texture_snapshot_release(&fresh);
    if(!cleanup_memory())return hint_bucket_check_fail(75,"pool drain left a linked hint node");
    puts("{\"scenario\":\"four-way-hint-bucket-lifecycle\",\"result\":\"pass\"}");
    return 0;
}
#endif
static int find_hint_collision_pair(u32 bytes,u32* aoff,u32* boff)
{
    const u32 limit=4u*1024u*1024u;
    *aoff=65536u;
    *boff=(*aoff+bytes+65536u+4095u)&~4095u;
    const u32 target=vp_texture_snapshot_hint_index(*aoff,bytes);
    while((u64)*boff+bytes<=limit&&vp_texture_snapshot_hint_index(*boff,bytes)!=target)
        *boff+=4096u;
    return (u64)*boff+bytes<=limit;
}
static int run_hint_collision(u32 bytes,u32 ops)
{
    u32 aoff=0,boff=0;
    if((ops&1u)||!find_hint_collision_pair(bytes,&aoff,&boff))return 80;
    if(!setup_memory(bytes,aoff,boff))return 81;
    memory[boff+bytes/2u]^=0x80u;
    if(ResetWriteWatch(memory,4u*1024u*1024u)!=ERROR_SUCCESS)return 82;
    VPTexSnapshot* a=capture(aoff,bytes);VPTexSnapshot* a_owner=a;vp_texture_snapshot_release(&a);
    VPTexSnapshot* b=capture(boff,bytes);VPTexSnapshot* b_owner=b;vp_texture_snapshot_release(&b);
    const u32 hint=vp_texture_snapshot_hint_index(aoff,bytes);
    if(hint!=vp_texture_snapshot_hint_index(boff,bytes)||a_owner==b_owner||
       !memcmp(a_owner->data,b_owner->data,bytes)||s_d3d.vp_tex_snapshot_pool_count!=2u||
       s_d3d.vp_tex_snapshot_hints[hint]!=b_owner)return 83;
    reset_probe_counts();LARGE_INTEGER freq,start,end;
    QueryPerformanceFrequency(&freq);QueryPerformanceCounter(&start);
    for(u32 i=0;i<ops;++i){
        const int use_a=(i&1u)==0;VPTexSnapshot* hit=capture(use_a?aoff:boff,bytes);
        if(hit!=(use_a?a_owner:b_owner))return 84;
        vp_texture_snapshot_release(&hit);
    }
    QueryPerformanceCounter(&end);const double ms=elapsed_ms(start,end,freq);
    printf("{\"scenario\":\"same-index-distinct-content-alternating-AB\",\"bytes\":%u,\"ops\":%u,\"aoff\":%u,\"boff\":%u,\"hint_index\":%u,\"elapsed_ms\":%.4f,\"hash_calls\":%llu,\"hash_bytes\":%llu,\"equal_calls\":%llu,\"equal_bytes\":%llu,\"write_watch_calls\":%llu,\"copy_calls\":%llu,\"hint_reuses\":%llu,\"pool_hash_reuses\":%llu}\n",
      bytes,ops,aoff,boff,hint,ms,(unsigned long long)bench_hash_calls,
      (unsigned long long)bench_hash_bytes,(unsigned long long)bench_equal_calls,
      (unsigned long long)bench_equal_bytes,(unsigned long long)bench_get_write_watch_calls,
      (unsigned long long)bench_copy_calls,(unsigned long long)s_vp_texture_churn.snapshot_pool_hint_reuses,
      (unsigned long long)s_vp_texture_churn.snapshot_pool_hash_reuses);
    vp_texture_snapshot_release(&a_owner);vp_texture_snapshot_release(&b_owner);
    return cleanup_memory()?0:85;
}
int main(int argc,char**argv)
{
    if(argc!=4){fprintf(stderr,"usage: alias-bench <size> <same|alias-off|alias-on|verify|verify-absent|collision|hint-bucket-check|hint-collision|gate> <ops>\n");return 2;}
    const u32 bytes=(u32)strtoul(argv[1],NULL,10),ops=(u32)strtoul(argv[3],NULL,10);
    const u32 aoff=65536u,boff=2u*1024u*1024u;
    if(!strcmp(argv[2],"gate")){
        vp_texture_snapshot_pool_init();
        if(vp_texture_snapshot_hint_retarget_enabled()){
            fprintf(stderr,"retarget gate ignored pool-off state\n");return 30;
        }
        puts("{\"scenario\":\"pool-off-retarget-gate\",\"result\":\"pass\"}");return 0;
    }
    if(!strcmp(argv[2],"verify")){
        const char* mode=getenv("TJ_TEXTURE_WRITE_WATCH");
        return verify_alias(bytes,mode?atoi(mode):1,0);
    }
    if(!strcmp(argv[2],"verify-absent"))return verify_alias(bytes,1,1);
    if(!strcmp(argv[2],"collision"))return verify_collision(bytes);
    if(!strcmp(argv[2],"hint-collision"))return run_hint_collision(bytes,ops);
    if(!strcmp(argv[2],"hint-bucket-check")){
#ifdef TEST_LINKED_HINTS
        return verify_hint_bucket();
#else
        fprintf(stderr,"hint bucket lifecycle check requires linked candidate helpers\n");return 2;
#endif
    }
    const int alias=!strcmp(argv[2],"alias-off")||!strcmp(argv[2],"alias-on");
    const int retarget=!strcmp(argv[2],"alias-on");
    if(strcmp(argv[2],"same") && !alias){fprintf(stderr,"unknown scenario %s\n",argv[2]);return 2;}
    if(!setup_memory(bytes,aoff,boff))return 2;
    VPTexSnapshot* warm=capture(aoff,bytes);
    vp_texture_snapshot_release(&warm);
    if(s_d3d.vp_tex_snapshot_pool_count!=1){fprintf(stderr,"warm snapshot did not enter pool\n");return 3;}
    VPTexSnapshot* owner=capture(aoff,bytes);
    if(owner->source_off!=aoff || owner->refs!=2){fprintf(stderr,"pooled snapshot was not reused\n");return 3;}
    if(vp_texture_snapshot_hint_index(aoff,bytes)==vp_texture_snapshot_hint_index(boff,bytes)){
        fprintf(stderr,"test offsets collide in hint index\n");return 3;
    }
    reset_probe_counts();
    LARGE_INTEGER freq,start,end;QueryPerformanceFrequency(&freq);QueryPerformanceCounter(&start);
    for(u32 i=0;i<ops;++i){
        VPTexSnapshot* hit=capture(alias?boff:aoff,bytes);
        if(hit!=owner){fprintf(stderr,"snapshot identity diverged at op %u\n",i);return 4;}
        vp_texture_snapshot_release(&hit);
    }
    QueryPerformanceCounter(&end);
    const double ms=elapsed_ms(start,end,freq);
    const u32 hint=vp_texture_snapshot_hint_index(alias?boff:aoff,bytes);
    VPTexSnapshot* target_hint=s_d3d.vp_tex_snapshot_hints[hint];
    const int target_hint_seeded=target_hint==owner;
    const u64 expected_hash=!alias?0:(retarget?1u:ops);
    const u64 expected_equal=expected_hash;
    const u64 expected_pool_hints=!alias?ops:(retarget?ops-1u:0u);
    const u64 expected_pool_hash=expected_hash;
    if(bench_hash_calls!=expected_hash||bench_hash_bytes!=(u64)expected_hash*bytes||
       bench_equal_calls!=expected_equal||bench_equal_bytes!=(u64)expected_equal*bytes||
       bench_get_write_watch_calls!=ops||bench_copy_calls||bench_copy_bytes||
       bench_struct_allocs||bench_data_allocs||
       s_vp_texture_churn.new_snapshot_count||s_vp_texture_churn.new_snapshot_bytes||
       s_vp_texture_churn.snapshot_pool_hint_reuses!=expected_pool_hints||
       s_vp_texture_churn.snapshot_pool_hash_reuses!=expected_pool_hash||
       s_vp_texture_churn.snapshot_pool_hash_other_off_reuses!=expected_pool_hash||
       s_vp_texture_churn.snapshot_pool_hint_retargets!=(retarget?1u:0u)||
       target_hint_seeded!=(!alias||retarget)||owner->refs!=2||
       owner->source_off!=(alias&&retarget?boff:aoff)||
       (alias&&retarget&&s_d3d.vp_tex_snapshot_hints[
          vp_texture_snapshot_hint_index(aoff,bytes)]==owner)||
       s_vp_write_watch.state!=1||s_vp_watch_mismatches){
        fprintf(stderr,"count mismatch scenario=%s hashes=%llu equal=%llu watch=%llu copy=%llu alloc=%llu new=%llu hint=%d refs=%u\n",
          argv[2],(unsigned long long)bench_hash_calls,
          (unsigned long long)bench_equal_calls,(unsigned long long)bench_get_write_watch_calls,
          (unsigned long long)bench_copy_calls,(unsigned long long)bench_data_allocs,
          (unsigned long long)s_vp_texture_churn.new_snapshot_count,target_hint_seeded,owner->refs);
        return 5;
    }
    printf("{\"scenario\":\"%s\",\"size_bytes\":%u,\"ops\":%u,\"elapsed_ms\":%.3f,"
           "\"hash_calls_total\":%llu,\"hash_bytes_total\":%llu,"
           "\"byte_equal_calls_total\":%llu,\"byte_equal_bytes_total\":%llu,"
           "\"get_write_watch_calls_total\":%llu,\"copy_calls_total\":%llu,"
           "\"hash_calls_per_op\":%.3f,\"hash_bytes_per_op\":%.3f,"
           "\"byte_equal_calls_per_op\":%.3f,\"byte_equal_bytes_per_op\":%.3f,"
           "\"get_write_watch_calls_per_op\":%.3f,\"copy_bytes_per_op\":%.3f,"
           "\"new_snapshot_count\":%llu,\"new_snapshot_bytes\":%llu,"
           "\"pool_hint_reuses\":%llu,\"pool_hash_reuses\":%llu,"
           "\"pool_other_off_reuses\":%llu,\"hint_retargets\":%llu,"
           "\"address_hint_seeded\":%s,\"same_snapshot\":true}\n",
           retarget?"alias-retarget-on":(alias?"alias-retarget-off":"same-address"),bytes,ops,ms,
           (unsigned long long)bench_hash_calls,(unsigned long long)bench_hash_bytes,
           (unsigned long long)bench_equal_calls,(unsigned long long)bench_equal_bytes,
           (unsigned long long)bench_get_write_watch_calls,(unsigned long long)bench_copy_calls,
           (double)bench_hash_calls/ops,(double)bench_hash_bytes/ops,
           (double)bench_equal_calls/ops,(double)bench_equal_bytes/ops,
           (double)bench_get_write_watch_calls/ops,(double)bench_copy_bytes/ops,
           (unsigned long long)s_vp_texture_churn.new_snapshot_count,
           (unsigned long long)s_vp_texture_churn.new_snapshot_bytes,
           (unsigned long long)s_vp_texture_churn.snapshot_pool_hint_reuses,
           (unsigned long long)s_vp_texture_churn.snapshot_pool_hash_reuses,
           (unsigned long long)s_vp_texture_churn.snapshot_pool_hash_other_off_reuses,
           (unsigned long long)s_vp_texture_churn.snapshot_pool_hint_retargets,
           target_hint_seeded?"true":"false");
    vp_texture_snapshot_release(&owner);
    if(!cleanup_memory())return 19;
    return 0;
}
'''
    return (source
        .replace("STRUCTURE\n", structure.group() + "\n")
        .replace("CHURN_STRUCT\n", churn.group() + "\n")
        .replace("HASH\n", hash_function + "\n")
        .replace("HINT\n", snapshot_hint_helpers(backend) + "\n")
        .replace("RETARGET_HELPER\n", retarget + "\n")
        .replace("TRACE_ENABLED\n", trace_enabled + "\n")
        .replace("POOL\n", pool_helpers + "\n")
        .replace("RECORD_NEW\n", record_new + "\n")
        .replace("RECORD_REJECT\n", record_reject + "\n")
        .replace("RECORD_TRIM\n", record_trim + "\n")
        .replace("BYTE_EQUAL\n", byte_equal + "\n")
        .replace("DISCARD\n", discard + "\n")
        .replace("RELEASE\n", function(backend, "static void vp_texture_snapshot_release(") + "\n")
        .replace("SET_SOURCE\n", function(backend, "static void vp_texture_cache_set_source(") + "\n")
        .replace("TRIM\n", function(backend, "static void vp_texture_cache_trim_sources(") + "\n")
        .replace("WATCH_MODE\n", function(backend, "static int vp_texture_write_watch_mode(") + "\n")
        .replace("STAMP\n", function(backend, "static u64 vp_texture_write_stamp(") + "\n")
        .replace("CAPTURE\n", capture + "\n")
        )


def run_sample(exe, size, scenario, ops, retarget=False, watch_mode=1, pool=True):
    env = dict(os.environ, TJ_TEXTURE_WRITE_WATCH=str(watch_mode),
               TJ_TEXTURE_SNAPSHOT_RETAIN="1" if pool else "0",
               TJ_TEXTURE_SNAPSHOT_HINT_RETARGET="1" if retarget else "0",
               TJ_TEXTURE_CHURN_TRACE="1")
    result = subprocess.run([str(exe), str(size), scenario, str(ops)],
                            env=env, check=True, capture_output=True, text=True)
    return json.loads(result.stdout)


def median(values):
    return sorted(values)[len(values) // 2]


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", action="store_true", help="compile and run bounded native probes")
    parser.add_argument("--checks-only", action="store_true", help="run native correctness gates without timing cases")
    parser.add_argument("--compare-hint-collision", action="store_true",
                        help="interleave same-index A/B baseline and candidate native samples")
    parser.add_argument("--baseline-source", type=Path,
                        help="saved pre-candidate renderer source for collision comparison")
    parser.add_argument("--json-out", type=Path,
                        help="write the complete report to a new path without overwriting")
    args = parser.parse_args()
    if not args.run and not args.checks_only:
        parser.print_help()
        return 0
    backend = BACKEND.read_text(encoding="utf-8")
    source_hash = hashlib.sha256(BACKEND.read_bytes()).hexdigest()
    source = make_source(backend)
    clang = shutil.which("clang") or r"C:\Program Files\LLVM\bin\clang.exe"
    results = []
    with tempfile.TemporaryDirectory(prefix="tj-snapshot-alias-") as temp:
        c_path, exe_path = Path(temp) / "snapshot_alias.c", Path(temp) / "snapshot_alias.exe"
        c_path.write_text(source, encoding="utf-8")
        subprocess.run([clang, "-std=c11", "-O2", "-D_CRT_SECURE_NO_WARNINGS",
                        "-DTEST_LINKED_HINTS=1", "-I", str(VIDEO), str(c_path), str(HASH_IMPL),
                        "-o", str(exe_path)], check=True)
        checks = []
        for scenario, watch_mode, retarget, absent_stamp, pool in (
            ("verify", 1, False, False, True),
            ("verify", 1, True, False, True),
            ("verify", 2, True, False, True),
            ("verify-absent", 1, True, True, True),
            ("collision", 1, True, False, True),
            ("gate", 1, True, False, False),
        ):
            env = dict(os.environ, TJ_TEXTURE_WRITE_WATCH=str(watch_mode),
                       TJ_TEXTURE_SNAPSHOT_RETAIN="1" if pool else "0",
                       TJ_TEXTURE_SNAPSHOT_HINT_RETARGET="1" if retarget else "0",
                       TJ_TEXTURE_CHURN_TRACE="1")
            check = subprocess.run([str(exe_path), str(16 * 1024), scenario, "1"],
                                   env=env, capture_output=True, text=True)
            if check.returncode:
                raise RuntimeError(f"native gate {scenario} exited {check.returncode}: {check.stderr}")
            checks.append(json.loads(check.stdout))
        for watch_mode in (1, 2):
            env = dict(os.environ, TJ_TEXTURE_WRITE_WATCH=str(watch_mode),
                       TJ_TEXTURE_SNAPSHOT_RETAIN="1", TJ_TEXTURE_SNAPSHOT_HINT_RETARGET="0",
                       TJ_TEXTURE_CHURN_TRACE="1")
            check = subprocess.run([str(exe_path), "64", "hint-bucket-check", "1"],
                                   env=env, capture_output=True, text=True)
            if check.returncode:
                raise RuntimeError(f"native linked-hint gate watch={watch_mode} exited {check.returncode}: {check.stderr}")
            checks.append(json.loads(check.stdout))
        collision_comparison = []
        if args.compare_hint_collision:
            if not args.baseline_source:
                raise ValueError("--compare-hint-collision requires --baseline-source")
            baseline_backend = args.baseline_source.read_text(encoding="utf-8")
            baseline_path = Path(temp) / "snapshot_alias_baseline.c"
            baseline_exe = Path(temp) / "snapshot_alias_baseline.exe"
            baseline_path.write_text(make_source(baseline_backend), encoding="utf-8")
            subprocess.run([clang, "-std=c11", "-O2", "-D_CRT_SECURE_NO_WARNINGS", "-I",
                            str(VIDEO), str(baseline_path), str(HASH_IMPL), "-o", str(baseline_exe)],
                           check=True)
            for size in SIZES:
                ops = {"baseline": HINT_COLLISION_START_OPS[size],
                       "candidate": HINT_COLLISION_START_OPS[size]}
                binaries = {"baseline": baseline_exe, "candidate": exe_path}
                # Calibrate each source against the same minimum sample time,
                # interleaving probes while allowing different bounded op counts.
                for probe in range(24):
                    order = ("baseline", "candidate") if probe & 1 == 0 else ("candidate", "baseline")
                    adequate = {"baseline": False, "candidate": False}
                    for label in order:
                        item = run_sample(binaries[label], size, "hint-collision", ops[label])
                        if item["elapsed_ms"] >= MIN_SAMPLE_MS:
                            adequate[label] = True
                        else:
                            if ops[label] >= HINT_COLLISION_MAX_OPS[size]:
                                raise RuntimeError(
                                    f"hint collision {label} sample stayed below {MIN_SAMPLE_MS}ms "
                                    f"at size={size}, ops={ops[label]}, elapsed={item['elapsed_ms']:.4f}ms")
                            ops[label] = min(ops[label] * 2, HINT_COLLISION_MAX_OPS[size])
                    if all(adequate.values()):
                        break
                else:
                    raise RuntimeError(f"hint collision calibration did not converge at size={size}")

                for retry in range(8):
                    samples = []
                    for rep in range(3):
                        order = ("baseline", "candidate") if rep & 1 == 0 else ("candidate", "baseline")
                        for label in order:
                            item = run_sample(binaries[label], size, "hint-collision", ops[label])
                            item["build"] = label
                            item["rep"] = rep + 1
                            samples.append(item)
                    short = [item for item in samples if item["elapsed_ms"] < MIN_SAMPLE_MS]
                    if not short:
                        break
                    for label in {item["build"] for item in short}:
                        if ops[label] >= HINT_COLLISION_MAX_OPS[size]:
                            raise RuntimeError(f"final {label} sample stayed below {MIN_SAMPLE_MS}ms at size={size}")
                        ops[label] = min(ops[label] * 2, HINT_COLLISION_MAX_OPS[size])
                else:
                    raise RuntimeError(f"hint collision final samples did not reach {MIN_SAMPLE_MS}ms at size={size}")
                for label in ("baseline", "candidate"):
                    group = [item for item in samples if item["build"] == label]
                    expected_hashes = ops[label] if label == "baseline" else 0
                    if any(item["hash_calls"] != expected_hashes or
                           item["equal_calls"] != expected_hashes or item["copy_calls"] or
                           item["hint_reuses"] != (0 if label == "baseline" else ops[label]) or
                           item["pool_hash_reuses"] != (ops[label] if label == "baseline" else 0)
                           for item in group):
                        raise RuntimeError(f"hint collision {label} counters did not match the expected path")
                collision_comparison.append({
                    "size_bytes": size,
                    "baseline_ops_per_sample": ops["baseline"],
                    "candidate_ops_per_sample": ops["candidate"],
                    "sample_ms_min": round(min(item["elapsed_ms"] for item in samples), 4),
                    "baseline_median_ms": round(median([item["elapsed_ms"] for item in samples if item["build"] == "baseline"]), 4),
                    "candidate_median_ms": round(median([item["elapsed_ms"] for item in samples if item["build"] == "candidate"]), 4),
                    "baseline_median_us_per_op": round(median([item["elapsed_ms"] for item in samples if item["build"] == "baseline"]) * 1000.0 / ops["baseline"], 4),
                    "candidate_median_us_per_op": round(median([item["elapsed_ms"] for item in samples if item["build"] == "candidate"]) * 1000.0 / ops["candidate"], 4),
                    "baseline_hash_calls_per_op": 1,
                    "candidate_hash_calls_per_op": 0,
                    "baseline_equal_calls_per_op": 1,
                    "candidate_equal_calls_per_op": 0,
                    "sample_order": [f"{item['build']}-{item['rep']}" for item in samples],
                })
        for size in (() if args.checks_only else SIZES):
            for scenario, retarget in (("same", False), ("alias-off", False), ("alias-on", True)):
                limit_key = scenario if scenario != "same" else "same"
                ops = START_OPS_SAME[size] if scenario == "same" else START_OPS[size]
                while True:
                    samples = [run_sample(exe_path, size, scenario, ops, retarget=retarget) for _ in range(3)]
                    if min(sample["elapsed_ms"] for sample in samples) >= MIN_SAMPLE_MS or ops >= MAX_OPS[limit_key if limit_key != "same" else "same"][size]:
                        break
                    ops = min(ops * 2, MAX_OPS[limit_key if limit_key != "same" else "same"][size])
                representative = samples[1]
                median_ms = median([sample["elapsed_ms"] for sample in samples])
                results.append({
                    "scenario": representative["scenario"],
                    "size_bytes": size,
                    "ops_per_sample": ops,
                    "median_ms": round(median_ms, 3),
                    "median_us_per_op": round(median_ms * 1000.0 / ops, 4),
                    "hash_calls_total": representative["hash_calls_total"],
                    "hash_bytes_total": representative["hash_bytes_total"],
                    "byte_equal_calls_total": representative["byte_equal_calls_total"],
                    "byte_equal_bytes_total": representative["byte_equal_bytes_total"],
                    "get_write_watch_calls_total": representative["get_write_watch_calls_total"],
                    "copy_calls_total": representative["copy_calls_total"],
                    "hash_calls_per_op": representative["hash_calls_per_op"],
                    "hash_bytes_per_op": representative["hash_bytes_per_op"],
                    "byte_equal_calls_per_op": representative["byte_equal_calls_per_op"],
                    "byte_equal_bytes_per_op": representative["byte_equal_bytes_per_op"],
                    "get_write_watch_calls_per_op": representative["get_write_watch_calls_per_op"],
                    "copy_bytes_per_op": representative["copy_bytes_per_op"],
                    "new_snapshot_count": representative["new_snapshot_count"],
                    "new_snapshot_bytes": representative["new_snapshot_bytes"],
                    "pool_hint_reuses": representative["pool_hint_reuses"],
                    "pool_hash_reuses": representative["pool_hash_reuses"],
                    "pool_other_off_reuses": representative["pool_other_off_reuses"],
                    "hint_retargets": representative["hint_retargets"],
                    "address_hint_seeded": representative["address_hint_seeded"],
                })
    report = {
        "renderer_sha256": source_hash,
        "hash": "actual production vp_tex_source_hash linked with rsx_stream_hash.c",
        "guest_memory": "real Windows MEM_WRITE_WATCH allocation; timed cases use mode 1",
        "owner": "address A is pooled then reacquired as a strong immutable owner; pool option is ON",
        "correctness_checks": checks,
        "scenarios": {
            "same-address": "repeat the warm address A; production hint can validate unchanged bytes",
            "alias-retarget-off": "repeat identical bytes at B while A remains pooled; each B lookup hashes and compares",
            "alias-retarget-on": "pool-enabled exact opt-in retargets the verified snapshot to B and seeds B hint",
            "same-index-distinct-content-alternating-AB": "alternate two non-overlapping same-index sources with different content; baseline direct mapping hashes each lookup, while the four-way candidate validates either hint",
        },
        "scope": "snapshot lookup CPU cost only; no GPU cache or FPS inference",
        "results": results,
        "hint_collision_comparison": collision_comparison,
    }
    payload = json.dumps(report, indent=2)
    if args.json_out:
        with args.json_out.open("x", encoding="utf-8") as output:
            output.write(payload + "\n")
    print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
