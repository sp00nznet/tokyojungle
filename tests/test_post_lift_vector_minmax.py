#!/usr/bin/env python3
"""Focused source/semantic checks for post_lift --vector-minmax-be-only."""
import importlib.util
import math
import re
import shutil
import struct
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
POST_LIFT = ROOT / "scripts" / "post_lift.py"
GENERATED = ROOT / "generated" / "ppu_recomp_000.cpp"
EMITTED_VECTOR_RE = re.compile(
    r"(?P<line>\{ float a\[4\],b\[4\],d\[4\]; "
    r"ppu_vldf4\(&ctx->vr\[(?P<a>\d+)\],a\); "
    r"ppu_vldf4\(&ctx->vr\[(?P<b>\d+)\],b\); "
    r"for\(int i=0;i<4;i\+\+\) d\[i\]=a\[i\](?P<op>[<>])b\[i\]\?a\[i\]:b\[i\]; "
    r"ppu_vstf4\(&ctx->vr\[(?P<d>\d+)\],d\); \} /\* tj-ppu-vminmax-be \*/)"
)


def load_module():
    spec = importlib.util.spec_from_file_location("post_lift", POST_LIFT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def be_words(values):
    return b"".join(struct.pack(">f", value) for value in values)


def words(blob):
    return [struct.unpack(">I", blob[index:index + 4])[0]
            for index in range(0, 16, 4)]


def select(a, b, op):
    return [left if (left < right if op == "<" else left > right) else right
            for left, right in zip(a, b)]


def check_guest_be_semantics():
    # Includes negative/positive, mixed lane ordering, infinity, equal/tie, and
    # NaN. The expected words prove ppu_vldf4/ppu_vstf4 preserve guest BE bytes.
    a = [-3.5, 8.0, float("inf"), float("nan")]
    b = [-4.0, 8.0, 7.0, 2.0]
    for op, expected_source in (("<", [-4.0, 8.0, 7.0, 2.0]),
                                (">", [-3.5, 8.0, float("inf"), 2.0])):
        guest_a = be_words(a)
        guest_b = be_words(b)
        # The transformed source snapshots both operands before output. This is
        # required when d aliases either a or b.
        loaded_a = list(struct.unpack(">4f", guest_a))
        loaded_b = list(struct.unpack(">4f", guest_b))
        actual = be_words(select(loaded_a, loaded_b, op))
        expected = be_words(expected_source)
        assert words(actual) == words(expected), (op, words(actual), words(expected))
        assert math.isinf(struct.unpack(">4f", actual)[2]) == (op == ">")


def check_rewrite(module):
    sample = (
        "        { float* d=(float*)&ctx->vr[1]; float* a=(float*)&ctx->vr[1]; "
        "float* b=(float*)&ctx->vr[0]; for(int i=0;i<4;i++) d[i]=a[i]<b[i]?a[i]:b[i]; }\n"
        "        { float* d=(float*)&ctx->vr[0]; float* a=(float*)&ctx->vr[1]; "
        "float* b=(float*)&ctx->vr[0]; for(int i=0;i<4;i++) d[i]=a[i]>b[i]?a[i]:b[i]; }\n"
    )
    with tempfile.TemporaryDirectory() as temp_dir:
        path = Path(temp_dir) / "ppu_recomp_000.cpp"
        path.write_text(sample, encoding="utf-8")
        assert module.patch_vector_minmax_file(path) == 2
        once = path.read_bytes()
        assert module.VECTOR_MINMAX_MARK.encode() in once
        assert b"float* d=(float*)&ctx->vr" not in once
        assert module.patch_vector_minmax_file(path) == 0
        assert path.read_bytes() == once


def check_generated_source(module):
    text = GENERATED.read_text(encoding="utf-8", errors="surrogateescape")
    assert len(module.RAW_VECTOR_MINMAX_RE.findall(text)) == 0
    assert not module.RAW_VECTOR_FLOAT_CAST_RE.search(text)
    assert text.count(module.VECTOR_MINMAX_MARK) == 80
    emitted = list(EMITTED_VECTOR_RE.finditer(text))
    assert sum(match.group("op") == "<" for match in emitted) == 41
    assert sum(match.group("op") == ">" for match in emitted) == 39


def check_native_emitted_fixture():
    """Compile actual helper/source text; this is not a Tokyo Jungle build."""
    compiler = (shutil.which("clang++") or
                ("C:/Program Files/LLVM/bin/clang++.exe" if
                 Path("C:/Program Files/LLVM/bin/clang++.exe").is_file() else None))
    if not compiler:
        raise AssertionError("clang++ is required for the focused native fixture")
    text = GENERATED.read_text(encoding="utf-8", errors="surrogateescape")
    helper_start = text.index("static inline uint32_t ppu_vbswap32")
    helper_end = text.index("static inline void ppu_vldu4", helper_start)
    helpers = text[helper_start:helper_end]
    emitted = list(EMITTED_VECTOR_RE.finditer(text))
    alias_a = next(match for match in emitted
                   if match.group("op") == "<" and match.group("d") == match.group("a")
                   and match.group("d") != match.group("b"))
    alias_b = next(match for match in emitted
                   if match.group("op") == ">" and match.group("d") == match.group("b")
                   and match.group("d") != match.group("a"))
    source = """#include <cstdint>
#include <cstring>
%s
struct TestContext { uint8_t vr[32][16]; };
static void alias_a(TestContext* ctx) { %s }
static void alias_b(TestContext* ctx) { %s }
static void set_be(uint8_t* dst, const uint32_t (&words)[4]) {
    for (int i = 0; i < 4; ++i) {
        dst[i * 4] = (uint8_t)(words[i] >> 24);
        dst[i * 4 + 1] = (uint8_t)(words[i] >> 16);
        dst[i * 4 + 2] = (uint8_t)(words[i] >> 8);
        dst[i * 4 + 3] = (uint8_t)words[i];
    }
}
int main() {
    const uint32_t a[4] = {0xC0600000u, 0x41000000u, 0x7F800000u, 0x80000000u};
    const uint32_t b[4] = {0xC0800000u, 0x41000000u, 0x40E00000u, 0x00000000u};
    const uint32_t want_min[4] = {0xC0800000u, 0x41000000u, 0x40E00000u, 0x00000000u};
    const uint32_t want_max[4] = {0xC0600000u, 0x41000000u, 0x7F800000u, 0x00000000u};
    uint8_t expected[16];
    TestContext ctx{};
    set_be(ctx.vr[%s], a); set_be(ctx.vr[%s], b); set_be(expected, want_min); alias_a(&ctx);
    if (std::memcmp(ctx.vr[%s], expected, sizeof(expected)) != 0) return 1;
    std::memset(&ctx, 0, sizeof(ctx));
    set_be(ctx.vr[%s], a); set_be(ctx.vr[%s], b); set_be(expected, want_max); alias_b(&ctx);
    if (std::memcmp(ctx.vr[%s], expected, sizeof(expected)) != 0) return 2;
    return 0;
}
""" % (helpers, alias_a.group("line"), alias_b.group("line"),
       alias_a.group("a"), alias_a.group("b"), alias_a.group("d"),
       alias_b.group("a"), alias_b.group("b"), alias_b.group("d"))
    with tempfile.TemporaryDirectory() as temp_dir:
        source_path = Path(temp_dir) / "vector_minmax_fixture.cpp"
        binary_path = Path(temp_dir) / "vector_minmax_fixture.exe"
        source_path.write_text(source, encoding="utf-8")
        subprocess.run([compiler, "-std=c++17", str(source_path), "-o", str(binary_path)],
                       check=True, capture_output=True, text=True)
        subprocess.run([str(binary_path)], check=True, capture_output=True, text=True)


def main():
    module = load_module()
    check_guest_be_semantics()
    check_rewrite(module)
    check_generated_source(module)
    check_native_emitted_fixture()
    print("test_post_lift_vector_minmax: PASS")


if __name__ == "__main__":
    sys.exit(main())
