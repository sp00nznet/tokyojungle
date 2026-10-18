"""Exercise the actual VP byte-hash helper and its one-bank memo."""
from pathlib import Path
import os
import re
import shutil
import subprocess
import tempfile

from test_fifo_present_boundary import function


ROOT = Path(__file__).resolve().parents[1]
VIDEO = ROOT.parent / "ps3recomp/libs/video"
BANK_BYTES = 16 * 1024


def source_fixture():
    backend = (VIDEO / "rsx_d3d12_backend.c").read_text(encoding="utf-8")
    hash_fn = function(backend, "static u32 vp_hash_ucode(")
    hash_fn, replacements = re.subn(
        r"(?m)^    u32 h = 2166136261u;$",
        "    if (count_hash) ++hash_calls;\n"
        "    if (mutate_source_on_hash) { mutate_source_on_hash=0; mutate_source[0]^=0x80; }\n"
        "    if (force_hash_collision) return 0x51A7u;\n"
        "    u32 h = 2166136261u;",
        hash_fn, count=1)
    assert replacements == 1, "could not instrument the production VP hash body"
    hash_fn, replacements = re.subn(
        r"(?m)^    return h \? h : 1u;$",
        "    if (count_hash && mutate_source_after_hash) {\n"
        "        mutate_source_after_hash=0; mutate_source[0]^=0x80;\n"
        "    }\n"
        "    return h ? h : 1u;",
        hash_fn, count=1)
    assert replacements == 1, "could not add post-FNV source mutation hook"

    memo_start = backend.index("typedef struct TJVpHashMemo {")
    memo_end = backend.index("static void vp_hash_ucode_reuse_report(", memo_start)
    memo_helpers = backend[memo_start:memo_end]
    assert "static int vp_hash_ucode_reuse_enabled(void)" in memo_helpers
    assert "static u32 vp_hash_ucode_reuse(const u8* p, u32 n)" in memo_helpers

    source = r'''
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <windows.h>
typedef uint8_t u8;
typedef uint32_t u32;
typedef uint64_t u64;
#define BANK_BYTES 16384u
typedef struct { u8 vp_ucode[16384]; } rsx_state;
static unsigned hash_calls;
static u8* mutate_source;
static int count_hash=1,force_hash_collision,mutate_source_on_hash;
static int mutate_source_after_hash,trace_enabled;
HASH_FUNCTION
MEMO_HELPERS
static int vp_texture_churn_trace_enabled(void) { return trace_enabled; }
static unsigned checks,failures;
static void check(int ok,const char* label)
{ ++checks; if(!ok){++failures;printf("FAIL %s\n",label);} }
static u32 raw_hash_without_count(const u8* p,u32 n)
{
    int saved=count_hash;count_hash=0;u32 h=vp_hash_ucode(p,n);count_hash=saved;return h;
}
static void check_reuse(const u8* p,u32 n,unsigned expected_delta,const char* label)
{
    u32 expected=raw_hash_without_count(p,n);unsigned before=hash_calls;
    u32 got=vp_hash_ucode_reuse(p,n);
    check(got==expected,label);
    check(hash_calls-before==expected_delta,"actual production FNV call count");
}
static LONG64 read_stat(volatile LONG64* value)
{ return InterlockedCompareExchange64(value,0,0); }
static void reset_stats(void)
{
    InterlockedExchange64(&s_tj_vp_hash_reuse_stats.calls,0);
    InterlockedExchange64(&s_tj_vp_hash_reuse_stats.hits,0);
    InterlockedExchange64(&s_tj_vp_hash_reuse_stats.misses,0);
    InterlockedExchange64(&s_tj_vp_hash_reuse_stats.bypass,0);
    InterlockedExchange64(&s_tj_vp_hash_reuse_stats.hash_bytes,0);
    InterlockedExchange64(&s_tj_vp_hash_reuse_stats.compare_bytes,0);
}
static void fill(u8* p,u32 n,u32 seed)
{ for(u32 i=0;i<n;++i)p[i]=(u8)((i*29u+(i>>5)+seed*17u)^(i>>8)); }
static int run(int expected_enabled)
{
    const unsigned hit_delta=expected_enabled?0u:1u;
    check(vp_hash_ucode_reuse_enabled()==expected_enabled,
          "environment enables memo only for exact value 1");
    check(sizeof(s_tj_vp_hash_memo.bank)==BANK_BYTES,
          "fixture memo capacity matches the 16 KiB production bank");
    check_reuse(NULL,0,1,"zero length NULL hash is safe and returns the production digest");
    check_reuse(NULL,0,hit_delta,"zero length repeat is safe");

    u8 a[BANK_BYTES+1],b[BANK_BYTES+1],oversize[BANK_BYTES+1];
    fill(a,BANK_BYTES+1,1);memcpy(b,a,BANK_BYTES);fill(oversize,BANK_BYTES+1,7);
    a[BANK_BYTES]=0xA1;b[BANK_BYTES]=0xB2;oversize[BANK_BYTES]=0xD3;
    check_reuse(a,BANK_BYTES,1,"first complete 16 KiB bank is hashed");
    check_reuse(a,BANK_BYTES,hit_delta,"same-address unchanged bank reuses cached raw hash");
    check_reuse(b,BANK_BYTES,hit_delta,"equal bytes at a different address reuse by full comparison");
    check_reuse(a,BANK_BYTES,hit_delta,"alternating equal-content addresses reuse one owned bank");
    check_reuse(b,BANK_BYTES,hit_delta,"second equal-content address remains reusable");

    b[BANK_BYTES-1]^=0x35;
    check_reuse(b,BANK_BYTES,1,"last byte of the full bank participates in equality");
    check_reuse(b,BANK_BYTES,hit_delta,"last-byte mutation becomes the next stable memo value");
    b[0]^=0x61;
    check_reuse(b,BANK_BYTES,1,"in-place same-address mutation forces a full rehash");
    check_reuse(b,BANK_BYTES,hit_delta,"mutated in-place bank can then be reused");

    /* Inject a source mutation at the actual hash-function entry. Both
     * hash-before-copy and copy-before-hash must leave a coherent memo: the
     * next call either hits the post-mutation bytes or detects and hashes them. */
    u8 mutation_buf[BANK_BYTES],pre_mutation[BANK_BYTES];
    fill(mutation_buf,BANK_BYTES,37);memcpy(pre_mutation,mutation_buf,BANK_BYTES);
    u32 pre_hash=raw_hash_without_count(pre_mutation,BANK_BYTES);
    mutate_source=mutation_buf;mutate_source_on_hash=1;
    unsigned mutation_before=hash_calls;
    u32 first_mutation_hash=vp_hash_ucode_reuse(mutation_buf,BANK_BYTES);
    check(hash_calls-mutation_before==1 &&
          mutation_buf[0]==(u8)(pre_mutation[0]^0x80),
          "controlled source mutation occurs during the actual hash call");
    u32 post_hash=raw_hash_without_count(mutation_buf,BANK_BYTES);
    check(first_mutation_hash==pre_hash || first_mutation_hash==post_hash,
          "mutation-time result belongs to a complete pre- or post-mutation byte bank");
    mutation_before=hash_calls;
    u32 followup_hash=vp_hash_ucode_reuse(mutation_buf,BANK_BYTES);
    check(followup_hash==post_hash,
          "follow-up never returns a digest paired with different memo bytes");
    unsigned followup_delta=expected_enabled ?
        (first_mutation_hash==pre_hash ? 1u : 0u) : 1u;
    check(hash_calls-mutation_before==followup_delta,
          "follow-up either reuses coherent bytes or rehashes the mutation");
    mutate_source=NULL;

    /* Mutate the original caller only after FNV has consumed the hash input,
     * immediately before the real hash function returns. With copy-first,
     * that input is the owned memo bank, so the returned digest and retained
     * bytes agree; the next caller-buffer comparison must miss and rehash. */
    u8 after_hash_buf[BANK_BYTES],after_hash_before[BANK_BYTES];
    fill(after_hash_buf,BANK_BYTES,53);
    memcpy(after_hash_before,after_hash_buf,BANK_BYTES);
    memset(&s_tj_vp_hash_memo,0,sizeof(s_tj_vp_hash_memo));
    u32 after_hash_expected=raw_hash_without_count(after_hash_before,BANK_BYTES);
    mutate_source=after_hash_buf;mutate_source_after_hash=1;
    unsigned after_hash_calls=hash_calls;
    u32 after_hash_first=vp_hash_ucode_reuse(after_hash_buf,BANK_BYTES);
    check(after_hash_first==after_hash_expected &&
          after_hash_buf[0]==(u8)(after_hash_before[0]^0x80) &&
          !mutate_source_after_hash,
          "post-FNV hook mutates caller after the digest is complete");
    check(hash_calls-after_hash_calls==1,
          "post-FNV mutation first call performs one actual hash");
    u32 after_hash_changed=raw_hash_without_count(after_hash_buf,BANK_BYTES);
    after_hash_calls=hash_calls;
    u32 after_hash_followup=vp_hash_ucode_reuse(after_hash_buf,BANK_BYTES);
    check(after_hash_followup==after_hash_changed,
          "post-FNV caller mutation cannot pair old digest with changed memo bytes");
    check(hash_calls-after_hash_calls==1,
          "post-FNV changed caller forces a fresh hash instead of stale memo hit");
    mutate_source=NULL;

    check_reuse(b,BANK_BYTES-1,1,"16 KiB minus one is a distinct memo length");
    check_reuse(b,BANK_BYTES-1,hit_delta,"stable shorter bank reuses");
    check_reuse(a,BANK_BYTES,1,"length transition back to exactly 16 KiB misses");
    check_reuse(a,BANK_BYTES,hit_delta,"exact-capacity bank reuses after miss");
    check_reuse(oversize,BANK_BYTES+1,1,"one byte above capacity takes full-hash fallback");
    check_reuse(oversize,BANK_BYTES+1,1,"oversize fallback hashes every call");
    check(oversize[BANK_BYTES]==0xD3,"oversize fallback leaves trailing canary untouched");
    check_reuse(a,BANK_BYTES,hit_delta,"oversize fallback does not corrupt the retained bank");

    memset(&s_tj_vp_hash_memo,0,sizeof(s_tj_vp_hash_memo));
    force_hash_collision=1;fill(a,BANK_BYTES,13);fill(b,BANK_BYTES,91);
    check_reuse(a,BANK_BYTES,1,"forced-collision bank first hash");
    check_reuse(b,BANK_BYTES,1,"equal FNV result cannot bypass full byte comparison");
    check_reuse(b,BANK_BYTES,hit_delta,"collision candidate is reusable only after its bytes are copied");
    force_hash_collision=0;

    /* With tracing disabled, all real helper statistics stay untouched. */
    trace_enabled=0;reset_stats();memset(&s_tj_vp_hash_memo,0,sizeof(s_tj_vp_hash_memo));
    unsigned before=hash_calls;
    check_reuse(a,BANK_BYTES,1,"trace-off initial call remains correct");
    check_reuse(a,BANK_BYTES,hit_delta,"trace-off stable repeat remains correct");
    check(read_stat(&s_tj_vp_hash_reuse_stats.calls)==0 &&
          read_stat(&s_tj_vp_hash_reuse_stats.hash_bytes)==0 &&
          read_stat(&s_tj_vp_hash_reuse_stats.compare_bytes)==0,
          "trace-off path leaves stats untouched");
    check(hash_calls-before==(expected_enabled?1u:2u),
          "trace-off statistics gate does not alter hash reuse");

    /* Exercise the actual stats branch with a deterministic miss/hit/miss/bypass. */
    trace_enabled=1;reset_stats();memset(&s_tj_vp_hash_memo,0,sizeof(s_tj_vp_hash_memo));
    fill(a,BANK_BYTES,3);fill(oversize,BANK_BYTES+1,11);unsigned trace_before=hash_calls;
    check_reuse(a,BANK_BYTES,1,"traced first bank misses");
    check_reuse(a,BANK_BYTES,hit_delta,"traced stable bank hits");
    a[BANK_BYTES-1]^=1;
    check_reuse(a,BANK_BYTES,1,"traced changed bank misses");
    check_reuse(oversize,BANK_BYTES+1,1,"traced oversize bank bypasses");
    LONG64 calls=read_stat(&s_tj_vp_hash_reuse_stats.calls);
    LONG64 hits=read_stat(&s_tj_vp_hash_reuse_stats.hits);
    LONG64 misses=read_stat(&s_tj_vp_hash_reuse_stats.misses);
    LONG64 bypass=read_stat(&s_tj_vp_hash_reuse_stats.bypass);
    LONG64 hash_bytes=read_stat(&s_tj_vp_hash_reuse_stats.hash_bytes);
    LONG64 compare_bytes=read_stat(&s_tj_vp_hash_reuse_stats.compare_bytes);
    check(calls==4 && calls==hits+misses+bypass,
          "traced call counter partitions into hit, miss, and bypass");
    check(hash_bytes==(expected_enabled ? 2*BANK_BYTES+BANK_BYTES+1 :
                                         3*BANK_BYTES+BANK_BYTES+1),
          "traced hash_bytes counts full-hash inputs only");
    check(compare_bytes==(expected_enabled ? 2*BANK_BYTES : 0),
          "traced compare_bytes counts requested full-bank comparisons");
    check(hash_calls-trace_before==(expected_enabled?3u:4u),
          "trace counters correspond to actual production hash calls");
    trace_enabled=0;
    printf("VP ucode hash reuse env=%s checks=%u failures=%u hash_calls=%u\n",
           expected_enabled?"1":"off",checks,failures,hash_calls);
    return failures?1:0;
}
int main(int argc,char** argv)
{ return run(argc>1?atoi(argv[1]):0); }
'''.replace("HASH_FUNCTION\n", hash_fn + "\n").replace(
        "MEMO_HELPERS\n", memo_helpers + "\n")
    return source


def main():
    clang = shutil.which("clang") or r"C:\Program Files\LLVM\bin\clang.exe"
    with tempfile.TemporaryDirectory(prefix="tj-vp-hash-reuse-") as temp:
        source_path = Path(temp) / "vp_hash_reuse.c"
        exe = Path(temp) / "vp_hash_reuse.exe"
        source_path.write_text(source_fixture(), encoding="utf-8")
        build = subprocess.run(
            [clang, "-std=c11", "-O2", "-D_CRT_SECURE_NO_WARNINGS",
             str(source_path), "-o", str(exe)], capture_output=True, text=True)
        if build.returncode:
            print(build.stdout, end="")
            print(build.stderr, end="")
            return build.returncode

        cases = ((None, 0), ("0", 0), ("01", 0), ("true", 0), ("1", 1))
        for env_value, enabled in cases:
            env = dict(os.environ)
            env.pop("TJ_VP_UCODE_HASH_REUSE", None)
            if env_value is not None:
                env["TJ_VP_UCODE_HASH_REUSE"] = env_value
            result = subprocess.run([str(exe), str(enabled)], env=env,
                                    capture_output=True, text=True)
            print(result.stdout, end="")
            print(result.stderr, end="")
            if result.returncode:
                return result.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
