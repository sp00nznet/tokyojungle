"""Run the actual lifted stride lookup against its original five-entry table."""
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT.parent / "ps3recomp"


def main():
    generated = (ROOT / "generated/spu_lifted/geometryjob17/spu_recomp.c").read_text()
    functions = [(int(m[1], 16), m[0]) for m in re.finditer(
        r"^void tj_geometryjob17spu_func_([0-9A-F]{8})\(spu_context\* ctx\) \{\n.*?^}\n",
        generated, re.M | re.S) if 0x2798 <= int(m[1], 16) < 0x2838]
    assert len(functions) >= 18
    declarations = "\n".join(body.split("{", 1)[0] + ";" for _, body in functions)
    animation = (ROOT / "generated/spu_lifted/animationjob16/spu_recomp.c").read_text()
    animation_cases = ((0x14BC, 6), (0x2130, 56), (0x2624, 17), (0x2790, 88))
    animation_bodies, animation_checks = [], []
    for pc, reg in animation_cases:
        body = re.search(r"^void tj_animationjob16spu_func_%08X\(spu_context\* ctx\) \{\n.*?^}\n" % pc,
                         animation, re.M | re.S).group()
        animation_bodies.append("void tj_animationjob16spu_func_%08X(spu_context* c) {}\n" % (pc+4) + body)
        animation_checks.append(r'''
        for(unsigned depth=0;depth<2;depth++) for(unsigned taken=0;taken<2;taken++) {
            memset(c->gpr,0,sizeof(c->gpr)); c->host_depth=depth; c->int_enable=0;
            c->gpr[0]=spu_ila(0x1110); c->gpr[2]=spu_ila(0x4000);
            c->gpr[%d]=spu_il(taken?0:1); g_spu_trampoline_fn=NULL;
            tj_animationjob16spu_func_%08X(c);
            if(c->pc!=(taken?0x4000:0x%X) || c->int_enable!=taken || !g_spu_trampoline_fn)
                failures++;
        }
''' % (reg, pc, pc+4))
    source = r'''
#include "spu_helpers.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#define SPU_IB_DISPATCH spu_indirect_branch
SPU_THREAD_LOCAL void (*g_spu_trampoline_fn)(spu_context*);
void spu_stop(spu_context* c) { fprintf(stderr,"unexpected stop %X\n",c->pc); exit(2); }
''' + declarations + "\n" + "\n".join(body for _, body in functions) + "\n" + "\n".join(animation_bodies) + r'''
void spu_indirect_branch(spu_context* c) {
    switch(c->pc) {
        case 0x27E0: tj_geometryjob17spu_func_000027E0(c); break;
        case 0x27E8: tj_geometryjob17spu_func_000027E8(c); break;
        case 0x27F0: tj_geometryjob17spu_func_000027F0(c); break;
        case 0x2830: tj_geometryjob17spu_func_00002830(c); break;
        default: fprintf(stderr,"unexpected branch %X\n",c->pc); exit(2);
    }
}
int main(int argc, char** argv) {
    if(argc!=2) return 2;
    static const unsigned expected[]={12,20,24,24,0};
    unsigned failures=0;
    spu_context* c=calloc(1,sizeof(*c));
    if(!c) return 2;
    FILE* f=fopen(argv[1],"rb");
    if(!f || fread(c->ls,48032,1,f)!=1 || fgetc(f)!=EOF) return 2;
    fclose(f);
    for(unsigned mode=0; mode<5; mode++) {
        memset(c->gpr,0,sizeof(c->gpr));
        c->host_depth=1; c->gpr[0]=spu_il(0x13A8);
        c->gpr[3]=spu_ila(0x2A570); c->gpr[4]=spu_il(mode);
        g_spu_trampoline_fn=tj_geometryjob17spu_func_00002798;
        unsigned steps=0;
        while(g_spu_trampoline_fn && steps++<100) {
            void (*fn)(spu_context*)=g_spu_trampoline_fn;
            g_spu_trampoline_fn=NULL; fn(c);
        }
        unsigned actual=c->gpr[3]._u32[0];
        printf("mode=%u stride=%u expected=%u\n",mode,actual,expected[mode]);
        if(g_spu_trampoline_fn || actual!=expected[mode] || c->pc!=0x13A8) failures++;
    }
''' + "\n".join(animation_checks) + r'''
    free(c);
    printf("GEOMETRY_STRIDE failures=%u\n",failures);
    return failures?1:0;
}
'''
    with tempfile.TemporaryDirectory(prefix="tj-stride-") as temp:
        path = Path(temp)
        cfile, exe = path / "test.c", path / "test.exe"
        cfile.write_text(source)
        clang = shutil.which("clang") or r"C:\Program Files\LLVM\bin\clang.exe"
        subprocess.run([clang, "-O2", "-std=c11", "-D_CRT_SECURE_NO_WARNINGS",
                        "-DSPU_DISABLE_LS_DIAGNOSTICS", "-I", str(RUNTIME / "include"),
                        "-I", str(RUNTIME / "runtime/spu"), str(cfile), "-o", str(exe)], check=True)
        subprocess.run([str(exe), str(ROOT / "generated/spu_images/spujob_DD6F7BFDDFCF7894_48032.bin")],
                       check=True)


if __name__ == "__main__":
    main()
