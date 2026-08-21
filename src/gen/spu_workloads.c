/* SPU workload registry for Tokyo Jungle.
 *
 * The runtime's job dispatcher (runtime/spu/spu_workload.c) matches a guest SPU
 * image by FNV-1a-64 content fingerprint and runs the lifted entry registered
 * here. Without a match it logs
 *
 *     [spurs-job] dispatch MISS fp=... size=... job=...
 *
 * and returns without running anything -- which left the title blocked in
 * event_queue_receive waiting for SPU work that never started.
 *
 * This image is the "soc-job" SPURS job chain. It is NOT an embedded SPU ELF:
 * SPURS job binaries are raw code+data blobs the title loads from its own data
 * files, so tools/extract_spu_images.py cannot find it (that finds only two
 * unrelated embedded ELFs in this EBOOT). It was captured with the runtime's
 * SPU_DUMP_MISS, then:
 *
 *   find_spu_functions.py --raw --base 0   -> 886 functions, 95.5% coverage
 *   spu_lifter.py --base 0 --functions ... --symbol-prefix tj_socjob
 *
 * Regenerating: re-dump with SPU_DUMP_MISS and repeat the two steps above. The
 * fingerprint below must match the dumped file's name.
 */
#include "spu_workload.h"

extern void spu_begin_image(int image_id);

extern void tj_socjobspu_func_00000000(spu_context*);
extern void tj_socjobspu_recomp_register(void);

/* fp/size are the dumped image's: spujob_F523F26F69764538_64912.bin */
#define TJ_SOCJOB_FP  0xF523F26F69764538ULL

void tj_spu_register_all(void)
{
    spu_begin_image(1);
    tj_socjobspu_recomp_register();
    spu_workload_register_img(TJ_SOCJOB_FP, tj_socjobspu_func_00000000, 1,
                              "socjob");
    spu_begin_image(0);
}

/* Register before the game creates any SPURS workload, task or job chain. */
__attribute__((constructor)) static void tj_spu_register_all_ctor(void)
{
    tj_spu_register_all();
}
