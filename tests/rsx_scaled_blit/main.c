#include "rsx_scaled_blit.h"
#include "rsx_ordered_display_alias.h"

#include <stdio.h>
#include <string.h>

static int fail(const char* message)
{
    fprintf(stderr, "FAIL: %s\n", message);
    return 1;
}

int main(void)
{
    uint8_t src[8 * 6 * 4];
    uint8_t dst[10 * 8 * 4];
    for (unsigned i = 0; i < sizeof(src); i++) src[i] = (uint8_t)(i + 1);
    memset(dst, 0, sizeof(dst));

    struct rsx_scaled_blit_desc d;
    memset(&d, 0, sizeof(d));
    d.src = src; d.dst = dst;
    d.src_pitch = 8 * 4; d.dst_pitch = 10 * 4;
    d.in_width = 8; d.in_height = 6;
    d.out_width = 8; d.out_height = 6;
    d.clip_x = 2; d.clip_y = 1; d.clip_width = 3; d.clip_height = 2;
    d.out_x = 4; d.out_y = 3;
    d.ds_dx = d.dt_dy = 0x100000;
    d.src_format = 3; d.dst_format = 10; d.origin = 2;
    if (rsx_scaled_blit(&d) != RSX_SCALED_BLIT_OK ||
        memcmp(dst + (4 * 10 + 6) * 4, src + (1 * 8 + 2) * 4, 3 * 4) != 0 ||
        memcmp(dst + (5 * 10 + 6) * 4, src + (2 * 8 + 2) * 4, 3 * 4) != 0 ||
        dst[(3 * 10 + 4) * 4] != 0)
        return fail("unscaled clipped A8R8G8B8 blit");

    /* Clip point is relative to IMAGE_OUT_POINT. Scaling must use the same
     * clipped source coordinates while preserving the unclipped destination. */
    uint8_t clipped_src[4 * 4 * 4];
    uint8_t clipped_dst[8 * 8 * 4];
    for (unsigned i = 0; i < sizeof(clipped_src); i++)
        clipped_src[i] = (uint8_t)(0x20 + i);
    memset(clipped_dst, 0, sizeof(clipped_dst));
    memset(&d, 0, sizeof(d));
    d.src = clipped_src; d.dst = clipped_dst;
    d.src_pitch = 4 * 4; d.dst_pitch = 8 * 4;
    d.in_width = d.in_height = 4;
    d.out_width = d.out_height = 8;
    d.clip_x = 2; d.clip_y = 2; d.clip_width = 2; d.clip_height = 2;
    d.out_x = 1; d.out_y = 1;
    d.ds_dx = d.dt_dy = 0x80000;
    d.src_format = 3; d.dst_format = 10; d.origin = 2;
    if (rsx_scaled_blit(&d) != RSX_SCALED_BLIT_OK ||
        memcmp(clipped_dst + (3 * 8 + 3) * 4,
               clipped_src + (1 * 4 + 1) * 4, 4) != 0 ||
        clipped_dst[(1 * 8 + 1) * 4] != 0)
        return fail("scaled clip point destination/source alignment");

    /* Tokyo Jungle's other func_001FC4EC branch emits source format 7 and
     * NV3062 destination format 4 for two-byte texture uploads. */
    uint8_t src565[6 * 3 * 2];
    uint8_t dst565[8 * 4 * 2];
    for (unsigned i = 0; i < sizeof(src565); i++)
        src565[i] = (uint8_t)(0x40 + i);
    memset(dst565, 0, sizeof(dst565));
    memset(&d, 0, sizeof(d));
    d.src = src565; d.dst = dst565;
    d.src_pitch = 6 * 2; d.dst_pitch = 8 * 2;
    d.in_width = 6; d.in_height = 3;
    d.out_width = d.clip_width = 4;
    d.out_height = d.clip_height = 2;
    d.out_x = 2; d.out_y = 1;
    d.ds_dx = d.dt_dy = 0x100000;
    d.src_format = 7; d.dst_format = 4; d.origin = 2;
    if (rsx_scaled_blit(&d) != RSX_SCALED_BLIT_OK ||
        memcmp(dst565 + (1 * 8 + 2) * 2, src565, 4 * 2) != 0 ||
        memcmp(dst565 + (2 * 8 + 2) * 2, src565 + 6 * 2, 4 * 2) != 0)
        return fail("Tokyo Jungle R5G6B5 upload branch");

    uint8_t tiny[2 * 2 * 4] = {
        1,2,3,4, 5,6,7,8, 9,10,11,12, 13,14,15,16
    };
    uint8_t large[4 * 4 * 4];
    memset(large, 0, sizeof(large));
    memset(&d, 0, sizeof(d));
    d.src = tiny; d.dst = large;
    d.src_pitch = 8; d.dst_pitch = 16;
    d.in_width = 2; d.in_height = 2;
    d.out_width = d.clip_width = 4; d.out_height = d.clip_height = 4;
    d.ds_dx = d.dt_dy = 0x80000;
    d.src_format = 3; d.dst_format = 10; d.origin = 2;
    if (rsx_scaled_blit(&d) != RSX_SCALED_BLIT_OK ||
        memcmp(large, tiny, 4) != 0 || memcmp(large + 4, tiny, 4) != 0 ||
        memcmp(large + 12, tiny + 4, 4) != 0 ||
        memcmp(large + 3 * 16 + 12, tiny + 12, 4) != 0)
        return fail("2x nearest scaled blit");

    /* NV3089 ZOH stays nearest; FOH bilinearly blends the same 11.20 source
     * coordinates. */
    uint8_t filtered[4 * 4 * 4];
    memset(filtered, 0, sizeof(filtered));
    d.dst = filtered;
    d.interpolator = RSX_SCALED_BLIT_INTERPOLATOR_FOH;
    if (rsx_scaled_blit(&d) != RSX_SCALED_BLIT_OK ||
        filtered[1 * 16 + 4 + 0] != 7 || filtered[1 * 16 + 4 + 1] != 8 ||
        filtered[1 * 16 + 4 + 2] != 9 || filtered[1 * 16 + 4 + 3] != 10 ||
        memcmp(filtered + 3 * 16 + 12, tiny + 12, 4) != 0)
        return fail("2x FOH scaled blit");
    d.interpolator = RSX_SCALED_BLIT_INTERPOLATOR_ZOH;
    d.ds_dx = 0;
    if (rsx_scaled_blit(&d) != RSX_SCALED_BLIT_UNSUPPORTED_OPERATION ||
        rsx_scaled_blit_exact_rgba_region(&d, NULL))
        return fail("zero horizontal scale rejects CPU and deferred blits");

    /* Signed NV3089 scale mirrors inside IMAGE_IN, including both axes. */
    uint8_t mirrored[sizeof(tiny)];
    const uint8_t mirrored_expected[sizeof(tiny)] = {
        13,14,15,16, 9,10,11,12, 5,6,7,8, 1,2,3,4
    };
    memset(mirrored, 0, sizeof(mirrored));
    memset(&d, 0, sizeof(d));
    d.src = tiny; d.dst = mirrored;
    d.src_pitch = d.dst_pitch = 2 * 4;
    d.in_width = d.in_height = d.out_width = d.out_height = 2;
    d.clip_width = d.clip_height = 2;
    d.ds_dx = d.dt_dy = -0x100000;
    d.src_format = 3; d.dst_format = 10; d.origin = 2;
    if (rsx_scaled_blit(&d) != RSX_SCALED_BLIT_OK ||
        memcmp(mirrored, mirrored_expected, sizeof(mirrored)) != 0)
        return fail("negative scale mirrors within source extent");

    /* A nonzero IMAGE_IN point reduces the usable source extent.  The scaler
     * must clamp there before it forms its source pointer. */
    uint8_t offset_src[4 * 2 * 4];
    uint8_t offset_dst[4 * 2 * 4];
    for (unsigned i = 0; i < sizeof(offset_src); ++i)
        offset_src[i] = (uint8_t)(0x60 + i);
    memset(offset_dst, 0, sizeof(offset_dst));
    memset(&d, 0, sizeof(d));
    d.src = offset_src; d.dst = offset_dst;
    d.src_pitch = d.dst_pitch = 4 * 4;
    d.in_width = d.out_width = 4; d.in_height = d.out_height = 2;
    d.clip_width = 4; d.clip_height = 2;
    d.in_x_4 = 2 * 16; d.ds_dx = d.dt_dy = 0x80000;
    d.src_format = 3; d.dst_format = 10; d.origin = 2;
    if (rsx_scaled_blit(&d) != RSX_SCALED_BLIT_OK ||
        memcmp(offset_dst, offset_src + 2 * 4, 4) != 0 ||
        memcmp(offset_dst + 4, offset_src + 2 * 4, 4) != 0 ||
        memcmp(offset_dst + 8, offset_src + 3 * 4, 4) != 0)
        return fail("scaled source point clamps to remaining extent");

    uint8_t rgb565[2] = {0xF8, 0x00};
    uint8_t argb[4] = {0};
    memset(&d, 0, sizeof(d));
    d.src = rgb565; d.dst = argb;
    d.in_width = d.in_height = d.out_width = d.out_height = 1;
    d.clip_width = d.clip_height = 1;
    d.ds_dx = d.dt_dy = 0x100000;
    d.src_format = 7; d.dst_format = 10; d.origin = 2;
    if (rsx_scaled_blit(&d) != RSX_SCALED_BLIT_OK ||
        argb[0] != 0xFF || argb[1] != 0xFF || argb[2] || argb[3])
        return fail("R5G6B5 to A8R8G8B8 conversion");

    /* Deferred D3D mirror is deliberately narrower than CPU NV3089: only
     * byte-exact 4-byte-pixel copies may enter the ordered GPU record stream. */
    struct rsx_scaled_blit_exact_region r;
    memset(&d, 0, sizeof(d));
    d.in_width = d.out_width = 8; d.in_height = d.out_height = 6;
    d.clip_x = 2; d.clip_y = 1; d.clip_width = 3; d.clip_height = 2;
    d.out_x = 4; d.out_y = 3;
    d.ds_dx = d.dt_dy = 0x100000;
    d.src_format = 3; d.dst_format = 10; d.origin = 2;
    if (!rsx_scaled_blit_exact_rgba_region(&d, &r) ||
        r.src_x != 2 || r.src_y != 1 || r.dst_x != 6 || r.dst_y != 4 ||
        r.width != 3 || r.height != 2)
        return fail("exact RGBA deferred-copy region");
    d.ds_dx = 0x80000;
    if (rsx_scaled_blit_exact_rgba_region(&d, &r))
        return fail("scaled blit must stay CPU-only");
    d.ds_dx = d.dt_dy = 0x100000; d.src_format = 7; d.dst_format = 4;
    if (rsx_scaled_blit_exact_rgba_region(&d, &r))
        return fail("format-converting blit must stay CPU-only");

    /* Split 2D aliases are one GPU backing only when the requested rectangle
     * remains within it.  A row-wrapping alias must retain the CPU fallback. */
    uint32_t ax = 0, ay = 0;
    if (!rsx_scaled_blit_alias_origin(0x003A0000u, 0x003A1000u,
                                      5120, 1280, 720, 256, 720, &ax, &ay) ||
        ax != 1024 || ay != 0)
        return fail("split alias origin");
    if (rsx_scaled_blit_alias_origin(0x003A0000u, 0x003A1000u,
                                     5120, 1280, 720, 1024, 720, &ax, &ay))
        return fail("row-wrapping alias must reject");

    uint64_t span = 0;
    if (!rsx_scaled_blit_guest_span_valid(0x100u, 40, 10, 8, 0x240u, &span) ||
        span != 320 || rsx_scaled_blit_guest_span_valid(0x100u, 40, 10, 8,
                                                          0x23Fu, &span) ||
        !rsx_scaled_blit_guest_span_valid(0x09263000u, 5120, 1280, 720,
                                          0, &span) || span != 3686400 ||
        rsx_scaled_blit_guest_span_valid(0xFFFFFFF0u, 4, 1, 8, 0, &span))
        return fail("guest span bounds");
    if (!rsx_scaled_blit_guest_surface_valid(0x100u, 12, 6, 3, 2, 0x124u) ||
        rsx_scaled_blit_guest_surface_valid(0x100u, 12, 6, 3, 2, 0x123u))
        return fail("generic guest surface bounds");

    uint8_t rgba[8] = {1,2,3,4, 5,6,7,8};
    uint8_t guest_argb[4 * 4 * 4];
    memset(guest_argb, 0xA5, sizeof(guest_argb));
    rsx_exact_blit_rgba_to_guest_argb(guest_argb + (1 * 4 + 1) * 4, rgba, 2);
    if (guest_argb[(1 * 4 + 1) * 4 + 0] != 4 ||
        guest_argb[(1 * 4 + 1) * 4 + 1] != 1 ||
        guest_argb[(1 * 4 + 2) * 4 + 0] != 8 ||
        guest_argb[(0 * 4 + 0) * 4] != 0xA5 ||
        guest_argb[(1 * 4 + 3) * 4] != 0xA5)
        return fail("RGBA readback converts only dirty guest ARGB rect");
    if (rsx_exact_blit_rects_overlap(0, 0, 1024, 720, 1024, 0, 256, 720) ||
        !rsx_exact_blit_rects_overlap(0, 0, 1024, 720, 1000, 0, 256, 720))
        return fail("split exact writebacks accept nonoverlap and reject overlap");
    if (!rsx_exact_blit_rect_fully_contains(0, 0, 8, 8, 2, 2, 2, 2) ||
        rsx_exact_blit_rect_fully_contains(0, 0, 8, 8, 7, 7, 2, 2) ||
        !rsx_exact_blit_rects_overlap(0, 0, 8, 8, 7, 7, 2, 2))
        return fail("terminal full-cover supersedes; partial overlap rejects");
    int main_authoritative = 0;
    int earlier_immutable_source = rsx_exact_blit_authority_step(
        main_authoritative, &main_authoritative);
    int later_display_producer = rsx_exact_blit_authority_step(1, &main_authoritative);
    if (earlier_immutable_source || !later_display_producer || !main_authoritative)
        return fail("future producer cannot authorize earlier MAIN source");
    main_authoritative = 0;
    int earlier_display_producer = rsx_exact_blit_authority_step(1, &main_authoritative);
    int later_main_source = rsx_exact_blit_authority_step(
        main_authoritative, NULL);
    if (!earlier_display_producer || !later_main_source)
        return fail("earlier authoritative producer enables later MAIN source");
    if (!rsx_exact_blit_mapping_generation_stable(8, 8) ||
        rsx_exact_blit_mapping_generation_stable(7, 7) ||
        rsx_exact_blit_mapping_generation_stable(8, 10))
        return fail("mapping seqlock rejects odd or changed generation");
    if (!rsx_exact_blit_record_mapping_matches(8, 8, 8,
                                                0x09263000u, 0x09263000u,
                                                0x09264000u, 0x09264000u) ||
        rsx_exact_blit_record_mapping_matches(7, 7, 7,
                                               0x09263000u, 0x09263000u,
                                               0x09264000u, 0x09264000u) ||
        rsx_exact_blit_record_mapping_matches(8, 8, 10,
                                               0x09263000u, 0x09263000u,
                                               0x09264000u, 0x09264000u) ||
        rsx_exact_blit_record_mapping_matches(8, 8, 8,
                                               0x09263000u, 0x09263000u,
                                               0x09264000u, 0x09265000u))
        return fail("record mapping binds both endpoints and rejects remap");
    /* Distinct raw IO offsets may resolve to one MAIN EA backing. Exact alias
     * is eligible for the prior authoritative mirror; partial aliases fail
     * closed, while disjoint MAIN spans remain independent. */
    if (!rsx_exact_blit_canonical_spans_overlap(0x09263000u, 5120, 0x09263000u, 5120) ||
        !rsx_exact_blit_canonical_spans_overlap(0x09263000u, 5120,
                                                 0x09263800u, 5120) ||
        rsx_exact_blit_canonical_spans_overlap(0x09263000u, 5120,
                                                0x09266000u, 5120))
        return fail("canonical MAIN alias overlap gate");
    if (!rsx_exact_blit_main_source_layout_matches(0x09263000u, 5120, 1280, 720,
                                                    0x09263000u, 5120, 1280, 720) ||
        rsx_exact_blit_main_source_layout_matches(0x09263000u, 5120, 1280, 720,
                                                   0x09263000u, 4096, 1024, 720) ||
        rsx_exact_blit_main_source_layout_matches(0x09263000u, 5120, 1280, 720,
                                                   0x09263800u, 5120, 1280, 720) ||
        !rsx_exact_blit_canonical_spans_overlap(0x09263000u, 5120,
                                                 0x09263800u, 5120) ||
        !rsx_exact_blit_canonical_spans_overlap(0x09263000u, 5120,
                                                 0x09263000u, 4096) ||
        rsx_exact_blit_canonical_spans_overlap(0x09263000u, 5120,
                                                0x09268000u, 5120))
        return fail("MAIN source aliases require exact layout or reject");
    uint8_t baseline[8]; memcpy(baseline, guest_argb + (1 * 4 + 1) * 4, 8);
    if (!rsx_exact_blit_baseline_matches(guest_argb + (1 * 4 + 1) * 4, baseline, 8))
        return fail("baseline compare");
    guest_argb[(1 * 4 + 1) * 4] ^= 1;
    if (rsx_exact_blit_baseline_matches(guest_argb + (1 * 4 + 1) * 4, baseline, 8))
        return fail("baseline mismatch must suppress commit");

    /* Sentinel rows prove conversion never uses a D3D-aligned row pitch and
     * that every planned dirty row participates in the precommit baseline. */
    uint8_t rows_rgba[2 * 12] = {1,2,3,4, 5,6,7,8, 0xEE,0xEE,0xEE,0xEE,
                                  9,10,11,12, 13,14,15,16, 0xEE,0xEE,0xEE,0xEE};
    uint8_t rows_guest[2 * 16];
    uint8_t rows_base[16];
    memset(rows_guest, 0xA5, sizeof(rows_guest));
    rsx_exact_blit_rgba_rows_to_guest_argb(rows_guest + 4, 16, rows_rgba, 12, 2, 2);
    memcpy(rows_base, rows_guest + 4, 8);
    memcpy(rows_base + 8, rows_guest + 20, 8);
    if (rows_guest[0] != 0xA5 || rows_guest[12] != 0xA5 ||
        rows_guest[4] != 4 || rows_guest[8] != 8 || rows_guest[20] != 12 ||
        rows_guest[24] != 16 ||
        !rsx_exact_blit_dirty_rows_match(rows_guest + 4, 16, rows_base, 8, 2))
        return fail("packed dirty conversion preserves row sentinels");
    rows_guest[20] ^= 1;
    if (rsx_exact_blit_dirty_rows_match(rows_guest + 4, 16, rows_base, 8, 2))
        return fail("all planned rows must match before writeback");

    rsx_display_alias_surface parent0 = { 0x00000000u, 5120, 1280, 720 };
    rsx_display_alias_surface parent1 = { 0x003A0000u, 5120, 1280, 720 };
    rsx_display_alias_surface alias = { 0x00740000u, 5120, 1280, 720 };
    uint32_t owner0 = 0, owner1 = 0;
    if (!rsx_display_history_alias_parents(&parent0, &parent1, &alias,
                                           &owner0, &owner1) ||
        owner0 != parent0.raw || owner1 != parent1.raw)
        return fail("strict display alias parents");
    alias.width = 1279;
    if (rsx_display_history_alias_parents(&parent0, &parent1, &alias, NULL, NULL))
        return fail("alias geometry mismatch");
    alias.width = 1280;
    alias.raw = 0x00741000u;
    if (rsx_display_history_alias_parents(&parent0, &parent1, &alias, NULL, NULL))
        return fail("non-next alias");
    alias.raw = 0x00740000u;
    if (rsx_display_history_alias_parents(&parent1, &parent0, &alias, NULL, NULL))
        return fail("reversed alias parents");
    int have_pair = 0;
    uint32_t pair0 = 0, pair1 = 0;
    if (rsx_display_history_alias_select_unique_pair(
            &parent0, &parent1, &alias, &pair0, &pair1, &have_pair) != 1 ||
        !have_pair || pair0 != parent0.raw || pair1 != parent1.raw ||
        rsx_display_history_alias_select_unique_pair(
            &parent0, &parent1, &alias, &pair0, &pair1, &have_pair) != 1)
        return fail("duplicate alias pair stays unique");
    rsx_display_alias_surface alternate0 = { 0x00100000u, 5120, 1280, 720 };
    rsx_display_alias_surface alternate1 = { 0x00420000u, 5120, 1280, 720 };
    if (rsx_display_history_alias_select_unique_pair(
            &alternate0, &alternate1, &alias, &pair0, &pair1, &have_pair) >= 0)
        return fail("distinct matching alias pair is ambiguous");

    /* History aliases select a live DisplaySurface only from an earlier
     * prepared exact copy to the same slot. */
    if (!rsx_ordered_display_alias_copy_precedes(5, 4, 1, 1, 2, 2))
        return fail("prior alias copy must own later reader");
    if (rsx_ordered_display_alias_copy_precedes(4, 5, 1, 1, 2, 2))
        return fail("later alias copy must not retarget earlier reader");
    if (rsx_ordered_display_alias_copy_precedes(5, 4, 0, 1, 2, 2))
        return fail("unprepared alias copy must not own reader");
    if (rsx_ordered_display_alias_copy_precedes(5, 4, 1, 0, 2, 2))
        return fail("non-display copy must not own reader");
    if (rsx_ordered_display_alias_copy_precedes(5, 4, 1, 1, 3, 2))
        return fail("different display slot must not own reader");
    rsx_ordered_display_alias_copy_rect split_parent[] = {
        { 4, 0,    0, 1024, 720, 1, 1, 1, 2 },
        { 6, 1024, 0,  256, 720, 1, 1, 1, 2 },
    };
    int latest_copy = -1;
    if (!rsx_ordered_display_alias_copy_union_covers(
            split_parent, 2, 8, 2, 0, 0, 0, 1280, 720, &latest_copy) ||
        latest_copy != 6)
        return fail("split prepared exact copies cover one live parent");
    split_parent[1].x = 1025;
    if (rsx_ordered_display_alias_copy_union_covers(
            split_parent, 2, 8, 2, 0, 0, 0, 1280, 720, NULL))
        return fail("gapped split copies must not own alias");
    split_parent[1].x = 1024;
    split_parent[1].w = 255;
    if (rsx_ordered_display_alias_copy_union_covers(
            split_parent, 2, 8, 2, 0, 0, 0, 1280, 720, NULL))
        return fail("incomplete split copies must not own alias");
    split_parent[1].w = 256;
    rsx_ordered_display_alias_copy_rect later_only[] = {
        { 9, 0, 0, 1280, 720, 1, 1, 1, 2 },
    };
    if (rsx_ordered_display_alias_copy_union_covers(
            later_only, 1, 8, 2, 0, 0, 0, 1280, 720, NULL))
        return fail("later copy must not retarget earlier alias read");
    rsx_ordered_display_alias_copy_rect second_parent[] = {
        { 5, 0, 0, 1280, 720, 1, 1, 1, 3 },
    };
    int first_live = rsx_ordered_display_alias_copy_union_covers(
        split_parent, 2, 8, 2, 0, 0, 0, 1280, 720, NULL);
    int second_live = rsx_ordered_display_alias_copy_union_covers(
        second_parent, 1, 8, 3, 0, 0, 0, 1280, 720, NULL);
    if (!first_live || !second_live ||
        rsx_ordered_display_alias_unique_owner(2, 3, first_live, second_live) >= 0)
        return fail("two fully covered parents remain ambiguous");
    rsx_ordered_display_alias_copy_rect capped_parent[
        RSX_ORDERED_DISPLAY_ALIAS_MAX_COPY_RECTS + 1] = {{0}};
    int capped_proof = rsx_ordered_display_alias_copy_union_covers(
        capped_parent, RSX_ORDERED_DISPLAY_ALIAS_MAX_COPY_RECTS + 1,
        8, 2, 0, 0, 0, 1280, 720, NULL);
    if (capped_proof != RSX_ORDERED_DISPLAY_ALIAS_COPY_PROOF_ERROR ||
        rsx_ordered_display_alias_unique_owner_proofs(
            2, 3, capped_proof, second_live) >= 0)
        return fail("capped parent proof must not select covered other parent");
    if (rsx_ordered_display_alias_unique_owner_proofs(
            2, 3, capped_proof,
            RSX_ORDERED_DISPLAY_ALIAS_COPY_PROOF_NO) >= 0)
        return fail("error plus no parent proof must stay non-live");
    split_parent[1].executed = 0;
    if (!rsx_ordered_display_alias_copy_union_covers(
            split_parent, 2, 8, 2, 0, 0, 0, 1280, 720, NULL) ||
        rsx_ordered_display_alias_copy_union_covers(
            split_parent, 2, 8, 2, 1, 0, 0, 1280, 720, NULL))
        return fail("failed contributing copy rejects executed union proof");
    split_parent[1].executed = 1;
    split_parent[1].x = UINT32_MAX;
    split_parent[1].w = 2;
    if (rsx_ordered_display_alias_copy_union_covers(
            split_parent, 2, 8, 2, 0, 0, 0, 1280, 720, NULL) !=
            RSX_ORDERED_DISPLAY_ALIAS_COPY_PROOF_ERROR)
        return fail("overflowing copy rectangle rejects alias proof");
    if (rsx_ordered_display_alias_unique_owner(2, 3, 1, 0) != 2)
        return fail("unique first parent owner");
    /* Two split packets may both write one parent slot and remain one owner. */
    if (!rsx_ordered_display_alias_copy_precedes(8, 4, 1, 1, 2, 2) ||
        !rsx_ordered_display_alias_copy_precedes(8, 6, 1, 1, 2, 2) ||
        rsx_ordered_display_alias_unique_owner(2, 3, 1, 0) != 2)
        return fail("split same-slot copies retain unique owner");
    if (rsx_ordered_display_alias_unique_owner(2, 3, 1, 1) >= 0)
        return fail("two prior parents must remain history");
    if (rsx_ordered_display_alias_unique_owner(2, 3, 0, 0) >= 0)
        return fail("no prior parent must remain history");
    if (rsx_ordered_display_alias_terminal_route(2, 1) !=
        RSX_ORDERED_DISPLAY_ALIAS_ROUTE_LIVE)
        return fail("unique live owner stays live");
    if (rsx_ordered_display_alias_terminal_route(-1, 1) !=
        RSX_ORDERED_DISPLAY_ALIAS_ROUTE_HISTORY)
        return fail("recognized display read uses history when available");
    /* A failed live snapshot clears ownership; with failed history allocation,
     * the terminal route is null, never direct/OffRT/mirror/guest fallback. */
    if (rsx_ordered_display_alias_terminal_route(-1, 0) !=
        RSX_ORDERED_DISPLAY_ALIAS_ROUTE_NULL)
        return fail("snapshot and history failure must use null SRV");

    puts("PASS: NV3089 clipped/scaled/converted blits");
    return 0;
}
