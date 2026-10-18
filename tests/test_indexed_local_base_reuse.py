"""Differential proof for per-draw LOCAL index-base reuse.

Extracts the actual indexed readers/uploaders plus the actual GCM resolver.
The pre-change renderer hash is pinned; after the product patch, the fixture
reconstructs that exact baseline and checks that only the expected hunk differs.
"""
from __future__ import annotations

import hashlib
import re
import shutil
import statistics
import subprocess
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
VIDEO = ROOT.parent / "ps3recomp" / "libs" / "video"
BACKEND = VIDEO / "rsx_d3d12_backend.c"
GCM = VIDEO / "cellGcmSys.c"
BASELINE_SHA256 = "ED53BA222559CD32E13FB5ED82839B75E16147E317BA9B8556E372A2648255B8"
UPLOADER_NAMES = (
    "upload_restart_vp_indexed",
    "upload_quads_vp_indexed",
    "upload_quad_strip_vp_indexed",
    "upload_tris_vp_indexed",
    "upload_linear_vp_indexed",
    "upload_line_loop_vp_indexed",
    "upload_strip_vp_indexed",
)


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest().upper()


def extract_function(text: str, signature: str) -> tuple[int, int, str]:
    at = text.find(signature)
    if at < 0:
        raise AssertionError(f"missing function: {signature}")
    start = text.rfind("\n", 0, at) + 1
    brace = text.find("{", at)
    depth, end = 1, brace + 1
    while depth:
        depth += (text[end] == "{") - (text[end] == "}")
        end += 1
    if text[end:end + 2] == "\r\n":
        end += 2
    elif text[end:end + 1] == "\n":
        end += 1
    return start, end, text[start:end]


def replace_function(text: str, signature: str, replacement: str) -> str:
    start, end, _ = extract_function(text, signature)
    return text[:start] + replacement + text[end:]


def edit_call_arguments(text: str, name: str, add: bool) -> str:
    needle = name + "("
    cursor = 0
    while True:
        at = text.find(needle, cursor)
        if at < 0:
            return text
        open_at = at + len(name)
        depth, end = 1, open_at + 1
        while depth:
            depth += (text[end] == "(") - (text[end] == ")")
            end += 1
        close_at = end - 1
        args = text[open_at + 1:close_at].rstrip()
        if add:
            if not args.endswith("local_index_base"):
                text = text[:close_at] + ", local_index_base" + text[close_at:]
                cursor = close_at + len(", local_index_base)")
            else:
                cursor = end
        else:
            suffix = ", local_index_base"
            if not args.endswith(suffix):
                raise AssertionError(f"unexpected {name} call arguments: {args[-80:]}")
            text = text[:close_at - len(suffix)] + text[close_at:]
            cursor = close_at - len(suffix) + 1


def change_header(function: str, add: bool) -> str:
    brace = function.find("{")
    header = function[:brace]
    close = header.rfind(")")
    if add:
        header = header[:close] + ", const GuestIndexLocalBase* local_index_base" + header[close:]
    else:
        suffix = ", const GuestIndexLocalBase* local_index_base"
        if not header[:close].endswith(suffix):
            raise AssertionError("unexpected indexed uploader signature")
        header = header[:close - len(suffix)] + header[close:]
    return header + function[brace:]


def original_raw(newline: str) -> str:
    return newline.join((
        "static u32 read_guest_index_raw(const rsx_state* st, u32 k)",
        "{",
        "    if (st->immediate_index_data && k < st->immediate_index_count)",
        "        return st->immediate_index_data[k];",
        "    extern uint8_t* vm_base;",
        "    u32 base;",
        "    if (!resolve_guest_index_base(st, &base)) return 0;",
        "    const u8* p = vm_base + base;",
        "    if (((st->index_array_dma >> 4) & 0xF) == 1) {",
        "        p += (u64)k * 2;",
        "        return ((u32)p[0] << 8) | p[1];",
        "    }",
        "    p += (u64)k * 4;",
        "    return ((u32)p[0] << 24) | ((u32)p[1] << 16) | ((u32)p[2] << 8) | p[3];",
        "}",
        "",
    ))


def original_read_index(newline: str) -> str:
    return newline.join((
        "static u32 read_guest_index(const rsx_state* st, u32 k)",
        "{",
        "    /* NV4097 adds BASE_INDEX after reading the guest element and keeps the",
        "     * low 20 bits. Primitive-restart comparison uses the raw element below. */",
        "    return (read_guest_index_raw(st, k) + st->vertex_data_base_index) & 0x000FFFFFu;",
        "}",
        "",
    ))


def make_candidate_from_baseline(text: str) -> str:
    newline = "\r\n" if "\r\n" in text else "\n"
    baseline_raw = original_raw(newline)
    _, _, actual_raw = extract_function(text, "static u32 read_guest_index_raw(")
    if actual_raw.replace("\r\n", "\n") != baseline_raw.replace("\r\n", "\n"):
        raise AssertionError("baseline raw index helper differs from pinned source")
    candidate_raw = newline.join((
        "static u32 read_guest_index_raw(const rsx_state* st, u32 k,",
        "                                const GuestIndexLocalBase* local_index_base)",
        "{",
        "    if (st->immediate_index_data && k < st->immediate_index_count)",
        "        return st->immediate_index_data[k];",
        "    extern uint8_t* vm_base;",
        "    u32 base;",
        "    if (st->immediate_index_data || !local_index_base ||",
        "        (st->index_array_dma & 0xFu) != 0 ||",
        "        st->index_array_offset != local_index_base->offset) {",
        "        if (!resolve_guest_index_base(st, &base)) return 0;",
        "    } else {",
        "        base = local_index_base->ea;",
        "    }",
        "    const u8* p = vm_base + base;",
        "    if (((st->index_array_dma >> 4) & 0xF) == 1) {",
        "        p += (u64)k * 2;",
        "        return ((u32)p[0] << 8) | p[1];",
        "    }",
        "    p += (u64)k * 4;",
        "    return ((u32)p[0] << 24) | ((u32)p[1] << 16) | ((u32)p[2] << 8) | p[3];",
        "}",
        "",
    ))
    text = replace_function(text, "static u32 read_guest_index_raw(", candidate_raw)

    _, _, actual_read = extract_function(text, "static u32 read_guest_index(")
    baseline_read = original_read_index(newline)
    if actual_read.replace("\r\n", "\n") != baseline_read.replace("\r\n", "\n"):
        raise AssertionError("baseline index helper differs from pinned source")
    candidate_read = newline.join((
        "static u32 read_guest_index(const rsx_state* st, u32 k,",
        "                             const GuestIndexLocalBase* local_index_base)",
        "{",
        "    /* Restart comparison reads raw indices before this base-index addition. */",
        "    return (read_guest_index_raw(st, k, local_index_base) +",
        "            st->vertex_data_base_index) & 0x000FFFFFu;",
        "}",
        "",
    ))
    text = replace_function(text, "static u32 read_guest_index(", candidate_read)

    marker = "/* Resolve the persistent guest index array before a whole indexed draw starts."
    text = text.replace(marker, "typedef struct GuestIndexLocalBase { u32 ea, offset; } GuestIndexLocalBase;" + newline + newline + marker, 1)

    for name in UPLOADER_NAMES:
        start, end, function = extract_function(text, f"static u32 {name}(")
        function = change_header(function, True)
        if name == "upload_restart_vp_indexed":
            function = edit_call_arguments(function, "read_guest_index_raw", True)
        function = edit_call_arguments(function, "read_guest_index", True)
        text = text[:start] + function + text[end:]

    _, _, draw = extract_function(text, "static void d3d12_draw_indexed(")
    if "(void)index_base;" not in draw:
        raise AssertionError("indexed entry preflight marker missing")
    draw = draw.replace(
        "    (void)index_base;",
        "    GuestIndexLocalBase local_index_base_storage;" + newline
        + "    const GuestIndexLocalBase* local_index_base = NULL;" + newline
        + "    if (!state->immediate_index_data && (state->index_array_dma & 0xFu) == 0) {" + newline
        + "        local_index_base_storage.ea = index_base;" + newline
        + "        local_index_base_storage.offset = state->index_array_offset;" + newline
        + "        local_index_base = &local_index_base_storage;" + newline
        + "    }",
        1,
    )
    for name in UPLOADER_NAMES:
        draw = edit_call_arguments(draw, name, True)
    text = replace_function(text, "static void d3d12_draw_indexed(", draw)
    return text


def restore_baseline(candidate: str) -> str:
    newline = "\r\n" if "\r\n" in candidate else "\n"
    text = candidate
    text = replace_function(text, "static u32 read_guest_index_raw(", original_raw(newline))
    text = replace_function(text, "static u32 read_guest_index(", original_read_index(newline))
    type_line = "typedef struct GuestIndexLocalBase { u32 ea, offset; } GuestIndexLocalBase;" + newline + newline
    if type_line not in text:
        raise AssertionError("missing local-base context type")
    text = text.replace(type_line, "", 1)
    for name in UPLOADER_NAMES:
        start, end, function = extract_function(text, f"static u32 {name}(")
        if name == "upload_restart_vp_indexed":
            function = edit_call_arguments(function, "read_guest_index_raw", False)
        function = edit_call_arguments(function, "read_guest_index", False)
        function = change_header(function, False)
        text = text[:start] + function + text[end:]
    _, _, draw = extract_function(text, "static void d3d12_draw_indexed(")
    block = (
        "    GuestIndexLocalBase local_index_base_storage;" + newline
        + "    const GuestIndexLocalBase* local_index_base = NULL;" + newline
        + "    if (!state->immediate_index_data && (state->index_array_dma & 0xFu) == 0) {" + newline
        + "        local_index_base_storage.ea = index_base;" + newline
        + "        local_index_base_storage.offset = state->index_array_offset;" + newline
        + "        local_index_base = &local_index_base_storage;" + newline
        + "    }"
    )
    if block not in draw:
        raise AssertionError("missing local-base entry block")
    draw = draw.replace(block, "    (void)index_base;", 1)
    for name in UPLOADER_NAMES:
        draw = edit_call_arguments(draw, name, False)
    return replace_function(text, "static void d3d12_draw_indexed(", draw)


def pin_source_pair() -> tuple[str, str]:
    raw = BACKEND.read_bytes()
    current_hash = sha(raw)
    text = raw.decode("utf-8")
    if current_hash == BASELINE_SHA256:
        baseline = text
        candidate = make_candidate_from_baseline(baseline)
    else:
        baseline = restore_baseline(text)
        if sha(baseline.encode("utf-8")) != BASELINE_SHA256:
            raise AssertionError("reconstructed baseline renderer hash mismatch")
        candidate = text
        if make_candidate_from_baseline(baseline) != candidate:
            raise AssertionError("active candidate differs outside the expected patch")
    if sha(baseline.encode("utf-8")) != BASELINE_SHA256:
        raise AssertionError("baseline renderer SHA mismatch")
    return baseline, candidate


def instrument_resolver(function: str) -> str:
    function = function.replace(
        "int cellGcmResolveLocatedChecked(int local, u32 offset, u32* out)\n{",
        "int cellGcmResolveLocatedChecked(int local, u32 offset, u32* out)\n{\n#ifdef TEST_HOOKS\n    ++g_resolve_calls;\n#endif",
        1,
    )
    old = "    *out = ((u32)ea_page << 20) | (offset & 0xFFFFFu);\n    return 1;"
    new = "    *out = ((u32)ea_page << 20) | (offset & 0xFFFFFu);\n#ifdef TEST_HOOKS\n    test_after_resolve(g_resolve_calls);\n#endif\n    return 1;"
    if old not in function:
        raise AssertionError("real resolver insertion point changed")
    return function.replace(old, new, 1)


def instrument_index_reader(function: str) -> str:
    function = function.replace(
        "        return st->immediate_index_data[k];",
        "        { u32 value = st->immediate_index_data[k]; TEST_AFTER_INDEX_READ(k, 4); return value; }",
        1,
    )
    function = function.replace(
        "        return ((u32)p[0] << 8) | p[1];",
        "        { u32 value = ((u32)p[0] << 8) | p[1]; TEST_AFTER_INDEX_READ(k, 2); return value; }",
        1,
    )
    function = function.replace(
        "    return ((u32)p[0] << 24) | ((u32)p[1] << 16) | ((u32)p[2] << 8) | p[3];",
        "    { u32 value = ((u32)p[0] << 24) | ((u32)p[1] << 16) | ((u32)p[2] << 8) | p[3]; TEST_AFTER_INDEX_READ(k, 4); return value; }",
        1,
    )
    return function


def compile_text(baseline: str, candidate: str, hooks: bool, temp: Path) -> Path:
    baseline = baseline.replace("\r\n", "\n")
    candidate = candidate.replace("\r\n", "\n")
    newline = "\r\n" if "\r\n" in candidate else "\n"
    backend_names = (
        "static float rd_bef(", "static u32 rd_beu32(", "static float rd_half_be(",
        "static int vp_frequency_override_disabled(", "static int rsx_has_vertex_position(",
        "static void read_vp_vertex(", "static int resolve_guest_index_base(",
        "static int emit_indexed_vp_vertex(",
    )
    common = [extract_function(candidate, name)[2] for name in backend_names]
    resolver_names = (
        "u64 cellGcmMainMappingGeneration(", "static u16 cellGcmMainEaPage(",
        "int cellGcmResolveLocatedChecked(",
    )
    resolver_src = GCM.read_bytes().decode("utf-8").replace("\r\n", "\n")
    resolver_functions = [extract_function(resolver_src, name)[2] for name in resolver_names]
    resolver_functions[-1] = instrument_resolver(resolver_functions[-1])
    raw_sig = "static u32 read_guest_index_raw("
    index_sig = "static u32 read_guest_index("
    base_raw = instrument_index_reader(extract_function(baseline, raw_sig)[2])
    cand_raw = instrument_index_reader(extract_function(candidate, raw_sig)[2])
    base_index = extract_function(baseline, index_sig)[2]
    cand_index = extract_function(candidate, index_sig)[2]
    type_def = "typedef struct GuestIndexLocalBase { u32 ea, offset; } GuestIndexLocalBase;"
    base_uploaders = []
    cand_uploaders = []
    for name in UPLOADER_NAMES:
        base_function = extract_function(baseline, f"static u32 {name}(")[2]
        cand_function = extract_function(candidate, f"static u32 {name}(")[2]
        base_function = base_function.replace(name, "base_" + name, 1)
        cand_function = cand_function.replace(name, "cand_" + name, 1)
        base_function = re.sub(r"\bread_guest_index_raw\(", "base_read_guest_index_raw(", base_function)
        base_function = re.sub(r"\bread_guest_index\(", "base_read_guest_index(", base_function)
        cand_function = re.sub(r"\bread_guest_index_raw\(", "cand_read_guest_index_raw(", cand_function)
        cand_function = re.sub(r"\bread_guest_index\(", "cand_read_guest_index(", cand_function)
        base_uploaders.append(base_function)
        cand_uploaders.append(cand_function)
    base_raw = base_raw.replace("read_guest_index_raw(", "base_read_guest_index_raw(", 1)
    base_index = base_index.replace("read_guest_index_raw(", "base_read_guest_index_raw(")
    base_index = base_index.replace("read_guest_index(", "base_read_guest_index(", 1)
    cand_raw = cand_raw.replace("read_guest_index_raw(", "cand_read_guest_index_raw(", 1)
    cand_index = cand_index.replace("read_guest_index_raw(", "cand_read_guest_index_raw(")
    cand_index = cand_index.replace("read_guest_index(", "cand_read_guest_index(", 1)

    header = r'''#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdatomic.h>
#include <windows.h>
#include "rsx_commands.h"
typedef struct { u32 localAddress; } TestGcmConfig;
extern TestGcmConfig s_config;
extern u16 s_ea_address_table[65536];
extern atomic_ullong s_main_mapping_generation;
extern SRWLOCK s_main_mapping_lock;
extern uint8_t* vm_base;
extern volatile u64 g_resolve_calls;
extern void test_after_resolve(u64 call);
int cellGcmResolveLocatedChecked(int local, u32 offset, u32* out);
void test_set_mapping(u32 page, u16 ea_page);
extern unsigned map_event_cursor, map_event_count;
extern u64 map_event_calls[4];
extern u32 map_event_page[4];
extern u16 map_event_ea[4];
void test_after_index_read(u32 k, u32 width);
#ifdef TEST_HOOKS
#define TEST_AFTER_INDEX_READ(k,w) test_after_index_read((k),(w))
#else
#define TEST_AFTER_INDEX_READ(k,w) ((void)0)
#endif
typedef struct { float v[4]; } VPSlot;
typedef struct { uint8_t* vp_vb_mapped; u32 vp_parity, vp_vb_offset; } TestD3D;
extern TestD3D s_d3d;
#define MAX_VERTICES 128u
#define VP_VERT_STRIDE (16u * sizeof(VPSlot))
#define D3D_TOPOLOGY_POINTLIST 1u
#define D3D_TOPOLOGY_LINELIST 2u
#define D3D_TOPOLOGY_LINESTRIP 3u
#define D3D_TOPOLOGY_TRIANGLELIST 4u
static int d3d12_prefix_owner_before_upload(int ignored) { (void)ignored; return 0; }
'''
    resolver_globals = r'''TestGcmConfig s_config = { 0x00100000u };
u16 s_ea_address_table[65536];
atomic_ullong s_main_mapping_generation = ATOMIC_VAR_INIT(2);
SRWLOCK s_main_mapping_lock = SRWLOCK_INIT;
volatile u64 g_resolve_calls;
unsigned map_event_cursor, map_event_count;
u64 map_event_calls[4];
u32 map_event_page[4];
u16 map_event_ea[4];
void test_set_mapping(u32 page, u16 ea_page) {
    AcquireSRWLockExclusive(&s_main_mapping_lock);
    atomic_fetch_add_explicit(&s_main_mapping_generation, 1, memory_order_acq_rel);
    s_ea_address_table[page] = ea_page;
    atomic_fetch_add_explicit(&s_main_mapping_generation, 1, memory_order_release);
    ReleaseSRWLockExclusive(&s_main_mapping_lock);
}
void test_after_resolve(u64 call) {
#ifdef TEST_HOOKS
    if (map_event_cursor < map_event_count && map_event_calls[map_event_cursor] == call) {
        test_set_mapping(map_event_page[map_event_cursor], map_event_ea[map_event_cursor]);
        ++map_event_cursor;
    }
#else
    (void)call;
#endif
}
'''
    functions = type_def + "\n" + newline.join(common) + "\n"
    functions += base_raw + base_index
    functions += "\n" + cand_raw + cand_index + "\n"
    functions += newline.join(base_uploaders + cand_uploaders)
    main = r'''static u64 base_resolves_by_kind[5], cand_resolves_by_kind[5];
static u32 run_base(const rsx_state* st, u32 primitive, u32 first, u32 count,
                    int restart, u32* topology) {
    if (restart) return base_upload_restart_vp_indexed(st, primitive, first, count, topology);
    switch (primitive) {
    case RSX_PRIMITIVE_POINTS: return base_upload_linear_vp_indexed(st, first, count);
    case RSX_PRIMITIVE_LINES: return base_upload_linear_vp_indexed(st, first, count);
    case RSX_PRIMITIVE_LINE_LOOP: return base_upload_line_loop_vp_indexed(st, first, count);
    case RSX_PRIMITIVE_LINE_STRIP: return base_upload_linear_vp_indexed(st, first, count);
    case RSX_PRIMITIVE_TRIANGLES: return base_upload_tris_vp_indexed(st, first, count);
    case RSX_PRIMITIVE_QUADS: return base_upload_quads_vp_indexed(st, first, count);
    case RSX_PRIMITIVE_QUAD_STRIP: return base_upload_quad_strip_vp_indexed(st, first, count);
    case RSX_PRIMITIVE_TRIANGLE_STRIP: return base_upload_strip_vp_indexed(st, first, count, 0);
    case RSX_PRIMITIVE_TRIANGLE_FAN: case RSX_PRIMITIVE_POLYGON:
        return base_upload_strip_vp_indexed(st, first, count, 1);
    default: return 0;
    }
}
static u32 run_cand(const rsx_state* st, u32 primitive, u32 first, u32 count,
                    int restart, const GuestIndexLocalBase* local_base,
                    u32* topology) {
    if (restart) return cand_upload_restart_vp_indexed(st, primitive, first, count, topology, local_base);
    switch (primitive) {
    case RSX_PRIMITIVE_POINTS: return cand_upload_linear_vp_indexed(st, first, count, local_base);
    case RSX_PRIMITIVE_LINES: return cand_upload_linear_vp_indexed(st, first, count, local_base);
    case RSX_PRIMITIVE_LINE_LOOP: return cand_upload_line_loop_vp_indexed(st, first, count, local_base);
    case RSX_PRIMITIVE_LINE_STRIP: return cand_upload_linear_vp_indexed(st, first, count, local_base);
    case RSX_PRIMITIVE_TRIANGLES: return cand_upload_tris_vp_indexed(st, first, count, local_base);
    case RSX_PRIMITIVE_QUADS: return cand_upload_quads_vp_indexed(st, first, count, local_base);
    case RSX_PRIMITIVE_QUAD_STRIP: return cand_upload_quad_strip_vp_indexed(st, first, count, local_base);
    case RSX_PRIMITIVE_TRIANGLE_STRIP: return cand_upload_strip_vp_indexed(st, first, count, 0, local_base);
    case RSX_PRIMITIVE_TRIANGLE_FAN: case RSX_PRIMITIVE_POLYGON:
        return cand_upload_strip_vp_indexed(st, first, count, 1, local_base);
    default: return 0;
    }
}
static u32 prepare_base(const rsx_state* st, GuestIndexLocalBase* local_base,
                        const GuestIndexLocalBase** local_ptr) {
    u32 base = 0;
    *local_ptr = NULL;
    if (st->immediate_index_data) return 1;
    if (!resolve_guest_index_base(st, &base)) return 0;
    if ((st->index_array_dma & 0xFu) == 0) {
        local_base->ea = base;
        local_base->offset = st->index_array_offset;
        *local_ptr = local_base;
    }
    return 1;
}
static void set_index_bytes(u8* dst, u32 width, u32 value) {
    if (width == 2) { dst[0] = (u8)(value >> 8); dst[1] = (u8)value; }
    else { dst[0] = (u8)(value >> 24); dst[1] = (u8)(value >> 16); dst[2] = (u8)(value >> 8); dst[3] = (u8)value; }
}
static void setup_state(rsx_state* st, u32 kind, int restart, u32 first,
                        u32* immediate, u32 count) {
    memset(st, 0, sizeof(*st));
    st->vertex_attrib_input_mask = 1;
    st->vertex_attribs[0].enabled = 1;
    st->vertex_attribs[0].offset = 0x3000;
    st->vertex_attribs[0].stride = 4;
    st->vertex_attribs[0].size = 1;
    st->vertex_attribs[0].type = 2;
    st->vertex_data_base_index = 0x000FFFF0u;
    st->restart_index_enable = restart;
    if (kind == 0) {
        st->immediate_index_data = immediate;
        st->immediate_index_count = count;
        st->index_array_dma = 0;
    } else {
        u32 width = (kind == 1 || kind == 3) ? 2u : 4u;
        int main_location = kind >= 3;
        st->index_array_dma = (main_location ? 1u : 0u) | (width == 2 ? 0x10u : 0u);
        st->index_array_offset = main_location ? 0x00200000u : 0x00001000u;
        st->restart_index = width == 2 ? 0xFFFFu : 0xFFFFFFFFu;
        if (main_location) test_set_mapping(2, 6);
        u32 ea;
        if (!cellGcmResolveLocatedChecked(!main_location, st->index_array_offset, &ea)) abort();
        u8* bytes = vm_base + ea;
        u32 values[12] = { 0, 0x30, 0x31, 0x32, 0x33, 0x34, 0x35, 0x36, 0x37, 0x38, 0x39, 0x3A };
        if (restart) { values[3] = st->restart_index; values[7] = st->restart_index; }
        for (u32 k = 0; k < 12; ++k) set_index_bytes(bytes + (u64)k * width, width, values[k]);
    }
    if (kind == 0) {
        immediate[0] = 0;
        for (u32 k = 1; k < count; ++k) immediate[k] = 0x2Fu + k;
        if (restart) { immediate[3] = 0xFFFFFFFFu; immediate[7] = 0xFFFFFFFFu; st->restart_index = 0xFFFFFFFFu; }
    }
    (void)first;
}
static unsigned run_one_case(u32 kind, u32 primitive, int restart) {
    rsx_state st;
    u32 immediate[16] = {0};
    const u32 first = 1, count = 9;
    setup_state(&st, kind, restart, first, immediate, 12);
    u8 base_out[2 * MAX_VERTICES * VP_VERT_STRIDE];
    u8 cand_out[2 * MAX_VERTICES * VP_VERT_STRIDE];
    memset(base_out, 0xA5, sizeof(base_out)); memset(cand_out, 0xA5, sizeof(cand_out));
    s_d3d.vp_vb_mapped = base_out; s_d3d.vp_parity = 0; s_d3d.vp_vb_offset = 0;
    u32 base_topology = D3D_TOPOLOGY_TRIANGLELIST;
    g_resolve_calls = 0;
    if (!st.immediate_index_data) { u32 checked; if (!resolve_guest_index_base(&st, &checked)) return 1; }
    g_resolve_calls = 0;
    u32 base_emitted = run_base(&st, primitive, first, count, restart, &base_topology);
    u64 base_resolves = g_resolve_calls;
    s_d3d.vp_vb_mapped = cand_out; s_d3d.vp_vb_offset = 0;
    u32 candidate_index_ea = 0;
    GuestIndexLocalBase local_storage = {0};
    const GuestIndexLocalBase* local_ptr = NULL;
    if (!st.immediate_index_data) {
        if (!resolve_guest_index_base(&st, &candidate_index_ea)) return 2;
        if ((st.index_array_dma & 0xFu) == 0) {
            local_storage.ea = candidate_index_ea;
            local_storage.offset = st.index_array_offset;
            local_ptr = &local_storage;
        }
    }
    g_resolve_calls = 0;
    u32 cand_topology = D3D_TOPOLOGY_TRIANGLELIST;
    u32 cand_emitted = run_cand(&st, primitive, first, count, restart, local_ptr, &cand_topology);
    u64 cand_resolves = g_resolve_calls;
    base_resolves_by_kind[kind] += base_resolves;
    cand_resolves_by_kind[kind] += cand_resolves;
    if (base_emitted != cand_emitted || base_topology != cand_topology ||
        s_d3d.vp_vb_offset != cand_emitted * VP_VERT_STRIDE ||
        memcmp(base_out, cand_out, (size_t)cand_emitted * VP_VERT_STRIDE) != 0) {
        fprintf(stderr, "equivalence mismatch kind=%u primitive=%u restart=%d emitted=%u/%u\n",
                kind, primitive, restart, base_emitted, cand_emitted);
        return 3;
    }
    if (kind == 1 || kind == 2) {
        if (cand_resolves >= base_resolves) return 4;
    } else if (cand_resolves != base_resolves) return 5;
    return 0;
}
static void test_after_index_read(u32 k, u32 width) {
#ifdef TEST_HOOKS
    extern u64 g_mutate_after;
    extern u64 g_mutate_base;
    extern u32 g_mutate_index;
    extern u32 g_mutate_value;
    extern u64 g_index_reads;
    if (g_mutate_after && ++g_index_reads == g_mutate_after) {
        set_index_bytes(vm_base + g_mutate_base + (u64)g_mutate_index * width,
                        width, g_mutate_value);
        g_mutate_after = 0;
    }
#else
    (void)k; (void)width;
#endif
}
''' + r'''
static volatile u64 bench_sink;
typedef u32 (*reader_fn)(const rsx_state*, u32, const GuestIndexLocalBase*);
__declspec(noinline) static u32 baseline_reader(const rsx_state* st, u32 k, const GuestIndexLocalBase* unused) {
    (void)unused; return base_read_guest_index(st, k);
}
__declspec(noinline) static u32 candidate_reader(const rsx_state* st, u32 k, const GuestIndexLocalBase* local) {
    return cand_read_guest_index(st, k, local);
}
static double time_reader(reader_fn fn, const rsx_state* st, const GuestIndexLocalBase* local,
                          u64* out_iters) {
    LARGE_INTEGER qpf, start, now;
    QueryPerformanceFrequency(&qpf); QueryPerformanceCounter(&start);
    u64 iters = 0, sum = 0;
    do {
        for (u32 i = 0; i < 4096; ++i, ++iters)
            sum += fn(st, i & 31u, local);
        QueryPerformanceCounter(&now);
    } while ((double)(now.QuadPart - start.QuadPart) / (double)qpf.QuadPart < 0.050);
    bench_sink ^= sum; *out_iters = iters;
    return (double)(now.QuadPart - start.QuadPart) * 1.0e9 / (double)qpf.QuadPart / (double)iters;
}
static void report_timing(u32 kind, const rsx_state* st, const GuestIndexLocalBase* local) {
    double b[5], c[5]; u64 n=0;
    for (unsigned r=0;r<5;++r) {
        if (r&1) { c[r]=time_reader(candidate_reader,st,local,&n); b[r]=time_reader(baseline_reader,st,local,&n); }
        else { b[r]=time_reader(baseline_reader,st,local,&n); c[r]=time_reader(candidate_reader,st,local,&n); }
    }
    double bs[5], cs[5]; memcpy(bs,b,sizeof b); memcpy(cs,c,sizeof c);
    for (unsigned i=0;i<5;++i) for (unsigned j=i+1;j<5;++j) {
        if(bs[j]<bs[i]){double t=bs[i];bs[i]=bs[j];bs[j]=t;}
        if(cs[j]<cs[i]){double t=cs[i];cs[i]=cs[j];cs[j]=t;}
    }
    printf("TIMING kind=%u baseline median/min/max=%.2f/%.2f/%.2f candidate=%.2f/%.2f/%.2f ns/index sink=%llu\n",
        kind,bs[2],bs[0],bs[4],cs[2],cs[0],cs[4],(unsigned long long)bench_sink);
}
int main(void) {
    vm_base = (u8*)calloc(1, 16u * 1024u * 1024u); if (!vm_base) return 10;
    static const u32 kinds[] = {0,1,2,3,4};
    unsigned cases=0;
#ifdef TEST_HOOKS
    for (u32 k=0;k<5;++k) for (u32 p=1;p<=10;++p) for(int restart=0;restart<=1;++restart) {
        int rc=run_one_case(kinds[k],p,restart); if(rc) return rc; ++cases;
    }
    printf("EQUIVALENCE PASS cases=%u primitives=1..10 restart=0/1 immediate+u16/u32 local+main\n",cases);
    for (u32 k=0;k<5;++k)
        printf("CALLCOUNTS kind=%u baseline=%llu candidate=%llu saved=%lld\n", k,
            (unsigned long long)base_resolves_by_kind[k],
            (unsigned long long)cand_resolves_by_kind[k],
            (long long)base_resolves_by_kind[k]-(long long)cand_resolves_by_kind[k]);
    /* EA zero is valid: localAddress+offset wraps to zero; optional pointer, not sentinel, selects it. */
    s_config.localAddress=1; rsx_state zero_state; u32 zero_indices[4]={0,1,2,3};
    setup_state(&zero_state,1,0,0,zero_indices,4); zero_state.index_array_offset=0xFFFFFFFFu;
    zero_state.index_array_dma=0; zero_state.vertex_data_base_index=0;
    u32 zero_ea=0xFFFFFFFFu; if(!resolve_guest_index_base(&zero_state,&zero_ea)||zero_ea!=0) return 11;
    set_index_bytes(vm_base,4,7); GuestIndexLocalBase zero_local={zero_ea,zero_state.index_array_offset};
    if(base_read_guest_index(&zero_state,0)!=7||cand_read_guest_index(&zero_state,0,&zero_local)!=7) return 12;
    s_config.localAddress=0x00100000u;
    /* A live local index-byte mutation between fetches must be observed by both readers. */
    rsx_state mutate_state; u32 im[16]={0}; setup_state(&mutate_state,2,0,0,im,12);
    mutate_state.vertex_data_base_index=0;
    u32 local_ea; if(!resolve_guest_index_base(&mutate_state,&local_ea)) return 13;
    set_index_bytes(vm_base+local_ea,4,1); set_index_bytes(vm_base+local_ea+4,4,2);
    GuestIndexLocalBase local={local_ea,mutate_state.index_array_offset};
    g_mutate_base=local_ea; g_mutate_index=1; g_mutate_value=9; g_mutate_after=1; g_index_reads=0;
    u32 before=base_read_guest_index(&mutate_state,0);
    u32 after=base_read_guest_index(&mutate_state,1);
    if(before!=1||after!=9) return 14;
    set_index_bytes(vm_base+local_ea+4,2,2); g_mutate_after=1; g_index_reads=0;
    before=cand_read_guest_index(&mutate_state,0,&local);
    after=cand_read_guest_index(&mutate_state,1,&local);
    if(before!=1||after!=9) return 15;
    /* MAIN mapping changes after fetch one; both paths must resolve every fetch. */
    rsx_state main_state; setup_state(&main_state,4,0,0,im,12);
    main_state.vertex_data_base_index=0;
    main_state.index_array_offset=0x00200100u;
    test_set_mapping(2,6); set_index_bytes(vm_base+0x00600100u,4,4);
    set_index_bytes(vm_base+0x00700100u,4,5);
    g_resolve_calls=0; u32 main_base; if(!resolve_guest_index_base(&main_state,&main_base)) return 16;
    map_event_cursor=0; map_event_count=2; map_event_calls[0]=g_resolve_calls+1; map_event_page[0]=2; map_event_ea[0]=7;
    map_event_calls[1]=g_resolve_calls+2; map_event_page[1]=2; map_event_ea[1]=0xFFFF;
    u32 v0=base_read_guest_index(&main_state,0), v1=base_read_guest_index(&main_state,0), v2=base_read_guest_index(&main_state,0);
    if(v0!=4||v1!=5||v2!=0||map_event_cursor!=2) return 17;
    test_set_mapping(2,6); g_resolve_calls=0; if(!resolve_guest_index_base(&main_state,&main_base)) return 18;
    map_event_cursor=0; map_event_calls[0]=g_resolve_calls+1; map_event_page[0]=2; map_event_ea[0]=7;
    map_event_calls[1]=g_resolve_calls+2; map_event_page[1]=2; map_event_ea[1]=0xFFFF;
    v0=cand_read_guest_index(&main_state,0,NULL); v1=cand_read_guest_index(&main_state,0,NULL); v2=cand_read_guest_index(&main_state,0,NULL);
    if(v0!=4||v1!=5||v2!=0||map_event_cursor!=2) return 19;
    printf("MUTATION PASS local live-byte-per-fetch; MAIN map/remap/unmap-per-fetch; EA-zero valid; index bytes uncached\n");
#else
    rsx_state st; u32 immediate[16]={0}; setup_state(&st,2,0,0,immediate,12);
    u32 base; if(!resolve_guest_index_base(&st,&base)) return 20;
    GuestIndexLocalBase local={base,st.index_array_offset};
    report_timing(2,&st,&local);
    st.index_array_dma=0x11; st.index_array_offset=0x00200000u; test_set_mapping(2,6);
    if(!resolve_guest_index_base(&st,&base)) return 21;
    report_timing(4,&st,NULL);
#endif
    free(vm_base); return 0;
}
'''
    main = main.replace("GuestIndexLocalBase*", "GuestIndexLocalBase*")
    common_types = "typedef struct GuestIndexLocalBase { u32 ea, offset; } GuestIndexLocalBase;\n"
    resolver_output = temp / "resolver_test.c"
    resolver_source = header + "\n" + resolver_globals + "\n" + newline.join(resolver_functions) + "\n"
    index_globals = "TestD3D s_d3d;\nuint8_t* vm_base;\nu64 g_mutate_after, g_mutate_base, g_index_reads;\nu32 g_mutate_index, g_mutate_value;\n"
    output = temp / "indexed_base_test.c"
    source = header + "\n" + index_globals + functions + "\n" + main
    if not hooks:
        resolver_source = resolver_source.replace("#ifdef TEST_HOOKS\n    ++g_resolve_calls;\n#endif", "")
    resolver_output.write_bytes(resolver_source.encode("utf-8"))
    output.write_bytes(source.encode("utf-8"))
    return resolver_output, output


def main() -> int:
    baseline, candidate = pin_source_pair()
    compiler = shutil.which("clang") or r"C:\Program Files\LLVM\bin\clang.exe"
    if not Path(compiler).exists():
        raise RuntimeError(f"clang not found: {compiler}")
    print(f"baseline_renderer_sha256={BASELINE_SHA256}")
    print(f"candidate_renderer_sha256={sha(candidate.encode('utf-8'))}")
    with tempfile.TemporaryDirectory(prefix="tj-index-base-reuse-") as temp_name:
        temp = Path(temp_name)
        for hooks, name in ((True, "equivalence"), (False, "timing")):
            resolver_file, c_file = compile_text(baseline, candidate, hooks, temp)
            exe = temp / f"{name}.exe"
            resolver_obj = temp / f"{name}_resolver.obj"
            index_obj = temp / f"{name}_index.obj"
            command = [compiler, "-O2", "-std=c11", "-Wno-unused-function", "-Wno-unused-parameter",
                       "-D_CRT_SECURE_NO_WARNINGS", "-I", str(VIDEO), "-I", str(VIDEO.parent.parent / "include")]
            if hooks: command.append("-DTEST_HOOKS=1")
            build = subprocess.run(command + ["-c", str(resolver_file), "-o", str(resolver_obj)], capture_output=True, text=True)
            if build.returncode:
                raise RuntimeError("resolver compile failed:\n" + build.stdout + build.stderr)
            build = subprocess.run(command + ["-c", str(c_file), "-o", str(index_obj)], capture_output=True, text=True)
            if build.returncode:
                raise RuntimeError("index compile failed:\n" + build.stdout + build.stderr)
            build = subprocess.run([compiler, str(resolver_obj), str(index_obj), "-o", str(exe)], capture_output=True, text=True)
            if build.returncode:
                raise RuntimeError("native compile failed:\n" + build.stdout + build.stderr)
            run = subprocess.run([str(exe)], capture_output=True, text=True)
            if run.returncode:
                raise RuntimeError(f"{name} failed ({run.returncode}):\n{run.stdout}{run.stderr}")
            print(run.stdout, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
