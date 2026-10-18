"""Check native SPU multiply helpers against exact integer SPU-format arithmetic."""
from pathlib import Path
import argparse
import random
import shutil
import struct
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT.parent / "ps3recomp"


def decode(bits):
    exponent = (bits >> 23) & 255
    if not exponent:
        return 0, 0  # SPU flushes denormal operands.
    mantissa = (bits & 0x7fffff) | 0x800000
    return (-mantissa if bits >> 31 else mantissa), exponent - 150


def reference(a, b, c, negate_product=False, negate_addend=False):
    am, ae = decode(a)
    bm, be = decode(b)
    cm, ce = decode(c)
    pm, pe = am * bm * (-1 if negate_product else 1), ae + be
    cm *= -1 if negate_addend else 1
    common = min(pe, ce)
    exact = (pm << (pe - common)) + (cm << (ce - common))
    if not exact:
        return 0
    sign = 0x80000000 if exact < 0 else 0
    magnitude = abs(exact)
    top = magnitude.bit_length() - 1
    exponent = top + common + 127
    if exponent <= 0:
        return 0
    if exponent > 255:
        return sign | 0x7fffffff
    mantissa = magnitude >> (top - 23) if top >= 23 else magnitude << (23 - top)
    return sign | (exponent << 23) | (mantissa & 0x7fffff)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", action="store_true")
    args = parser.parse_args()
    # The first lane is the actual image-17 0x4F60 culling failure. Include
    # saturation, cancellation, tiny opposite addends, zeros and denormals.
    edge = [0, 0x80000000, 1, 0x807fffff, 0x00800000, 0x80800000,
            0x3f000000, 0x3f800000, 0xbf800000, 0x40000000, 0xc0000000,
            0x7f7fffff, 0xff7fffff, 0x7f800000, 0xff800000, 0x7fffffff, 0xffffffff]
    cases = [(0, 0xffffffff, 0)]
    for special in (0x7f800000, 0xff800000, 0x7fffffff, 0xffffffff):
        for a in edge:
            for b in edge:
                cases.extend(((special, a, b), (a, special, b), (a, b, special)))
    rng = random.Random(0x4f60)
    for _ in range(4096):
        values = [rng.getrandbits(32) for _ in range(3)]
        values[rng.randrange(3)] |= 0x7f800000
        cases.append(tuple(values))
    while len(cases) % 4:
        cases.append(cases[0])

    source = r'''
#include "spu_helpers.h"
#include <stdio.h>
#ifdef BASELINE
static u128 old_fm(u128 a,u128 b) {u128 r;for(int i=0;i<4;i++)r._f32[i]=a._f32[i]*b._f32[i];return r;}
static u128 old_fma(u128 a,u128 b,u128 c) {u128 r;for(int i=0;i<4;i++)r._f32[i]=a._f32[i]*b._f32[i]+c._f32[i];return r;}
static u128 old_fms(u128 a,u128 b,u128 c) {u128 r;for(int i=0;i<4;i++)r._f32[i]=a._f32[i]*b._f32[i]-c._f32[i];return r;}
static u128 old_fnms(u128 a,u128 b,u128 c) {u128 r;for(int i=0;i<4;i++)r._f32[i]=c._f32[i]-a._f32[i]*b._f32[i];return r;}
#define spu_fm old_fm
#define spu_fma old_fma
#define spu_fms old_fms
#define spu_fnms old_fnms
#endif
int main(int argc,char** argv) {
    if(argc!=2) return 2;
    FILE* f=fopen(argv[1],"rb");if(!f) return 2;
    uint32_t rows[7][4]; unsigned checked=0,failures=0;
    while(fread(rows,sizeof(rows),1,f)==1) {
        u128 a,b,c;memcpy(&a,rows[0],16);memcpy(&b,rows[1],16);memcpy(&c,rows[2],16);
        u128 actual[]={spu_fm(a,b),spu_fma(a,b,c),spu_fms(a,b,c),spu_fnms(a,b,c)};
        for(unsigned op=0;op<4;op++) for(unsigned lane=0;lane<4;lane++) {
            if(op==0 && (a._u32[lane]&0x7f800000)!=0x7f800000 &&
                        (b._u32[lane]&0x7f800000)!=0x7f800000) continue;
            checked++;
            if(actual[op]._u32[lane]!=rows[3+op][lane] && failures++<3)
                fprintf(stderr,"FAIL op=%u a=%08X b=%08X c=%08X got=%08X expected=%08X\n",
                    op,a._u32[lane],b._u32[lane],c._u32[lane],actual[op]._u32[lane],rows[3+op][lane]);
        }
    }
    if(ferror(f)) return 2;
    fclose(f);
    printf("SPU_EXTENDED_MULTIPLY checked=%u failures=%u\n",checked,failures);
    return failures?1:0;
}
'''
    clang = shutil.which("clang") or r"C:\Program Files\LLVM\bin\clang.exe"
    with tempfile.TemporaryDirectory(prefix="tj-spu-extended-") as temp:
        temp = Path(temp)
        cfile, data, exe = temp / "test.c", temp / "cases.bin", temp / "test.exe"
        cfile.write_text(source, encoding="utf-8")
        with data.open("wb") as stream:
            for start in range(0, len(cases), 4):
                rows = [[], [], [], [], [], [], []]
                for a, b, c in cases[start:start+4]:
                    values = [a, b, c, reference(a, b, 0), reference(a, b, c),
                              reference(a, b, c, negate_addend=True),
                              reference(a, b, c, negate_product=True)]
                    for row, value in zip(rows, values):
                        row.append(value)
                stream.write(struct.pack("<28I", *(value for row in rows for value in row)))
        subprocess.run([clang, "-O2", "-std=c11", "-D_CRT_SECURE_NO_WARNINGS",
                        *(["-DBASELINE"] if args.baseline else []),
                        "-I", str(RUNTIME / "include"), "-I", str(RUNTIME / "runtime/spu"),
                        str(cfile), "-o", str(exe)], check=True)
        subprocess.run([str(exe), str(data)], check=True)


if __name__ == "__main__":
    main()
