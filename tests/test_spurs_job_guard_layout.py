"""Exercise the real SPURS staging owner against the game's original guard CRT."""
from pathlib import Path
import hashlib
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT.parent / "ps3recomp"
IMAGE = ROOT / "generated/spu_images/spujob_D8945A0BA2D3D054_40896.bin"


def main():
    source = r'''
#include "spu_workload.h"
#include "spu_interp.h"
#include "spu_helpers.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
static unsigned char memory[0x50000];
uint8_t* vm_base=memory;
int g_wws_code_probe,g_wws_read_probe;
static unsigned failures, tests, mode, entered;
static uint32_t io_size,out_size,scratch_size,cache_size;
static void check(int ok,const char* what) {
    tests++; if(!ok) { failures++; printf("FAIL mode=%u %s\n",mode,what); }
}
void tj_spu_pc_hist_job_complete(int i) {(void)i;}
void tj_spu_pc_time_job_begin(int i) {(void)i;}
void tj_spu_pc_time_job_complete(int i) {(void)i;}
spu_lifted_fn spu_lifted_lookup(const spu_context* c,uint32_t pc) {return NULL;}
u128 spu_rdch(spu_context* c,uint32_t ch) {fprintf(stderr,"unexpected rdch %u\n",ch);exit(2);}
void spu_wrch(spu_context* c,uint32_t ch,u128 v) {fprintf(stderr,"unexpected wrch %u\n",ch);exit(2);}
uint32_t spu_rchcnt(spu_context* c,uint32_t ch) {exit(2);}
void spu_spurs_taskset_syscall(spu_context* c) {exit(2);}
int spu_run_with_halt(void (*entry)(spu_context*),spu_context* c) {entered++;entry(c);return 0;}
static void be32(unsigned char* p,uint32_t v) {p[0]=v>>24;p[1]=v>>16;p[2]=v>>8;p[3]=v;}
static void be16(unsigned char* p,uint16_t v) {p[0]=v>>8;p[1]=v;}
static uint32_t rd(const spu_context* c,uint32_t a) {return spu_ls_read32(c,a);}
static void entry(spu_context* c) {
    uint32_t context=c->gpr[3]._u32[0],desc=c->gpr[4]._u32[0];
    uint32_t io=rd(c,context),cache=rd(c,context+4),out=rd(c,context+0x1c);
    unsigned char descriptor[256];memcpy(descriptor,memory+0x10000,sizeof(descriptor));
    check(!io_size || !memcmp(c->ls+io,memory+0x20000,io_size),"IO correct before CRT");
    check(!cache_size || !memcmp(c->ls+cache,memory+0x22000,cache_size),"cache correct before CRT");
    /* Original guard setup, not a rewrite of its arithmetic. r16/r17/r79
     * are descriptor/context/zero relocation delta at this CRT boundary.
     * Stop before subsequent startup work; no channels or fake DMA needed. */
    c->gpr[16]._u32[0]=desc;c->gpr[17]._u32[0]=context;c->gpr[79]=spu_il(0);
    c->pc=0x92e8;c->image_id=16;c->host_depth=1;
    for(unsigned steps=0;c->pc!=0x950c && steps<10000;steps++) {
        int branch=0;
        if(spu_interp_step(c,&branch)) {check(0,"CRT unexpectedly stopped");return;}
    }
    check(c->pc==0x950c,"CRT guard setup completed");
    check(!memcmp(descriptor,memory+0x10000,sizeof(descriptor)),"guest descriptor unchanged");
    check(rd(c,desc+0x14)==io_size,"CRT exposes original IO size");
    check(rd(c,desc+0x18)==out_size,"CRT exposes original output size");
    check((rd(c,desc+0x1c)&0xffffu)*16u==scratch_size,"CRT exposes original scratch size");
    check(rd(c,desc+0x40)==cache_size,"CRT exposes original cache size");
    check(!io_size || !memcmp(c->ls+io,memory+0x20000,io_size),"CRT preserves IO payload and final bone row");
    check(!cache_size || !memcmp(c->ls+cache,memory+0x22000,cache_size),"CRT preserves cache payload");
    if(mode&2) {
        check(!io_size || rd(c,io+io_size)==0xdeadbeef,"IO guard follows payload");
        check(cache>=0xa000 && cache+cache_size+16<=SPU_LS_SIZE,
            "declared cache has guard storage even for zero bytes");
        check(rd(c,cache+cache_size)==0xdeadbeef,"cache guard follows payload");
        /* CRT's slot7 deliberately guards the relocated binary header at
         * LS0 in this loader; its executable job body must stay intact. */
        check(!memcmp(c->ls+0x180,memory+0x1180,0x100),"cache guard cannot overwrite job code");
        check(!out_size || rd(c,out+out_size)==0xdeadbeef,"output guard follows payload");
        uint32_t guard=rd(c,0x9db8),scratch=rd(c,context+0x20);
        check((!scratch_size || guard>=scratch+scratch_size) &&
            guard+16<=c->gpr[1]._u32[0] && rd(c,guard)==0xdeadbeef,
            "scratch/stack guard lies outside payload");
        check(c->gpr[1]._u32[0]-(guard+16)>=8192-16,
            "guard preserves requested stack capacity");
    }
}
int main(int argc,char** argv) {
    if(argc!=2)return 2;
    FILE* f=fopen(argv[1],"rb");
    if(!f || fread(memory+0x1000,40896,1,f)!=1 || fgetc(f)!=EOF)return 2;
    fclose(f);
    for(unsigned i=0;i<0x1800;i++)memory[0x20000+i]=(unsigned char)(i*37+11);
    for(unsigned i=0;i<0x800;i++)memory[0x22000+i]=(unsigned char)(i*71+3);
    const uint32_t sizes[][4]={{0x820,0x840,0x400,0x30},{0x400,0x400,0x400,0x400},{0,0,0,0}};
    for(mode=0;mode<=2;mode+=2)for(unsigned row=0;row<3;row++) {
        io_size=sizes[row][0];out_size=sizes[row][1];scratch_size=sizes[row][2];cache_size=sizes[row][3];
        unsigned char* d=memory+0x10000;memset(d,0,256);
        be32(d+4,0x1000);be16(d+8,40896/16);be16(d+0xa,16);
        be32(d+0x14,io_size);be32(d+0x18,out_size);be16(d+0x1e,scratch_size/16);
        be32(d+0x24,8);d[0x2c]=(unsigned char)mode;
        /* Zero-sized list entry retained; second entry contains the payload. */
        be32(d+0x30,0);be32(d+0x34,0x20000);
        be32(d+0x38,io_size);be32(d+0x3c,0x20000);
        be32(d+0x40,cache_size);be32(d+0x44,0x22000);
        check(spu_run_spurs_job(entry,16,0x10000,256,0)==0,"staging accepted valid job");
    }
    check(entered==6,"all six guarded and unguarded cases executed");
    printf("SPURS_GUARD_LAYOUT checks=%u failures=%u\n",tests,failures);
    return failures?1:0;
}
'''
    image = IMAGE.read_bytes()
    if len(image) != 40896 or hashlib.sha256(image[0x92E8:0x950C]).hexdigest() != "bd819a7755b7459f78910f45c825fe755c585a59be30273239a3a61bc50f3eaf":
        raise RuntimeError("Animation image size changed")
    print("Original animation CRT SHA256:", hashlib.sha256(image[0x92E8:0x950C]).hexdigest(), flush=True)
    clang = shutil.which("clang") or r"C:\Program Files\LLVM\bin\clang.exe"
    with tempfile.TemporaryDirectory(prefix="tj-spurs-guard-") as tmp:
        path = Path(tmp)
        c, exe = path / "test.c", path / "test.exe"
        c.write_text(source)
        subprocess.run([clang, "-O2", "-std=c11", "-D_CRT_SECURE_NO_WARNINGS",
                        "-I", str(RUNTIME / "include"), "-I", str(RUNTIME / "runtime/spu"),
                        str(c), str(RUNTIME / "runtime/spu/spurs_job.c"),
                        str(RUNTIME / "runtime/spu/spu_interp.c"), "-o", str(exe)], check=True)
        subprocess.run([str(exe), str(IMAGE)], check=True)


if __name__ == "__main__":
    main()
