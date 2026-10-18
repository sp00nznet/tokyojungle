#!/usr/bin/env python3
"""Idempotent post-lift patches for Tokyo Jungle generated PPU sources.

Run this after lifting and before compiling.  Generated sources are disposable:
runtime bridges belong here, never in a hand-only generated edit.
"""
import argparse
import glob
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RECOMP_GLOB = os.path.join(ROOT, "generated", "ppu_recomp_*.cpp")
SIG = re.compile(r"void func_([0-9A-F]{8})\(ppu_context\* ctx\) \{\s*$")

CRT_SKIPS = {}
HEAP_PATCHES = {
    "0024DDC0": ("tj_heap_malloc", "malloc"),
    "0024DD94": ("tj_heap_free", "free"),
    "0024DCBC": ("tj_heap_free", "free (realloc-internal)"),
    "0024DCEC": ("tj_heap_memalign", "memalign"),
    "0024DD24": ("tj_heap_realloc", "realloc"),
    "0023E380": ("tj_abort", "abort"),
    "0025279C": ("tj_pure_virtual", "__cxa_pure_virtual"),
}
VRFIZ_PATCHES = {
    "0013293C": (
        "/* TODO: vrfiz v0, v0, v1 */;",
        "{ float b[4],d[4]; ppu_vldf4(&ctx->vr[1],b); for(int i=0;i<4;i++) d[i]=truncf(b[i]); ppu_vstf4(&ctx->vr[0],d); } /* tj-ppu-vrfiz */;",
    ),
    "00132A2C": (
        "/* TODO: vrfiz v1, v0, v13 */;",
        "{ float b[4],d[4]; ppu_vldf4(&ctx->vr[13],b); for(int i=0;i<4;i++) d[i]=truncf(b[i]); ppu_vstf4(&ctx->vr[1],d); } /* tj-ppu-vrfiz */;",
    ),
}

# Generated vminfp/vmaxfp sites used host-endian float aliases directly over
# big-endian guest vector storage. Keep this exact to avoid changing any other
# vector operation during the targeted repair pass.
VECTOR_MINMAX_MARK = "/* tj-ppu-vminmax-be */"
VECTOR_MINMAX_EXPECTED = {"<": 41, ">": 39}
RAW_VECTOR_MINMAX_RE = re.compile(
    r"(?m)^(?P<indent>\s*)\{ float\* d=\(float\*\)&ctx->vr\[(?P<d>\d+)\]; "
    r"float\* a=\(float\*\)&ctx->vr\[(?P<a>\d+)\]; "
    r"float\* b=\(float\*\)&ctx->vr\[(?P<b>\d+)\]; "
    r"for\(int i=0;i<4;i\+\+\) d\[i\]=a\[i\](?P<op>[<>])b\[i\]\?a\[i\]:b\[i\]; \}(?P<eol>\r?)$"
)
RAW_VECTOR_FLOAT_CAST_RE = re.compile(
    r"float\*\s+[dab]\s*=\s*\(float\*\)\s*&ctx->vr\["
)
MARKED_VECTOR_MINMAX_RE = re.compile(
    r"\{ float a\[4\],b\[4\],d\[4\]; ppu_vldf4\(&ctx->vr\[\d+\],a\); "
    r"ppu_vldf4\(&ctx->vr\[\d+\],b\); for\(int i=0;i<4;i\+\+\) "
    r"d\[i\]=a\[i\](?P<op>[<>])b\[i\]\?a\[i\]:b\[i\]; "
    r"ppu_vstf4\(&ctx->vr\[\d+\],d\); \} /\* tj-ppu-vminmax-be \*/"
)

TASK_WAKE_ADDR = "0025C904"
TASK_WAKE_MARK = "/* tj-spu-task-group-wake */"
TASK_WAKE_AFTER = "vm_write32(ctx->gpr[3] + 0x2C, ctx->gpr[0]);"
TASK_WAKE_PRE_CLEAR_MARK = "/* tj-spu-task-group-reservation-write */"
TASK_WAKE_PRE_CLEAR_DECL = "extern void tj_publish_spu_task_group_write(uint32_t);"
TASK_WAKE_PRE_CLEAR_CALL = "tj_publish_spu_task_group_write((uint32_t)ctx->gpr[3]);"
TASK_WAKE_DECL = "extern void tj_signal_spu_task_group(uint32_t);"
TASK_WAKE_CALL = "tj_signal_spu_task_group((uint32_t)ctx->gpr[3]);"
TASK_SIGNAL_ADDR = "0025CA9C"
TASK_SIGNAL_MARK = "/* tj-spu-task-signal-substitution */"
TASK_SIGNAL_CALL = "ctx->lr = 0x0025CAE4; func_0025EA20(ctx); DRAIN_TRAMPOLINE(ctx);"
COMPLETION_WAIT_ADDR = "0025CA50"
COMPLETION_WAIT_MARK = "/* tj-spu-completion-wait-trace */"
COMPLETION_WAIT_ENTRY_AFTER = "if ((!((ctx->cr >> 0) & 2))) goto loc_0025CA88;"
COMPLETION_WAIT_POLL_AFTER = "ctx->gpr[29] = vm_read32(ctx->gpr[31] + 0x2C);"
COMPLETION_WAIT_EXIT_LABEL = "loc_0025CA88:"
COMPLETION_WAIT_INITIAL_READ = "ctx->gpr[0] = vm_read32(ctx->gpr[3] + 0x2C);"
COMPLETION_WAIT_LIFECYCLE_BEGIN_DECL = "extern uint32_t tj_spu_completion_wait_begin(uint32_t);"
COMPLETION_WAIT_LIFECYCLE_END_DECL = "extern void tj_spu_completion_wait_end(uint32_t);"
COMPLETION_WAIT_LIFECYCLE_BEGIN = "ctx->gpr[0] = tj_spu_completion_wait_begin((uint32_t)ctx->gpr[3]);"
COMPLETION_WAIT_LIFECYCLE_ACTIVE = "tj_completion_wait_lifecycle_active = ((uint32_t)ctx->gpr[0] == 0);"
COMPLETION_WAIT_LIFECYCLE_END = "if (tj_completion_wait_lifecycle_active) tj_spu_completion_wait_end((uint32_t)ctx->gpr[31]);"
COMPLETION_WAIT_LEGACY_BEGIN = "uint32_t tj_completion_wait_completion = tj_spu_completion_wait_begin((uint32_t)ctx->gpr[31]);"
COMPLETION_WAIT_LEGACY_ACTIVE = "tj_completion_wait_lifecycle_active = true;"
COMPLETION_WAIT_LEGACY_RECHECK = "if (tj_completion_wait_completion != 0) goto loc_0025CA88;"
COMPLETION_WAIT_PROLOGUE = [
    "uint64_t _cs_29 = ctx->gpr[29];",
    "uint64_t _cs_31 = ctx->gpr[31];",
    "vm_write64(ctx->gpr[1] + -0x8, ctx->gpr[31]);",
    "ctx->gpr[0] = ctx->lr;",
    "vm_write64(ctx->gpr[1] + -0x18, ctx->gpr[29]);",
]
SCENE_STATE_ADDR = "001F0BC0"
SCENE_STATE_MARK = "/* tj-survival-scene-state */"
SCENE_JOIN_AFTER = "ctx->lr = 0x001F0EBC; func_0017C0E4(ctx); DRAIN_TRAMPOLINE(ctx);"
TITLE_GATE_SCENE_KEY = "title-gate-scene-evidence"
TITLE_GATE_SCENE_MARK = "/* tj-title-scene-evidence */"
TITLE_GATE_SCENE_CALL = "ctx->lr = 0x001F0E40; func_001EFF74(ctx); DRAIN_TRAMPOLINE(ctx);"
TITLE_GATE_SCENE_NORMALIZE = "ctx->gpr[3] = (uint64_t)ppc_rlwinm((uint32_t)ctx->gpr[3], 0, 24, 31);"
FADE_STATE_ADDR = "00183524"
FADE_STATE_MARK = "/* tj-survival-fade-state */"
FADE_TRACE_SITES = {
    "ctx->lr = 0x0018355C; func_001834FC(ctx); DRAIN_TRAMPOLINE(ctx);": 1,
    "ctx->lr = 0x001835AC; func_00181F38(ctx); DRAIN_TRAMPOLINE(ctx);": 2,
    "ctx->lr = 0x001836C4; func_00183340(ctx); DRAIN_TRAMPOLINE(ctx);": 3,
    "vm_write8(ctx->gpr[31] + 0x2C, ctx->gpr[25]);": 4,
}
NO_MEDIA_COMPLETE_ADDR = "001EFC5C"
NO_MEDIA_COMPLETE_MARK = "/* tj-cellSail-no-media-complete */"
TITLE_STATE_ADDR = "001E7404"
TITLE_STATE_MARK = "/* tj-title-menu-state-v2 */"
TITLE_STATE_LEGACY_MARK = "/* tj-title-menu-state */"
TITLE_STATE_RETURN_AFTER = "ctx->lr = ctx->gpr[0];"
TITLE_GATE_INPUT_KEY = "title-gate-input-poll"
TITLE_GATE_INPUT_MARK = "/* tj-title-ready-input-poll */"
TITLE_GATE_INPUT_NEXT = "ctx->lr = 0x001E93E8; func_0011FF50(ctx); DRAIN_TRAMPOLINE(ctx);"
TITLE_GATE_INPUT_DECL = "extern void tj_title_ready_input_poll(ppu_context*, uint32_t, uint32_t);"
TITLE_GATE_INPUT_CALL = "tj_title_ready_input_poll(ctx, (uint32_t)ctx->gpr[31], (uint32_t)ctx->gpr[24]);"
MAIN_MENU_GATE_KEY = "main-menu-gate-input-poll"
MAIN_MENU_GATE_MARK = "/* tj-main-menu-ready-input-poll */"
MAIN_MENU_GATE_BRANCH = "if ((!((ctx->cr >> 0) & 2))) goto loc_001E9190;"
MAIN_MENU_GATE_NEXT = "ctx->gpr[0] = vm_read8(ctx->gpr[24] + 0x0);"
MAIN_MENU_GATE_DECL = "extern void tj_main_menu_ready_input_poll(ppu_context*, uint32_t, uint32_t);"
MAIN_MENU_GATE_CALL = "tj_main_menu_ready_input_poll(ctx, (uint32_t)ctx->gpr[31], (uint32_t)ctx->gpr[24]);"


def function_block(lines, start):
    end = start + 1
    while end < len(lines) and lines[end].rstrip("\n") != "}":
        end += 1
    return lines[start:end + 1], end


def entry_trace(block, mark, declaration, call):
    if any(mark in line for line in block):
        return block, False
    return [block[0], "        " + mark + "\n",
            "        extern void " + declaration + ";\n",
            "        " + call + ";\n"] + block[1:], True


def remove_legacy_title_entry_trace(block):
    for index, line in enumerate(block):
        if TITLE_STATE_LEGACY_MARK not in line:
            continue
        expected = [
            "extern void tj_title_menu_state_trace(ppu_context*, uint32_t);",
            "tj_title_menu_state_trace(ctx, (uint32_t)ctx->gpr[3]);",
        ]
        actual = [entry.strip() for entry in block[index + 1:index + 3]]
        if actual != expected:
            raise RuntimeError("unexpected legacy title trace shape")
        return block[:index] + block[index + 3:]
    return block


def patch_vector_minmax_file(path):
    """Repair only raw float vminfp/vmaxfp code in one generated PPU source."""
    with open(path, "r", encoding="utf-8", errors="surrogateescape", newline="") as src:
        text = src.read()

    def replacement(match):
        groups = match.groupdict()
        return (
            groups["indent"] + "{{ float a[4],b[4],d[4]; "
            "ppu_vldf4(&ctx->vr[{a}],a); ppu_vldf4(&ctx->vr[{b}],b); "
            "for(int i=0;i<4;i++) d[i]=a[i]{op}b[i]?a[i]:b[i]; "
            "ppu_vstf4(&ctx->vr[{d}],d); }} {mark}{eol}"
        ).format(mark=VECTOR_MINMAX_MARK, **groups)

    patched_text, count = RAW_VECTOR_MINMAX_RE.subn(replacement, text)
    if RAW_VECTOR_FLOAT_CAST_RE.search(patched_text):
        raise RuntimeError("unexpected raw float vector cast remains in %s" % path)
    if patched_text != text:
        # Write encoded bytes directly: raw CRLF bytes from newline='' survive
        # unchanged outside the exact lines this pass replaces.
        with open(path, "wb") as dst:
            dst.write(patched_text.encode("utf-8", "surrogateescape"))
    return count


def patch_vector_minmax_files(files):
    """Verify the known current cohort, then rewrite only that cohort."""
    observed = {"<": 0, ">": 0}
    for path in files:
        with open(path, "r", encoding="utf-8", errors="surrogateescape", newline="") as src:
            text = src.read()
        for match in RAW_VECTOR_MINMAX_RE.finditer(text):
            observed[match.group("op")] += 1
        for match in MARKED_VECTOR_MINMAX_RE.finditer(text):
            observed[match.group("op")] += 1
    if observed != VECTOR_MINMAX_EXPECTED:
        raise RuntimeError("unexpected vector min/max cohort: %r (expected %r)" %
                           (observed, VECTOR_MINMAX_EXPECTED))
    return sum(patch_vector_minmax_file(path) for path in files)


GEOMETRY_PRODUCER_SITES = {"0008C660": 0, "0008922C": 1, "00083B8C": 2,
                           "001FCC5C": 3}


def patch_geometry_producer_file(path):
    # Binary edit: preserve legacy non-UTF8 comments and exact line endings.
    with open(path, "rb") as src:
        original = src.read()
    result, changed = original, 0
    for address, site in GEOMETRY_PRODUCER_SITES.items():
        signature = ("void func_%s(ppu_context* ctx) {" % address).encode()
        count = result.count(signature)
        if not count:
            continue
        if count != 1:
            raise RuntimeError("ambiguous geometry producer " + address)
        start = result.index(signature) + len(signature)
        newline = b"\r\n" if result[start:start + 2] == b"\r\n" else b"\n"
        if result[start:start + len(newline)] != newline:
            raise RuntimeError("unexpected geometry producer signature " + address)
        at = start + len(newline)
        addition = newline.join(line.encode() for line in (
            "        /* tj-geometry-producer-trace */",
            "        extern void tj_geometry_producer_trace(ppu_context*, uint32_t);",
            "        tj_geometry_producer_trace(ctx, %d);" % site, ""))
        if result[at:at + len(addition)] == addition:
            continue
        if b"tj-geometry-producer-trace" in result[at:at + 100]:
            raise RuntimeError("unexpected geometry producer hook " + address)
        result = result[:at] + addition + result[at:]
        changed += 1
    if changed:
        with open(path, "wb") as dst:
            dst.write(result)
    return changed


MLAA_SKIP_ADDR = "00153834"
MLAA_SKIP_LINES = (
    "        /* tj-mlaa-skip */",
    "        extern bool tj_mlaa_skip(void);",
    "        if (tj_mlaa_skip()) return;",
)


def patch_mlaa_skip_file(path):
    """Insert the env-gated per-frame MLAA skip at func_00153834 entry."""
    # Binary edit: preserve legacy non-UTF8 comments and exact line endings.
    with open(path, "rb") as src:
        original = src.read()
    signature = ("void func_%s(ppu_context* ctx) {" % MLAA_SKIP_ADDR).encode()
    count = original.count(signature)
    if not count:
        return 0
    if count != 1:
        raise RuntimeError("ambiguous MLAA wrapper " + MLAA_SKIP_ADDR)
    start = original.index(signature) + len(signature)
    newline = b"\r\n" if original[start:start + 2] == b"\r\n" else b"\n"
    if original[start:start + len(newline)] != newline:
        raise RuntimeError("unexpected MLAA wrapper signature")
    at = start + len(newline)
    addition = newline.join(line.encode() for line in MLAA_SKIP_LINES + ("",))
    if original[at:at + len(addition)] == addition:
        return 1
    if b"tj-mlaa-skip" in original:
        raise RuntimeError("unexpected MLAA skip hook shape")
    with open(path, "wb") as dst:
        dst.write(original[:at] + addition + original[at:])
    return 1


def patch_hook_site(data, address, anchor, before, marker, lines):
    """Insert one hook block next to its anchor (binary, CRLF-safe).

    Returns (data, found). Raises when the anchor is missing or not unique in
    the function, or when the marker exists in an unexpected shape."""
    signature = ("void func_%s(ppu_context* ctx) {" % address).encode()
    count = data.count(signature)
    if not count:
        return data, 0
    if count != 1:
        raise RuntimeError("ambiguous hook function " + address)
    start = data.index(signature)
    end = data.index(b"\n}", start)
    if data.count(anchor, start, end) != 1:
        raise RuntimeError("hook anchor drift in func_%s (%s)" % (address, marker))
    pos = data.index(anchor, start, end)
    line_start = data.rindex(b"\n", start, pos) + 1
    line_end = data.index(b"\n", pos)
    newline = b"\r\n" if data[line_end - 1:line_end] == b"\r" else b"\n"
    tag = ("        /* %s */" % marker).encode()
    addition = newline.join([tag] + [line.encode() for line in lines]) + newline
    at = line_start if before else line_end + 1
    if before and data[at - len(addition):at] == addition:
        return data, 1
    if not before and data[at:at + len(addition)] == addition:
        return data, 1
    if data.count(("/* %s */" % marker).encode(), start, end):
        raise RuntimeError("unexpected hook shape: " + marker)
    return data[:at] + addition + data[at:], 1


# PC key names in game text (src/tj_prompt_text.cpp). func_00198590 is the one
# text job every UI string goes through (r4 = job; private UTF-8 copy at
# job+0x28). The hook runs before the game measures, fits, centres and draws
# the string. Site shape: (function, anchor, before?, marker, lines).
PROMPT_TEXT_SITE = (
    "00198590", b"ctx->gpr[0] = vm_read32(ctx->gpr[4] + 0xC);", True,
    "tj-prompt-text", (
        "        { extern void tj_prompt_rewrite_job(uint32_t);",
        "          tj_prompt_rewrite_job((uint32_t)ctx->gpr[4]); }",
    ))
PROMPT_TEXT_MARK = PROMPT_TEXT_SITE[3]


def patch_prompt_text_file(path):
    """Insert the text-job rewrite hook (tj_prompt_rewrite_job) in func_00198590."""
    # Binary edit: preserve legacy non-UTF8 comments and exact line endings.
    with open(path, "rb") as src:
        original = src.read()
    data, hit = patch_hook_site(original, *PROMPT_TEXT_SITE)
    if data != original:
        with open(path, "wb") as dst:
            dst.write(data)
    return hit


def patch_file(path):
    with open(path, "r", encoding="utf-8", errors="surrogateescape") as src:
        lines = src.readlines()

    out, index, patched = [], 0, set()
    while index < len(lines):
        match = SIG.match(lines[index])
        address = match.group(1) if match else None
        if address is None:
            out.append(lines[index]); index += 1; continue

        block, end = function_block(lines, index)
        if address in GEOMETRY_PRODUCER_SITES:
            block, _ = entry_trace(
                block, "/* tj-geometry-producer-trace */",
                "tj_geometry_producer_trace(ppu_context*, uint32_t)",
                "tj_geometry_producer_trace(ctx, %d)" % GEOMETRY_PRODUCER_SITES[address])
            patched.add(address)
        elif address == TITLE_STATE_ADDR:
            if any(TITLE_STATE_MARK in line for line in block):
                patched.add(address)
            else:
                block = remove_legacy_title_entry_trace(block)
                seams = [n for n, line in enumerate(block)
                         if TITLE_STATE_RETURN_AFTER in line]
                if len(seams) != 1:
                    print("WARNING: title return seams in %s: %d" %
                          (path, len(seams)), file=sys.stderr)
                else:
                    at = seams[0] + 1
                    block[at:at] = [
                        "        " + TITLE_STATE_MARK + "\n",
                        "        extern void tj_title_menu_state_trace(ppu_context*, uint32_t, uint32_t);\n",
                        "        tj_title_menu_state_trace(ctx, (uint32_t)ctx->gpr[31], (uint32_t)ctx->gpr[3]);\n",
                    ]
                    patched.add(address)
            marker_sites = [n for n, line in enumerate(block)
                            if TITLE_GATE_INPUT_MARK in line]
            if len(marker_sites) > 1:
                raise RuntimeError("multiple title input-poll markers")
            if marker_sites:
                at = marker_sites[0]
                actual = [entry.strip() for entry in block[at + 1:at + 3]]
                expected = [TITLE_GATE_INPUT_DECL, TITLE_GATE_INPUT_CALL]
                if actual != expected:
                    raise RuntimeError("unexpected title input-poll shape")
                del block[at:at + 3]

            seams = [n for n, line in enumerate(block)
                     if line.strip() == TITLE_GATE_INPUT_NEXT]
            if len(seams) != 1:
                print("WARNING: title controller-tick seams in %s: %d" %
                      (path, len(seams)), file=sys.stderr)
            else:
                at = seams[0]
                block[at:at] = [
                    "        " + TITLE_GATE_INPUT_MARK + "\n",
                    "        " + TITLE_GATE_INPUT_DECL + "\n",
                    "        " + TITLE_GATE_INPUT_CALL + "\n",
                ]
                patched.add(TITLE_GATE_INPUT_KEY)

            marker_sites = [n for n, line in enumerate(block)
                            if MAIN_MENU_GATE_MARK in line]
            if len(marker_sites) > 1:
                raise RuntimeError("multiple main-menu input-poll markers")
            if marker_sites:
                at = marker_sites[0]
                actual = [entry.strip() for entry in block[at + 1:at + 3]]
                expected = [MAIN_MENU_GATE_DECL, MAIN_MENU_GATE_CALL]
                if actual != expected:
                    raise RuntimeError("unexpected main-menu input-poll shape")
                del block[at:at + 3]

            seams = [n for n in range(len(block) - 1)
                     if block[n].strip() == MAIN_MENU_GATE_BRANCH and
                     block[n + 1].strip() == MAIN_MENU_GATE_NEXT]
            if len(seams) != 1:
                print("WARNING: main-menu ready seam in %s: %d" %
                      (path, len(seams)), file=sys.stderr)
            else:
                at = seams[0] + 1
                block[at:at] = [
                    "        " + MAIN_MENU_GATE_MARK + "\n",
                    "        " + MAIN_MENU_GATE_DECL + "\n",
                    "        " + MAIN_MENU_GATE_CALL + "\n",
                ]
                patched.add(MAIN_MENU_GATE_KEY)
        elif address == SCENE_STATE_ADDR:
            if any(SCENE_STATE_MARK in line for line in block):
                patched.add(address)
            else:
                injected, _ = entry_trace(
                    block, SCENE_STATE_MARK,
                    "tj_survival_scene_state_trace(ppu_context*, uint32_t, uint32_t)",
                    "tj_survival_scene_state_trace(ctx, (uint32_t)ctx->gpr[3], 0)")
                rebuilt = injected[:4]
                for line in block[1:]:
                    rebuilt.append(line)
                    if SCENE_JOIN_AFTER in line:
                        rebuilt.append("        tj_survival_scene_state_trace(ctx, (uint32_t)ctx->gpr[31], 1);\n")
                    elif "vm_write32(ctx->gpr[31] + 0x498" in line:
                        rebuilt.append("        tj_survival_scene_state_trace(ctx, (uint32_t)ctx->gpr[31], 2);\n")
                block = rebuilt; patched.add(address)
            if any(TITLE_GATE_SCENE_MARK in line for line in block):
                patched.add(TITLE_GATE_SCENE_KEY)
            else:
                seams = [n for n in range(len(block) - 1)
                         if TITLE_GATE_SCENE_CALL in block[n] and
                         block[n + 1].strip() == TITLE_GATE_SCENE_NORMALIZE]
                if len(seams) != 1:
                    print("WARNING: title scene-evidence seams in %s: %d" %
                          (path, len(seams)), file=sys.stderr)
                else:
                    at = seams[0] + 2
                    block[at:at] = [
                        "        " + TITLE_GATE_SCENE_MARK + "\n",
                        "        tj_survival_scene_state_trace(ctx, (uint32_t)ctx->gpr[31], 3);\n",
                    ]
                    patched.add(TITLE_GATE_SCENE_KEY)
        elif address == FADE_STATE_ADDR:
            if any(FADE_STATE_MARK in line for line in block):
                patched.add(address)
            else:
                injected, _ = entry_trace(
                    block, FADE_STATE_MARK,
                    "tj_survival_fade_state_trace(ppu_context*, uint32_t, uint32_t)",
                    "tj_survival_fade_state_trace(ctx, (uint32_t)ctx->gpr[3], 0)")
                rebuilt, found = injected[:4], set()
                for line in block[1:]:
                    rebuilt.append(line)
                    for needle, site in FADE_TRACE_SITES.items():
                        if needle in line:
                            rebuilt.append("        tj_survival_fade_state_trace(ctx, (uint32_t)ctx->gpr[31], %d);\n" % site)
                            found.add(site)
                            break
                if found == set(FADE_TRACE_SITES.values()):
                    block = rebuilt; patched.add(address)
                else:
                    print("WARNING: fade trace sites missing in %s: %s" %
                          (path, sorted(set(FADE_TRACE_SITES.values()) - found)), file=sys.stderr)
        elif address == NO_MEDIA_COMPLETE_ADDR:
            if any(NO_MEDIA_COMPLETE_MARK in line for line in block):
                patched.add(address)
            else:
                returns = [n for n, line in enumerate(block) if line.strip() == "return;"]
                if len(returns) == 1:
                    at = returns[0]
                    block[at:at] = ["        " + NO_MEDIA_COMPLETE_MARK + "\n",
                                  "        extern void tj_cellSailCompleteNoMediaAfterLoad(void);\n",
                                  "        tj_cellSailCompleteNoMediaAfterLoad();\n"]
                    patched.add(address)
                else:
                    print("WARNING: expected one loader return in %s, found %d" % (path, len(returns)), file=sys.stderr)
        elif address == TASK_SIGNAL_ADDR:
            if any(TASK_SIGNAL_MARK in line for line in block):
                patched.add(address)
            else:
                seams = [n for n, line in enumerate(block)
                         if line.strip() == TASK_SIGNAL_CALL]
                if len(seams) != 1:
                    print("WARNING: expected one task-signal call in %s, found %d" %
                          (path, len(seams)), file=sys.stderr)
                else:
                    at = seams[0]
                    block[at:at + 1] = [
                        "        " + TASK_SIGNAL_MARK + "\n",
                        "        ctx->lr = 0x0025CAE4;\n",
                        "        extern bool tj_consume_spu_task_signal_substitution(uint32_t, uint32_t);\n",
                        "        if (tj_consume_spu_task_signal_substitution(\n",
                        "                (uint32_t)ctx->gpr[3], (uint32_t)ctx->gpr[4])) {\n",
                        "            ctx->gpr[3] = (int64_t)(int32_t)(0);\n",
                        "        } else {\n",
                        "            func_0025EA20(ctx); DRAIN_TRAMPOLINE(ctx);\n",
                        "        }\n",
                    ]
                    patched.add(address)
        elif address == TASK_WAKE_ADDR:
            clear_sites = [n for n, line in enumerate(block)
                           if line.strip() == TASK_WAKE_AFTER]
            if len(clear_sites) != 1:
                raise RuntimeError("expected one task-group completion clear")
            clear_at = clear_sites[0]
            pre_markers = [n for n, line in enumerate(block)
                           if TASK_WAKE_PRE_CLEAR_MARK in line]
            wake_markers = [n for n, line in enumerate(block)
                            if TASK_WAKE_MARK in line]
            if len(pre_markers) > 1 or len(wake_markers) > 1:
                raise RuntimeError("multiple task-group wake markers")
            if pre_markers and wake_markers:
                pre_at, wake_at = pre_markers[0], wake_markers[0]
                if (pre_at + 3 != clear_at or
                        [entry.strip() for entry in block[pre_at + 1:pre_at + 3]] !=
                            [TASK_WAKE_PRE_CLEAR_DECL, TASK_WAKE_PRE_CLEAR_CALL] or
                        wake_at != clear_at + 1 or
                        [entry.strip() for entry in block[wake_at + 1:wake_at + 3]] !=
                            [TASK_WAKE_DECL, TASK_WAKE_CALL]):
                    raise RuntimeError("unexpected task-group wake marker shape")
                patched.add(address)
            else:
                if wake_markers:
                    wake_at = wake_markers[0]
                    if (wake_at != clear_at + 1 or
                            [entry.strip() for entry in block[wake_at + 1:wake_at + 3]] !=
                                [TASK_WAKE_DECL, TASK_WAKE_CALL]):
                        raise RuntimeError("unexpected legacy task-group wake marker shape")
                    del block[wake_at:wake_at + 3]
                    clear_at = next(n for n, line in enumerate(block)
                                    if line.strip() == TASK_WAKE_AFTER)
                if pre_markers:
                    raise RuntimeError("task-group pre-clear marker lacks wake marker")
                block[clear_at:clear_at] = [
                    "        " + TASK_WAKE_PRE_CLEAR_MARK + "\n",
                    "        " + TASK_WAKE_PRE_CLEAR_DECL + "\n",
                    "        " + TASK_WAKE_PRE_CLEAR_CALL + "\n",
                ]
                clear_at += 3
                block[clear_at + 1:clear_at + 1] = [
                    "        " + TASK_WAKE_MARK + "\n",
                    "        " + TASK_WAKE_DECL + "\n",
                    "        " + TASK_WAKE_CALL + "\n",
                ]
                patched.add(address)
        elif address == COMPLETION_WAIT_ADDR:
            trace_jump = "if (tj_completion_wait_trace) goto loc_tj_completion_wait_trace;"
            entry_seams = [n for n, line in enumerate(block)
                           if line.strip() == COMPLETION_WAIT_ENTRY_AFTER]
            current_trace = any(
                block[n].strip() == "if (((ctx->cr >> 0) & 2)) goto loc_0025CA70;" and
                block[n + 1].strip() == "goto loc_0025CA88;"
                for n in range(len(block) - 1)) and \
                sum(line.strip() == trace_jump for line in block) == 1 and \
                sum(line.strip() == "goto loc_0025CA88;" for line in block) == 2
            marker_sites = [n for n, line in enumerate(block)
                            if COMPLETION_WAIT_MARK in line]
            current_lifecycle = False
            if len(marker_sites) == 1 and len(entry_seams) == 1:
                marker_at = marker_sites[0]
                header = [entry.strip() for entry in block[marker_at + 1:marker_at + 7]]
                initial_read_sites = [n for n, line in enumerate(block)
                                      if line.strip() == COMPLETION_WAIT_LIFECYCLE_BEGIN]
                initial_shape = []
                if len(initial_read_sites) == 1:
                    initial_at = initial_read_sites[0]
                    initial_shape = [entry.strip() for entry in
                                     block[initial_at:initial_at + 2]]
                entry_at = entry_seams[0] + 1
                entry_shape = [entry.strip() for entry in block[entry_at:entry_at + 1]]
                exit_sites = [n for n, line in enumerate(block)
                              if line.strip() == COMPLETION_WAIT_EXIT_LABEL]
                current_lifecycle = (
                    header == [
                        "extern bool tj_spu_completion_wait_trace_enabled(void);",
                        "extern void tj_spu_completion_wait_trace_record(ppu_context*, uint32_t, uint32_t, uint32_t);",
                        COMPLETION_WAIT_LIFECYCLE_BEGIN_DECL,
                        COMPLETION_WAIT_LIFECYCLE_END_DECL,
                        "bool tj_completion_wait_trace = tj_spu_completion_wait_trace_enabled();",
                        "bool tj_completion_wait_lifecycle_active = false;",
                    ] and
                    initial_shape == [
                        COMPLETION_WAIT_LIFECYCLE_BEGIN,
                        COMPLETION_WAIT_LIFECYCLE_ACTIVE,
                    ] and
                    entry_shape == [
                        trace_jump,
                    ] and
                    len(exit_sites) == 1 and exit_sites[0] + 1 < len(block) and
                    block[exit_sites[0] + 1].strip() == COMPLETION_WAIT_LIFECYCLE_END and
                    [entry.strip() for entry in block[marker_at + 7:marker_at + 12]] ==
                        COMPLETION_WAIT_PROLOGUE)
            if len(marker_sites) == 1 and current_trace and current_lifecycle:
                patched.add(address)
            else:
                if marker_sites:
                    if len(marker_sites) != 1:
                        raise RuntimeError("multiple completion-wait markers")
                    marker_at = marker_sites[0]
                    old_header = [
                        "extern bool tj_spu_completion_wait_trace_enabled(void);",
                        "extern void tj_spu_completion_wait_trace_record(ppu_context*, uint32_t, uint32_t, uint32_t);",
                        "bool tj_completion_wait_trace = tj_spu_completion_wait_trace_enabled();",
                    ]
                    new_header = old_header[:2] + [
                        COMPLETION_WAIT_LIFECYCLE_BEGIN_DECL,
                        COMPLETION_WAIT_LIFECYCLE_END_DECL,
                    ] + old_header[2:] + [
                        "bool tj_completion_wait_lifecycle_active = false;",
                    ]
                    if [entry.strip() for entry in block[marker_at + 1:marker_at + 1 + len(new_header)]] == new_header:
                        del block[marker_at:marker_at + 1 + len(new_header)]
                    elif [entry.strip() for entry in block[marker_at + 1:marker_at + 1 + len(old_header)]] == old_header:
                        del block[marker_at:marker_at + 1 + len(old_header)]
                    else:
                        raise RuntimeError("unexpected completion-wait marker shape")
                    trace_at = next((n for n, line in enumerate(block)
                                     if line.strip() == "loc_tj_completion_wait_trace:"),
                                    None)
                    if trace_at is not None:
                        exit_at = next(n for n, line in enumerate(block)
                                       if n > trace_at and
                                       line.strip() == COMPLETION_WAIT_EXIT_LABEL)
                        del block[trace_at:exit_at]
                    block = [line for line in block
                             if line.strip() not in (
                                 "if (tj_completion_wait_trace) tj_spu_completion_wait_trace_record(ctx, (uint32_t)ctx->gpr[31], (uint32_t)ctx->gpr[0], 0);",
                                 trace_jump,
                                 COMPLETION_WAIT_LIFECYCLE_BEGIN,
                                 COMPLETION_WAIT_LIFECYCLE_ACTIVE,
                                 COMPLETION_WAIT_LEGACY_BEGIN,
                                 COMPLETION_WAIT_LEGACY_ACTIVE,
                                 COMPLETION_WAIT_LEGACY_RECHECK,
                                 COMPLETION_WAIT_LIFECYCLE_END,
                                 "tj_completion_wait_active = tj_completion_wait_trace;",
                                 "if (tj_completion_wait_active) tj_spu_completion_wait_trace_record(ctx, (uint32_t)ctx->gpr[31], (uint32_t)ctx->gpr[29], 1);",
                             )]
                    old_loop_branch = "if (((ctx->cr >> 0) & 2)) goto loc_0025CA70;"
                    old_loop_at = next(n for n, line in enumerate(block)
                                       if line.strip() == old_loop_branch)
                    while (old_loop_at + 1 < len(block) and
                           block[old_loop_at + 1].strip() == "goto loc_0025CA88;"):
                        del block[old_loop_at + 1]
                prologue_tail_sites = [n for n, line in enumerate(block)
                                       if line.strip() == COMPLETION_WAIT_PROLOGUE[-1]]
                if len(prologue_tail_sites) != 1:
                    raise RuntimeError("expected one completion-wait prologue tail")
                prologue_tail_at = prologue_tail_sites[0]
                prologue_prefix = [entry.strip() for entry in block[1:prologue_tail_at]]
                if prologue_prefix != COMPLETION_WAIT_PROLOGUE[:-1]:
                    if any(entry not in COMPLETION_WAIT_PROLOGUE[:-1]
                           for entry in prologue_prefix):
                        raise RuntimeError("unexpected completion-wait prologue shape")
                    block[1:prologue_tail_at] = ["        " + entry + "\n"
                                                   for entry in COMPLETION_WAIT_PROLOGUE[:-1]]
                initial_read_sites = [n for n, line in enumerate(block)
                                      if line.strip() in (
                                          COMPLETION_WAIT_INITIAL_READ,
                                          COMPLETION_WAIT_LIFECYCLE_BEGIN)]
                if len(initial_read_sites) != 1:
                    raise RuntimeError("expected one completion-wait initial read")
                initial_at = initial_read_sites[0]
                block[initial_at] = "        " + COMPLETION_WAIT_LIFECYCLE_BEGIN + "\n"
                block[initial_at + 1:initial_at + 1] = [
                    "        " + COMPLETION_WAIT_LIFECYCLE_ACTIVE + "\n",
                ]
                entry_seams = [n for n, line in enumerate(block)
                               if line.strip() == COMPLETION_WAIT_ENTRY_AFTER]
                poll_seams = [n for n, line in enumerate(block)
                              if line.strip() == COMPLETION_WAIT_POLL_AFTER]
                if len(entry_seams) != 1 or len(poll_seams) != 1:
                    print("WARNING: completion-wait seams in %s: entry=%d poll=%d" %
                          (path, len(entry_seams), len(poll_seams)), file=sys.stderr)
                else:
                    block[1:1] = [
                        "        " + COMPLETION_WAIT_MARK + "\n",
                        "        extern bool tj_spu_completion_wait_trace_enabled(void);\n",
                        "        extern void tj_spu_completion_wait_trace_record(ppu_context*, uint32_t, uint32_t, uint32_t);\n",
                        "        " + COMPLETION_WAIT_LIFECYCLE_BEGIN_DECL + "\n",
                        "        " + COMPLETION_WAIT_LIFECYCLE_END_DECL + "\n",
                        "        bool tj_completion_wait_trace = tj_spu_completion_wait_trace_enabled();\n",
                        "        bool tj_completion_wait_lifecycle_active = false;\n",
                    ]
                    entry_seams = [n for n, line in enumerate(block)
                                   if line.strip() == COMPLETION_WAIT_ENTRY_AFTER]
                    entry_at = entry_seams[0] + 1
                    block[entry_at:entry_at] = [
                        "        " + trace_jump + "\n",
                    ]
                    loop_body = [line for line in block
                                 if line.strip() in (
                                     "ctx->gpr[3] = (int64_t)(int32_t)(0x1E);",
                                     "ctx->gpr[11] = (int64_t)(int32_t)(0x8D);",
                                     "lv2_syscall(ctx);",
                                     COMPLETION_WAIT_POLL_AFTER,
                                 )]
                    if len(loop_body) != 4:
                        print("WARNING: completion-wait loop body in %s: %d" %
                              (path, len(loop_body)), file=sys.stderr)
                        continue
                    exit_seams = [n for n, line in enumerate(block)
                                  if line.strip() == COMPLETION_WAIT_EXIT_LABEL]
                    if len(exit_seams) != 1:
                        print("WARNING: completion-wait exit seam in %s: %d" %
                              (path, len(exit_seams)), file=sys.stderr)
                        continue
                    exit_at = exit_seams[0]
                    block[exit_at:exit_at] = [
                        "        goto loc_0025CA88;\n",
                        "loc_tj_completion_wait_trace:\n",
                        "        tj_spu_completion_wait_trace_record(ctx, (uint32_t)ctx->gpr[31], (uint32_t)ctx->gpr[0], 0);\n",
                        "loc_tj_completion_wait_trace_loop:\n",
                        loop_body[0], loop_body[1], loop_body[2], loop_body[3],
                        "        tj_spu_completion_wait_trace_record(ctx, (uint32_t)ctx->gpr[31], (uint32_t)ctx->gpr[29], 1);\n",
                        "        { int64_t a = (int32_t)ctx->gpr[29]; int64_t b = (int64_t)0; uint32_t cr_val = (a < b) ? 8 : (a > b) ? 4 : 2; ctx->cr = (ctx->cr & ~(0xFu << 0)) | (cr_val << 0); }\n",
                        "        if (((ctx->cr >> 0) & 2)) goto loc_tj_completion_wait_trace_loop;\n",
                        "        goto loc_0025CA88;\n",
                    ]
                    exit_at = next(n for n, line in enumerate(block)
                                   if line.strip() == COMPLETION_WAIT_EXIT_LABEL)
                    block[exit_at + 1:exit_at + 1] = [
                        "        " + COMPLETION_WAIT_LIFECYCLE_END + "\n",
                    ]
                    patched.add(address)
        elif address in VRFIZ_PATCHES:
            old, replacement = VRFIZ_PATCHES[address]
            if any("/* tj-ppu-vrfiz */" in line for line in block):
                patched.add(address)
            else:
                seams = [n for n, line in enumerate(block)
                         if line.strip() == old]
                if len(seams) != 1:
                    raise RuntimeError("expected one vrfiz seam at %s" % address)
                block[seams[0]] = "        " + replacement + "\n"
                patched.add(address)
        elif address in HEAP_PATCHES:
            symbol, name = HEAP_PATCHES[address]
            if "/* tj-heap" in block[1]:
                patched.add(address)
            else:
                block = [block[0], "    /* tj-heap: %s -> %s */\n" % (name, symbol),
                         "    extern void %s(ppu_context*);\n" % symbol,
                         "    %s(ctx);\n" % symbol, "}\n"]
                patched.add(address)
        out.extend(block)
        index = end + 1

    # Do not churn timestamps on already-patched 38 MB translation units.
    # Ninja otherwise recompiles unchanged bulk-lifted sources on every pass.
    if patched and out != lines:
        with open(path, "w", encoding="utf-8", errors="surrogateescape") as dst:
            dst.writelines(out)
    return patched


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--vector-minmax-be-only", action="store_true",
                        help="repair only big-endian vminfp/vmaxfp storage")
    parser.add_argument("--vector-compare-record-only", action="store_true",
                        help="restore only original floating VMX compare CR6 effects")
    parser.add_argument("--geometry-producer-trace-only", action="store_true",
                        help="insert only three read-only geometry producer hooks")
    parser.add_argument("--loader-result-trace-only", action="store_true",
                        help="insert only byte-preserving loader-result observers")
    parser.add_argument("--mlaa-skip-only", action="store_true",
                        help="insert only the TJ_DISABLE_MLAA entry gate")
    parser.add_argument("--prompts-only", action="store_true",
                        help="insert only the PC key-prompt text hook (func_00198590)")
    args = parser.parse_args()
    if sum((args.vector_minmax_be_only, args.vector_compare_record_only,
            args.geometry_producer_trace_only, args.loader_result_trace_only,
            args.mlaa_skip_only,
            args.prompts_only)) > 1:
        parser.error("select only one targeted repair")
    files = sorted(glob.glob(RECOMP_GLOB))
    if not files:
        print("post_lift: no generated PPU sources -- run the lifter first", file=sys.stderr)
        return 1
    if args.prompts_only:
        found = sum(patch_prompt_text_file(path) for path in files)
        print("post_lift: prompt-text %d/1 sites" % found)
        return 0 if found == 1 else 1
    if args.mlaa_skip_only:
        found = sum(patch_mlaa_skip_file(path) for path in files)
        print("post_lift: mlaa-skip %d/1 sites" % found)
        return 0 if found == 1 else 1
    from loader_result_trace import repair as repair_loader_result_trace
    if args.loader_result_trace_only:
        changed = sum(repair_loader_result_trace(path, apply=True) for path in files)
        print("post_lift: loader-result-trace %d insertions" % changed)
        return 0
    if args.geometry_producer_trace_only:
        changed = sum(patch_geometry_producer_file(path) for path in files)
        print("post_lift: geometry-producer-trace %d patches" % changed)
        return 0
    if args.vector_minmax_be_only:
        changed = patch_vector_minmax_files(files)
        print("post_lift: vector-minmax-be %d patches" % changed)
        return 0
    from vector_compare_record import patch_files as patch_compare_records
    if args.vector_compare_record_only:
        print("post_lift: vector-compare-record %d patches" % patch_compare_records(files))
        return 0
    vector_compare_changed = patch_compare_records(files)
    vector_minmax_changed = patch_vector_minmax_files(files)
    from restore_gcm_reference_wait import repair as repair_reference_wait
    from depth_emit_trace import repair as repair_depth_emit_trace
    from vector_word_be import repair as repair_vector_word_be
    from vector_element_memory import repair as repair_vector_element_memory
    for path in files:
        repair_reference_wait(path, apply=True)
        repair_depth_emit_trace(path, apply=True)
        repair_vector_word_be(path, apply=True)
        repair_vector_element_memory(path, apply=True)
        repair_loader_result_trace(path, apply=True)
    done = set()
    for path in files:
        done |= patch_file(path)
        if patch_mlaa_skip_file(path):
            done.add(MLAA_SKIP_ADDR)
        if patch_prompt_text_file(path):
            done.add(PROMPT_TEXT_MARK)
    want = set(CRT_SKIPS) | set(HEAP_PATCHES) | set(VRFIZ_PATCHES) | set(GEOMETRY_PRODUCER_SITES) | {TASK_WAKE_ADDR, TASK_SIGNAL_ADDR, COMPLETION_WAIT_ADDR, SCENE_STATE_ADDR,
        FADE_STATE_ADDR, NO_MEDIA_COMPLETE_ADDR, TITLE_STATE_ADDR,
        TITLE_GATE_SCENE_KEY, TITLE_GATE_INPUT_KEY, MAIN_MENU_GATE_KEY, MLAA_SKIP_ADDR,
        PROMPT_TEXT_MARK}
    print("post_lift: %d/%d patches; vector-minmax-be %d patches" %
          (len(done), len(want), vector_minmax_changed))
    print("post_lift: vector-compare-record %d patches" % vector_compare_changed)
    missing = sorted(want - done)
    if missing:
        print("WARNING: not found: %s" % ", ".join(missing), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
