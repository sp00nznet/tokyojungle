/* Exact lifted-code regression for Tokyo Jungle's socjob13 SPU event-flag Set.
 * Runs only the title's lifted SDK routine against a 128-byte flag line; no
 * game window, assets, save data, PPU thread, or D3D12 device is involved. */

#include "spu_recomp.h"
#include "spu_helpers.h"

#include <stdint.h>
#include <stdio.h>
#include <string.h>

enum {
    JOB_EA = 0x1000,
    BIN_EA = 0x2000,
    FLAG_EA = 0x8000,
    MEM_SIZE = 0x10000,
    DESC_LS = SPU_LS_SIZE - 0x400,
    CTX_LS = DESC_LS - 0x40,
};

static uint8_t s_mem[MEM_SIZE];
uint8_t* vm_base = s_mem;
static int s_unexpected_control_flow;

typedef struct {
    uint32_t addr;
    void (*fn)(spu_context*);
} test_spu_fn;
static test_spu_fn s_functions[512];
static unsigned s_function_count;

/* Diagnostic counters referenced by inline SPU memory helpers. */
int g_wws_code_probe = 0;
int g_wws_read_probe = 0;

SPU_THREAD_LOCAL void (*g_spu_trampoline_fn)(spu_context*) = 0;
SPU_THREAD_LOCAL int g_task9a00_transfer_diagnostics = 0;

void yz_lockstep_tick(spu_context* ctx) { (void)ctx; }
void spu_task_launch_check(spu_context* ctx, void* fn)
{
    (void)ctx;
    (void)fn;
}
void spu_img_restore(spu_context* ctx, int32_t saved_img)
{
    ctx->image_id = saved_img;
}
void spu_ch_wake(spu_context* ctx) { (void)ctx; }
void (*spu_take_interrupt(spu_context* ctx,
                          void (*fn)(spu_context*)))(spu_context*)
{
    (void)ctx;
    return fn;
}

void spu_indirect_branch(spu_context* ctx)
{
    if (ctx->pc == 0) {
        g_spu_trampoline_fn = 0; /* normal job return to the jm2 manager */
        return;
    }
    for (unsigned i = 0; i < s_function_count; i++) {
        if (s_functions[i].addr == ctx->pc) {
            s_functions[i].fn(ctx);
            return;
        }
    }
    fprintf(stderr, "unexpected indirect branch to 0x%05X\n", ctx->pc);
    s_unexpected_control_flow = 1;
    g_spu_trampoline_fn = 0;
}
void spu_indirect_branch_mt(spu_context* ctx) { spu_indirect_branch(ctx); }
void spu_register_function(uint32_t addr, void (*fn)(spu_context*))
{
    if (s_function_count < sizeof(s_functions) / sizeof(s_functions[0])) {
        s_functions[s_function_count].addr = addr;
        s_functions[s_function_count].fn = fn;
        s_function_count++;
    }
}
void spu_stop(spu_context* ctx)
{
    fprintf(stderr, "unexpected SPU stop 0x%X\n", ctx->stop_code);
    s_unexpected_control_flow = 1;
    g_spu_trampoline_fn = 0;
}
void spu_halt(spu_context* ctx)
{
    (void)ctx;
    fprintf(stderr, "unexpected SPU halt\n");
    s_unexpected_control_flow = 1;
    g_spu_trampoline_fn = 0;
}

void spu_trace_init(const char* path) { (void)path; }
void spu_trace_pc(spu_context* ctx, uint32_t pc) { (void)ctx; (void)pc; }
void spu_trace_rt(spu_context* ctx, uint32_t rt) { (void)ctx; (void)rt; }

u128 spu_rdch(spu_context* ctx, uint32_t channel)
{
    if (channel == MFC_RdAtomicStat)
        return spu_splat_u32(ctx->atomic_stat);
    return spu_zero();
}

uint32_t spu_rchcnt(spu_context* ctx, uint32_t channel)
{
    (void)ctx;
    (void)channel;
    return 1;
}

void spu_wrch(spu_context* ctx, uint32_t channel, u128 value)
{
    uint32_t v = value._u32[0];
    switch (channel) {
    case MFC_LSA:   ctx->mfc_lsa = v; return;
    case MFC_EAH:   ctx->mfc_eah = v; return;
    case MFC_EAL:   ctx->mfc_eal = v; return;
    case MFC_Size:  ctx->mfc_size = v; return;
    case MFC_TagID: ctx->mfc_tag = v; return;
    case MFC_Cmd:
        break;
    default:
        return;
    }

    uint32_t ea = ctx->mfc_eal & ~(MFC_ATOMIC_LINE - 1u);
    uint32_t lsa = ctx->mfc_lsa & SPU_LS_MASK;
    if (ea + MFC_ATOMIC_LINE > MEM_SIZE ||
        lsa + MFC_ATOMIC_LINE > SPU_LS_SIZE) {
        s_unexpected_control_flow = 1;
        ctx->atomic_stat = 1;
        return;
    }

    if (v == MFC_GETLLAR_CMD) {
        memcpy(&ctx->ls[lsa], &s_mem[ea], MFC_ATOMIC_LINE);
        memcpy(ctx->resv_line, &s_mem[ea], MFC_ATOMIC_LINE);
        ctx->resv_ea = ea;
        ctx->resv_valid = 1;
        ctx->atomic_stat = 0;
    } else if (v == MFC_PUTLLC_CMD) {
        if (ctx->resv_valid && ctx->resv_ea == ea &&
            memcmp(ctx->resv_line, &s_mem[ea], MFC_ATOMIC_LINE) == 0) {
            memcpy(&s_mem[ea], &ctx->ls[lsa], MFC_ATOMIC_LINE);
            ctx->atomic_stat = 0;
        } else {
            ctx->atomic_stat = 1;
        }
        ctx->resv_valid = 0;
    } else {
        fprintf(stderr, "unexpected MFC command 0x%X\n", v);
        s_unexpected_control_flow = 1;
    }
}

static void print_ctrl(const char* label)
{
    printf("%s=%02X%02X%02X%02X%02X%02X%02X%02X\n", label,
           s_mem[FLAG_EA + 0], s_mem[FLAG_EA + 1],
           s_mem[FLAG_EA + 2], s_mem[FLAG_EA + 3],
           s_mem[FLAG_EA + 4], s_mem[FLAG_EA + 5],
           s_mem[FLAG_EA + 6], s_mem[FLAG_EA + 7]);
}

static void put_be16(uint8_t* p, uint16_t v)
{
    p[0] = (uint8_t)(v >> 8);
    p[1] = (uint8_t)v;
}

static void put_be32(uint8_t* p, uint32_t v)
{
    p[0] = (uint8_t)(v >> 24);
    p[1] = (uint8_t)(v >> 16);
    p[2] = (uint8_t)(v >> 8);
    p[3] = (uint8_t)v;
}

static void init_waiting_flag(void)
{
    memset(s_mem + FLAG_EA, 0, MFC_ATOMIC_LINE);
    /* PPU waiter: mask=0x0001, slot=0, AND mode, not yet pending. */
    s_mem[FLAG_EA + 5] = 0x01;
    s_mem[FLAG_EA + 6] = 0x01;
    s_mem[FLAG_EA + 14] = 1; /* SPU2PPU */
    s_mem[FLAG_EA + 15] = 0; /* AUTO clear */
}

static int flag_was_delivered(void)
{
    return s_mem[FLAG_EA + 4] == 0 && s_mem[FLAG_EA + 5] == 0 &&
           s_mem[FLAG_EA + 7] == 1 &&
           s_mem[FLAG_EA + 0x30] == 0 && s_mem[FLAG_EA + 0x31] == 1;
}

static int stage_job(spu_context* ctx)
{
    static const char* image_path =
        "generated/spu_images/spujob_78FEA84E1C23BB5C_6352.bin";
    enum { IMAGE_SIZE = 6352, STACK_TOP = 0x3C00 };

    FILE* f = fopen(image_path, "rb");
    if (!f) {
        fprintf(stderr, "cannot open %s\n", image_path);
        return 0;
    }
    size_t got = fread(s_mem + BIN_EA, 1, IMAGE_SIZE, f);
    fclose(f);
    if (got != IMAGE_SIZE) {
        fprintf(stderr, "short socjob13 image: %zu/%u\n", got, IMAGE_SIZE);
        return 0;
    }

    memset(s_mem + JOB_EA, 0, 0x100);
    put_be32(s_mem + JOB_EA + 0x00, 0);
    put_be32(s_mem + JOB_EA + 0x04, BIN_EA);
    put_be16(s_mem + JOB_EA + 0x08, IMAGE_SIZE / 16);
    /* SPU effective address is a big-endian u64: high word, then low word. */
    put_be32(s_mem + JOB_EA + 0x30, 0);
    put_be32(s_mem + JOB_EA + 0x34, FLAG_EA);
    put_be16(s_mem + JOB_EA + 0x3E, 0x0001);

    spu_context_init(ctx, 0);
    ctx->image_id = 13;
    memcpy(ctx->ls, s_mem + BIN_EA, IMAGE_SIZE);
    memcpy(ctx->ls + DESC_LS, s_mem + JOB_EA, 0x100);

    /* Exact CellSpursJobContext2 fields produced by spu_run_spurs_job. */
    put_be32(ctx->ls + CTX_LS + 0x14, 2u << 28); /* 256-byte descriptor */
    put_be32(ctx->ls + CTX_LS + 0x24, 20);       /* jm2 DMA tag */
    put_be32(ctx->ls + CTX_LS + 0x28, 0);
    put_be32(ctx->ls + CTX_LS + 0x2C, JOB_EA);
    ctx->gpr[1]._u32[0] = STACK_TOP - 16;
    ctx->gpr[3]._u32[0] = CTX_LS;
    ctx->gpr[4]._u32[0] = DESC_LS;

    s_function_count = 0;
    tj_socjob13spu_recomp_register();
    return 1;
}

static int run_job_entry(void (*entry)(spu_context*))
{
    spu_context ctx;
    if (!stage_job(&ctx)) return 0;
    g_spu_trampoline_fn = 0;
    entry(&ctx);
    SPU_DRAIN(&ctx);
    return !s_unexpected_control_flow && flag_was_delivered();
}

int main(void)
{
    memset(s_mem, 0, sizeof(s_mem));
    init_waiting_flag();
    print_ctrl("before");

    spu_context ctx;
    spu_context_init(&ctx, 0);
    ctx.image_id = 13;
    ctx.host_depth = 1;
    ctx.gpr[1] = spu_splat_u32(0x3F00);
    /* SPU ABI arguments: r3=event-flag EA, r4=bits to set. */
    ctx.gpr[3] = spu_zero();
    ctx.gpr[3]._u32[1] = FLAG_EA;
    ctx.gpr[4] = spu_zero();
    ctx.gpr[4]._u32[0] = 0x0001;

    tj_socjob13spu_func_000001B0(&ctx);
    SPU_DRAIN(&ctx);
    print_ctrl("after ");

    int mask_cleared = s_mem[FLAG_EA + 4] == 0 &&
                       s_mem[FLAG_EA + 5] == 0;
    int pending_set = s_mem[FLAG_EA + 7] == 1;
    int received_set = s_mem[FLAG_EA + 0x30] == 0 &&
                       s_mem[FLAG_EA + 0x31] == 1;
    int primitive_ok = !s_unexpected_control_flow && mask_cleared && pending_set &&
                       received_set;

    printf("mask_cleared=%d pending_set=%d received=0x%02X%02X\n",
           mask_cleared, pending_set,
           s_mem[FLAG_EA + 0x30], s_mem[FLAG_EA + 0x31]);
    printf("%s: lifted socjob13 SPU EventFlag Set primitive\n",
           primitive_ok ? "PASS" : "FAIL");

    s_unexpected_control_flow = 0;
    init_waiting_flag();
    int descriptor_ok = run_job_entry(tj_socjob13spu_func_00000180);
    print_ctrl("desc  ");
    printf("%s: descriptor extraction -> EventFlag delivery\n",
           descriptor_ok ? "PASS" : "FAIL");

    s_unexpected_control_flow = 0;
    init_waiting_flag();
    int main_ok = run_job_entry(tj_socjob13spu_func_000000E0);
    print_ctrl("main  ");
    printf("%s: CRT main entry -> EventFlag delivery\n",
           main_ok ? "PASS" : "FAIL");

    s_unexpected_control_flow = 0;
    init_waiting_flag();
    int full_job_ok = run_job_entry(tj_socjob13spu_func_00000000);
    print_ctrl("job   ");
    printf("%s: full socjob13 jm2 ABI -> EventFlag delivery\n",
           full_job_ok ? "PASS" : "FAIL");
    return primitive_ok && descriptor_ok && main_ok && full_job_ok ? 0 : 1;
}
