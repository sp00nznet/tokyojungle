"""Repair existing VMX word add/sub/greater-than emissions byte-for-byte.

No relift: only recognized instruction bodies change. Move/bitwise operations
already preserve guest bytes and must not acquire a second endian conversion.
"""
import argparse
from collections import Counter
from pathlib import Path
import re

MARK = b"/* ppu-vmx-word-be */"
ARITH = re.compile(
    rb"\{ uint32_t\* d=\(uint32_t\*\)&ctx->vr\[(?P<d>\d+)\]; "
    rb"uint32_t\* a=\(uint32_t\*\)&ctx->vr\[(?P<a>\d+)\]; "
    rb"uint32_t\* b=\(uint32_t\*\)&ctx->vr\[(?P<b>\d+)\]; "
    rb"for\(int i=0;i<4;i\+\+\) d\[i\]=a\[i\](?P<op>[+\-])b\[i\]; \}"
)
CMP = re.compile(
    rb"\{ (?P<ty>u?int32_t)\* a=\((?P=ty)\*\)&ctx->vr\[(?P<a>\d+)\]; "
    rb"(?P=ty)\* b=\((?P=ty)\*\)&ctx->vr\[(?P<b>\d+)\]; "
    rb"uint32_t\* d=\(uint32_t\*\)&ctx->vr\[(?P<d>\d+)\]; int t=0; "
    rb"for\(int i=0;i<4;i\+\+\)\{ uint32_t r=a\[i\]>b\[i\]\?"
    rb"\(uint32_t\)~\(uint32_t\)0:0; d\[i\]=r; t\+=r\?1:0; \}"
    rb"(?P<record> uint32_t c6=\(t==4\?8u:0u\)\|\(t==0\?2u:0u\); "
    rb"ctx->cr=\(ctx->cr & ~\(0xFu<<4\)\)\|\(c6<<4\);)? \}"
)


def emit(match, compare=False):
    a, b, d = (int(match[k]) for k in ("a", "b", "d"))
    body = (f"{{ uint32_t a[4],b[4],d[4]; "
            f"ppu_vldu4(&ctx->vr[{a}],a); ppu_vldu4(&ctx->vr[{b}],b); ")
    if compare:
        ty = match["ty"].decode("ascii")
        body += (f"int t=0; for(int i=0;i<4;i++){{ uint32_t r=({ty})a[i]>({ty})b[i]?~0u:0u; "
                 "d[i]=r; t+=r?1:0; } ")
    else:
        body += f"for(int i=0;i<4;i++) d[i]=a[i]{match['op'].decode('ascii')}b[i]; "
    body += f"ppu_vstu4(&ctx->vr[{d}],d);"
    if compare and match["record"]:
        body += match["record"].decode("ascii")
    return body.encode("ascii") + b" } " + MARK


def patched_bytes(data):
    stats = Counter()
    def arithmetic(match):
        stats["add" if match["op"] == b"+" else "sub"] += 1
        return emit(match)
    def compare(match):
        stats["compare"] += 1
        return emit(match, True)
    result = ARITH.sub(arithmetic, data)
    result = CMP.sub(compare, result)
    if result != data and b"static inline void ppu_vldu4(" not in data:
        raise ValueError("Missing guest-BE helpers; source unchanged")
    return result, stats


def repair(path, apply=False):
    path = Path(path)
    before = path.read_bytes()
    after, stats = patched_bytes(before)
    if apply and after != before:
        path.write_bytes(after)
    return stats


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    stats = Counter()
    for path in sorted((Path(__file__).resolve().parents[1] / "generated").glob("ppu_recomp_*.cpp")):
        stats.update(repair(path, args.apply))
    print("VMX word-BE patches:", dict(stats))
