"""Exercise the production RSX context reset without a GPU or game launch.

Contract: RPCS3 9b5cc7cda7821b3819a1f6d1b8fedb974bebf5fa,
rpcs3/Emu/RSX/rsx_methods.cpp, rsx_state::reset (not init).
"""
from pathlib import Path
import shutil
import subprocess
import tempfile

from test_fifo_present_boundary import function

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT.parent / "ps3recomp"
VIDEO = RUNTIME / "libs/video"


def main():
    commands = VIDEO / "rsx_commands.c"
    # A missing reset is a behavioral baseline, not a compile-error baseline.
    missing = "void rsx_state_reset(" not in commands.read_text(encoding="utf-8")
    gcm = (VIDEO / "cellGcmSys.c").read_text(encoding="utf-8")
    objects_start = gcm.index("static struct {\n    u32 src_dma;")
    objects_end = gcm.index("static GcmFifoObjectClass s_gcm_fifo_object_class[8];", objects_start)
    object_state = gcm[objects_start:objects_end]
    source = r'''
#include "rsx_commands.h"
#include <stdio.h>
#include <string.h>
static unsigned guest_writes;
void vm_write32(uint32_t ea,uint32_t value) { (void)ea; (void)value; ++guest_writes; }
void vm_write16(uint32_t ea,uint16_t value) { (void)ea; (void)value; ++guest_writes; }
u32 ppu_vm_size=0;
RESET_DECL
OBJECT_STATE
static GcmFifoObjectClass s_gcm_fifo_object_class[8];
static u32 s_gcm_fifo_object_handle[8],s_fifo_semaphore_offset,s_fifo_semaphore_dma;
static u8 s_gcm_fifo_object_trusted[8];
static u64 s_fifo_semaphore_wait_since_ns;
static int cellGcmResolveLocatedChecked(int local,u32 offset,u32* address) {
    (void)local; (void)offset; (void)address; return 0;
}
static int gcm_m2mf_buffer_notify(u32 data) { (void)data; return -1; }
static int gcm_scaled_image_in(u32 data) { (void)data; return -1; }
OBJECT_CLASS
OBJECT_BIND
CONTEXT_RESET
SUBCHANNEL
static rsx_state state, before;
static unsigned checks,failures,draws;
static u32 drawn_mask,drawn_first,drawn_count;
#define CHECK(c,label) do { ++checks; if (!(c)) { ++failures; fprintf(stderr,"FAIL: %s\n",label); } } while(0)
#define SAME(field) CHECK(!memcmp(&state.field,&before.field,sizeof(state.field)),"preserve " #field)
static void draw(void* ud,u32 primitive,u32 first,u32 count) {
    (void)ud; (void)primitive; ++draws;
    drawn_mask=state.depth_mask; drawn_first=first; drawn_count=count;
}
int main(void) {
    rsx_backend backend={0}; backend.draw_indexed=draw; rsx_set_backend(&backend);
    memset(&state,0x5a,sizeof(state));
    state.depth_mask=0;
    state.pending_indexed_valid=1;
    state.pending_indexed_primitive=RSX_PRIMITIVE_TRIANGLES;
    state.pending_indexed_first=11; state.pending_indexed_count=6;
    before=state;
    rsx_state_reset(&state);
    CHECK(draws==1 && drawn_mask==0 && drawn_first==11 && drawn_count==6 &&
          !state.pending_indexed_valid,"deferred draw must keep pre-reset state");
    CHECK(state.surface_format==0x121 && state.color_target==1,"surface defaults");
    CHECK(!state.surface_clip_x && !state.surface_clip_y && !state.surface_clip_w &&
          !state.surface_clip_h && !state.window_offset_x && !state.window_offset_y,"surface rectangle defaults");
    CHECK(!state.surface_zeta_offset && state.surface_zeta_pitch==64 &&
          state.surface_zeta_dma==RSX_CONTEXT_DMA_LOCAL,"depth attachment defaults");
    for(unsigned i=0;i<4;++i)
        CHECK(!state.surface_color_offset[i] && state.surface_color_pitch[i]==64 &&
              state.surface_color_dma[i]==RSX_CONTEXT_DMA_LOCAL,"color attachment defaults");
    CHECK(!state.viewport_x && !state.viewport_y && state.viewport_w==4096 && state.viewport_h==4096,
          "context viewport is 4096 square, not host window size");
    const float viewport[4]={2048,2048,0.5f,0};
    CHECK(!memcmp(state.viewport_offset,viewport,sizeof(viewport)) &&
          !memcmp(state.viewport_scale,viewport,sizeof(viewport)),"viewport transform defaults");
    CHECK(state.clip_min==0 && state.clip_max==1 && !state.scissor_x && !state.scissor_y &&
          state.scissor_w==4096 && state.scissor_h==4096,"clip/scissor defaults");
    CHECK(state.fog_mode==0x800 && state.fog_param[0]==0 && state.fog_param[1]==0,"fog defaults");
    CHECK(!state.color_clear_value && state.zstencil_clear_value==0xffffff00u,"clear values only, no clear operation");
    CHECK(!state.depth_test_enable && state.depth_mask==1 && state.depth_func==0x201,"depth reset cohort");
    CHECK(!state.depth_bounds_test_enable && state.depth_bounds_min==0 && state.depth_bounds_max==1,
          "depth bounds defaults");
    CHECK(!state.poly_offset_point_enable && !state.poly_offset_line_enable && !state.poly_offset_fill_enable &&
          state.polygon_offset_scale_factor==0 && state.polygon_offset_bias==0 && state.zmin_max_control==1,
          "polygon/depth clipping defaults");
    CHECK(!state.blend_enable && !state.blend_enable_mrt && state.blend_sfactor==0x10001 &&
          !state.blend_dfactor && state.blend_equation==0x80068006,"blend defaults");
    CHECK(!state.blend_color && !state.blend_color2 && !state.logic_op_enable && state.logic_op==0x1503,
          "blend constant/logic defaults");
    CHECK(!state.stencil_test_enable && !state.two_sided_stencil_test_enable,"stencil disabled");
    CHECK(state.stencil_write_mask==255 && state.stencil_mask==255 && state.stencil_func==0x207 &&
          !state.stencil_ref && state.stencil_op_fail==0x1e00 && state.stencil_op_zfail==0x1e00 &&
          state.stencil_op_zpass==0x1e00,"front stencil defaults");
    CHECK(state.back_stencil_write_mask==255 && state.back_stencil_mask==255 && state.back_stencil_func==0x207 &&
          !state.back_stencil_ref && state.back_stencil_op_fail==0x1e00 && state.back_stencil_op_zfail==0x1e00 &&
          state.back_stencil_op_zpass==0x1e00,"back stencil defaults");
    CHECK(!state.cull_face_enable && state.cull_face==0x405 && state.front_face==0x901 &&
          state.front_polygon_mode==0x1b02 && state.back_polygon_mode==0x1b02 && state.shade_mode==0x1d01,
          "raster defaults");
    CHECK(state.color_mask==0x1010101 && !state.color_mask_mrt && !state.alpha_test_enable &&
          state.alpha_func==0x207 && !state.alpha_ref,"color/alpha defaults");
    CHECK(!state.restart_index_enable && state.restart_index==0xffffffffu &&
          !state.vertex_data_base_offset && !state.vertex_data_base_index && !state.frequency_divider_op,
          "vertex fetch defaults");
    for(unsigned i=0;i<16;++i) {
        const rsx_vertex_attrib* a=&state.vertex_attribs[i];
        CHECK(a->format==2 && a->type==2 && !a->size && !a->stride && !a->frequency && !a->enabled && !a->offset,
              "disabled float vertex array defaults");
    }
    for(unsigned bank=0;bank<2;++bank) for(unsigned i=0;i<(bank?4u:16u);++i) {
        const rsx_texture_state* t=bank?&state.vertex_textures[i]:&state.textures[i];
        const rsx_texture_state* b=bank?&before.vertex_textures[i]:&before.textures[i];
        CHECK(t->address==(bank?0x101u:0x30101u) && t->control0==0x60000 &&
              t->filter==(bank?0u:0x2052000u) && !t->border_color && t->dirty==1,
              "texture sampling state reset");
        CHECK(t->offset==b->offset && t->format==b->format && t->control1==b->control1 &&
              t->image_rect==b->image_rect && t->pitch==b->pitch && t->depth==b->depth,
              "texture storage and remap survive context reset");
    }
    CHECK(!state.shader_packer && state.shader_window==0x1000 && !state.texcoord_control_mask &&
          !state.two_side_light_enable && state.vertex_attrib_input_mask==0xffff &&
          state.vertex_attrib_output_mask==0x3fffff && !state.user_clip_plane_control &&
          !state.transform_branch_bits,"shader interface defaults");
    CHECK(state.surface_dirty==1 && state.viewport_dirty==1 && state.blend_dirty==1 &&
          state.depth_dirty==1 && state.stencil_dirty==1 && state.texture_dirty==1 &&
          state.vertex_texture_dirty==1 && state.vertex_dirty==1 && state.color_mask_dirty==1 &&
          state.alpha_dirty==1 && state.shader_dirty==1,"reset invalidates changed backend state");
    SAME(shader_program); SAME(shader_control); SAME(fragment_program_addr);
    SAME(transform_program_load); SAME(transform_program_start); SAME(transform_constant_load);
    SAME(vertex_constants); SAME(vertex_constants_lo); SAME(vertex_constants_hi); SAME(vertex_constants_dirty);
    SAME(vertex_data4f); SAME(vp_ucode); SAME(vp_ucode_write); SAME(vp_ucode_bytes); SAME(vp_dirty);
    SAME(index_array_offset); SAME(index_array_dma); SAME(tconst_probe_enabled);
    SAME(tconst_probe_method_sequence); SAME(tconst_probe_last_sequence); SAME(tconst_probe_last_raw);
    SAME(tconst_probe_last_load); SAME(tconst_probe_last_slot); SAME(tconst_probe_last_lane);
    rsx_state_reset(&state);
    CHECK(draws==1,"repeat reset cannot duplicate submitted draw");
    rsx_process_method(&state,NV4097_SET_DEPTH_MASK,0);
    rsx_process_method(&state,NV4097_SET_DEPTH_TEST_ENABLE,1);
    CHECK(!state.depth_mask && state.depth_test_enable,"guest can disable depth writes after reset");
    memset(&s_gcm2d,0x5a,sizeof(s_gcm2d));
    gcm_fifo_bind_object(2,0x31337000u);
    gcm_fifo_bind_object(3,0x313371c3u);
    gcm_fifo_bind_object(6,0x3137af00u);
    cellGcmFifoSubchannelMethod(6,0x184,RSX_CONTEXT_DMA_MAIN);
    cellGcmFifoSubchannelMethod(3,0x184,RSX_CONTEXT_DMA_LOCAL);
    CHECK(s_gcm2d.scaled.src_dma==RSX_CONTEXT_DMA_MAIN && s_gcm2d.src_dma==RSX_CONTEXT_DMA_LOCAL,
          "NV3062 and NV3089 source DMA have independent ownership");
    s_gcm2d.dst_dma=RSX_CONTEXT_DMA_MAIN;
    s_gcm2d.m2mf.dma_in=RSX_CONTEXT_DMA_LOCAL;
    s_gcm2d.m2mf.dma_out=RSX_CONTEXT_DMA_MAIN;
    s_gcm2d.swizzled.dma=RSX_CONTEXT_DMA_MAIN;
    s_fifo_semaphore_dma=0x66606660u; s_fifo_semaphore_offset=0x40;
    s_fifo_semaphore_wait_since_ns=123;
    unsigned char scaled_before[sizeof(s_gcm2d.scaled)];
    memcpy(scaled_before,&s_gcm2d.scaled,sizeof(scaled_before));
    gcm_fifo_context_reset(&state);
    CHECK(!state.depth_test_enable && state.depth_mask==1 && state.depth_func==0x201,
          "actual FIFO context reset reaches actual RSX state");
    CHECK(s_gcm_fifo_object_class[0]==GCM_FIFO_OBJECT_NV4097 &&
          s_gcm_fifo_object_class[1]==GCM_FIFO_OBJECT_NV0039 &&
          s_gcm_fifo_object_class[3]==GCM_FIFO_OBJECT_NV3062 &&
          s_gcm_fifo_object_class[4]==GCM_FIFO_OBJECT_NV309E &&
          s_gcm_fifo_object_class[5]==GCM_FIFO_OBJECT_NV308A &&
          s_gcm_fifo_object_class[6]==GCM_FIFO_OBJECT_NV3089 &&
          s_gcm_fifo_object_class[7]==GCM_FIFO_OBJECT_DRIVER,"driver object bindings restored");
    CHECK(s_gcm_fifo_object_trusted[0] && s_gcm_fifo_object_trusted[1] &&
          s_gcm_fifo_object_trusted[3] && s_gcm_fifo_object_trusted[4] &&
          s_gcm_fifo_object_trusted[5] && s_gcm_fifo_object_trusted[6] &&
          s_gcm_fifo_object_trusted[7],"context reset restores trust for its known bindings");
    CHECK(s_gcm_fifo_object_class[2]==GCM_FIFO_OBJECT_NV4097 &&
          s_gcm_fifo_object_handle[2]==0x31337000u,"unmentioned subchannel binding preserved");
    CHECK(s_fifo_semaphore_dma==0x66616661u && s_fifo_semaphore_offset==0x10 &&
          !s_fifo_semaphore_wait_since_ns && !guest_writes,"frontend reset does not manufacture label completion");
    CHECK(s_gcm2d.m2mf.dma_in==RSX_CONTEXT_DMA_MAIN && s_gcm2d.m2mf.dma_out==RSX_CONTEXT_DMA_LOCAL &&
          s_gcm2d.src_dma==RSX_CONTEXT_DMA_LOCAL && s_gcm2d.dst_dma==RSX_CONTEXT_DMA_LOCAL &&
          s_gcm2d.swizzled.dma==RSX_CONTEXT_DMA_LOCAL,"copy-engine DMA defaults restored");
    CHECK(!memcmp(scaled_before,&s_gcm2d.scaled,sizeof(scaled_before)) &&
          s_gcm2d.m2mf.format==0x5a5a5a5a && s_gcm2d.m2mf.offset_in==0x5a5a5a5a &&
          s_gcm2d.pitch==0x5a5a5a5a && s_gcm2d.dst_offset==0x5a5a5a5a &&
          s_gcm2d.swizzled.format==0x5a5a5a5a && s_gcm2d.swizzled.offset==0x5a5a5a5a,
          "copy-engine storage, geometry and NV3089 state preserved");
    gcm_fifo_bind_object(2,0);
    CHECK(!s_gcm_fifo_object_trusted[2],"unknown preserved-subchannel bind clears trust");
    gcm_fifo_context_reset(&state);
    CHECK(!s_gcm_fifo_object_trusted[2] && s_gcm_fifo_object_handle[2]==0,
          "context reset preserves an unmentioned subchannel binding and trust");
    printf("RSX context reset: %u checks, %u failures\n",checks,failures);
    return failures!=0;
}
'''.replace("RESET_DECL", "void rsx_state_reset(rsx_state* state) { (void)state; }" if missing else "")
    replacements = {
        "OBJECT_STATE": object_state,
        "OBJECT_CLASS": function(gcm, "static GcmFifoObjectClass gcm_fifo_object_class_from_handle("),
        "OBJECT_BIND": function(gcm, "static void gcm_fifo_bind_object("),
        "CONTEXT_RESET": function(gcm, "static void gcm_fifo_context_reset("),
        "SUBCHANNEL": function(gcm, "int cellGcmFifoSubchannelMethod("),
    }
    for marker, value in replacements.items():
        source = source.replace("\n" + marker + "\n", "\n" + value + "\n")
    compiler = shutil.which("clang") or r"C:\Program Files\LLVM\bin\clang.exe"
    with tempfile.TemporaryDirectory(prefix="tj-context-reset-") as directory:
        path = Path(directory)
        test = path / "reset.c"
        exe = path / "reset.exe"
        test.write_text(source, encoding="utf-8")
        subprocess.run([compiler, "-std=c11", "-D_CRT_SECURE_NO_WARNINGS", "-I", str(VIDEO),
                        "-I", str(RUNTIME / "include"), str(test), str(commands), "-o", str(exe)], check=True)
        return subprocess.run([str(exe)], timeout=10).returncode


if __name__ == "__main__":
    raise SystemExit(main())
