"""Exercise the real job loader, original SPU-ID accessor and allocator lookup."""
from pathlib import Path
import hashlib
import re
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT.parent / "ps3recomp"
IMAGE = ROOT / "generated/spu_images/spujob_DD6F7BFDDFCF7894_48032.bin"


def main():
    lifted = (ROOT / "diagnostics/spu_tail/geometryjob17_spu_recomp.c").read_text()
    accessor = re.search(
        r"^void tj_geometryjob17spu_func_0000A200\(spu_context\* ctx\) \{\n.*?^}\n",
        lifted, re.M | re.S).group()
    source = r'''
#include "spu_workload.h"
#include "spu_interp.h"
#include "spu_helpers.h"
#include <windows.h>
#include <stdio.h>
#include <stdlib.h>
static unsigned char memory[0x80000];
uint8_t* vm_base=memory;
static volatile LONG checks,failures,entered;
static HANDLE two_entered,third_started,third_entered,release_entries;
static SRWLOCK entry_lock=SRWLOCK_INIT;
static unsigned active_mask;
static __declspec(thread) unsigned actor,concurrent,dma_ea,dma_lsa,dma_command;
SPU_THREAD_LOCAL void (*g_spu_trampoline_fn)(spu_context*);
static void check(int ok,const char* what) {
    InterlockedIncrement(&checks);
    if(!ok) { InterlockedIncrement(&failures);printf("FAIL %s\n",what); }
}
void tj_spu_pc_hist_job_complete(int i) {(void)i;}
void tj_spu_pc_time_job_begin(int i) {(void)i;}
void tj_spu_pc_time_job_complete(int i) {(void)i;}
spu_lifted_fn spu_lifted_lookup(const spu_context* c,uint32_t pc) {return NULL;}
void spu_indirect_branch(spu_context* c) {fprintf(stderr,"unexpected branch %X\n",c->pc);exit(2);}
void spu_spurs_taskset_syscall(spu_context* c) {exit(2);}
u128 spu_rdch(spu_context* c,uint32_t ch) {
    if(ch==SPU_RdMachStat || ch==MFC_RdAtomicStat)return spu_il(0);
    fprintf(stderr,"unexpected rdch %u\n",ch);exit(2);
}
void spu_wrch(spu_context* c,uint32_t ch,u128 v) {
    if(ch==MFC_LSA)dma_lsa=v._u32[0];
    else if(ch==MFC_EAL)dma_ea=v._u32[0];
    else if(ch==MFC_Cmd) {
        dma_command=v._u32[0];
        if(dma_command!=MFC_GETLLAR_CMD || dma_ea>sizeof(memory)-128 || dma_lsa>SPU_LS_SIZE-128)exit(2);
        memcpy(c->ls+dma_lsa,memory+dma_ea,128);
    } else if(ch!=MFC_EAH && ch!=MFC_Size && ch!=MFC_TagID)exit(2);
}
uint32_t spu_rchcnt(spu_context* c,uint32_t ch) {exit(2);}
int spu_run_with_halt(void (*entry)(spu_context*),spu_context* c) {entry(c);return 0;}
static void be32(unsigned char* p,uint32_t v) {p[0]=v>>24;p[1]=v>>16;p[2]=v>>8;p[3]=v;}
static void be16(unsigned char* p,uint16_t v) {p[0]=v>>8;p[1]=v;}
''' + accessor + r'''
static void entry(spu_context* c) {
    unsigned char code[16];memcpy(code,c->ls+0x1c0,16);
    c->host_depth=1;c->gpr[0]=spu_il(0x4c0);
    tj_geometryjob17spu_func_0000A200(c);
    unsigned id=c->gpr[3]._u32[0],nspus=memory[0x40076];
    check(id<nspus,"original accessor returns an in-range SPU number");
    check(id==c->spu_id,"kernel identity matches executing context");
    u128 kernel=spu_ls_read128(c,0x1c0);
    check(kernel._u32[0]==0 && kernel._u32[1]==0x40000,"kernel owner is the actual CellSpurs instance");
    check(kernel._u32[2]==id && kernel._u32[3]==8,"kernel SPU number and DMA tag have ABI lanes");
    check(!memcmp(code,c->ls+0x1c0,16),"job code remains intact under kernel data view");
    check(spu_ls_read32(c,0x1c8)==0x20000183,"instruction fetch still reads original job opcode");
    c->pc=0xa200;
    for(unsigned i=0;i<2;i++){int branch=0;check(!spu_interp_step(c,&branch),"original accessor instruction executes");}
    check(c->gpr[3]._u32[0]==id,"interpreter and lifted accessor agree");
    if(!concurrent) {
        /* Original allocator prologue selects the per-SPU record before its
         * first GETLLAR. This fixture stops there; it invents no GPU release. */
        c->gpr[3]=spu_ila(0x30000);c->gpr[4]=spu_splat_u32(0x50000);
        c->gpr[5]=spu_il(1024);c->gpr[6]=spu_ila(0x34000);
        c->gpr[7]=spu_il(id);c->gpr[8]=spu_il(0);
        c->gpr[80]=c->gpr[6];c->gpr[81]=c->gpr[4];c->gpr[85]=c->gpr[3];
        c->gpr[13]=spu_ila(0x1d48);c->gpr[126]=c->gpr[13];c->pc=0x1d48;
        dma_command=0;
        for(unsigned i=0;!dma_command && i<160;i++) {int branch=0;if(spu_interp_step(c,&branch))break;}
        printf("SPURS_KERNEL allocator spu=%u first_atomic_ea=%08X expected=%08X pc=%05X\n",
               id,dma_ea,0x50080+id*128,c->pc);
        check(dma_command==MFC_GETLLAR_CMD,"original allocator issues GETLLAR");
        check(dma_ea==0x50080+id*128 && id<nspus,"allocator selects a real worker record, not opcode-indexed memory");
        return;
    }
    AcquireSRWLockExclusive(&entry_lock);
    unsigned bit=id<8?1u<<id:0;
    check(bit && !(active_mask&bit),"simultaneous jobs own different SPU numbers");
    active_mask|=bit;
    ReleaseSRWLockExclusive(&entry_lock);
    if(InterlockedIncrement(&entered)==2)SetEvent(two_entered);
    if(actor==2)SetEvent(third_entered);
    check(WaitForSingleObject(release_entries,5000)==WAIT_OBJECT_0,"entry release completed");
    AcquireSRWLockExclusive(&entry_lock);active_mask&=~bit;ReleaseSRWLockExclusive(&entry_lock);
}
static DWORD WINAPI run_job(void* p) {
    actor=(unsigned)(uintptr_t)p;concurrent=1;
    if(actor==2)SetEvent(third_started);
    check(spu_run_spurs_job(entry,17,0x10000,256,0x40000)==0,"concurrent job completed");
    return 0;
}
int main(int argc,char** argv) {
    if(argc!=2)return 2;
    FILE* f=fopen(argv[1],"rb");
    if(!f || fread(memory+0x1000,48032,1,f)!=1 || fgetc(f)!=EOF)return 2;
    fclose(f);
    unsigned char* d=memory+0x10000;
    be32(d+4,0x1000);be16(d+8,48032/16);memory[0x40076]=1;
    check(spu_run_spurs_job(entry,17,0x10000,256,0x40000)==0,"single-worker job completed");
    /* Non-job contexts must retain normal LS semantics. */
    spu_context* ordinary=calloc(1,sizeof(*ordinary));if(!ordinary)return 2;
    memcpy(ordinary->ls+0x1c0,memory+0x11c0,16);
    check(spu_ls_read128(ordinary,0x1c0)._u32[2]==0x20000183,"ordinary SPU context has no kernel substitution");
    free(ordinary);
    memory[0x40076]=2;
    two_entered=CreateEvent(NULL,TRUE,FALSE,NULL);third_started=CreateEvent(NULL,TRUE,FALSE,NULL);
    third_entered=CreateEvent(NULL,TRUE,FALSE,NULL);release_entries=CreateEvent(NULL,TRUE,FALSE,NULL);
    HANDLE threads[3];
    threads[0]=CreateThread(NULL,1<<20,run_job,(void*)0,0,NULL);
    threads[1]=CreateThread(NULL,1<<20,run_job,(void*)1,0,NULL);
    check(WaitForSingleObject(two_entered,5000)==WAIT_OBJECT_0,"two configured workers enter concurrently");
    threads[2]=CreateThread(NULL,1<<20,run_job,(void*)2,0,NULL);
    check(WaitForSingleObject(third_started,5000)==WAIT_OBJECT_0,"third job requested execution");
    check(WaitForSingleObject(third_entered,150)==WAIT_TIMEOUT,"third job cannot borrow an occupied SPU number");
    SetEvent(release_entries);
    if(WaitForMultipleObjects(3,threads,TRUE,5000)!=WAIT_OBJECT_0)return 2;
    check(WaitForSingleObject(third_entered,0)==WAIT_OBJECT_0,"released worker accepts queued job");
    for(unsigned i=0;i<3;i++)CloseHandle(threads[i]);
    CloseHandle(two_entered);CloseHandle(third_started);CloseHandle(third_entered);CloseHandle(release_entries);
    printf("SPURS_JOB_KERNEL checks=%ld failures=%ld\n",checks,failures);
    return failures?1:0;
}
'''
    digest = hashlib.sha256(IMAGE.read_bytes()[0xA200:0xA210]).hexdigest()
    if digest != "03cf5dfdcc9fb003a51cbe066056b8a8785a43ac1115602ee7e8d13ba828fa20":
        raise RuntimeError("Original geometry SPU-ID accessor changed")
    print("Original SPU-ID accessor SHA256:", digest, flush=True)
    clang = shutil.which("clang") or r"C:\Program Files\LLVM\bin\clang.exe"
    with tempfile.TemporaryDirectory(prefix="tj-spurs-kernel-") as tmp:
        path = Path(tmp)
        c, exe = path / "test.c", path / "test.exe"
        c.write_text(source)
        subprocess.run([clang, "-O2", "-std=c11", "-D_CRT_SECURE_NO_WARNINGS",
                        "-DSPU_DISABLE_LS_DIAGNOSTICS", "-I", str(RUNTIME / "include"),
                        "-I", str(RUNTIME / "runtime/spu"), str(c),
                        str(RUNTIME / "runtime/spu/spurs_job.c"),
                        str(RUNTIME / "runtime/spu/spu_interp.c"), "-o", str(exe)], check=True)
        subprocess.run([str(exe), str(IMAGE)], check=True)


if __name__ == "__main__":
    main()
