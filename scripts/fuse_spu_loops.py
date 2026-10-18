#!/usr/bin/env python3
"""Fuse image-17's two-block trampoline loop into one local C loop.

Target: generated/spu_lifted/geometryjob17/spu_recomp.c
Loop:   tj_geometryjob17spu_func_0000412C -> _0000415C -> back to _0000412C
        (about 349 iterations per job; every block returned to SPU_DRAIN).

Post-lift patch, stdlib only, safe to run with ``python -I``:

  python -I fuse_spu_loops.py            read-only status (default)
  python -I fuse_spu_loops.py --apply    fuse the loop (idempotent)
  python -I fuse_spu_loops.py --revert   restore the lifted text (idempotent)
  python -I fuse_spu_loops.py --emit F   write the fused block (LF) to F
  python -I fuse_spu_loops.py --check    exit 0 only if fused + runtime contract ok
  --file PATH                            operate on another copy
  --runtime DIR                          ps3recomp runtime/spu to check (default:
                                         ../ps3recomp/runtime/spu)

Guards: both functions are located by exact signature and must match the
lifted text embedded below (SHA-256 pinned); the generated block is pinned
too. Anything else (drift, partial or duplicate markers, unknown statement
shapes or helpers) is refused with exit status 1 and nothing is written.
The runtime rules the loop copies or skips (SPU_DRAIN, SPU_TRANSFER_HOOKS and
the per-transfer hook bodies, ls_hot/yz_lockstep_tick, the LS helpers) are
pinned as RUNTIME_CONTRACT: drift refuses --apply, fails --check and is
reported as a warning in status mode; --revert still works.
"""
from pathlib import Path
import argparse
import hashlib
import os
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FILE = ROOT / "generated" / "spu_lifted" / "geometryjob17" / "spu_recomp.c"

# Exact lifted text (LF-normalized) of the two loop blocks.
ORIG_0000412C = """\
void tj_geometryjob17spu_func_0000412C(spu_context* ctx) {
        ctx->gpr[74] = spu_fma(ctx->gpr[29], ctx->gpr[41], ctx->gpr[28]);
        ctx->gpr[72] = spu_fma(ctx->gpr[29], ctx->gpr[43], ctx->gpr[28]);
        spu_ls_write128(ctx, ctx->gpr[26]._u32[0] + ctx->gpr[12]._u32[0], ctx->gpr[18]);
        ctx->gpr[70] = spu_fma(ctx->gpr[34], ctx->gpr[33], ctx->gpr[42]);
        spu_ls_write128(ctx, ctx->gpr[23]._u32[0] + ctx->gpr[12]._u32[0], ctx->gpr[9]);
        ctx->gpr[68] = spu_fma(ctx->gpr[34], ctx->gpr[37], ctx->gpr[42]);
        ctx->gpr[71] = spu_shufb(ctx->gpr[25], ctx->gpr[18], ctx->gpr[46]);
        ctx->gpr[75] = spu_selb(ctx->gpr[17], ctx->gpr[38], ctx->gpr[47]);
        ctx->gpr[9] = spu_shufb(ctx->gpr[25], ctx->gpr[9], ctx->gpr[45]);
        ctx->gpr[38] = spu_ai(ctx->gpr[11], 32);
        /* branch hint (ignored) */;
        ctx->gpr[64] = spu_cflts(ctx->gpr[74], 142);
        { ctx->pc = 0x415C; g_spu_trampoline_fn = tj_geometryjob17spu_func_0000415C; return; }
}
"""

ORIG_0000415C = """\
void tj_geometryjob17spu_func_0000415C(spu_context* ctx) {
        spu_ls_write128(ctx, ctx->gpr[27]._u32[0] + ctx->gpr[12]._u32[0], ctx->gpr[17]);
        ctx->gpr[25] = spu_cflts(ctx->gpr[72], 142);
        ctx->gpr[67] = spu_shufb(ctx->gpr[16], ctx->gpr[71], ctx->gpr[44]);
        ctx->gpr[69] = spu_cflts(ctx->gpr[70], 157);
        ctx->gpr[24] = spu_ls_read128(ctx, ctx->gpr[14]._u32[0] + 0x10);
        ctx->gpr[65] = spu_cflts(ctx->gpr[68], 157);
        ctx->gpr[22] = spu_ls_read128(ctx, ctx->gpr[14]._u32[0] + 0x0);
        ctx->gpr[17] = spu_ai(ctx->gpr[12], 48);
        ctx->gpr[41] = spu_ls_read128(ctx, ctx->gpr[11]._u32[0] + 0x0);
        ctx->gpr[66] = spu_ai(ctx->gpr[14], 32);
        ctx->gpr[43] = spu_ls_read128(ctx, ctx->gpr[11]._u32[0] + 0x10);
        ctx->gpr[12] = spu_ori(ctx->gpr[17], 0);
        ctx->gpr[33] = spu_ls_read128(ctx, ctx->gpr[13]._u32[0] + 0x0);
        ctx->gpr[4] = spu_rotm(ctx->gpr[64], ctx->gpr[20]);
        ctx->gpr[37] = spu_ls_read128(ctx, ctx->gpr[13]._u32[0] + 0x10);
        ctx->gpr[6] = spu_rotm(ctx->gpr[25], ctx->gpr[20]);
        ctx->gpr[17] = spu_shufb(ctx->gpr[10], ctx->gpr[75], ctx->gpr[21]);
        ctx->gpr[63] = spu_ai(ctx->gpr[13], 32);
        ctx->gpr[9] = spu_shufb(ctx->gpr[19], ctx->gpr[9], ctx->gpr[31]);
        ctx->gpr[11] = spu_ori(ctx->gpr[38], 0);
        ctx->gpr[18] = spu_shufb(ctx->gpr[15], ctx->gpr[67], ctx->gpr[32]);
        ctx->gpr[19] = spu_ori(ctx->gpr[69], 0);
        ctx->gpr[14] = spu_shlqbyi(ctx->gpr[66], 0);
        ctx->gpr[15] = spu_ori(ctx->gpr[65], 0);
        ctx->gpr[62] = spu_shufb(ctx->gpr[4], ctx->gpr[6], ctx->gpr[30]);
        ctx->gpr[38] = spu_ori(ctx->gpr[40], 0);
        ctx->gpr[3] = spu_shufb(ctx->gpr[4], ctx->gpr[6], ctx->gpr[36]);
        ctx->gpr[13] = spu_ori(ctx->gpr[63], 0);
        ctx->gpr[25] = spu_shlqbyi(ctx->gpr[39], 0);
        ctx->gpr[10] = spu_selb(ctx->gpr[3], ctx->gpr[62], ctx->gpr[35]);
        ctx->gpr[16] = spu_rotqbyi(ctx->gpr[10], 4);
        if (ctx->gpr[12]._u32[0] == 0) { ctx->pc = 0x45EC; g_spu_trampoline_fn = tj_geometryjob17spu_func_000045EC; return; }
        ctx->gpr[40] = spu_ori(ctx->gpr[22], 0);
        ctx->gpr[39] = spu_ori(ctx->gpr[24], 0);
        { ctx->pc = 0x412C; g_spu_trampoline_fn = tj_geometryjob17spu_func_0000412C; return; }
}
"""

LOOP = {
    "tag": "tj_geometryjob17spu 0x412C<->0x415C",
    "version": "v1",
    "prefix": "tj_geometryjob17spu_func_",
    "loop_fn": "tj_geometryjob17spu_loop_0000412C",
    "yield_fn": "tj_geometryjob17spu_loop_must_yield",
    "blocks": [0x412C, 0x415C],  # cycle order; entry 0 is the loop head
    "original": {0x412C: ORIG_0000412C, 0x415C: ORIG_0000415C},
    "sha256": {
        0x412C: "e8a1371092e3954f9453cdd285619b3aaa07da55c4cd9ea5eac8257a4545bea8",
        0x415C: "c3cb1b5674f3b090f43793cabf7983363d233e9d9da32701884b9c12bbaa4a5a",
    },
    # SHA-256 of the generated block (LF); pins the generator's output.
    "fused_sha256": "e11c98d0d24d19e0eb7b0582d7278c3debda9238c796589f839bf10607f65d53",
}

# Runtime contract the fused loop relies on but does not contain.  The loop
# inlines a copy of SPU_DRAIN's interrupt test plus the lockstep "not off" test
# (ls_hot), and skips SPU_TRANSFER_HOOKS at its internal boundaries because the
# hooks are inert there for image 17 under the default environment.  Its
# register locals are safe only while spu_ls_read128/spu_ls_write128 read no
# guest register but r0.  Each item is pinned (LF-normalized text, SHA-256);
# --apply refuses and status/--check report drift when any of them changes, so
# a new per-transfer hook or LS diagnostic forces a fresh review of the fusion.
RUNTIME_DIR = ROOT.parent / "ps3recomp" / "runtime" / "spu"
RUNTIME_CONTRACT = [
    # (file, kind, anchor line, sha256 of the extracted text)
    ("spu_context.h", "func", "static inline u128 spu_ls_read128(const spu_context* ctx, uint32_t lsa)",
     "2c77c3b7726d092bbaf02c76ca2264f34770958110358657c0b6cf61456a1e9b"),
    ("spu_context.h", "func", "static inline void spu_ls_write128(spu_context* ctx, uint32_t lsa, u128 val)",
     "c5fd1c140e73dbc18afb73a5a452f3868d1523d705d4b299d09d8dc26010bded"),
    ("spu_context.h", "macro", "#define SPU_TASK_LAUNCH_CHECK(ctx, tf) do {",
     "a71a44c152eec1650798b676303217994ed9027d584c4cc984fbbeaf470445d7"),
    ("spu_context.h", "block:#endif", "#if defined(TJ_DIAG_GEOMETRY_FAST_DRAIN)",
     "9639c037452b88ef4c71cb42981c614a3308ee39297a82d4bbf994eddc313185"),
    ("spu_context.h", "macro", "#define SPU_DRAIN(ctx) do {",
     "02c57ee62ae7e6b0bbe9c84bb3d6305e3b79afd0238470931a1d9579a380d8b0"),
    ("spu_drain.c", "block:};", "static const u32 s_tj_geometry_trace_pcs[] = {",
     "afdb9ccadc7d2bc25f8400a85576b4bd592ba6b34675018588a0ce8096371f83"),
    ("spu_drain.c", "func", "static void tj_geometry_stage_trace(const spu_context* ctx)",
     "9a4bebf53f86749ce5698f5e6d5c19ec1f8a7cf331edb1bb9081c4a28e22ac4c"),
    ("spu_drain.c", "func", "void tj_animation_stage_trace_at(const spu_context* ctx, u32 pc)",
     "22286f9d14f9f8d849d88e418db3fffea7c8f640c8656f2e58ee27fce5eaaec5"),
    ("spu_drain.c", "func", "static void tj_pc_hist_transfer(spu_context* ctx)",
     "f25375355d37827afba72b7213bf422a76904427bc9e965a5bdc818caeea93a4"),
    ("spu_drain.c", "func", "static void tj_pc_time_transfer(spu_context* ctx)",
     "d3ad9ea5693bc33f6b35e012400bbbc3c34752a2a096b5b309dca1791db4b3d3"),
    ("spu_drain.c", "func", "static void tj_late_image13_transfer_trace(spu_context* ctx)",
     "0d2de1a96a0f20d340de06815ac290cbca4198ab4e27d9c9caa714ba9e1c7b9d"),
    ("spu_drain.c", "func", "void spu_task_launch_check(spu_context* ctx, void* fn)",
     "9b3f7e0ec59ec0ceba235fe5d00ec26f73b01ec7f198d526461bd07124283cb9"),
    ("spu_lockstep.c", "func", "static int ls_hot(void)",
     "074f158c6c3ed4b43a174b5ee15023827083f35e223c7156c149093d1ef07703"),
    ("spu_lockstep.c", "func", "void yz_lockstep_tick(spu_context* ctx)",
     "a6872038fcce33f8120e73d5fa81eb940cc21b1e163b666f0df67aad7e4aecfa"),
]

# Helpers that are pure functions of by-value u128/int arguments (no ctx).
PURE_HELPERS = {
    "spu_fma", "spu_shufb", "spu_selb", "spu_ai", "spu_cflts", "spu_rotm",
    "spu_ori", "spu_shlqbyi", "spu_rotqbyi",
}
# LS helpers take ctx: they read/write ctx->ls only, plus (diagnostic builds)
# ctx->pc, ctx->gpr[0], image/policy fields and the 0x1C0 job-kernel fields.
LS_HELPERS = {"spu_ls_read128", "spu_ls_write128"}

IND = " " * 8
FN = r"tj_geometryjob17spu_func_[0-9A-F]{8}"
RE_ASSIGN = re.compile(r"^ {8}ctx->gpr\[(\d+)\] = (spu_[a-z0-9_]+\(.*\));$")
RE_STORE = re.compile(r"^ {8}(spu_ls_write128\(ctx, .*\);)$")
RE_HINT = re.compile(r"^ {8}(/\* branch hint \(ignored\) \*/;)$")
RE_XFER = re.compile(r"^ {8}\{ ctx->pc = (0x[0-9A-F]+); g_spu_trampoline_fn = (" + FN
                     + r"); return; \}$")
RE_CONDX = re.compile(r"^ {8}if \((.+)\) \{ ctx->pc = (0x[0-9A-F]+); g_spu_trampoline_fn = ("
                      + FN + r"); return; \}$")
RE_GPR = re.compile(r"ctx->gpr\[(\d+)\]")
RE_CALL = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\(")


class Refuse(Exception):
    pass


def sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def fn_name(pc):
    return f"{LOOP['prefix']}{pc:08X}"


def header(pc):
    return f"void {fn_name(pc)}(spu_context* ctx) {{"


def markers():
    begin = f"/* TJ_SPU_LOOP_FUSE BEGIN {LOOP['tag']} {LOOP['version']}"
    end = f"/* TJ_SPU_LOOP_FUSE END {LOOP['tag']} {LOOP['version']} */"
    return begin, end


def rewrite(expr, read):
    """ctx->gpr[N] -> rN; only whitelisted helpers; ctx only as LS-helper arg."""
    for name in RE_CALL.findall(expr):
        if name not in PURE_HELPERS and name not in LS_HELPERS:
            raise Refuse(f"helper not whitelisted: {name}")
    read.update(int(n) for n in RE_GPR.findall(expr))
    out = RE_GPR.sub(lambda m: f"r{int(m.group(1))}", expr)
    rest = re.sub(r"\bspu_ls_(?:read|write)128\(ctx, ", "", out)
    if re.search(r"\bctx\b", rest):
        raise Refuse(f"ctx used outside an LS helper: {expr}")
    return out


def check_originals():
    for pc in LOOP["blocks"]:
        if sha(LOOP["original"][pc]) != LOOP["sha256"][pc]:
            raise Refuse(f"embedded original text for {fn_name(pc)} does not match its pin")


def build_fused():
    """Mechanically derive the fused block (LF) from the pinned lifted text."""
    check_originals()
    blocks = LOOP["blocks"]
    read, written, parsed = set(), set(), []
    for i, pc in enumerate(blocks):
        nxt = blocks[(i + 1) % len(blocks)]
        lines = LOOP["original"][pc].split("\n")
        if lines[0] != header(pc) or lines[-2:] != ["}", ""]:
            raise Refuse(f"unexpected function shape: {fn_name(pc)}")
        body, stmts = lines[1:-2], []
        for j, line in enumerate(body):
            m = RE_XFER.match(line)
            if m:
                tpc, tfn = int(m.group(1), 16), m.group(2)
                if j != len(body) - 1 or tpc != nxt or tfn != fn_name(nxt):
                    raise Refuse(f"{fn_name(pc)}: transfer is not a fall into the next loop block")
                stmts.append(("next", nxt))
                continue
            if j == len(body) - 1:
                raise Refuse(f"{fn_name(pc)}: block must end with an unconditional transfer")
            m = RE_CONDX.match(line)
            if m:
                tpc, tfn = int(m.group(2), 16), m.group(3)
                if tpc in blocks or tfn != fn_name(tpc):
                    raise Refuse(f"{fn_name(pc)}: unsupported conditional transfer {line.strip()}")
                stmts.append(("exit", rewrite(m.group(1), read), tpc, tfn))
                continue
            m = RE_ASSIGN.match(line)
            if m:
                reg = int(m.group(1))
                stmts.append(("stmt", f"r{reg} = {rewrite(m.group(2), read)};"))
                written.add(reg)
                continue
            m = RE_STORE.match(line)
            if m:
                stmts.append(("stmt", rewrite(m.group(1), read)))
                continue
            m = RE_HINT.match(line)
            if m:
                stmts.append(("stmt", m.group(1)))
                continue
            raise Refuse(f"{fn_name(pc)}: unsupported statement: {line.strip()}")
        parsed.append((pc, stmts))
    if 0 in written:
        raise Refuse("r0 is written; LS diagnostics read ctx->gpr[0], keep it write-through")

    loop_fn, yield_fn = LOOP["loop_fn"], LOOP["yield_fn"]
    head = blocks[0]
    rw = sorted(written)
    ro = sorted(read - written)
    begin, end = markers()
    out = [
        begin,
        " * Post-lift patch from tokyojungle/scripts/fuse_spu_loops.py; do not edit",
        " * by hand. `fuse_spu_loops.py --revert` restores the lifted text, which the",
        " * script keeps verbatim and SHA-256 pinned.",
        " *",
        " * Guest loop 0x412C -> 0x415C -> 0x412C (about 349 iterations per job) used",
        " * to return to SPU_DRAIN after every block. Both blocks now run as one",
        " * local loop, the guest registers it touches held in locals:",
        " *  - same statements, same order; LS accesses still go through",
        " *    spu_ls_read128/spu_ls_write128, which never read the cached registers",
        " *    (r0 is not written here) and see ctx->pc set at every block boundary;",
        " *  - every exit stores all written registers back to ctx->gpr and leaves",
        " *    ctx->pc and g_spu_trampoline_fn exactly as the lifted block exit did;",
        " *  - at each internal boundary the loop yields to SPU_DRAIN with that",
        " *    original transfer when an SPU interrupt is pending (int_enable &&",
        " *    event_status & event_mask, read fresh) or lockstep is not off",
        " *    (g_yz_lockstep_on != 0), so hooks + spu_take_interrupt run unchanged;",
        " *  - otherwise the internal boundary skips SPU_TRANSFER_HOOKS. For image 17",
        " *    at these PCs they only feed opt-in diagnostics (e.g. TJ_SPU_PC_HIST,",
        " *    TJ_SPU_PC_TIME, SPU_DRAIN_TRACE_IMG), which now see the loop's entry",
        " *    and exit transfers instead of two transfers per guest iteration. */",
        "extern volatile int g_yz_lockstep_on; /* spu_lockstep.c: -1 unarmed, 0 off, 1 on */",
        "",
        f"static SPU_FORCEINLINE int {yield_fn}(const spu_context* ctx) {{",
        IND + "return g_yz_lockstep_on != 0 ||",
        IND + "       (*(volatile const uint32_t*)&ctx->int_enable &&",
        IND + "        (*(volatile const uint32_t*)&ctx->event_status &",
        IND + "         *(volatile const uint32_t*)&ctx->event_mask));",
        "}",
        "",
        f"static SPU_FORCEINLINE void {loop_fn}(spu_context* ctx, uint32_t entry_pc) {{",
    ]
    for i in range(0, len(rw), 6):
        out.append(IND + "u128 " + ", ".join(f"r{r} = ctx->gpr[{r}]" for r in rw[i:i + 6]) + ";")
    for i in range(0, len(ro), 6):
        out.append(IND + "const u128 " + ", ".join(f"r{r} = ctx->gpr[{r}]" for r in ro[i:i + 6]) + ";")
    out.append(IND + "void (*next)(spu_context*) = 0;")
    for pc in blocks[1:]:
        out.append(IND + f"if (entry_pc == 0x{pc:X}u) goto blk_{pc:08X};")
    for pc, stmts in parsed:
        out.append(f"blk_{pc:08X}:")
        for st in stmts:
            if st[0] == "stmt":
                out.append(IND + st[1])
            elif st[0] == "exit":
                _, cond, tpc, tfn = st
                out.append(IND + f"if ({cond}) {{ ctx->pc = 0x{tpc:X}; next = {tfn}; goto leave; }}")
            else:
                nxt = st[1]
                out.append(IND + f"ctx->pc = 0x{nxt:X};")
                out.append(IND + f"if ({yield_fn}(ctx)) {{ next = {fn_name(nxt)}; goto leave; }}")
                if nxt == head:
                    out.append(IND + f"goto blk_{head:08X};")
    out.append("leave:")
    for i in range(0, len(rw), 4):
        out.append(IND + " ".join(f"ctx->gpr[{r}] = r{r};" for r in rw[i:i + 4]))
    out.append(IND + "g_spu_trampoline_fn = next;")
    out.append("}")
    for pc in blocks:
        out += ["", header(pc), IND + f"{loop_fn}(ctx, 0x{pc:X}u);", "}"]
    out.append(end)
    text = "\n".join(out) + "\n"
    if sha(text) != LOOP["fused_sha256"]:
        raise Refuse("generated block does not match its pin (generator changed?)")
    return text


def original_span(text):
    """Return (start, end, newline) of the contiguous original 412C+415C text."""
    spans = []
    for pc in LOOP["blocks"]:
        if text.count(header(pc)) != 1:
            raise Refuse(f"expected exactly one definition of {fn_name(pc)}")
        pat = re.compile(r"^" + re.escape(header(pc)) + r"\r?\n.*?^\}\r?\n", re.M | re.S)
        found = list(pat.finditer(text))
        if len(found) != 1:
            raise Refuse(f"cannot isolate {fn_name(pc)}")
        body = found[0].group()
        if sha(body.replace("\r\n", "\n")) != LOOP["sha256"][pc]:
            raise Refuse(f"{fn_name(pc)} drifted from the pinned lifted text")
        spans.append((found[0].start(), found[0].end(), "\r\n" if "\r\n" in body else "\n"))
    (s0, e0, nl0), (s1, e1, nl1) = spans
    if nl0 != nl1 or text[e0:s1] != nl0:
        raise Refuse("the two loop functions are not adjacent (one blank line apart)")
    return s0, e1, nl0


def fused_span(text):
    begin, end = markers()
    tag_begin = f"/* TJ_SPU_LOOP_FUSE BEGIN {LOOP['tag']} "
    tag_end = f"/* TJ_SPU_LOOP_FUSE END {LOOP['tag']} "
    nb, ne = text.count(tag_begin), text.count(tag_end)
    if nb == 0 and ne == 0:
        return None
    if nb != 1 or ne != 1 or text.count(begin) != 1 or text.count(end) != 1:
        raise Refuse("marker drift: partial, duplicate or other-version fusion markers")
    s = text.index(begin)
    e = text.index(end) + len(end)
    if e < s:
        raise Refuse("marker drift: END before BEGIN")
    nl = "\r\n" if text[e:e + 2] == "\r\n" else "\n"
    if text[e:e + len(nl)] != nl or (s and text[s - 1] != "\n"):
        raise Refuse("marker drift: markers not on their own lines")
    block = text[s:e + len(nl)]
    if block.replace("\r\n", "\n") != build_fused():
        raise Refuse("fused block differs from what this script generates")
    for pc in LOOP["blocks"]:
        if text.count(header(pc)) != 1 or not (s < text.index(header(pc)) < e):
            raise Refuse(f"{fn_name(pc)} must be defined once, inside the fused block")
    return s, e + len(nl), nl


def unfused_text(text):
    """Return (text with the lifted 412C/415C restored, was_fused).

    Verifies the fused block (or, when unfused, the pinned originals) and
    refuses on any drift.  For tools that need the plain lift, such as
    work/make_geometry_tail_variant.py; independent of the runtime contract.
    """
    span = fused_span(text)
    if not span:
        original_span(text)
        return text, False
    s, e, nl = span
    orig = "\n".join(LOOP["original"][pc] for pc in LOOP["blocks"])
    new = text[:s] + orig.replace("\n", nl) + text[e:]
    if fused_span(new) or not original_span(new):
        raise Refuse("internal: unfused text does not verify")
    return new, True


def extract_item(text, kind, anchor):
    """Return the LF text of one pinned runtime item, or None if not unique."""
    lines = text.replace("\r\n", "\n").split("\n")
    starts = [i for i, line in enumerate(lines)
              if line.rstrip().rstrip("\\").rstrip() == anchor]
    if len(starts) != 1:
        return None
    i = starts[0]
    if kind == "macro":
        j = i
        while j < len(lines) and lines[j].rstrip().endswith("\\"):
            j += 1
    else:
        end = "}" if kind == "func" else kind.split(":", 1)[1]
        j = i + 1
        while j < len(lines) and lines[j].rstrip() != end:
            j += 1
    if j >= len(lines):
        return None
    return "\n".join(lines[i:j + 1]) + "\n"


def runtime_drift(runtime_dir):
    """List contract items whose text is missing, ambiguous or changed."""
    drift, cache = [], {}
    for name, kind, anchor, want in RUNTIME_CONTRACT:
        path = runtime_dir / name
        if name not in cache:
            try:
                cache[name] = path.read_bytes().decode("utf-8")
            except (OSError, UnicodeDecodeError):
                cache[name] = None
        text = cache[name]
        got = extract_item(text, kind, anchor) if text is not None else None
        if got is None:
            drift.append(f"{name}: {anchor!r} missing or not unique")
        elif sha(got) != want:
            drift.append(f"{name}: {anchor!r} changed (sha256 {sha(got)})")
    return drift


def write_atomic(path, before, after):
    if path.read_bytes() != before:
        raise Refuse("file changed while it was being patched")
    tmp = path.with_name(path.name + ".fuse_tmp")
    tmp.write_bytes(after)
    os.replace(tmp, path)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true", help="fuse the loop (idempotent)")
    mode.add_argument("--revert", action="store_true", help="restore the lifted text (idempotent)")
    mode.add_argument("--emit", type=Path, help="write the fused block (LF) to this path")
    mode.add_argument("--check", action="store_true",
                      help="exit 0 only if the file is fused and the runtime contract matches")
    ap.add_argument("--file", type=Path, default=DEFAULT_FILE, help="lifted spu_recomp.c to patch")
    ap.add_argument("--runtime", type=Path, default=RUNTIME_DIR,
                    help="ps3recomp runtime/spu directory holding the pinned contract")
    args = ap.parse_args(argv)
    try:
        fused = build_fused()
        if args.emit:
            args.emit.write_text(fused, encoding="utf-8", newline="\n")
            print(f"emitted fused block sha256={sha(fused)} -> {args.emit}")
            return 0
        raw = args.file.read_bytes()
        text = raw.decode("utf-8")
        span = fused_span(text)
        state = "fused" if span else "original"
        if not span:
            span = original_span(text)
        s, e, nl = span
        print(f"{args.file}: {state} ({LOOP['tag']} {LOOP['version']}), "
              f"file sha256={hashlib.sha256(raw).hexdigest()}")
        drift = runtime_drift(args.runtime)
        if drift:
            print(f"WARNING: runtime contract drift in {args.runtime} ({len(drift)} item(s)); "
                  "re-review the fused loop against SPU_DRAIN/hooks/LS helpers, then re-pin:",
                  file=sys.stderr)
            for item in drift:
                print(f"  - {item}", file=sys.stderr)
        else:
            print(f"runtime contract: ok ({len(RUNTIME_CONTRACT)} items pinned)")
        if args.check:
            ok = state == "fused" and not drift
            print("check: " + ("ok" if ok else "FAILED"))
            return 0 if ok else 1
        if args.apply and drift:
            raise Refuse("runtime contract drifted; the fused loop's copied/skipped runtime "
                         "rules may no longer hold (see warnings above)")
        if args.apply and state == "original":
            new = text[:s] + fused.replace("\n", nl) + text[e:]
        elif args.revert and state == "fused":
            new = unfused_text(text)[0]
        else:
            if args.apply or args.revert:
                print("no change needed")
            return 0
        out = new.encode("utf-8")
        # Re-parse the result before writing: it must be in the target state.
        if args.apply and not fused_span(new):
            raise Refuse("internal: applied text does not verify")
        if args.revert and (fused_span(new) or not original_span(new)):
            raise Refuse("internal: reverted text does not verify")
        write_atomic(args.file, raw, out)
        print(f"{'applied' if args.apply else 'reverted'}: sha256 "
              f"{hashlib.sha256(raw).hexdigest()} -> {hashlib.sha256(out).hexdigest()}")
        return 0
    except Refuse as err:
        print(f"REFUSED: {err}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
