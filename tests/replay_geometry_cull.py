"""Replay captured SPU inputs without launching the game (geometry by default)."""
from pathlib import Path
import argparse
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT.parent / "ps3recomp"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("snapshot", type=Path)
    parser.add_argument("--expect-indices", type=int,
                        help="Fail if the captured job returns a different index count")
    parser.add_argument("--start-pc", type=lambda v: int(v, 0), default=0x4A50)
    parser.add_argument("--stop-pc", type=lambda v: int(v, 0), default=0x4A0)
    parser.add_argument("--watch-ls", type=lambda v: int(v, 0), default=0,
                        help="Report instruction and operands when this aligned LS quadword changes")
    parser.add_argument("--image-id", type=int, default=17,
                        help="Captured workload ID; geometry-only statistics require image 17")
    parser.add_argument("--object-gpr", type=int, default=3,
                        help="Register holding the geometry object at the chosen entry PC")
    parser.add_argument("--output", type=Path, help="Save registers and local store at the stop PC")
    parser.add_argument("--runtime-local-channels", action="store_true",
                        help="Mirror current synchronous runtime's running status/tag waits; DMA still fails closed")
    parser.add_argument("--trace-estimates", action="store_true",
                        help="Report the first eight original FI operand/result pairs and total estimate counts")
    parser.add_argument("--capture-dma-puts", type=Path,
                        help="Record PUT-only outputs in this folder; GET/atomic/list DMA still fail closed")
    args = parser.parse_args()
    if not 0 <= args.object_gpr < 128:
        parser.error("geometry object register must be between 0 and 127")
    if args.capture_dma_puts:
        if not args.runtime_local_channels:
            parser.error("PUT capture requires --runtime-local-channels")
        args.capture_dma_puts.mkdir(parents=True, exist_ok=True)
    if any(pc < 0 or pc >= 0x40000 or pc % 4 for pc in (args.start_pc, args.stop_pc)):
        parser.error("start/stop PC must be aligned addresses inside SPU local store")
    if args.watch_ls < 0 or args.watch_ls >= 0x40000 or args.watch_ls % 16:
        parser.error("watched LS quadword must be aligned and inside local store")
    source = r'''
#include "spu_interp.h"
#include "spu_helpers.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <math.h>
int g_wws_code_probe, g_wws_read_probe;
spu_lifted_fn spu_lifted_lookup(const spu_context* c,uint32_t pc) {return NULL;}
static void unsupported(const spu_context* c,const char* operation) {
    fprintf(stderr,"unsupported offline operation %s at %05X\n",operation,c->pc);
    exit(2);
}
static int runtime_local_channels, tag_mask_written, tag_update_written;
static const char* put_capture_dir;
static uint32_t put_registers[5], put_register_mask, put_count;
u128 spu_rdch(spu_context* c,uint32_t ch) {
    u128 z={0};
    /* Replay of the current runtime, NOT an independent ISA oracle:
     * spu_channels.c reports 1 for RdMachStat while running. spu_dma.h
     * completes DMA synchronously; only waits on those already-loaded inputs
     * are supported here. Any actual DMA command still stops the replay. */
    if(runtime_local_channels && ch==SPU_RdMachStat) { z._u32[0]=1; return z; }
    if(runtime_local_channels && ch==MFC_RdTagStat && tag_update_written) {
        z._u32[0]=c->mfc_tag_status; return z;
    }
    fprintf(stderr,"channel=%u ",ch); unsupported(c,"rdch"); return z;
}
void spu_wrch(spu_context* c,uint32_t ch,u128 v) {
    /* A write-only sink for recorded original instructions. Never invent
     * guest input or execute DMA in the live process. Register persistence
     * and synchronous tag completion follow the current runtime. */
    if(put_capture_dir[0] && ch>=MFC_LSA && ch<=MFC_TagID) {
        put_registers[ch-MFC_LSA]=v._u32[0];
        put_register_mask|=1u<<(ch-MFC_LSA); return;
    }
    if(put_capture_dir[0] && ch==MFC_Cmd) {
        uint32_t cmd=v._u32[0], lsa=put_registers[0], ea=put_registers[2], size=put_registers[3];
        if(put_register_mask!=31 || (cmd!=0x20 && cmd!=0x21 && cmd!=0x22) ||
           put_registers[1]!=0 || put_registers[4]>=32 || !size || size>16384 ||
           (uint64_t)lsa+size>SPU_LS_SIZE || (uint64_t)ea+size>(1ULL<<32) || put_count>=256)
            unsupported(c,"non-PUT or invalid DMA capture");
        char path[2048];
        int n=snprintf(path,sizeof(path),"%s/put_%03u_ea%08X_lsa%05X.bin",put_capture_dir,++put_count,ea,lsa);
        FILE* f=n>0 && n<sizeof(path)?fopen(path,"wb"):NULL;
        int ok=f && fwrite(c->ls+lsa,size,1,f)==1;
        if(f && fclose(f)!=0) ok=0;
        if(!ok) unsupported(c,"PUT capture write");
        printf("REPLAY_PUT cmd=%02X ea=%08X lsa=%05X size=%u path=%s\n",cmd,ea,lsa,size,path);
        return;
    }
    if(runtime_local_channels && ch==MFC_WrTagMask) {
        c->mfc_tag_mask=v._u32[0]; tag_mask_written=1; tag_update_written=0; return;
    }
    if(runtime_local_channels && ch==MFC_WrTagUpdate && tag_mask_written && v._u32[0]<=2) {
        c->mfc_tag_status=c->mfc_tag_mask; tag_update_written=1; return;
    }
    fprintf(stderr,"channel=%u value=%08X ",ch,v._u32[0]); unsupported(c,"wrch");
}
uint32_t spu_rchcnt(spu_context* c,uint32_t ch) { unsupported(c,"rchcnt");return 0; }
void spu_spurs_taskset_syscall(spu_context* c) { unsupported(c,"taskset syscall"); }
static void positions(const spu_context* c,uint32_t object,const char* label) {
    uint32_t base=spu_ls_read32(c,object+0x10);
    uint32_t count=spu_ls_read32(c,object+8)>>16;
    unsigned bad=0;
    if(base+(uint64_t)count*16>SPU_LS_SIZE) unsupported(c,"position span");
    for(uint32_t i=0;i<count;i++) {
        u128 p=spu_ls_read128(c,base+i*16); int invalid=0;
        for(unsigned k=0;k<3;k++) invalid|=!isfinite(p._f32[k]) || fabsf(p._f32[k])>1e6f;
        if(invalid && bad++<3) printf("%s bad_vertex=%u xyz=%.8g,%.8g,%.8g\n",
                                     label,i,p._f32[0],p._f32[1],p._f32[2]);
    }
    printf("%s positions=%u bad=%u base=%05X\n",label,count,bad,base);
}
int main(int argc,char** argv) {
    if(argc!=12) return 2;
    runtime_local_channels=atoi(argv[8]);
    put_capture_dir=argv[10];
    spu_context* c=calloc(1,sizeof(*c));
    FILE* f=fopen(argv[1],"rb");
    if(!c || !f || fread(c->gpr,sizeof(c->gpr),1,f)!=1 ||
       fread(c->ls,SPU_LS_SIZE,1,f)!=1 || fgetc(f)!=EOF) return 2;
    fclose(f); c->pc=(uint32_t)strtoul(argv[2],NULL,0); c->image_id=atoi(argv[6]); c->host_depth=1;
    uint32_t stop_pc=(uint32_t)strtoul(argv[3],NULL,0);
    long long expected=strtoll(argv[4],NULL,10);
    uint32_t object=c->gpr[atoi(argv[9])]._u32[0];
    uint32_t watch=(uint32_t)strtoul(argv[5],NULL,0), writes=0, bad_math=0;
    if(c->image_id==17) positions(c,object,"captured");
    uint32_t last=0, branches=0, masks=0, estimates[3]={0};
    int trace_estimates=atoi(argv[11]);
    static uint16_t visits[SPU_LS_SIZE/4];
    for(uint32_t steps=0;steps<1000000;steps++) {
        if(c->pc==stop_pc) {
            printf("REPLAY_STOP pc=%05X steps=%u r3=%u\n",c->pc,steps,c->gpr[3]._u32[0]);
            if(trace_estimates) printf("REPLAY_ESTIMATES frest=%u frsqest=%u fi=%u\n",
                                      estimates[0],estimates[1],estimates[2]);
            if(c->image_id==17) positions(c,object,"after");
            if(argv[7][0]) {
                FILE* out=fopen(argv[7],"wb");
                int ok=out && fwrite(c->gpr,sizeof(c->gpr),1,out)==1 &&
                    fwrite(c->ls,SPU_LS_SIZE,1,out)==1;
                if(out && fclose(out)!=0) ok=0;
                if(!ok) unsupported(c,"output snapshot write");
            }
            int mismatch=expected>=0 && c->gpr[3]._u32[0]!=(uint32_t)expected;
            free(c);return mismatch?1:0;
        }
        if(!steps || last) {
            if(visits[(c->pc & SPU_LS_MASK)/4]++<2 && branches++<70)
                printf("pc=%05X r3=%08X r4=%08X r69=%08X r80=%08X r81=%08X r88=%08X\n",
                    c->pc,c->gpr[3]._u32[0],c->gpr[4]._u32[0],c->gpr[69]._u32[0],
                    c->gpr[80]._u32[0],c->gpr[81]._u32[0],c->gpr[88]._u32[0]);
        }
        int branch=0;
        uint32_t pc=c->pc, insn=spu_ls_read32(c,pc);
        u128 before=spu_ls_read128(c,watch), operands[4];
        unsigned registers[4]={insn&127,(insn>>7)&127,(insn>>14)&127,(insn>>21)&127};
        if(watch || (trace_estimates && (insn>>21)==0x3d4))
            for(unsigned j=0;j<4;j++) operands[j]=c->gpr[registers[j]];
        int watch_iteration=watch && c->image_id==17 && c->gpr[86]._u32[0]==watch;
        int mask_op=c->image_id==17 && pc>=0x4DD4 && pc<0x5020 && (insn>>28)==0xD;
        if(mask_op && masks++<3) {
            unsigned regs[]={ (insn>>7)&127, (insn>>14)&127, insn&127 };
            printf("CULL_FNMS pc=%05X insn=%08X",pc,insn);
            for(unsigned j=0;j<3;j++) {
                u128 v=c->gpr[regs[j]];
                printf(" r%u=%08X,%08X,%08X,%08X",regs[j],v._u32[0],v._u32[1],v._u32[2],v._u32[3]);
            }
            puts("");
        }
        if(spu_interp_step(c,&branch)) unsupported(c,"STOP/HALT");
        unsigned op11=insn>>21, op4=insn>>28;
        if(trace_estimates) {
            if(op11==0x1b8) ++estimates[0];
            if(op11==0x1b9) ++estimates[1];
            if(op11==0x3d4 && estimates[2]++<8) {
                u128 result=c->gpr[registers[0]];
                printf("REPLAY_FI pc=%05X a=%08X,%08X,%08X,%08X b=%08X,%08X,%08X,%08X result=%08X,%08X,%08X,%08X\n",
                    pc,operands[1]._u32[0],operands[1]._u32[1],operands[1]._u32[2],operands[1]._u32[3],
                    operands[2]._u32[0],operands[2]._u32[1],operands[2]._u32[2],operands[2]._u32[3],
                    result._u32[0],result._u32[1],result._u32[2],result._u32[3]);
            }
        }
        if(watch_iteration && op11==0x1c4) {
            u128 value=c->gpr[registers[0]]; int bad=0;
            for(unsigned k=0;k<3;k++) bad|=!isfinite(value._f32[k]) || fabsf(value._f32[k])>1e6f;
            if(bad) printf("BAD_LOAD pc=%05X dst=r%u base=r%u:%08X index=r%u:%08X ls=%05X value=%08X,%08X,%08X,%08X\n",
                pc,registers[0],registers[1],operands[1]._u32[0],registers[2],operands[2]._u32[0],
                (operands[1]._u32[0]+operands[2]._u32[0])&0x3fff0,
                value._u32[0],value._u32[1],value._u32[2],value._u32[3]);
        }
        if(watch_iteration && ((op11>=0x2c4 && op11<=0x2c6) || op4>=0xd)) {
            unsigned dst=op4>=0xd?(insn>>21)&127:insn&127;
            u128 result=c->gpr[dst]; int bad=0;
            for(unsigned k=0;k<3;k++) bad|=!isfinite(result._f32[k]) || fabsf(result._f32[k])>1e6f;
            if(bad && bad_math++<8) {
                printf("BAD_MATH pc=%05X insn=%08X dst=r%u\n",pc,insn,dst);
                for(unsigned j=0;j<4;j++) printf("MATH_OPERAND r%u=%08X,%08X,%08X,%08X floats=%.8g,%.8g,%.8g,%.8g\n",
                    registers[j],operands[j]._u32[0],operands[j]._u32[1],operands[j]._u32[2],operands[j]._u32[3],
                    operands[j]._f32[0],operands[j]._f32[1],operands[j]._f32[2],operands[j]._f32[3]);
            }
        }
        u128 after=spu_ls_read128(c,watch);
        if(watch && memcmp(&before,&after,sizeof(before)) && writes++<16) {
            printf("LS_WRITE pc=%05X insn=%08X ls=%05X value=%08X,%08X,%08X,%08X\n",
                pc,insn,watch,after._u32[0],after._u32[1],after._u32[2],after._u32[3]);
            for(unsigned j=0;j<4;j++) printf("LS_WRITE_OPERAND r%u=%08X,%08X,%08X,%08X\n",
                registers[j],operands[j]._u32[0],operands[j]._u32[1],operands[j]._u32[2],operands[j]._u32[3]);
        }
        if(mask_op && masks<=3) {
            u128 v=c->gpr[(insn>>21)&127];
            printf("CULL_FNMS_RESULT %08X,%08X,%08X,%08X\n",v._u32[0],v._u32[1],v._u32[2],v._u32[3]);
        }
        last=(uint32_t)branch;
    }
    unsupported(c,"step limit");return 2;
}
'''
    clang = shutil.which("clang") or r"C:\Program Files\LLVM\bin\clang.exe"
    with tempfile.TemporaryDirectory(prefix="tj-cull-replay-") as temp:
        c = Path(temp) / "replay.c"
        c.write_text(source, encoding="utf-8")
        exe = Path(temp) / "replay.exe"
        subprocess.run([clang, "-O2", "-std=c11", "-D_CRT_SECURE_NO_WARNINGS",
                        "-I", str(RUNTIME / "include"), "-I", str(RUNTIME / "runtime/spu"),
                        str(c), str(RUNTIME / "runtime/spu/spu_interp.c"),
                        "-o", str(exe)], check=True)
        subprocess.run([str(exe), str(args.snapshot.resolve()), hex(args.start_pc), hex(args.stop_pc),
                        str(args.expect_indices if args.expect_indices is not None else -1),
                        hex(args.watch_ls), str(args.image_id),
                        str(args.output.resolve()) if args.output else "",
                        str(int(args.runtime_local_channels)), str(args.object_gpr),
                        str(args.capture_dma_puts.resolve()) if args.capture_dma_puts else "",
                        str(int(args.trace_estimates))], check=True)


if __name__ == "__main__":
    main()
