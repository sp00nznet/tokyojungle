"""Focused FREST/FRSQEST/FI contract and original-interpreter regression.

Estimate payloads are hardware lookup data documented by RPCS3 at
54014a7de4b2ccec98c9c0cb7dbebec0606c5cd6, Emu/Cell/SPUThread.cpp.
The separate double-precision oracle follows FI's field arithmetic, with
SPU truncation checked against published ps3autotests output (see below).
This is instruction proof, not gameplay proof.
"""
from pathlib import Path
import random
import shutil
import struct
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT.parent / "ps3recomp"
RECIP = [int(x, 16) for x in """
7FFBE0 7F87A6 70EF72 708B40 638B12 633AEA 5792C4 574AA0
4CCA7E 4C9262 430A44 42D62A 3A2E12 39FDFA 3215E4 31F1D2
2AA9BE 2A85AC 23D59A 23BD8E 1D8576 1D8576 17AD5A 17AD5A
124543 124543 0D392D 0D392D 08851A 08851A 041D07 041D07
""".split()]
RSQRT = [int(x, 16) for x in """
350160 34E954 2F993D 2F993D 2AA523 2AA523 26190D 26190D
21E4F9 21E4F9 1E00E9 1E00E9 1A5CD9 1A5CD9 16F8CB 16F8CB
13CCC0 13CCC0 10CCB3 10CCB3 0E00AA 0E00AA 0B58A1 0B58A1
08D498 08D498 067491 067491 043089 043089 020C83 020C83
7FFDF4 7FD1DE 7859C8 783DBA 71559C 71559C 6AE57C 6AE57C
64F561 64F561 5F7149 5F7149 5A4D33 5A4D33 55811F 55811F
51050F 51050F 4CC8FE 4CC8FE 48D0F0 48D0F0 4510E4 4510E4
4180D7 4180D7 3E24CC 3E24CC 3AF4C3 3AF4C3 37E8BA 37E8BA
""".split()]


def bits(value):
    return struct.unpack("<I", struct.pack("<f", value))[0]


def number(value):
    return struct.unpack("<f", struct.pack("<I", value))[0]


def f32(value):
    return number(bits(value))


def estimate(value, square_root=False):
    exponent = (value >> 23) & 255
    if square_root:
        result_exponent = 255 if not exponent else (380 - exponent) // 2
        return (result_exponent << 23) | RSQRT[(value >> 18) & 63]
    result_exponent = 255 if not exponent else max(253 - exponent, 0)
    return (value & 0x80000000) | (result_exponent << 23) | RECIP[(value >> 18) & 31]


def interpolate(a, b):
    base = number(0x3f800000 | (b & 0x007ffc00))
    step = (b & 1023) / 8192
    y = (a & 0x7ffff) / 524288
    # All 33 significant intermediate bits fit exactly in a Python double.
    # Convert to single, then correct a rounded-up value to round-toward-zero.
    exact = base - step * y
    encoded = bits(exact)
    if number(encoded) > exact:
        encoded -= 1
    fraction = encoded & 0x7fffff
    return (b & 0xff800000) | fraction


def main():
    # Published SPU printf output from RPCS3/ps3autotests commit
    # b5cff063354cbfaa8027621f39561e4a2a1543dc, tests/cpu/spu_fpu.
    # These are the upper 32 bits after promotion to double, not raw floats;
    # they do not certify the bottom three single-precision mantissa bits.
    hardware_fi = [
        (1.6, 1.6, 0x3ff95bff), (1.6, -1.6, 0xbff95bff),
        (1.6, -10000000.4, 0xc162527f), (1.6, 20000000., 0x4172527f),
        (20000.6, 1.6, 0x3ff962fc), (20000.6, 1.4, 0x3ff58c36),
        (20000.6, 20000000., 0x41726850), (20000.6, 20000.6, 0x40d3365c),
    ]
    for a, b, high_word in hardware_fi:
        value = number(interpolate(bits(a), bits(b)))
        assert struct.unpack(">Q", struct.pack(">d", value))[0] >> 32 == high_word
    # Sweep both signs, all table buckets, exponent extremes, and bucket ends.
    cases = [(sign | (exp << 23) | (bucket << 18) | tail, 0x3f7fffff)
             for sign in (0, 0x80000000)
             for exp in (0, 1, 2, 126, 127, 128, 129, 252, 253, 254, 255)
             for bucket in range(32) for tail in (0, 0x3ffff)]
    rng = random.Random(0x3d4)
    cases += [(rng.getrandbits(32), rng.getrandbits(32)) for _ in range(512)]
    cases += [(bits(x), bits(y)) for x in (.25, 1, 1.6, 1.9, 2, 4, 9, 16)
              for y in (0, .25, .5, 1, 1.6, -1.6)]
    cases += [(bits(a), bits(b)) for a, b, _ in hardware_fi]
    source = r'''
#include "spu_helpers.h"
#include "spu_interp.h"
#include <stdio.h>
#include <stdlib.h>
#ifdef _WIN32
#include <fcntl.h>
#include <io.h>
#endif
int g_wws_code_probe, g_wws_read_probe;
spu_lifted_fn spu_lifted_lookup(const spu_context* c,uint32_t pc) {return NULL;}
u128 spu_rdch(spu_context* c,uint32_t ch) {abort();}
void spu_wrch(spu_context* c,uint32_t ch,u128 v) {abort();}
uint32_t spu_rchcnt(spu_context* c,uint32_t ch) {abort();}
void spu_spurs_taskset_syscall(spu_context* c) {abort();}
int main(void) {
#ifdef _WIN32
    _setmode(_fileno(stdin),_O_BINARY); _setmode(_fileno(stdout),_O_BINARY);
#endif
    spu_context* c=calloc(1,sizeof(*c)); uint32_t pair[2];
    if(!c) return 2;
    while(fread(pair,sizeof(pair),1,stdin)==1) {
        u128 a=spu_splat_u32(pair[0]), b=spu_splat_u32(pair[1]);
        u128 re=spu_frest(a), sq=spu_frsqest(a);
        u128 results[]={re,sq,spu_fi(a,b),spu_fi(a,re),spu_fi(a,sq)};
        uint32_t opcodes[]={0x1b8,0x1b9,0x3d4};
        for(unsigned op=0;op<3;op++) {
            c->gpr[3]=a; c->gpr[4]=b; c->pc=0x1000; int branch=0;
            spu_ls_write32(c,c->pc,(opcodes[op]<<21)|(4<<14)|(3<<7)|5);
            if(spu_interp_step(c,&branch) || memcmp(&c->gpr[5],&results[op],16)) return 3;
        }
        if(fwrite(results,sizeof(results),1,stdout)!=1) return 4;
    }
    free(c); return 0;
}
'''
    clang = shutil.which("clang") or r"C:\Program Files\LLVM\bin\clang.exe"
    with tempfile.TemporaryDirectory(prefix="tj-spu-estimates-") as temp:
        c, exe = Path(temp) / "test.c", Path(temp) / "test.exe"
        c.write_text(source, encoding="utf-8")
        subprocess.run([clang, "-O2", "-std=c11", "-D_CRT_SECURE_NO_WARNINGS",
                        "-I", str(RUNTIME / "include"), "-I", str(RUNTIME / "runtime/spu"),
                        str(c), str(RUNTIME / "runtime/spu/spu_interp.c"), "-o", str(exe)], check=True)
        result = subprocess.run([str(exe)], input=b"".join(struct.pack("<II", *v) for v in cases),
                                stdout=subprocess.PIPE, check=True).stdout
    assert len(result) == len(cases) * 80
    failures = [0] * 5
    for i, (a, b) in enumerate(cases):
        re, sq = estimate(a), estimate(a, True)
        expected = [re, sq, interpolate(a, b), interpolate(a, re), interpolate(a, sq)]
        got = struct.unpack_from("<20I", result, i * 80)
        for op, value in enumerate(expected):
            if any(v != value for v in got[op * 4:op * 4 + 4]):
                if sum(failures) < 4:
                    print(f"mismatch op={op} a={a:08X} b={b:08X} got={got[op*4]:08X} expected={value:08X}")
                failures[op] += 1
    print(f"SPU estimates: cases={len(cases)} four-lane operations={len(cases)*5} failures={failures}; interpreter agrees")
    assert not any(failures)
    # Independent numerical sanity: FI must work after either kind of estimate.
    for x in (.25, 1, 1.6, 1.9, 2, 4, 9, 16):
        for sq in (False, True):
            want = 1 / (x ** .5 if sq else x)
            actual = number(interpolate(bits(x), estimate(bits(x), sq)))
            assert abs(actual / want - 1) < .002, (x, sq, actual, want)


if __name__ == "__main__":
    main()
