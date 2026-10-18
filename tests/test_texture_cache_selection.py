"""Compile the production texture-slot scan and preserve its bounded policy."""
from pathlib import Path
import os
import shutil
import subprocess
import tempfile

from test_fifo_present_boundary import function

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT.parent / "ps3recomp/libs/video/rsx_d3d12_backend.c"


def test_texture_cache_selection():
    backend = BACKEND.read_text(encoding="utf-8")
    upload = function(backend, "static int vp_upload_tex_slot(")
    reset_usage = function(backend, "static void vp_texture_cache_reset_usage(")
    selection_start = upload.index("    const int churn_trace = vp_texture_churn_trace_enabled();")
    selection = upload[selection_start:upload.index("    D3D12_RESOURCE_DESC td", selection_start)]

    source = r'''
#include <stdint.h>
#include <stdio.h>
#include <string.h>
typedef uint8_t u8; typedef uint32_t u32; typedef uint64_t u64;
typedef struct { u8* data; u32 bytes,refs; u64 hash; } VPTexSnapshot;
typedef enum VPTexChurnProbeResult {
    VP_TEX_CHURN_PROBE_NONE=0, VP_TEX_CHURN_PROBE_EQUAL=1,
    VP_TEX_CHURN_PROBE_DIFFERENT=2, VP_TEX_CHURN_PROBE_CAPPED=3
} VPTexChurnProbeResult;
typedef struct {
    int used; void *res,*up;
    u32 off,w,h,depth,fmt,pitch,mips,cube,dimension,has_border,dxgi;
    u64 source_hash; VPTexSnapshot* source_snapshot;
} VPTexSlot;
#define VP_TEX_SLOTS 4
static struct { VPTexSlot vp_tex[VP_TEX_SLOTS]; } s_d3d;
static VPTexSnapshot current,stale;
static unsigned hits,checks,failures;
static unsigned reserved_mask,captures;
static u32 request_off=4096;
static u64 request_hash=123;
static VPTexSnapshot* request_snapshot=&current;
static int vp_texture_churn_trace_enabled(void){return 0;}
static VPTexChurnProbeResult vp_texture_churn_probe_snapshot_rows(
        const VPTexSnapshot* old_snapshot,const VPTexSnapshot* new_snapshot,
        u32 row_bytes,u32 rows,u32 pitch,u64 required_span) {
    (void)old_snapshot;(void)new_snapshot;(void)row_bytes;(void)rows;
    (void)pitch;(void)required_span;return VP_TEX_CHURN_PROBE_NONE;
}
static void vp_texture_churn_record_upload_candidates(int a,int b,int c){(void)a;(void)b;(void)c;}
static int d3d12_prefix_owner_texture_reserved(u32 slot){return (reserved_mask>>slot)&1;}
static int d3d12_prefix_owner_texture_capture(u32 slot){(void)slot;++captures;return 0;}
static int d3d12_prefix_owner_texture_mutable(u32 slot){return d3d12_prefix_owner_texture_reserved(slot)?-1:0;}
static void vp_tex_cache_trace(int hit) { hits+=hit; }
RESET_USAGE
static int select_slot(void) {
    u32 off=request_off,w=64,h=32,depth=1,fmt=0x85,src_pitch=256,mip_count=1;
    u32 cube=0,dimension=2,has_border=0,dxfmt=87;
    u32 blkrow=256,blkrows=32,total_source_span=8192;
    u64 source_hash=request_hash;
    VPTexSnapshot* source_snapshot=request_snapshot;
    /* Diagnostic shape classification is outside this selector fixture. */
    const int churn_plain_linear_shape=0,churn_plain_linear_padded=0;
SELECTION
    (void)t;
    return slot; /* Continuing here means footprint/upload preparation occurs. */
}
#define CHECK(c,m) do { ++checks; if(!(c)){++failures;printf("FAIL: %s\n",m);} } while(0)
static VPTexSlot exact(void) {
    VPTexSlot s={0}; s.res=s.up=(void*)1; s.off=4096;s.w=64;s.h=32;
    s.depth=1;s.fmt=0x85;s.pitch=256;s.mips=1;s.dimension=2;s.dxgi=87;
    s.source_hash=123;s.source_snapshot=&current;return s;
}
static void reset(void) {
    memset(&s_d3d,0,sizeof s_d3d);hits=reserved_mask=captures=0;
    request_off=4096;request_hash=123;request_snapshot=&current;
    vp_texture_cache_reset_usage();
}
static void run_cases(void) {
    reset();s_d3d.vp_tex[2]=exact();
    CHECK(select_slot()==2 && hits==1 && s_d3d.vp_tex[2].used,
          "unused exact content returns before footprint/upload work");
    reset();s_d3d.vp_tex[0]=exact();s_d3d.vp_tex[0].source_snapshot=&stale;
    s_d3d.vp_tex[2]=exact();
    CHECK(select_slot()==2 && hits==1 && !s_d3d.vp_tex[0].used,
          "later correct identity beats earlier stale same-address slot");
    reset();s_d3d.vp_tex[2]=exact();s_d3d.vp_tex[2].used=1;
    CHECK(select_slot()==2 && hits==1,"already-used exact content remains reusable");
    for(unsigned field=0;field<15;field++) {
        reset();s_d3d.vp_tex[0]=exact();VPTexSlot* s=&s_d3d.vp_tex[0];
        switch(field) {
        case 0:s->off++;break;case 1:s->w++;break;case 2:s->h++;break;
        case 3:s->depth++;break;case 4:s->fmt++;break;case 5:s->pitch++;break;
        case 6:s->mips++;break;case 7:s->cube++;break;case 8:s->dimension++;break;
        case 9:s->has_border++;break;case 10:s->source_hash++;break;
        case 11:s->source_snapshot=&stale;break;case 12:s->res=NULL;break;
        case 13:s->up=NULL;break;case 14:s->dxgi++;break;
        }
        select_slot();CHECK(hits==0,"changed metadata/content/resource must miss");
    }
    reset();s_d3d.vp_tex[0]=exact();s_d3d.vp_tex[0].used=1;
    s_d3d.vp_tex[0].source_snapshot=&stale;
    CHECK(select_slot()==1 && !hits,"live stale slot is never overwritten");
    reset();s_d3d.vp_tex[2]=exact();s_d3d.vp_tex[2].source_snapshot=&stale;
    CHECK(select_slot()==2 && !hits,"unused matching layout retained on content miss");
    reset();s_d3d.vp_tex[2]=exact();s_d3d.vp_tex[2].off+=8192;
    CHECK(select_slot()==2 && !hits && !s_d3d.vp_tex[2].used,
          "different-address matching storage is selected for a fresh upload");
    reset();s_d3d.vp_tex[0]=exact();s_d3d.vp_tex[0].w*=2;
    s_d3d.vp_tex[2]=exact();s_d3d.vp_tex[2].off+=8192;
    CHECK(select_slot()==2 && !hits,
          "matching storage beats an earlier incompatible allocation");
    reset();s_d3d.vp_tex[0]=exact();s_d3d.vp_tex[0].off+=8192;
    s_d3d.vp_tex[2]=exact();s_d3d.vp_tex[2].source_snapshot=&stale;
    CHECK(select_slot()==2 && !hits,"same-address matching storage retains priority");
    reset();s_d3d.vp_tex[0]=exact();s_d3d.vp_tex[0].off+=8192;
    s_d3d.vp_tex[1]=exact();s_d3d.vp_tex[1].source_snapshot=&stale;
    s_d3d.vp_tex[3]=exact();
    CHECK(select_slot()==3 && hits==1 && s_d3d.vp_tex[3].used,
          "exact immutable content beats both storage reuse candidates");
    reset();s_d3d.vp_tex[2]=exact();s_d3d.vp_tex[2].off+=8192;
    s_d3d.vp_tex[2].used=1;
    CHECK(select_slot()==0 && !hits,"in-flight matching storage is never selected");
    reset();s_d3d.vp_tex[0]=exact();s_d3d.vp_tex[0].w*=2;
    CHECK(select_slot()==1 && !hits,
          "empty storage beats destroying an unrelated cached allocation");
    reset();for(unsigned i=0;i<3;i++) {
        s_d3d.vp_tex[i]=exact();s_d3d.vp_tex[i].w*=2;
    }
    CHECK(select_slot()==3 && !hits,"later empty storage preserves earlier allocations");
    reset();for(unsigned i=0;i<VP_TEX_SLOTS;i++) {
        s_d3d.vp_tex[i]=exact();s_d3d.vp_tex[i].w*=2;
    }
    CHECK(select_slot()==0 && !hits,"full incompatible cache retains bounded replacement");
    reset();s_d3d.vp_tex[0]=exact();s_d3d.vp_tex[0].w*=2;
    s_d3d.vp_tex[2]=exact();s_d3d.vp_tex[2].off+=8192;
    CHECK(select_slot()==2 && !hits,"matching storage still beats empty storage");
    for(unsigned field=0;field<12;field++) {
        reset();s_d3d.vp_tex[2]=exact();VPTexSlot* s=&s_d3d.vp_tex[2];
        s->off+=8192;
        switch(field) {
        case 0:s->w++;break;case 1:s->h++;break;case 2:s->depth++;break;
        case 3:s->fmt++;break;case 4:s->pitch++;break;case 5:s->mips++;break;
        case 6:s->cube++;break;case 7:s->dimension++;break;
        case 8:s->has_border++;break;case 9:s->res=NULL;break;
        case 10:s->up=NULL;break;case 11:s->dxgi++;break;
        }
        CHECK(select_slot()==0 && !hits,
              "incompatible or incomplete storage is not preferred over a free slot");
    }
    reset();for(unsigned i=0;i<VP_TEX_SLOTS;i++) {
        s_d3d.vp_tex[i]=exact();s_d3d.vp_tex[i].used=1;
        s_d3d.vp_tex[i].source_snapshot=&stale;
    }
    CHECK(select_slot()==-1 && !hits,"exhausted live slots still reject");
    reset();s_d3d.vp_tex[0]=exact();s_d3d.vp_tex[0].source_snapshot=&stale;reserved_mask=1;
    CHECK(select_slot()==1&&!hits&&!captures,"retained unused version was selected for overwrite");
    reset();s_d3d.vp_tex[0]=exact();reserved_mask=1;
    CHECK(select_slot()==0&&hits==1&&captures==1,"identical retained version cannot be shared for reading");
    reset();reserved_mask=(1u<<VP_TEX_SLOTS)-1;
    CHECK(select_slot()==-1&&!captures,"all retained slots did not exhaust safely");
}
int main(void) {
    run_cases();
    printf("Texture slot selection: %u checks, %u failures\n",checks,failures);
    return failures?1:0;
}
'''
    source = source.replace("RESET_USAGE", reset_usage)
    source = source.replace("SELECTION", selection)

    clang = shutil.which("clang") or r"C:\Program Files\LLVM\bin\clang.exe"
    with tempfile.TemporaryDirectory(prefix="tj-texture-selection-") as temp:
        c, exe = Path(temp) / "selection.c", Path(temp) / "selection.exe"
        c.write_text(source, encoding="utf-8")
        subprocess.run([clang, "-std=c11", "-O2", str(c), "-o", str(exe)], check=True)
        env = os.environ.copy()
        env.pop("TJ_TEXTURE_CACHE_FILL_EMPTY", None)
        subprocess.run([str(exe)], check=True, env=env)


if __name__ == "__main__":
    test_texture_cache_selection()
