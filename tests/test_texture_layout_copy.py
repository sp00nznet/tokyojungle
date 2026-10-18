"""Differential proof and CPU benchmark for the 2D swizzle-log hoist.

Compiles the exact production conversion helpers at -O3. The pre-hoist copy
helper is reconstructed from the current function and checked against its
captured source hash before it is used as the baseline.
"""
from hashlib import sha256
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
VIDEO = ROOT.parent / "ps3recomp" / "libs" / "video"
BACKEND = VIDEO / "rsx_d3d12_backend.c"
PRE_HOIST_RENDERER_SHA256 = "B20BD53381FA65D34B55FB8E655936C95E06A5B24C62C4E822402753F2930B4F"
BASELINE_HELPER_SHA256 = "04D5BA6786A3CD65C4650DB71D4B61C274251A0E03B587C226AADB2242155815"
CANDIDATE_HELPER_SHA256 = "B223443C2DCE3D48FCA1068750AB0C09BC1E00E400ABC1C1FC584B331D2AC632"


def extract(text, marker):
    start = text.index(marker)
    brace = text.index("{", start)
    depth, end = 1, brace + 1
    while depth:
        depth += (text[end] == "{") - (text[end] == "}")
        end += 1
    return text[start:end]


def digest(text):
    return sha256(text.encode("utf-8")).hexdigest().upper()


def source_helpers(text):
    markers = [
        ("swz2", "static inline u32 rsx_swz_off("),
        ("swz3", "static inline u32 rsx_swz3_off("),
        ("log2", "static inline u32 rsx_log2u("),
        ("format_struct", "typedef struct VPTexFormatInfo {"),
        ("format_info", "static int vp_tex_format_info("),
        ("row_checked", "static int vp_tex_source_row_bytes_checked("),
        ("row_bytes", "static u32 vp_tex_source_row_bytes("),
        ("rows_checked", "static int vp_tex_source_rows_checked("),
        ("rows", "static u32 vp_tex_source_rows("),
        ("be16", "static inline u16 vp_tex_be16("),
        ("be32", "static inline u32 vp_tex_be32("),
        ("store16", "static inline void vp_tex_store_u16("),
        ("store32", "static inline void vp_tex_store_u32("),
        ("unorm", "static inline u8 vp_tex_unorm("),
        ("depth16", "static inline u32 vp_tex_depth16f_to_f32_bits("),
        ("depth24", "static inline u32 vp_tex_depth24f_to_f32_bits("),
        ("copy_level", "static void vp_copy_tex_level("),
        ("is_swizzled", "static int vp_tex_source_is_swizzled("),
        ("is_vtc", "static int vp_tex_source_is_vtc("),
        ("source_struct", "typedef struct {\n    u32 logical_width_blocks, logical_rows;"),
        ("next_pow2", "static int vp_tex_next_pow2("),
        ("source_level", "static int vp_tex_source_level("),
        ("source_block", "static const u8* vp_tex_source_block("),
        ("dxt_rgb565", "static inline void vp_dxt_rgb565("),
        ("dxt_decode", "static void vp_decode_dxt_block("),
        ("copy_layout", "static int vp_copy_tex_level_layout("),
        ("copy_vtc", "static int vp_copy_dxt_level_3d_rgba("),
    ]
    result = {name: extract(text, marker) for name, marker in markers}
    result["format_struct"] += " VPTexFormatInfo;"
    result["source_struct"] += " VPTexSourceLevel;"
    return result


def make_target_pair(text):
    candidate = extract(text, "static int vp_copy_tex_level_layout(")
    if digest(candidate) != CANDIDATE_HELPER_SHA256:
        raise AssertionError("production vp_copy_tex_level_layout changed; review fixture hashes")
    hoist = """    const int swizzled_2d = layout->swizzled && !is_3d;
    const u32 swizzled_log2w = swizzled_2d
        ? rsx_log2u(layout->padded_width_blocks) : 0u;
    const u32 swizzled_log2h = swizzled_2d
        ? rsx_log2u(layout->padded_rows) : 0u;
"""
    candidate_loop = """            const u8* source_block = swizzled_2d
                ? src + (u64)rsx_swz_off(x + layout->border,
                                         y + layout->border,
                                         swizzled_log2w, swizzled_log2h) *
                        info.src_block_bytes
                : vp_tex_source_block(src, layout, x, y, z, depth, is_3d,
                                      0, info.src_block_bytes);
            memcpy(linear + ((u64)y * layout->logical_width_blocks + x) *
                   info.src_block_bytes,
                   source_block, info.src_block_bytes);
"""
    baseline_loop = """            memcpy(linear + ((u64)y * layout->logical_width_blocks + x) *
                   info.src_block_bytes,
                   vp_tex_source_block(src, layout, x, y, z, depth, is_3d,
                                       0, info.src_block_bytes),
                   info.src_block_bytes);
"""
    if candidate.count(hoist) != 1 or candidate.count(candidate_loop) != 1:
        raise AssertionError("could not reverse the one approved 2D hoist")
    baseline = candidate.replace(hoist, "", 1).replace(candidate_loop, baseline_loop, 1)
    if digest(baseline) != BASELINE_HELPER_SHA256:
        raise AssertionError("reconstructed baseline does not match pre-hoist B20 renderer helper")
    return (
        re.sub(r"\bvp_copy_tex_level_layout\b", "copy_layout_baseline", baseline, count=1),
        re.sub(r"\bvp_copy_tex_level_layout\b", "copy_layout_candidate", candidate, count=1),
    )


def main():
    backend_bytes = BACKEND.read_bytes()
    backend_text = backend_bytes.decode("utf-8").replace("\r\n", "\n")
    helpers = source_helpers(backend_text)
    baseline, candidate = make_target_pair(backend_text)
    print("renderer_sha256=" + sha256(backend_bytes).hexdigest().upper())
    print("baseline_helper_sha256=" + BASELINE_HELPER_SHA256)
    print("candidate_helper_sha256=" + CANDIDATE_HELPER_SHA256)
    ordered = [
        "swz2", "swz3", "log2", "format_struct", "format_info", "row_checked",
        "row_bytes", "rows_checked", "rows", "be16", "be32", "store16", "store32",
        "unorm", "depth16", "depth24", "copy_level", "is_swizzled", "is_vtc",
        "source_struct", "next_pow2", "source_level", "source_block", "dxt_rgb565",
        "dxt_decode",
    ]
    helper_text = "\n\n".join(helpers[name] for name in ordered)
    helper_text += "\n\n" + baseline + "\n\n" + candidate + "\n\n" + helpers["copy_vtc"]
    c_source = r"""
#include <windows.h>
#include <stdint.h>
#include <stddef.h>
#include <stdlib.h>
#include <string.h>
#include <stdio.h>
typedef uint8_t u8; typedef uint16_t u16; typedef uint32_t u32; typedef uint64_t u64;
typedef enum {
 DXGI_FORMAT_UNKNOWN, DXGI_FORMAT_R8_UNORM, DXGI_FORMAT_R8G8B8A8_UNORM,
 DXGI_FORMAT_BC1_UNORM, DXGI_FORMAT_BC2_UNORM, DXGI_FORMAT_BC3_UNORM,
 DXGI_FORMAT_R8G8_UNORM, DXGI_FORMAT_R8G8_SNORM, DXGI_FORMAT_R32_FLOAT,
 DXGI_FORMAT_R16_UNORM, DXGI_FORMAT_R16G16_UNORM,
 DXGI_FORMAT_R16G16B16A16_FLOAT, DXGI_FORMAT_R32G32B32A32_FLOAT,
 DXGI_FORMAT_R16G16_FLOAT
} DXGI_FORMAT;
""" + helper_text + r"""
typedef int (*CopyLayout)(u8*,u32,const u8*,const VPTexSourceLevel*,u32,u32,u32,u32,u32,int);
static unsigned checks, failures;
static volatile unsigned long long sink;
static unsigned long long hash_bytes(const u8* p,size_t n) {
    unsigned long long h=1469598103934665603ull;
    for(size_t i=0;i<n;i++) h=(h^p[i])*1099511628211ull;
    return h;
}
static void fill_source(u8* p,size_t n,u32 seed) {
    for(size_t i=0;i<n;i++) p[i]=(u8)(i*29u+(i>>7u)*17u+seed*3u);
}
static int run_layout_case(u32 fmt,u32 layout_fmt,u32 w,u32 h,u32 depth,
                           u32 dimension,int border,u32 programmed_pitch,
                           u32 z) {
    VPTexFormatInfo f; VPTexSourceLevel layout;
    if(!vp_tex_format_info(fmt,&f) || vp_tex_source_level(&f,layout_fmt,w,h,depth,
        dimension,border,programmed_pitch,&layout)) return 1;
    u64 source_size=layout.slice_bytes*(dimension==3u?depth:1u);
    if(layout.swizzled && dimension==3u)
        source_size=(u64)layout.padded_width_blocks*layout.padded_rows*depth*f.src_block_bytes;
    const u64 row_bytes=(u64)w*f.dst_texel_bytes;
    const u32 dst_pitch=(u32)row_bytes+13u;
    const size_t dst_bytes=(size_t)dst_pitch*h;
    if(!source_size || source_size>SIZE_MAX-128u || dst_bytes>SIZE_MAX-128u) return 2;
    const size_t src_bytes=(size_t)source_size;
    u8* raw_src=(u8*)malloc(src_bytes+128u);
    u8* raw_a=(u8*)malloc(dst_bytes+128u); u8* raw_b=(u8*)malloc(dst_bytes+128u);
    if(!raw_src||!raw_a||!raw_b) return 3;
    memset(raw_src,0xA5,src_bytes+128u); memset(raw_a,0xCD,dst_bytes+128u);
    memset(raw_b,0xCD,dst_bytes+128u);
    u8* src=raw_src+64u; u8* a=raw_a+64u; u8* b=raw_b+64u;
    fill_source(src,src_bytes,fmt);
    const unsigned long long input_hash=hash_bytes(src,src_bytes);
    const int is_3d=(dimension==3u);
    const int ra=copy_layout_baseline(a,dst_pitch,src,&layout,w,h,z,depth,fmt,is_3d);
    const int rb=copy_layout_candidate(b,dst_pitch,src,&layout,w,h,z,depth,fmt,is_3d);
    int ok=ra==0 && rb==0 && !memcmp(raw_a,raw_b,dst_bytes+128u) &&
           hash_bytes(src,src_bytes)==input_hash;
    for(size_t i=0;i<64u;i++) if(raw_src[i]!=0xA5 || raw_src[64u+src_bytes+i]!=0xA5 ||
        raw_a[i]!=0xCD || raw_b[i]!=0xCD || raw_a[64u+dst_bytes+i]!=0xCD ||
        raw_b[64u+dst_bytes+i]!=0xCD) ok=0;
    if(!ok) printf("FAIL layout fmt=%x flags=%x %ux%u depth=%u dim=%u border=%d z=%u\n",
                   fmt,layout_fmt,w,h,depth,dimension,border,z);
    ++checks; if(!ok) ++failures;
    free(raw_src);free(raw_a);free(raw_b);return ok?0:1;
}
static int run_vtc_case(u32 fmt) {
    const u32 w=32,h=16,depth=4;
    VPTexFormatInfo f; VPTexSourceLevel layout;
    if(!vp_tex_format_info(fmt,&f) || !f.host_compressed ||
       vp_tex_source_level(&f,fmt,w,h,depth,3,0,0,&layout)) return 1;
    const size_t src_bytes=(size_t)layout.logical_width_blocks*layout.logical_rows*depth*f.src_block_bytes;
    const u32 dst_pitch=w*4u+13u; const u64 slice_pitch=(u64)dst_pitch*h;
    const size_t dst_bytes=(size_t)(slice_pitch*depth);
    u8* raw_src=(u8*)malloc(src_bytes+128u);u8* raw_a=(u8*)malloc(dst_bytes+128u);
    u8* raw_b=(u8*)malloc(dst_bytes+128u);
    if(!raw_src||!raw_a||!raw_b)return 2;
    memset(raw_src,0xA5,src_bytes+128u);memset(raw_a,0xCD,dst_bytes+128u);
    memset(raw_b,0xCD,dst_bytes+128u);
    u8* src=raw_src+64u;u8* actual=raw_a+64u;u8* expected=raw_b+64u;
    fill_source(src,src_bytes,fmt);
    const unsigned long long input_hash=hash_bytes(src,src_bytes);
    for(u32 z=0;z<depth;z++) for(u32 by=0;by<layout.logical_rows;by++)
      for(u32 bx=0;bx<layout.logical_width_blocks;bx++) {
        const u32 group_first=(z>>2u)<<2u;
        const u32 group_depth=(depth-group_first)<4u?depth-group_first:4u;
        const u64 plane=(u64)layout.logical_width_blocks*layout.logical_rows;
        const u64 block=(u64)group_first*plane+
            ((u64)by*layout.logical_width_blocks+bx)*group_depth+(z-group_first);
        u8 rgba[64];vp_decode_dxt_block(src+block*f.src_block_bytes,fmt,rgba);
        for(u32 py=0;py<4u && by*4u+py<h;py++)
          memcpy(expected+z*slice_pitch+(u64)(by*4u+py)*dst_pitch+bx*16u,
                 rgba+py*16u,16u);
      }
    const int rc=vp_copy_dxt_level_3d_rgba(actual,dst_pitch,slice_pitch,src,&layout,
                                           w,h,depth,fmt,1);
    int ok=rc==0 && !memcmp(raw_a,raw_b,dst_bytes+128u) &&
           hash_bytes(src,src_bytes)==input_hash;
    for(size_t i=0;i<64u;i++) if(raw_src[i]!=0xA5 || raw_src[64u+src_bytes+i]!=0xA5 ||
        raw_a[i]!=0xCD || raw_a[64u+dst_bytes+i]!=0xCD) ok=0;
    if(!ok)printf("FAIL VTC fmt=%x\n",fmt);
    ++checks;if(!ok)++failures;
    free(raw_src);free(raw_a);free(raw_b);return ok?0:1;
}
static double now_seconds(void) {
    LARGE_INTEGER q,hz;
    if(!QueryPerformanceFrequency(&hz) || hz.QuadPart<=0 || !QueryPerformanceCounter(&q))
        return -1.0;
    return (double)q.QuadPart/(double)hz.QuadPart;
}
static double bench(CopyLayout copy,u32 rounds) {
    const u32 fmt=0x85,w=512,h=256;
    VPTexFormatInfo f;VPTexSourceLevel layout;
    if(!copy || !rounds || !vp_tex_format_info(fmt,&f) ||
       vp_tex_source_level(&f,fmt,w,h,1,2,0,0,&layout)!=0) return -1.0;
    const u32 dst_pitch=w*4u;const size_t dst_bytes=(size_t)dst_pitch*h;
    const size_t src_bytes=(size_t)layout.slice_bytes;
    u8* src=(u8*)malloc(src_bytes);u8* dst=(u8*)malloc(dst_bytes);
    if(!src||!dst){free(src);free(dst);return -1.0;}
    fill_source(src,src_bytes,fmt);memset(dst,0xCD,dst_bytes);
    const double t0=now_seconds();
    if(!(t0>0.0)){free(src);free(dst);return -1.0;}
    CopyLayout volatile call_target=copy;
    for(u32 i=0;i<rounds;i++) {
        const int rc=call_target(dst,dst_pitch,src,&layout,w,h,0,1,fmt,0);
        if(rc!=0){free(src);free(dst);return -1.0;}
    }
    const double elapsed=now_seconds()-t0;
    if(!(elapsed>0.0)){free(src);free(dst);return -1.0;}
    sink^=hash_bytes(dst,dst_bytes);
    free(src);free(dst);return elapsed*1.0e6/(double)rounds;
}
int main(void) {
    const u32 formats[]={0x81,0x82,0x83,0x84,0x85,0x8B,0x98,0x99,0x90,0x91,
                         0x92,0x93,0x94,0x95,0x97,0x9A,0x9B,0x9C,0x9F};
    const u32 dims[][2]={{1,1},{8,32},{64,8},{128,32},{512,256}};
    for(u32 fi=0;fi<sizeof(formats)/sizeof(formats[0]);fi++)
      for(u32 di=0;di<sizeof(dims)/sizeof(dims[0]);di++)
       for(int border=0;border<2;border++)
        if(run_layout_case(formats[fi],formats[fi],dims[di][0],dims[di][1],1,2,
                           border,0,0)) return 1;
    /* LN linear and 3D Morton controls select the original source-block path. */
    if(run_layout_case(0x85,0xA5,17,13,1,2,1,(17u+10u)*4u,0))return 1;
    if(run_layout_case(0x8D,0xAD,17,13,1,2,1,(9u+10u)*4u,0))return 1;
    if(run_layout_case(0x8E,0xAE,17,13,1,2,1,(9u+10u)*4u,0))return 1;
    if(run_layout_case(0x85,0x85,8,32,4,3,1,0,2))return 1;
    if(run_vtc_case(0x86)||run_vtc_case(0x87)||run_vtc_case(0x88))return 1;
    printf("texture layout parity: %u checks, %u failures; prehoist_renderer=%s\n",
           checks,failures,"B20BD53381FA65D34B55FB8E655936C95E06A5B24C62C4E822402753F2930B4F");
    if(failures)return 1;
    const u32 rounds=300;double base[6],cand[6];
    for(int i=0;i<6;i++) {
        if(i&1){cand[i]=bench(copy_layout_candidate,rounds);base[i]=bench(copy_layout_baseline,rounds);}
        else {base[i]=bench(copy_layout_baseline,rounds);cand[i]=bench(copy_layout_candidate,rounds);}
    }
    for(int i=0;i<6;i++) if(!(base[i]>0.0) || !(cand[i]>0.0)) {
        fprintf(stderr,"benchmark failed for pair%d: baseline=%.2f candidate=%.2f\n",
                i,base[i],cand[i]);
        return 1;
    }
    for(int i=0;i<6;i++)printf("512x256 RGBA8 full-copy pair%d: baseline=%.2fus candidate=%.2fus\n",
                                i,base[i],cand[i]);
    return 0;
}
"""
    clang = shutil.which("clang") or r"C:\Program Files\LLVM\bin\clang.exe"
    with tempfile.TemporaryDirectory(prefix="tj-texture-layout-") as temp:
        c_file, exe = Path(temp) / "texture_layout.c", Path(temp) / "texture_layout.exe"
        c_file.write_text(c_source, encoding="utf-8")
        subprocess.run([clang, "-std=c11", "-O3", str(c_file), "-o", str(exe)], check=True)
        return subprocess.run([str(exe)], check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
