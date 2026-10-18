"""Exercise production snapshot ownership and byte-verified repeated-source reuse."""
from pathlib import Path
import argparse
import os
import re
import shutil
import subprocess
import tempfile

from test_fifo_present_boundary import function
from bench_texture_snapshot_hash import churn_helpers, snapshot_hint_helpers

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", action="store_true")
    watch_modes = parser.add_mutually_exclusive_group()
    watch_modes.add_argument("--write-watch", action="store_true")
    watch_modes.add_argument("--audit-write-watch", action="store_true")
    parser.add_argument("--snapshot-retention", action="store_true",
                        help="append real pool ownership/Audit smoke checks")
    args = parser.parse_args()
    backend = (ROOT.parent / "ps3recomp/libs/video/rsx_d3d12_backend.c").read_text(encoding="utf-8")
    structure = re.search(r"typedef struct VPTexSnapshot \{.*?\} VPTexSnapshot;", backend, re.S).group()
    release = function(backend, "static void vp_texture_snapshot_release(")
    watch_mode = function(backend, "static int vp_texture_write_watch_mode(")
    stamp = function(backend, "static u64 vp_texture_write_stamp(")
    stamp, stamp_changes = re.subn(r"(\)\s*\{)", r"\1\n    ++write_stamp_calls;", stamp, count=1)
    assert stamp_changes == 1
    byte_equal = function(backend, "static int vp_snapshot_bytes_equal(")
    byte_equal, equal_changes = re.subn(r"(\)\s*\{)", r"\1\n    ++snapshot_equal_calls;", byte_equal, count=1)
    assert equal_changes == 1
    capture_impl = function(backend, "static int vp_capture_tex_snapshot_impl(")
    hash_reuse_threshold = re.search(
        r"(?m)^#define VP_TEX_SNAPSHOT_HASH_REUSE_MIN_BYTES .+$", backend
    )
    assert hash_reuse_threshold and hash_reuse_threshold.group().endswith("(16u * 1024u)")
    copy_call = "memcpy(snapshot->data, source, bytes);"
    assert capture_impl.count(copy_call) == 1
    capture_impl = capture_impl.replace(copy_call, "fixture_snapshot_copy(snapshot->data, source, bytes);")
    capture = (watch_mode + "\n" + stamp + "\n" + byte_equal + "\n" + capture_impl + "\n" +
               function(backend, "static int vp_capture_tex_snapshot("))
    assert "post_stamp == write_stamp" in capture_impl
    assert "snapshot->source_write_stamp = write_stamp;" in capture_impl
    # These are the previous non-owning assignment/reset operations. They
    # reproduce the cache-boundary defect without touching production source.
    legacy = args.baseline or "static void vp_texture_cache_set_source(" not in backend
    set_source = ("static void vp_texture_cache_set_source(VPTexSlot* slot, VPTexSnapshot* source)"
                  "{slot->source_snapshot=source;}" if legacy else
                  function(backend, "static void vp_texture_cache_set_source("))
    reset_usage = ("static void vp_texture_cache_reset_usage(void)"
                   "{for(int i=0;i<VP_TEX_SLOTS;++i){s_d3d.vp_tex[i].used=0;"
                   "s_d3d.vp_tex[i].source_snapshot=NULL;}}" if legacy else
                   function(backend, "static void vp_texture_cache_reset_usage("))
    trim = ("static void vp_texture_cache_trim_sources(u32 bytes){(void)bytes;}" if legacy else
            function(backend, "static void vp_texture_cache_trim_sources("))
    if not legacy:
        upload = function(backend, "static int vp_upload_tex_slot(")
        shutdown = function(backend, "void rsx_d3d12_backend_shutdown(")
        assert "vp_texture_cache_set_source(t, NULL);" in upload
        assert upload.index("vp_texture_cache_set_source(t, source_snapshot);") > upload.index("CreateShaderResourceView")
        assert "vp_texture_cache_set_source(&s_d3d.vp_tex[i], NULL);" in shutdown
        assert "s_d3d.vp_tex[_i].source_snapshot = NULL;" not in backend
        assert capture.index("vp_texture_cache_trim_sources(bytes);") < capture.index("calloc(1, sizeof(*snapshot))")
    source = r'''
#include <stdint.h>
#include <windows.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
static int suppress_next_write_watch;
static UINT WINAPI fixture_get_write_watch(DWORD flags,PVOID base,SIZE_T size,
    PVOID* addresses,ULONG_PTR* count,LPDWORD granularity)
{
    if(suppress_next_write_watch){
        SYSTEM_INFO info;GetSystemInfo(&info);
        *granularity=info.dwPageSize;*count=0;suppress_next_write_watch=0;
        return ERROR_SUCCESS;
    }
    return GetWriteWatch(flags,base,size,addresses,count,granularity);
}
#define GetWriteWatch fixture_get_write_watch
#include "rsx_bytes_equal.h"
#include "rsx_texture_write_watch.h"
#undef GetWriteWatch
typedef uint8_t u8;
typedef uint32_t u32;
typedef uint64_t u64;
static RsxTextureWriteWatch s_vp_write_watch;
static u64 s_vp_watch_hits,s_vp_watch_bytes,s_vp_watch_mismatches;
static unsigned write_stamp_calls,snapshot_equal_calls;
static int tj_gcm2d_backend_perf_enabled(void) { return 0; }
static u64 tj_gcm2d_backend_perf_now(void) { return 0; }
static u64 tj_gcm2d_backend_perf_us(u64 ticks) { return ticks; }
#define VP_TEX_SNAPSHOT_BUCKETS 1024u
#define VP_TEX_SNAPSHOT_HINTS 256u
#define VP_TEX_SNAPSHOT_HINT_WAYS 4u
#define VP_TEX_SNAPSHOT_MAX_BYTES 32768u
#define VP_TEX_SNAPSHOT_SINGLE_MAX_BYTES 32768u
#define VP_TEX_SNAPSHOT_RETAIN_MAX_BYTES (64ull * 1024ull * 1024ull)
#define VP_TEX_SNAPSHOT_RETAIN_MAX_ENTRIES 256u
HASH_REUSE_THRESHOLD
#define VP_TEX_SLOTS 96
STRUCTURE
typedef struct { VPTexSnapshot* source_snapshot; int used; } VPTexSlot;
static struct {
    u64 vp_tex_snapshot_bytes;
    VPTexSnapshot* vp_tex_snapshot_buckets[VP_TEX_SNAPSHOT_BUCKETS];
    VPTexSnapshot* vp_tex_snapshot_hints[VP_TEX_SNAPSHOT_HINTS];
    VPTexSnapshot* vp_tex_snapshot_pool[VP_TEX_SNAPSHOT_RETAIN_MAX_ENTRIES];
    u32 vp_tex_snapshot_pool_head, vp_tex_snapshot_pool_count;
    u64 vp_tex_snapshot_pool_bytes;
    int vp_tex_snapshot_pool_enabled;
    u32 vp_tex_snapshot_pool_suppress;
    VPTexSlot vp_tex[VP_TEX_SLOTS];
} s_d3d;
static u8* memory;
u8* vm_base;
u32 ppu_vm_size=65536;
static unsigned checks,failures,hash_calls;
static int force_hash_collision;
static int mutate_after_hash,mutate_after_copy,suppress_write_watch_after_hash;
static int vp_tex_source_span(u32 w,u32 h,u32 d,u32 fmt,u32 pitch,int cube,
    u32 dimension,u32 mips,int border,u32* bytes,void* unused)
{
    (void)fmt;(void)pitch;(void)cube;(void)dimension;(void)mips;(void)border;(void)unused;
    if(w==99) return -1;
    *bytes=w*h*d;return 0;
}
static u64 fixture_hash_bytes(const u8* data,u32 bytes)
{
    u64 hash=14695981039346656037ull;
    for(u32 i=0;i<bytes;++i)hash=(hash^data[i])*1099511628211ull;
    return hash;
}
static u64 vp_tex_source_hash(const u8* data,u32 bytes,u32 rows,u32 pitch,int contiguous)
{
    (void)rows;(void)pitch;(void)contiguous;++hash_calls;
    if(force_hash_collision)return 17;
    u64 hash=fixture_hash_bytes(data,bytes);
    if(mutate_after_hash){
        ((u8*)data)[bytes-1]^=0x40;mutate_after_hash=0;
        if(suppress_write_watch_after_hash){
            suppress_next_write_watch=1;suppress_write_watch_after_hash=0;
        }
    }
    return hash;
}
static void fixture_snapshot_copy(u8* dest,const u8* source,u32 bytes)
{
    memcpy(dest,source,bytes);
    if(mutate_after_copy){((u8*)source)[0]^=0x20;mutate_after_copy=0;}
}
static int vp_texture_snapshot_pool_admit(VPTexSnapshot* snapshot);
HINT
DISCARD
RELEASE
SET_SOURCE
RESET_USAGE
CHURN_HELPERS
static int reserved_slot_zero;
static int d3d12_prefix_owner_texture_reserved(u32 slot){return reserved_slot_zero&&slot==0;}
TRIM
CAPTURE
static void check(int ok,const char* label)
{++checks;if(!ok){++failures;printf("FAIL %s\n",label);}}
static VPTexSnapshot* take(u32 off,u32 bytes)
{
    VPTexSnapshot* snapshot=NULL;
    check(vp_capture_tex_snapshot(off,bytes,1,1,0,bytes,0,2,1,0,&snapshot)==0 && snapshot,
          "capture succeeds");
    return snapshot;
}
static void release_all(VPTexSnapshot** snapshots,u32 count)
{
    for(u32 i=0;i<count;++i)vp_texture_snapshot_release(&snapshots[i]);
    check(s_d3d.vp_tex_snapshot_bytes==0,"all source bytes released");
    for(u32 i=0;i<VP_TEX_SNAPSHOT_BUCKETS;++i)
        if(s_d3d.vp_tex_snapshot_buckets[i]){check(0,"content bucket dangling");break;}
    for(u32 i=0;i<VP_TEX_SNAPSHOT_HINTS;++i)
        if(s_d3d.vp_tex_snapshot_hints[i]){check(0,"source hint dangling");break;}
}
int main(void)
{
    memory=VirtualAlloc(NULL,65536,MEM_RESERVE|MEM_COMMIT|MEM_WRITE_WATCH,PAGE_READWRITE);
    check(memory!=NULL,"tracked guest fixture allocated");if(!memory)return 1;
    vm_base=memory;
    const int watch_mode=vp_texture_write_watch_mode();
    for(u32 i=0;i<65536;++i)memory[i]=(u8)((i*19u)^(i>>8));
    VPTexSnapshot* snapshots[600]={0};
    /* A cached GPU texture must keep real immutable ownership across batches.
     * The legacy reset clears this identity and necessarily reuploads it. */
    snapshots[0]=take(4096,257);
    vp_texture_cache_set_source(&s_d3d.vp_tex[0],snapshots[0]);
    vp_texture_snapshot_release(&snapshots[0]);
    vp_texture_cache_reset_usage();
    check(s_d3d.vp_tex[0].source_snapshot!=NULL,"batch boundary retains owned cache identity");
    if(failures) return 1; /* legacy identity was freed; never dereference it */
    unsigned cached_hash_calls=hash_calls;
    VPTexSnapshot* cached=s_d3d.vp_tex[0].source_snapshot;
    for(u32 i=0;i<200;++i){
        snapshots[0]=take(4096,257);
        check(snapshots[0]==cached && snapshots[0]->refs==2,
              "fresh byte check finds retained cross-batch identity");
        vp_texture_cache_set_source(&s_d3d.vp_tex[0],snapshots[0]);
        vp_texture_snapshot_release(&snapshots[0]);
        s_d3d.vp_tex[0].used=1;vp_texture_cache_reset_usage();
        check(!s_d3d.vp_tex[0].used && cached->refs==1,"cache hit neither leaks references nor pins usage");
    }
    check(hash_calls==cached_hash_calls,"unchanged batches avoid hash allocation and recopy");
    snapshots[0]=take(4096,257);
    memory[4096+256]^=1;snapshots[1]=take(4096,257);
    vp_texture_cache_set_source(&s_d3d.vp_tex[0],snapshots[1]);
    check(snapshots[0]->data[256]!=snapshots[1]->data[256] && snapshots[0]->refs==1 &&
          snapshots[1]->refs==2,"cache replacement preserves older live draw bytes");
    vp_texture_cache_set_source(&s_d3d.vp_tex[0],NULL);
    release_all(snapshots,2);

    /* Shared cache owners may outlive all records. Pressure must release all
     * cache references without removing references owned by active records. */
    snapshots[0]=take(4096,257);
    vp_texture_cache_set_source(&s_d3d.vp_tex[0],snapshots[0]);
    vp_texture_cache_set_source(&s_d3d.vp_tex[1],snapshots[0]);
    s_d3d.vp_tex[0].used=s_d3d.vp_tex[1].used=1;
    vp_texture_cache_trim_sources(VP_TEX_SNAPSHOT_MAX_BYTES);
    check(snapshots[0]->refs==1 && s_d3d.vp_tex_snapshot_bytes==257 &&
          !s_d3d.vp_tex[0].source_snapshot && !s_d3d.vp_tex[1].source_snapshot,
          "pressure drops only cache ownership even for currently used slots");
    vp_texture_cache_set_source(&s_d3d.vp_tex[0],snapshots[0]);
    vp_texture_cache_set_source(&s_d3d.vp_tex[1],snapshots[0]);
    vp_texture_snapshot_release(&snapshots[0]);
    vp_texture_cache_trim_sources(VP_TEX_SNAPSHOT_MAX_BYTES);
    release_all(snapshots,1);

    snapshots[0]=take(4096,257);
    vp_texture_cache_set_source(&s_d3d.vp_tex[0],snapshots[0]);
    vp_texture_cache_set_source(&s_d3d.vp_tex[1],snapshots[0]);
    reserved_slot_zero=1;
    vp_texture_cache_trim_sources(VP_TEX_SNAPSHOT_MAX_BYTES);
    check(s_d3d.vp_tex[0].source_snapshot==snapshots[0]&&
          !s_d3d.vp_tex[1].source_snapshot&&snapshots[0]->refs==2,
          "budget pressure preserves retained cache version while trimming unreserved identity");
    reserved_slot_zero=0;
    vp_texture_cache_trim_sources(VP_TEX_SNAPSHOT_MAX_BYTES);
    check(!s_d3d.vp_tex[0].source_snapshot&&snapshots[0]->refs==1,
          "retired version is available for budget trimming");
    release_all(snapshots,1);

    for(u32 i=0;i<8;++i){
        memset(memory+i*4096,(int)i,4096);snapshots[i]=take(i*4096,4096);
    }
    vp_texture_cache_set_source(&s_d3d.vp_tex[0],snapshots[0]);
    vp_texture_snapshot_release(&snapshots[0]);
    memset(memory+32768,23,4096);snapshots[8]=take(32768,4096);
    check(!s_d3d.vp_tex[0].source_snapshot && s_d3d.vp_tex_snapshot_bytes==VP_TEX_SNAPSHOT_MAX_BYTES,
          "new source evicts cache-only bytes before original budget rejection");
    for(u32 i=1;i<8;++i)
        check(snapshots[i]->refs==1 && snapshots[i]->data[0]==i,"pressure preserves active draw ownership");
    release_all(snapshots,9);
    for(u32 i=0;i<65536;++i)memory[i]=(u8)((i*19u)^(i>>8));
    snapshots[0]=take(4096,257);
    unsigned before=hash_calls;
    for(u32 i=1;i<9;++i)snapshots[i]=take(4096,257);
    check(hash_calls==before,"unchanged repeated source avoids duplicate full hash");
    check(snapshots[0]==snapshots[8] && snapshots[0]->refs==9,"repeated source retains shared ownership");
    u8 original=memory[4096+256];memory[4096+256]^=1;
    snapshots[9]=take(4096,257);
    check(snapshots[9]!=snapshots[0] && snapshots[0]->data[256]==original &&
          snapshots[9]->data[256]==memory[4096+256],"last-byte write gets immutable new snapshot");
    before=hash_calls;snapshots[10]=take(4096,257);
    check(hash_calls==before && snapshots[10]==snapshots[9],"new contents become byte-verified reuse candidate");
    vp_texture_snapshot_release(&snapshots[0]);
    snapshots[11]=take(4096,257);
    check(snapshots[11]==snapshots[9],"older release preserves live candidate");
    memcpy(memory+8192,memory+4096,257);
    snapshots[12]=take(8192,257);
    check(snapshots[12]==snapshots[9],"cross-address content dedup retained");
    snapshots[13]=take(4096,128);
    check(snapshots[13]!=snapshots[9] && snapshots[13]->bytes==128,"same address different span kept separate");
    release_all(snapshots,14);
    snapshots[0]=take(4096,257);
    check(snapshots[0] && !memcmp(snapshots[0]->data,memory+4096,257),"source can be recaptured after last release");
    release_all(snapshots,1);

    force_hash_collision=1;
    snapshots[0]=take(4096,257);memory[4096+128]^=1;
    snapshots[1]=take(4096,257);
    check(snapshots[0]!=snapshots[1],"hash collision still requires complete byte equality");
    release_all(snapshots,2);force_hash_collision=0;

    /* More live addresses than hint slots guarantee replacement collisions. */
    for(u32 i=0;i<300;++i){memory[i*32]=(u8)i;memory[i*32+1]=(u8)(i>>8);snapshots[i]=take(i*32,16);}
    for(u32 i=0;i<300;++i){
        VPTexSnapshot* current=take(i*32,16);
        check(!memcmp(current->data,memory+i*32,16),"address collision preserves exact content");
        vp_texture_snapshot_release(&snapshots[i]);vp_texture_snapshot_release(&current);
    }
    release_all(snapshots,300);
    for(u32 i=0;i<8;++i){
        memset(memory+i*4096,(int)i,4096);
        snapshots[i]=take(i*4096,4096);
    }
    check(s_d3d.vp_tex_snapshot_bytes==VP_TEX_SNAPSHOT_MAX_BYTES,"fixture fills snapshot budget");
    VPTexSnapshot* at_capacity=NULL;
    int capacity_result=vp_capture_tex_snapshot(0,4096,1,1,0,4096,0,2,1,0,&at_capacity);
    check(capacity_result==0 && at_capacity==snapshots[0],"existing snapshot reusable at allocation budget");
    vp_texture_snapshot_release(&at_capacity);
    memcpy(memory+32768,memory,4096);
    capacity_result=vp_capture_tex_snapshot(32768,4096,1,1,0,4096,0,2,1,0,&at_capacity);
    check(capacity_result==0 && at_capacity==snapshots[0],"content dedup reusable at allocation budget");
    vp_texture_snapshot_release(&at_capacity);
    memory[32768]^=1;
    check(vp_capture_tex_snapshot(32768,4096,1,1,0,4096,0,2,1,0,&at_capacity)==-1 && !at_capacity,
          "new allocation still rejected at full budget");
    release_all(snapshots,8);
    /* A nested texture can consume the parent's page dirty notification.
     * The shared page epoch must still invalidate that parent's old bytes. */
    snapshots[0]=take(5120,512);snapshots[1]=take(5248,128);
    memory[5420]^=1;
    snapshots[2]=take(5248,128);
    check(snapshots[1]==snapshots[2],"nested unchanged bytes remain reusable");
    snapshots[3]=take(5120,512);
    check(snapshots[0]!=snapshots[3] && snapshots[3]->data[300]==memory[5420],
          "overlapping observer cannot hide parent mutation");
    release_all(snapshots,4);
    before=hash_calls;mutate_after_hash=1;snapshots[0]=take(4096,257);
    check(hash_calls-before==2 &&
          snapshots[0]->hash==fixture_hash_bytes(snapshots[0]->data,257),
          "lookup-hash mutation falls back to the exact copied-byte digest");
    before=hash_calls;unsigned equal_before=snapshot_equal_calls;
    snapshots[1]=take(4096,257);
    check(snapshots[1]==snapshots[0] && snapshot_equal_calls>equal_before,
          "pre-copy stamp forces byte comparison after lookup-hash mutation");
    check(hash_calls==before,"byte-equal follow-up reuses without rehashing");
    release_all(snapshots,2);

    if(watch_mode==1){
        memory[4096]^=0x11;
        before=hash_calls;unsigned stamps_before=write_stamp_calls;
        snapshots[0]=take(4096,257);
        check(hash_calls-before==2,"4 KiB tracked miss hashes source and owned copy below the cutoff");
        check(write_stamp_calls-stamps_before==1,"4 KiB tracked miss uses only its pre-copy stamp");
        check(snapshots[0]->hash==fixture_hash_bytes(snapshots[0]->data,257),
              "small tracked miss stores the digest of its owned bytes");
        release_all(snapshots,1);

        const u32 large_bytes=VP_TEX_SNAPSHOT_HASH_REUSE_MIN_BYTES;
        for(u32 i=0;i<large_bytes;++i)memory[16384+i]=(u8)(i*7u+3u);
        before=hash_calls;stamps_before=write_stamp_calls;
        snapshots[0]=take(16384,large_bytes);
        check(hash_calls-before==1,"16 KiB stable tracked miss reuses lookup hash");
        check(write_stamp_calls-stamps_before==2,"16 KiB tracked miss brackets copy with both stamps");
        check(snapshots[0]->hash==fixture_hash_bytes(snapshots[0]->data,large_bytes),
              "large stable miss stores the digest of its owned bytes");
        release_all(snapshots,1);

        for(u32 i=0;i<large_bytes;++i)memory[16384+i]=(u8)(i*11u+5u);
        before=hash_calls;mutate_after_copy=1;snapshots[0]=take(16384,large_bytes);
        check(hash_calls-before==2,"post-copy write at threshold falls back to hashing owned bytes");
        check(snapshots[0]->data[0]!=(u8)memory[16384],
              "copy hook mutates the source only after the snapshot copy");
        const u64 copied_stamp=snapshots[0]->source_write_stamp;
        equal_before=snapshot_equal_calls;before=hash_calls;
        snapshots[1]=take(16384,large_bytes);
        check(snapshots[1]!=snapshots[0] && snapshot_equal_calls>equal_before,
              "post-copy mutation keeps the pre-copy stamp so next capture compares bytes");
        check(snapshots[1]->source_write_stamp!=copied_stamp &&
              !memcmp(snapshots[1]->data,memory+16384,large_bytes),
              "next capture publishes the changed source version");
        check(hash_calls-before==1,"changed source hashes once after byte comparison");
        release_all(snapshots,2);
    }else if(watch_mode==2){
        memory[4096]^=0x31;
        before=hash_calls;unsigned stamps_before=write_stamp_calls;
        u64 mismatch_before=s_vp_watch_mismatches;
        snapshots[0]=take(4096,257);
        check(hash_calls-before==2,"audit mode hashes the actual owned copy");
        check(write_stamp_calls-stamps_before==2,"small audit mode retains its post-copy stamp");
        check(s_vp_watch_mismatches==mismatch_before && s_vp_write_watch.state==1,
              "clean audit leaves write tracking enabled");
        release_all(snapshots,1);

        for(u32 i=0;i<257;++i)memory[4096+i]=(u8)(i*13u+9u);
        before=hash_calls;mismatch_before=s_vp_watch_mismatches;
        mutate_after_hash=1;suppress_write_watch_after_hash=1;
        snapshots[0]=take(4096,257);
        check(hash_calls-before==2,"audit mismatch hashes source and copied bytes");
        check(s_vp_watch_mismatches==mismatch_before+1 && s_vp_write_watch.state<0,
              "audit detects a missed dirty report and disables write tracking");
        check(snapshots[0]->hash==fixture_hash_bytes(snapshots[0]->data,257),
              "audit mismatch retains the copied-byte digest");
        release_all(snapshots,1);
    }else{
        memory[4096]^=0x51;
        before=hash_calls;snapshots[0]=take(4096,257);
        check(hash_calls-before==2,"write-watch OFF hashes source and owned copy");
        check(snapshots[0]->source_write_stamp==0 &&
              snapshots[0]->hash==fixture_hash_bytes(snapshots[0]->data,257),
              "write-watch OFF retains copied-byte identity");
        release_all(snapshots,1);
    }

    if(watch_mode){
        if(s_vp_write_watch.state>=0)
            rsx_texture_write_watch_fail(&s_vp_write_watch,ERROR_INVALID_DATA);
        memory[8192]^=0x63;
        before=hash_calls;snapshots[0]=take(8192,257);
        check(hash_calls-before==2 && snapshots[0]->source_write_stamp==0,
              "failed write tracking falls back to the owned-byte hash");
        check(s_vp_write_watch.state<0,"write-watch failure remains disabled");
        release_all(snapshots,1);
    }
    VPTexSnapshot* rejected=(VPTexSnapshot*)(uintptr_t)1;
    before=hash_calls;
    check(vp_capture_tex_snapshot(65530,16,1,1,0,16,0,2,1,0,&rejected)==-1 &&
          !rejected && hash_calls==before,"out-of-range rejected before source access");
    check(vp_capture_tex_snapshot(0,99,1,1,0,99,0,2,1,0,&rejected)==0 && !rejected,
          "unsupported span retains null path");
    check(vp_capture_tex_snapshot(0,32769,1,1,0,32769,0,2,1,0,&rejected)==-1 && !rejected,
          "single-snapshot limit unchanged");
    check(vp_capture_tex_snapshot(0,0,1,1,0,0,0,2,1,0,&rejected)==0 && !rejected,
          "empty dimensions retain null path");
    if(watch_mode) {
        check(s_vp_watch_hits>0 &&
              ((watch_mode==1 && !s_vp_watch_mismatches) ||
               (watch_mode==2 && s_vp_watch_mismatches==1)),
              "tracked reuse and expected audit outcome recorded");
        printf("Write-watch clean hits=%llu bytes=%llu\n",(unsigned long long)s_vp_watch_hits,
               (unsigned long long)s_vp_watch_bytes);
    }
    if(getenv("TJ_TEXTURE_SNAPSHOT_RETAIN") &&
       !strcmp(getenv("TJ_TEXTURE_SNAPSHOT_RETAIN"),"1")) {
        /* Restart real OS tracking after the earlier deliberate failure cases. */
        rsx_texture_write_watch_destroy(&s_vp_write_watch);
        s_vp_watch_hits=s_vp_watch_bytes=s_vp_watch_mismatches=0;
        vp_texture_snapshot_pool_init();
        VPTexSnapshot* borrowed=take(4096,8192);
        VPTexSnapshot* retained=borrowed;
        vp_texture_snapshot_release(&borrowed);
        check(!borrowed && retained->refs==1 && retained->pool_retained &&
              s_d3d.vp_tex_snapshot_pool_count==1 &&
              s_d3d.vp_tex_snapshot_bytes==8192,"last ref transfers into real pool owner");
        before=hash_calls;unsigned equal_before=snapshot_equal_calls;
        borrowed=take(4096,8192);
        check(borrowed==retained && borrowed->refs==2 && hash_calls==before,
              "unchanged pooled identity reacquired without hash or allocation");
        if(watch_mode) check(s_vp_write_watch.state==1 && s_vp_watch_hits>0,
                             "retention uses actual tracked Windows pages");
        if(watch_mode==2) check(snapshot_equal_calls>equal_before,
                               "pooled Audit reuse compares owned bytes");
        vp_texture_snapshot_release(&borrowed);
        memory[4096]^=0x3D;
        borrowed=take(4096,8192);
        check(borrowed!=retained && !memcmp(borrowed->data,memory+4096,8192),
              "changed tracked source never reuses stale pool bytes");
        vp_texture_snapshot_release(&borrowed);
        borrowed=take(32768,24576);
        check(s_d3d.vp_tex_snapshot_pool_count==1 &&
              s_d3d.vp_tex_snapshot_pool_bytes==8192 &&
              s_d3d.vp_tex_snapshot_bytes==32768,"pool reclaims enough space before shared budget rejection");
        vp_texture_cache_set_source(&s_d3d.vp_tex[0],borrowed);
        vp_texture_snapshot_release(&borrowed);
        vp_texture_snapshot_pool_drain(VP_TEX_SNAPSHOT_POOL_EVICT_SHUTDOWN);
        check(s_d3d.vp_tex[0].source_snapshot &&
              !memcmp(s_d3d.vp_tex[0].source_snapshot->data,memory+32768,24576),
              "pool drain preserves active cache owner");
        vp_texture_cache_set_source(&s_d3d.vp_tex[0],NULL);
        vp_texture_snapshot_pool_drain(VP_TEX_SNAPSHOT_POOL_EVICT_SHUTDOWN);
        check(!s_d3d.vp_tex_snapshot_pool_count && !s_d3d.vp_tex_snapshot_bytes,
              "pool drain releases all retired allocation bytes");
        for(u32 i=0;i<257;++i) {
            const u32 off=512+i*128;
            memcpy(memory+off,&i,sizeof(i));
            borrowed=take(off,64);
            vp_texture_snapshot_release(&borrowed);
        }
        check(s_d3d.vp_tex_snapshot_pool_count==256 &&
              s_d3d.vp_tex_snapshot_pool_bytes==256*64 &&
              s_d3d.vp_tex_snapshot_bytes==256*64,"actual FIFO node cap evicts oldest owner");
        check(!s_vp_watch_mismatches,"valid retention has zero Audit/copy mismatches");
        vp_texture_snapshot_pool_drain(VP_TEX_SNAPSHOT_POOL_EVICT_SHUTDOWN);
        s_d3d.vp_tex_snapshot_pool_enabled=0;
        release_all(snapshots,0);
    }
    rsx_texture_write_watch_destroy(&s_vp_write_watch);
    VirtualFree(memory,0,MEM_RELEASE);
    printf("Texture snapshot reuse: %u checks, %u failures\n",checks,failures);
    return failures?1:0;
}
'''.replace("STRUCTURE\n", structure + "\n").replace("HINT\n", snapshot_hint_helpers(backend) + "\n").replace("DISCARD\n", function(backend, "static void vp_texture_snapshot_discard(") + "\n").replace("RELEASE\n", release + "\n").replace("SET_SOURCE\n", set_source + "\n").replace("RESET_USAGE\n", reset_usage + "\n").replace("CHURN_HELPERS\n", churn_helpers(backend) + "\n").replace("TRIM\n", trim + "\n").replace("CAPTURE\n", capture + "\n").replace("HASH_REUSE_THRESHOLD\n", hash_reuse_threshold.group() + "\n")
    clang = shutil.which("clang") or r"C:\Program Files\LLVM\bin\clang.exe"
    with tempfile.TemporaryDirectory(prefix="tj-snapshot-reuse-") as temp:
        path, exe = Path(temp) / "snapshot.c", Path(temp) / "snapshot.exe"
        path.write_text(source, encoding="utf-8")
        subprocess.run([clang, "-std=c11", "-O2", "-D_CRT_SECURE_NO_WARNINGS", "-I",
                        str(ROOT.parent / "ps3recomp/libs/video"), str(path), "-o", str(exe)], check=True)
        mode="2" if args.audit_write_watch else "1" if args.write_watch else "0"
        env=dict(os.environ,TJ_TEXTURE_WRITE_WATCH=mode,
                 TJ_TEXTURE_SNAPSHOT_RETAIN="1" if args.snapshot_retention else "0",
                 TJ_TEXTURE_SNAPSHOT_HINT_RETARGET="0")
        return subprocess.run([str(exe)], env=env, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
