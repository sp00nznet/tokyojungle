"""Compile production snapshot lifetime helpers and exercise the opt-in pool."""
from pathlib import Path
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


def function(source, signature):
    """Extract one definition, skipping a forward declaration if present."""
    start = source.find(signature)
    while start >= 0:
        brace = source.find("{", start)
        semi = source.find(";", start)
        if brace >= 0 and (semi < 0 or brace < semi):
            depth = 1
            end = brace + 1
            while depth:
                depth += (source[end] == "{") - (source[end] == "}")
                end += 1
            return source[start:end]
        start = source.find(signature, start + len(signature))
    raise ValueError(f"definition not found: {signature}")


def make_source(backend):
    structure = re.search(r"typedef struct VPTexSnapshot \{.*?\} VPTexSnapshot;", backend, re.S)
    churn = re.search(r"typedef struct VPTexChurnCounters \{.*?\} VPTexChurnCounters;", backend, re.S)
    assert structure and "u8 pool_retained;" in structure.group()
    assert churn and "snapshot_pool_budget_reclaims" in churn.group()
    pool_start = backend.index("typedef enum VPTexSnapshotPoolEvictReason {")
    pool_end = backend.index("static void vp_texture_churn_record_new_snapshot(", pool_start)
    pool_helpers = backend[pool_start:pool_end]

    constants = {
        "VP_TEX_SNAPSHOT_MAX_BYTES": r"128ull\s*\*\s*1024ull\s*\*\s*1024ull",
        "VP_TEX_SNAPSHOT_SINGLE_MAX_BYTES": r"32ull\s*\*\s*1024ull\s*\*\s*1024ull",
        "VP_TEX_SNAPSHOT_RETAIN_MAX_BYTES": r"64ull\s*\*\s*1024ull\s*\*\s*1024ull",
        "VP_TEX_SNAPSHOT_RETAIN_MAX_ENTRIES": r"256u",
    }
    for name, expected in constants.items():
        match = re.search(rf"(?m)^#define\s+{name}\s+(.+)$", backend)
        actual = match.group(1).strip() if match else ""
        if actual.startswith("(") and actual.endswith(")"):
            actual = actual[1:-1].strip()
        assert match and re.fullmatch(expected, actual), (name, match.group(1) if match else None)
    threshold = re.search(r"(?m)^#define VP_TEX_SNAPSHOT_HASH_REUSE_MIN_BYTES .+$", backend)
    assert threshold and threshold.group().endswith("(16u * 1024u)")

    release = function(backend, "static void vp_texture_snapshot_release(")
    discard = function(backend, "static void vp_texture_snapshot_discard(")
    capture = function(backend, "static int vp_capture_tex_snapshot_impl(")
    trim = function(backend, "static void vp_texture_cache_trim_sources(")
    set_source = function(backend, "static void vp_texture_cache_set_source(")
    assert "vp_texture_snapshot_pool_admit(snapshot)" in release
    assert release.index("vp_texture_snapshot_pool_admit(snapshot)") < release.index("--snapshot->refs")
    assert "vp_texture_snapshot_pool_reclaim_for_budget(bytes);" in trim
    assert trim.index("vp_texture_snapshot_pool_reclaim_for_budget(bytes);") < trim.index("for (int pass")
    assert capture.index("vp_texture_cache_trim_sources(bytes);") < capture.index(
        "if (s_d3d.vp_tex_snapshot_bytes + bytes > VP_TEX_SNAPSHOT_MAX_BYTES)"
    )
    init = function(backend, "int rsx_d3d12_backend_init(")
    shutdown = function(backend, "void rsx_d3d12_backend_shutdown(")
    assert init.index("vp_texture_snapshot_pool_drain(") < init.index("memset(&s_d3d") < \
        init.index("vp_texture_snapshot_pool_init();")
    assert shutdown.index("wait_for_gpu_checked()") < shutdown.index(
        "s_d3d.vp_tex_snapshot_pool_enabled = 0;"
    ) < shutdown.index("vp_texture_snapshot_pool_drain(") < \
        shutdown.index("d3d12_prefix_owner_destroy")

    actual_hash = function(backend, "static u64 vp_tex_source_hash(")
    actual_hash = actual_hash.replace("return hash;", "return force_hash_collision ? 0x12345678ull : hash;")
    assert "force_hash_collision ?" in actual_hash
    byte_equal = function(backend, "static int vp_snapshot_bytes_equal(")
    byte_equal = byte_equal.replace(")\n{", ")\n{\n    ++snapshot_equal_calls;", 1)
    capture = capture.replace(
        "memcpy(snapshot->data, source, bytes);",
        "fixture_copied_bytes += bytes; ++fixture_copy_calls; memcpy(snapshot->data, source, bytes);",
    )
    assert "fixture_copy_calls" in capture
    capture = capture.replace("(VPTexSnapshot*)calloc(1, sizeof(*snapshot))", "fixture_snapshot_struct_alloc()")
    capture = capture.replace("(u8*)malloc(bytes)", "fixture_snapshot_data_alloc(bytes)")
    capture = capture.replace("free(snapshot);", "fixture_snapshot_struct_free(snapshot);")
    discard = discard.replace("free(snapshot->data);", "fixture_snapshot_data_free(snapshot->data);")
    discard = discard.replace("free(snapshot);", "fixture_snapshot_struct_free(snapshot);")

    source = r'''
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <windows.h>
#include "rsx_bytes_equal.h"
#include "rsx_texture_write_watch.h"
#include "rsx_stream_hash.h"
typedef uint8_t u8; typedef uint32_t u32; typedef uint64_t u64;
#define VP_TEX_SNAPSHOT_BUCKETS 1024u
#define VP_TEX_SNAPSHOT_HINTS 256u
#define VP_TEX_SNAPSHOT_HINT_WAYS 4u
#define VP_TEX_SNAPSHOT_MAX_BYTES 4096u
#define VP_TEX_SNAPSHOT_SINGLE_MAX_BYTES 32768u
#define VP_TEX_SNAPSHOT_RETAIN_MAX_BYTES 1024ull
#define VP_TEX_SNAPSHOT_RETAIN_MAX_ENTRIES 256u
#define VP_TEX_SNAPSHOT_HASH_REUSE_MIN_BYTES (16u * 1024u)
#define VP_TEX_SLOTS 4
STRUCTURE
typedef struct { VPTexSnapshot* source_snapshot; int used; } VPTexSlot;
typedef struct {
    u64 vp_tex_snapshot_bytes;
    VPTexSnapshot* vp_tex_snapshot_buckets[VP_TEX_SNAPSHOT_BUCKETS];
    VPTexSnapshot* vp_tex_snapshot_hints[VP_TEX_SNAPSHOT_HINTS];
    VPTexSnapshot* vp_tex_snapshot_pool[VP_TEX_SNAPSHOT_RETAIN_MAX_ENTRIES];
    u32 vp_tex_snapshot_pool_head, vp_tex_snapshot_pool_count;
    u64 vp_tex_snapshot_pool_bytes;
    int vp_tex_snapshot_pool_enabled;
    u32 vp_tex_snapshot_pool_suppress;
    VPTexSlot vp_tex[VP_TEX_SLOTS];
} FixtureD3dState;
static FixtureD3dState s_d3d;
CHURN_STRUCT
static VPTexChurnCounters s_vp_texture_churn;
static int s_vp_texture_churn_enabled = 1;
static int s_vp_texture_hint_retarget_config;
static u32 s_vp_texture_churn_probe_window_attempts;
static u64 s_vp_texture_churn_probe_window_bytes;
static RsxTextureWriteWatch s_vp_write_watch;
static u64 s_vp_watch_hits, s_vp_watch_bytes, s_vp_watch_mismatches;
typedef struct { u64 tex_hash_ticks, tex_hash_calls, tex_hash_bytes; } PerfPhase;
static PerfPhase s_tj_perf_phase;
static u8* memory; u8* vm_base; u32 ppu_vm_size;
static unsigned checks, failures, fixture_copy_calls, snapshot_equal_calls;
static u64 fixture_copied_bytes, fixture_struct_allocs, fixture_data_allocs;
static u64 fixture_struct_frees, fixture_data_frees;
static int force_hash_collision;
static int tj_perf_trace_enabled(void) { return 0; }
static LONGLONG tj_perf_now(void) { return 0; }
static int tj_gcm2d_backend_perf_enabled(void) { return 0; }
static u64 tj_gcm2d_backend_perf_now(void) { return 0; }
static u64 tj_gcm2d_backend_perf_us(u64 ticks) { return ticks; }
static int vp_texture_churn_trace_enabled(void);
static void vp_texture_churn_record_new_snapshot(u64 bytes);
static void vp_texture_churn_record_budget_reject(void);
static void vp_texture_churn_record_trim(u32 pass,u64 bytes,u64 freed_bytes);
static void vp_texture_cache_trim_sources(u32 bytes);
static void vp_texture_snapshot_release(VPTexSnapshot** owner);
static int d3d12_prefix_owner_texture_reserved(u32 slot) { (void)slot; return 0; }
static VPTexSnapshot* fixture_snapshot_struct_alloc(void)
{ VPTexSnapshot* p=(VPTexSnapshot*)calloc(1,sizeof(*p)); if(p)++fixture_struct_allocs; return p; }
static u8* fixture_snapshot_data_alloc(u32 bytes)
{ u8* p=(u8*)malloc(bytes); if(p)++fixture_data_allocs; return p; }
static void fixture_snapshot_struct_free(void* p)
{ if(p)++fixture_struct_frees; free(p); }
static void fixture_snapshot_data_free(void* p)
{ if(p)++fixture_data_frees; free(p); }
static int vp_tex_source_span(u32 w,u32 h,u32 d,u32 fmt,u32 pitch,int cube,
    u32 dimension,u32 mips,int border,u32* bytes,u64* face_stride)
{
    (void)fmt;(void)pitch;(void)cube;(void)dimension;(void)mips;(void)border;(void)face_stride;
    if(!bytes||!w||!h||!d)return -1; *bytes=w*h*d; return 0;
}
HASH
HINT
RETARGET
static int vp_texture_write_watch_mode(void);
static u64 vp_texture_write_stamp(const u8* base,u64 span,u32 off,u32 bytes,
                                  RsxTextureWriteStampHint* hint);
static int vp_snapshot_bytes_equal(const void* a,const void* b,size_t bytes);
static void vp_texture_snapshot_pool_evict_oldest(u32 reason);
static int vp_texture_snapshot_pool_admit(VPTexSnapshot* snapshot);
static void vp_texture_snapshot_pool_reclaim_for_budget(u32 incoming_bytes);
static void vp_texture_snapshot_pool_drain(u32 reason);
POOL
TRACE_ENABLED
RECORD_NEW
RECORD_REJECT
RECORD_TRIM
DISCARD
RELEASE
SET_SOURCE
TRIM
WATCH_MODE
STAMP
BYTE_EQUAL
CAPTURE
static void check(int ok,const char* label)
{ ++checks;if(!ok){++failures;printf("FAIL %s\n",label);} }
static VPTexSnapshot* take(u32 off,u32 bytes)
{
    VPTexSnapshot* result=NULL;
    if(vp_capture_tex_snapshot_impl(off,bytes,1,1,0,bytes,0,2,1,0,&result)!=0||!result){
        printf("FAIL capture off=%u bytes=%u\n",off,bytes);++failures;return NULL;
    }
    return result;
}
static void fill(u32 off,u32 bytes,u8 seed)
{ for(u32 i=0;i<bytes;++i) memory[off+i]=(u8)(seed+i*13u+(i>>3)); }
static void set_retain_env(const char* value) { _putenv_s("TJ_TEXTURE_SNAPSHOT_RETAIN",value); }
static void clear_fixture(void)
{
    s_d3d.vp_tex_snapshot_pool_enabled=0;
    ++s_d3d.vp_tex_snapshot_pool_suppress;
    vp_texture_snapshot_pool_drain(VP_TEX_SNAPSHOT_POOL_EVICT_SHUTDOWN);
    --s_d3d.vp_tex_snapshot_pool_suppress;
    for(u32 i=0;i<VP_TEX_SLOTS;++i) vp_texture_cache_set_source(&s_d3d.vp_tex[i],NULL);
    check(s_d3d.vp_tex_snapshot_bytes==0&&s_d3d.vp_tex_snapshot_pool_count==0&&
          s_d3d.vp_tex_snapshot_pool_bytes==0,"cleanup releases all owners and pool bytes");
    for(u32 i=0;i<VP_TEX_SNAPSHOT_BUCKETS;++i)
        if(s_d3d.vp_tex_snapshot_buckets[i]){check(0,"cleanup leaves no hash bucket node");break;}
    for(u32 i=0;i<VP_TEX_SNAPSHOT_HINTS;++i)
        if(s_d3d.vp_tex_snapshot_hints[i]){check(0,"cleanup leaves no hint node");break;}
    memset(&s_d3d,0,sizeof(s_d3d));
    memset(&s_vp_texture_churn,0,sizeof(s_vp_texture_churn));
    fixture_copy_calls=snapshot_equal_calls=0;fixture_copied_bytes=0;
    fixture_struct_allocs=fixture_data_allocs=fixture_struct_frees=fixture_data_frees=0;
    rsx_texture_write_watch_destroy(&s_vp_write_watch);
    s_vp_watch_hits=s_vp_watch_bytes=s_vp_watch_mismatches=0;
}
static void begin_case(int enabled)
{
    clear_fixture();set_retain_env(enabled?"1":"0");vp_texture_snapshot_pool_init();
    check(s_d3d.vp_tex_snapshot_pool_enabled==enabled,"pool option has exact opt-in semantics");
}
static void test_option_and_off_path(void)
{
    clear_fixture();
    set_retain_env("01");vp_texture_snapshot_pool_init();
    check(!s_d3d.vp_tex_snapshot_pool_enabled,"retention rejects near-miss environment value");
    set_retain_env("true");vp_texture_snapshot_pool_init();
    check(!s_d3d.vp_tex_snapshot_pool_enabled,"retention rejects nonnumeric environment value");
    set_retain_env("");vp_texture_snapshot_pool_init();
    check(!s_d3d.vp_tex_snapshot_pool_enabled,"unset retention remains disabled");
    set_retain_env("0");vp_texture_snapshot_pool_init();
    check(!s_d3d.vp_tex_snapshot_pool_enabled,"explicit zero retention remains disabled");
    fill(64,64,17);
    VPTexSnapshot* a=take(64,64);vp_texture_snapshot_release(&a);
    check(s_d3d.vp_tex_snapshot_pool_count==0&&s_d3d.vp_tex_snapshot_bytes==0,
          "OFF final release frees instead of retaining");
    VPTexSnapshot* b=take(64,64);vp_texture_snapshot_release(&b);
    check(fixture_struct_allocs==2&&fixture_data_allocs==2&&fixture_copy_calls==2&&
          fixture_copied_bytes==128,"OFF repeated capture performs two allocations and copies");
    check(s_vp_texture_churn.new_snapshot_count==2&&s_vp_texture_churn.new_snapshot_bytes==128,
          "OFF new-snapshot diagnostics match actual copies");
}
static void test_reacquire_and_mutation(void)
{
    begin_case(1);fill(512,64,33);
    VPTexSnapshot* first=take(512,64);if(!first)return;
    const u64 allocs=fixture_data_allocs,copies=fixture_copied_bytes,new_count=s_vp_texture_churn.new_snapshot_count;
    vp_texture_snapshot_release(&first);
    VPTexSnapshot* retained=s_d3d.vp_tex_snapshot_pool[0];
    check(retained&&retained->refs==1&&retained->pool_retained&&
          s_d3d.vp_tex_snapshot_pool_count==1&&s_d3d.vp_tex_snapshot_pool_bytes==64,
          "last release transfers one strong owner into the pool");
    VPTexSnapshot* same=take(512,64);
    check(same==retained&&same&&same->refs==2,"unchanged source reacquires retained immutable snapshot");
    check(fixture_data_allocs==allocs&&fixture_copied_bytes==copies&&
          s_vp_texture_churn.new_snapshot_count==new_count,
          "pool reacquire avoids allocation, copy, and new-snapshot diagnostic");
    check(s_vp_texture_churn.snapshot_pool_hint_reuses==1,
          "same-address reuse is counted as a pooled hint hit");
    vp_texture_snapshot_release(&same);
    const u8 old_byte=retained->data[7];memory[512+7]^=1;
    VPTexSnapshot* changed=take(512,64);
    check(changed&&changed!=retained&&retained->data[7]==old_byte&&
          changed->data[7]==memory[512+7],"changed guest bytes create a new immutable version");
    check(s_vp_texture_churn.new_snapshot_count==new_count+1&&
          fixture_data_allocs==allocs+1&&fixture_copied_bytes==copies+64,
          "changed source metrics match its one real allocation and copy");
    if(vp_texture_write_watch_mode())
        check(changed&&changed->source_write_stamp&&
              changed->source_write_stamp!=retained->source_write_stamp,
              "changed guest page invalidates the retained source stamp");
    vp_texture_snapshot_release(&changed);
    if(vp_texture_write_watch_mode()==2)
        check(s_vp_watch_mismatches==0&&s_vp_write_watch.state==1,
              "Audit mode uses real MEM_WRITE_WATCH with no clean audit mismatch");
    clear_fixture();
}
static void find_hint_pair(u32* first,u32* second,u32 bytes)
{
    int seen[VP_TEX_SNAPSHOT_HINTS];for(u32 i=0;i<VP_TEX_SNAPSHOT_HINTS;++i)seen[i]=-1;
    for(u32 i=0;i<2000;++i){
        u32 off=8192u+i*32u,hint=vp_texture_snapshot_hint_index(off,bytes);
        if(seen[hint]>=0){*first=8192u+(u32)seen[hint]*32u;*second=off;return;}
        seen[hint]=(int)i;
    }
    *first=*second=0;
}
static void test_hint_and_hash_collisions(void)
{
    begin_case(1);u32 aoff=0,boff=0;find_hint_pair(&aoff,&boff,16);
    check(aoff&&boff&&vp_texture_snapshot_hint_index(aoff,16)==vp_texture_snapshot_hint_index(boff,16),
          "fixture selects two addresses sharing one source hint");
    fill(aoff,16,91);fill(boff,16,141);
    VPTexSnapshot* a=take(aoff,16);if(!a)return;vp_texture_snapshot_release(&a);
    VPTexSnapshot* b=take(boff,16);if(!b){clear_fixture();return;}vp_texture_snapshot_release(&b);
    const u64 allocs=fixture_data_allocs,new_count=s_vp_texture_churn.new_snapshot_count;
    const unsigned hint_equal_before=snapshot_equal_calls;
    VPTexSnapshot* same=take(aoff,16);
    check(same&&same->source_off==aoff&&same->data[7]==memory[aoff+7]&&
          fixture_data_allocs==allocs&&
          s_vp_texture_churn.new_snapshot_count==new_count&&
          s_vp_texture_churn.snapshot_pool_hash_reuses==0&&
          s_vp_texture_churn.snapshot_pool_hint_reuses==1&&
          snapshot_equal_calls==hint_equal_before+(vp_texture_write_watch_mode()==1?0u:1u),
          "same-index source uses its bounded hint after a distinct snapshot collides");
    vp_texture_snapshot_release(&same);clear_fixture();

    begin_case(1);u32 hash_a=1024,hash_b=2048;
    while(vp_texture_snapshot_hint_index(hash_a,32)==vp_texture_snapshot_hint_index(hash_b,32))
        hash_b+=4096;
    check(vp_texture_snapshot_hint_index(hash_a,32)!=vp_texture_snapshot_hint_index(hash_b,32),
          "forced content-hash collision uses separate hint buckets");
    fill(hash_a,32,11);fill(hash_b,32,79);force_hash_collision=1;
    VPTexSnapshot* old=take(hash_a,32);if(!old){force_hash_collision=0;clear_fixture();return;}
    VPTexSnapshot* retained_old=old;
    const u8 saved=old->data[13];vp_texture_snapshot_release(&old);
    const unsigned equal_before=snapshot_equal_calls;
    VPTexSnapshot* other=take(hash_b,32);
    check(other&&other!=retained_old&&snapshot_equal_calls>equal_before,
          "full hash collision requires byte comparison before deduplication");
    check(other&&other->data[13]==memory[hash_b+13]&&retained_old->data[13]==saved,
          "collision preserves both distinct immutable snapshots");
    vp_texture_snapshot_release(&other);force_hash_collision=0;clear_fixture();
}
static void test_fifo_cache_pin_and_byte_cap(void)
{
    begin_case(1);fill(4096,4,7);
    VPTexSnapshot* first=take(4096,4);if(!first)return;vp_texture_snapshot_release(&first);
    first=take(4096,4);vp_texture_cache_set_source(&s_d3d.vp_tex[0],first);
    vp_texture_snapshot_release(&first);
    VPTexSnapshot* pinned=s_d3d.vp_tex[0].source_snapshot;
    for(u32 i=0;i<256;++i){
        u32 off=8192u+i*8u;u32 tag=i+1u;memcpy(memory+off,&tag,sizeof(tag));
        VPTexSnapshot* node=take(off,4);if(node)vp_texture_snapshot_release(&node);
    }
    check(s_d3d.vp_tex_snapshot_pool_count==256&&s_d3d.vp_tex_snapshot_pool_bytes==1024&&
          s_vp_texture_churn.snapshot_pool_cap_evictions==1,
          "256-node FIFO evicts exactly its oldest entry at node capacity");
    check(s_d3d.vp_tex[0].source_snapshot==pinned&&pinned->refs==1&&!pinned->pool_retained&&
          pinned->data[0]==7,"eviction drops only the pool owner while cache identity stays live");
    check(s_d3d.vp_tex_snapshot_pool[s_d3d.vp_tex_snapshot_pool_head]->source_off==8192,
          "FIFO head advances to the second admitted source");
    vp_texture_cache_set_source(&s_d3d.vp_tex[0],NULL);
    check(pinned->refs==1&&pinned->pool_retained&&s_d3d.vp_tex_snapshot_pool_count==256,
          "last cache release safely re-admits the still-indexed snapshot");
    const u64 before=s_vp_texture_churn.new_snapshot_count;
    VPTexSnapshot* evicted=take(8192,4);
    check(evicted&&s_vp_texture_churn.new_snapshot_count==before+1,
          "recapturing an evicted source allocates a new snapshot");
    vp_texture_snapshot_release(&evicted);
    check(s_d3d.vp_tex_snapshot_pool[s_d3d.vp_tex_snapshot_pool_head]->source_off==8208,
          "FIFO order remains correct after cache-pinned re-admission");
    clear_fixture();

    begin_case(1);fill(4096,600,23);fill(8192,500,61);fill(12288,1025,73);
    VPTexSnapshot* large=take(4096,600);vp_texture_snapshot_release(&large);
    VPTexSnapshot* next=take(8192,500);vp_texture_snapshot_release(&next);
    check(s_d3d.vp_tex_snapshot_pool_count==1&&s_d3d.vp_tex_snapshot_pool_bytes==500&&
          s_vp_texture_churn.snapshot_pool_cap_evictions==1,
          "byte cap evicts oldest pooled owner before admitting the next snapshot");
    VPTexSnapshot* too_big=take(12288,1025);vp_texture_snapshot_release(&too_big);
    check(s_d3d.vp_tex_snapshot_pool_count==1&&s_d3d.vp_tex_snapshot_pool_bytes==500&&
          s_vp_texture_churn.snapshot_pool_oversize_skips==1&&
          s_vp_texture_churn.snapshot_pool_oversize_skip_bytes==1025,
          "oversized source is freed and counted without disturbing retained pool bytes");
    clear_fixture();
}
static void test_budget_reclaim_and_shutdown(void)
{
    begin_case(1);fill(1024,256,9);fill(4096,3800,41);fill(8192,300,87);
    VPTexSnapshot* pooled=take(1024,256);vp_texture_snapshot_release(&pooled);
    VPTexSnapshot* active=take(4096,3800);
    const u64 new_before=s_vp_texture_churn.new_snapshot_count;
    VPTexSnapshot* rejected=NULL;
    check(vp_capture_tex_snapshot_impl(8192,300,1,1,0,300,0,2,1,0,&rejected)==-1&&!rejected,
          "budget pressure rejects when active snapshots still exceed the shared cap");
    check(s_vp_texture_churn.snapshot_pool_budget_reclaims==1&&
          s_vp_texture_churn.snapshot_pool_budget_owner_drop_bytes==256&&
          s_d3d.vp_tex_snapshot_pool_count==0&&s_d3d.vp_tex_snapshot_bytes==3800,
          "pool owners are reclaimed before rejection while active bytes survive");
    check(s_vp_texture_churn.budget_rejections==1&&
          s_vp_texture_churn.new_snapshot_count==new_before&&fixture_data_allocs==2&&
          fixture_copied_bytes==4056,"rejected input adds no allocation, copy, or new-snapshot metric");
    check(active&&active->data[17]==memory[4096+17],"budget trim preserves the active snapshot owner");
    vp_texture_snapshot_release(&active);clear_fixture();

    begin_case(1);fill(256,128,14);
    VPTexSnapshot* retained=take(256,128);vp_texture_snapshot_release(&retained);
    check(s_d3d.vp_tex_snapshot_pool_count==1,"shutdown case has a real retained owner");
    const u64 before=s_vp_texture_churn.new_snapshot_count;
    s_d3d.vp_tex_snapshot_pool_enabled=0;++s_d3d.vp_tex_snapshot_pool_suppress;
    vp_texture_snapshot_pool_drain(VP_TEX_SNAPSHOT_POOL_EVICT_SHUTDOWN);
    check(s_d3d.vp_tex_snapshot_pool_count==0&&s_d3d.vp_tex_snapshot_pool_bytes==0&&
          s_d3d.vp_tex_snapshot_bytes==0,"shutdown drain releases every retained owner");
    --s_d3d.vp_tex_snapshot_pool_suppress;
    set_retain_env("1");vp_texture_snapshot_pool_init();
    VPTexSnapshot* fresh=take(256,128);
    check(fresh&&s_vp_texture_churn.new_snapshot_count==before+1,
          "reinit captures a fresh object after the prior pool was drained");
    vp_texture_snapshot_release(&fresh);clear_fixture();
}
int main(void)
{
    setvbuf(stdout,NULL,_IONBF,0);
    memory=(u8*)VirtualAlloc(NULL,65536,MEM_RESERVE|MEM_COMMIT|MEM_WRITE_WATCH,PAGE_READWRITE);
    check(memory!=NULL,"native guest allocation uses real MEM_WRITE_WATCH");if(!memory)return 2;
    vm_base=memory;ppu_vm_size=65536;
    test_option_and_off_path();test_reacquire_and_mutation();test_hint_and_hash_collisions();
    test_fifo_cache_pin_and_byte_cap();test_budget_reclaim_and_shutdown();
    if(vp_texture_write_watch_mode()==2)
        check(s_vp_watch_mismatches==0,"Audit plus retention has no audit mismatch");
    rsx_texture_write_watch_destroy(&s_vp_write_watch);VirtualFree(memory,0,MEM_RELEASE);
    printf("Texture snapshot retention (watch=%d): %u checks, %u failures\n",
           vp_texture_write_watch_mode(),checks,failures);
    return failures?1:0;
}
'''
    return (source
        .replace("STRUCTURE\n", structure.group() + "\n")
        .replace("CHURN_STRUCT\n", churn.group() + "\n")
        .replace("HASH\n", actual_hash + "\n")
        .replace("HINT\n", snapshot_hint_helpers(backend) + "\n")
        .replace("RETARGET\n", function(backend, "static int vp_texture_snapshot_hint_retarget(") + "\n" if "static int vp_texture_snapshot_hint_retarget(" in backend else "")
        .replace("POOL\n", pool_helpers + "\n")
        .replace("TRACE_ENABLED\n", function(backend, "static int vp_texture_churn_trace_enabled(") + "\n")
        .replace("RECORD_NEW\n", function(backend, "static void vp_texture_churn_record_new_snapshot(") + "\n")
        .replace("RECORD_REJECT\n", function(backend, "static void vp_texture_churn_record_budget_reject(") + "\n")
        .replace("RECORD_TRIM\n", function(backend, "static void vp_texture_churn_record_trim(") + "\n")
        .replace("DISCARD\n", discard + "\n")
        .replace("RELEASE\n", release + "\n")
        .replace("SET_SOURCE\n", set_source + "\n")
        .replace("TRIM\n", trim + "\n")
        .replace("WATCH_MODE\n", function(backend, "static int vp_texture_write_watch_mode(") + "\n")
        .replace("STAMP\n", function(backend, "static u64 vp_texture_write_stamp(") + "\n")
        .replace("BYTE_EQUAL\n", byte_equal + "\n")
        .replace("CAPTURE\n", capture + "\n"))


def main():
    backend = BACKEND.read_text(encoding="utf-8")
    source = make_source(backend)
    clang = shutil.which("clang") or r"C:\Program Files\LLVM\bin\clang.exe"
    with tempfile.TemporaryDirectory(prefix="tj-snapshot-retention-") as temp:
        c_path, exe_path = Path(temp) / "snapshot_retention.c", Path(temp) / "snapshot_retention.exe"
        c_path.write_text(source, encoding="utf-8")
        subprocess.run([clang, "-std=c11", "-O2", "-D_CRT_SECURE_NO_WARNINGS", "-I",
                        str(VIDEO), str(c_path), str(HASH_IMPL), "-o", str(exe_path)], check=True)
        for watch in ("0", "1", "2"):
            env = dict(os.environ, TJ_TEXTURE_WRITE_WATCH=watch, TJ_TEXTURE_CHURN_TRACE="1",
                       TJ_TEXTURE_SNAPSHOT_HINT_RETARGET="0")
            subprocess.run([str(exe_path)], env=env, check=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
