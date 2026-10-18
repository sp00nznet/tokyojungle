"""Compile production mapping publication and deferred writeback commit.

Reproduce LOCAL page discovery between preflight and commit. Exercise real
Win32 mapping locks; stub the guest-world rendezvous, not the commit checks.
This is host-only regression evidence, not GPU or gameplay proof.
"""
from pathlib import Path
import shutil
import subprocess
import tempfile

from test_fifo_present_boundary import function

ROOT = Path(__file__).resolve().parents[1]
VIDEO = ROOT.parent / "ps3recomp/libs/video"


def struct(text, name):
    end = text.index("} " + name + ";") + len("} " + name + ";")
    return text[text.rfind("typedef struct {", 0, end):end]


def main():
    gcm = (VIDEO / "cellGcmSys.c").read_text(encoding="utf-8")
    backend = (VIDEO / "rsx_d3d12_backend.c").read_text(encoding="utf-8")
    mapping = "\n".join(function(gcm, signature) for signature in (
        "u64 cellGcmMainMappingGeneration(",
        "static void cellGcmMainMappingWriteBeginLocked(",
        "static void cellGcmMainMappingWriteEndLocked(",
        "static void cellGcmMainMappingWriteBegin(",
        "static void cellGcmMainMappingWriteEnd(",
        "static void cellGcmMainMappingMarkLocalPage(",
        "static void populate_offset_table(",
        "static void clear_offset_table(",
        "static u16 cellGcmMainEaPage(",
        "u32 cellGcmResolveOffset(",
        "int cellGcmResolveLocatedChecked("))
    commit = "\n".join(function(backend, signature) for signature in (
        "static void exact_writeback_failure_set(",
        "static int guest_world_succeeded(",
        "static int exact_writeback_commit_all("))
    source = r'''
#include <windows.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <stdatomic.h>
#include "rsx_scaled_blit.h"
typedef uint8_t u8; typedef uint16_t u16;
typedef uint32_t u32; typedef uint64_t u64;
typedef void ID3D12Resource;
#define MAX_DRAWS 2
static u8 s_local_offset_page[1024];
static u16 s_io_address_table[65536],s_ea_address_table[65536];
static struct { u32 localAddress; } s_config = {0xC0000000u};
static atomic_ullong s_main_mapping_generation = ATOMIC_VAR_INIT(256);
static atomic_flag s_main_mapping_writer = ATOMIC_FLAG_INIT;
static SRWLOCK s_main_mapping_lock = SRWLOCK_INIT;
MAPPING
PLAN
FAILURE
typedef struct { u32 src_raw,dst_raw,src_ea,dst_ea; } RsxD3D12ExactBlit;
static struct { struct { RsxD3D12ExactBlit scaled_blit; } draws[MAX_DRAWS]; } s_d3d;
static ExactBlitWritebackPlan s_exact_writeback[MAX_DRAWS];
static u32 s_exact_writeback_count;
static int world, begins, ends, bumps, poison, begin_ok=1, end_ok=1;
static int guest_world_is_poisoned(void) { return poison; }
static int guest_world_exclusive_begin(void) { ++begins; world=begin_ok; return begin_ok; }
static int guest_world_exclusive_end(void) { ++ends; world=0; return end_ok; }
static unsigned checks, failures;
#define CHECK(c,m) do { ++checks; if(!(c)) { ++failures; printf("FAIL: %s\n",m); } } while(0)
static void spu_reservation_bump_locked(u32 line) {
    CHECK(world && !(line&127),"reservation invalidation requires exclusive aligned commit");
    ++bumps;
}
COMMIT
static u8 guest[2][32], baseline[2][32], packed[2][32], mapped_rgba[2][32];
static void prepare(void) {
    memset(guest,0x11,sizeof guest); memset(baseline,0x11,sizeof baseline);
    memset(packed[0],0x22,32); memset(packed[1],0x33,32);
    memset(s_exact_writeback,0,sizeof s_exact_writeback);
    s_exact_writeback_count=2;
    for(u32 i=0;i<2;i++) {
        ExactBlitWritebackPlan* p=&s_exact_writeback[i];
        p->record=i; p->guest_rows=guest[i]; p->baseline=baseline[i];
        p->packed_argb=packed[i]; p->mapping_generation=cellGcmMainMappingGeneration();
        p->guest_ea=0x0A063000u+i*128; p->guest_pitch=16; p->w=4; p->h=2;
    }
    begins=ends=bumps=world=poison=0; begin_ok=end_ok=1;
}
static void prepare_mapped(void) {
    prepare();
    for(u32 i=0;i<2;i++) {
        ExactBlitWritebackPlan* p=&s_exact_writeback[i];
        for(u32 j=0;j<sizeof mapped_rgba[i];j++) mapped_rgba[i][j]=(u8)(i*41u+j*7u+3u);
        p->mapped_commit=1; p->packed_argb=NULL; p->mapped_rgba=mapped_rgba[i];
        p->readback_mapped=1;
        p->readback=(ID3D12Resource*)1; p->readback_pitch=16; p->queued=1;
        for(u32 row=0;row<2;row++)
            rsx_exact_blit_rgba_to_guest_argb(packed[i]+row*16,mapped_rgba[i]+row*16,4);
    }
}
static void reject(const char* expected) {
    u8 before[sizeof guest]; memcpy(before,guest,sizeof guest);
    ExactWritebackFailure f={0};
    CHECK(exact_writeback_commit_all(&f)==-1,"unsafe commit must fail");
    CHECK(f.code && !strcmp(f.code,expected),"failure identifies actual cause");
    CHECK(!memcmp(before,guest,sizeof guest) && !bumps,"all plans preserved on rejection");
    CHECK(!world && ends==1,"rejection releases guest world");
}
int main(void) {
    memset(s_io_address_table,0xff,sizeof s_io_address_table);
    memset(s_ea_address_table,0xff,sizeof s_ea_address_table);
    populate_offset_table(0x09A00000,0x03D00000,0xB00000);
    prepare();
    u32 ea=0;
    CHECK(cellGcmResolveLocatedChecked(0,0x04363000,&ea) && ea==0x0A063000,
          "recorded destination resolves through MAIN mapping");
    /* Fifteen first-use LOCAL pages reproduce the observed old 256->286 drift.
     * Include the same raw page as MAIN to prove location-aware independence. */
    for(u32 page=0x43;page<0x52;page++) cellGcmMainMappingMarkLocalPage(page);
    CHECK(cellGcmMainMappingGeneration()==256,"LOCAL discovery preserves MAIN lifetime");
    CHECK(cellGcmResolveLocatedChecked(0,0x04363000,&ea) && ea==0x0A063000,
          "LOCAL marker never redirects explicit MAIN");
    CHECK(cellGcmResolveLocatedChecked(1,0x04363000,&ea) && ea==0xC4363000,
          "explicit LOCAL remains LOCAL");
    CHECK(cellGcmResolveOffset(0x04363000)==0xC4363000,
          "legacy unlocated resolver still honors LOCAL marker");
    ExactWritebackFailure f={0};
    CHECK(exact_writeback_commit_all(&f)==0 && !f.code,"pending writeback survives LOCAL discovery");
    CHECK(!memcmp(guest,packed,sizeof guest) && bumps==4,"all rows commit with reservation invalidation");
    CHECK(begins==1 && ends==1 && !world,"successful commit releases guest world");
    prepare_mapped();
    for(u32 page=0x43;page<0x52;page++) cellGcmMainMappingMarkLocalPage(page);
    f=(ExactWritebackFailure){0};
    CHECK(exact_writeback_commit_all(&f)==0 && !f.code,"mapped-view commit after LOCAL discovery");
    CHECK(!memcmp(guest,packed,sizeof guest) && bumps==4,"mapped rows convert and commit");
    CHECK(s_exact_writeback[0].mapped_rgba==mapped_rgba[0] &&
          s_exact_writeback[1].mapped_rgba==mapped_rgba[1],"mapped views remain available through commit");
    prepare_mapped(); s_exact_writeback[1].mapped_rgba=NULL;
    u8 storage_before[sizeof guest]; memcpy(storage_before,guest,sizeof guest);
    f=(ExactWritebackFailure){0};
    CHECK(exact_writeback_commit_all(&f)==-1 && f.code && !strcmp(f.code,"COMMIT_STORAGE"),
          "later mapped storage failure");
    CHECK(!begins && !ends && !bumps && !memcmp(storage_before,guest,sizeof guest),
          "storage prepass rejects before any plan writes");
    prepare_mapped(); s_exact_writeback[1].readback_mapped=0;
    memcpy(storage_before,guest,sizeof guest); f=(ExactWritebackFailure){0};
    CHECK(exact_writeback_commit_all(&f)==-1 && f.code && !strcmp(f.code,"COMMIT_STORAGE"),
          "mapped storage requires successful Map ownership");
    CHECK(!begins && !ends && !bumps && !memcmp(storage_before,guest,sizeof guest),
          "missing Map ownership rejects before all guest writes");
    u64 stable=cellGcmMainMappingGeneration();
    cellGcmMainMappingMarkLocalPage(0x43); cellGcmMainMappingMarkLocalPage(1024);
    CHECK(cellGcmMainMappingGeneration()==stable,"repeat/out-of-range LOCAL lookup is inert");
    prepare(); cellGcmMainMappingWriteBegin();
    CHECK(cellGcmMainMappingGeneration()&1,"MAIN writer publishes odd generation");
    reject("GEN"); cellGcmMainMappingWriteEnd();
    prepare(); cellGcmMainMappingWriteBegin();
    populate_offset_table(0x0B000000,0x04300000,0x100000);
    cellGcmMainMappingWriteEnd(); reject("GEN");
    CHECK(cellGcmResolveLocatedChecked(0,0x04363000,&ea) && ea==0x0B063000,
          "true remap changes destination");
    prepare(); cellGcmMainMappingWriteBegin();
    clear_offset_table(0x0B000000,0x04300000,0x100000);
    cellGcmMainMappingWriteEnd(); reject("GEN");
    CHECK(!cellGcmResolveLocatedChecked(0,0x04363000,&ea),"unmap invalidates translation");
    cellGcmMainMappingWriteBegin(); populate_offset_table(0x0B000000,0x04300000,0x100000);
    cellGcmMainMappingWriteEnd(); prepare();
    cellGcmMainMappingWriteBegin(); clear_offset_table(0x0B000000,0x04300000,0x100000);
    cellGcmMainMappingWriteEnd();
    cellGcmMainMappingWriteBegin(); populate_offset_table(0x0B000000,0x04300000,0x100000);
    cellGcmMainMappingWriteEnd(); reject("GEN");
    CHECK(cellGcmResolveLocatedChecked(0,0x04363000,&ea) && ea==0x0B063000,
          "same-address ABA still rejected despite equal endpoint");
    prepare(); cellGcmMainMappingWriteBegin();
    populate_offset_table(0x10000000,0x08000000,0x100000);
    cellGcmMainMappingWriteEnd(); reject("GEN");
    prepare_mapped(); s_exact_writeback[1].mapping_generation-=2; reject("GEN");
    prepare_mapped(); guest[1][31]^=1; reject("BASELINE");
    prepare(); guest[1][31]^=1; reject("BASELINE");
    prepare(); s_exact_writeback[1].mapping_generation-=2; reject("GEN");
    prepare_mapped(); poison=1; f=(ExactWritebackFailure){0};
    CHECK(exact_writeback_commit_all(&f)==-1 && !strcmp(f.code,"POISON") && !begins,
          "poison rejects before rendezvous");
    prepare_mapped(); begin_ok=0; f=(ExactWritebackFailure){0};
    CHECK(exact_writeback_commit_all(&f)==-1 && !strcmp(f.code,"BEGIN") && !ends && !bumps,
          "failed rendezvous cannot commit");
    prepare_mapped(); end_ok=0; f=(ExactWritebackFailure){0};
    CHECK(exact_writeback_commit_all(&f)==-1 && f.code && !strcmp(f.code,"END"),
          "mapped end failure is reported");
    CHECK(begins==1 && ends==1 && bumps==4 && !world && !memcmp(guest,packed,sizeof guest),
          "end failure preserves completed batch writes and closes exclusive");
    printf("MAIN mapping lifetime: %u checks, %u failures\n",checks,failures);
    return failures ? 1:0;
}
'''
    for key, value in {"MAPPING": mapping, "PLAN": struct(backend, "ExactBlitWritebackPlan"),
                       "FAILURE": struct(backend, "ExactWritebackFailure"), "COMMIT": commit}.items():
        source = source.replace(key + "\n", value + "\n")
    clang = shutil.which("clang") or r"C:\Program Files\LLVM\bin\clang.exe"
    with tempfile.TemporaryDirectory(prefix="tj-main-lifetime-") as temp:
        c, exe = Path(temp)/"lifetime.c", Path(temp)/"lifetime.exe"
        c.write_text(source, encoding="utf-8")
        subprocess.run([clang, "-std=c11", "-D_CRT_SECURE_NO_WARNINGS", "-I", str(VIDEO),
                        "-I", str(ROOT.parent/"ps3recomp/include"), str(c), "-o", str(exe)], check=True)
        return subprocess.run([str(exe)], check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
