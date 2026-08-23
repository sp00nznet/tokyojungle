/* SPU workload registry for Tokyo Jungle -- GENERATED, see the note below.
 *
 * The runtime matches a guest SPU image by FNV-1a-64 content fingerprint and
 * runs the lifted entry registered here. Without a match it logs
 *
 *     [spurs-job] dispatch MISS fp=... size=... job=...
 *
 * and returns WITHOUT RUNNING ANYTHING -- the job silently does no work, and
 * whatever the title was waiting on never happens. With only the sound job
 * registered this binary logged 1327 misses in 45 s and sat in a condvar wait.
 *
 * These are NOT embedded SPU ELFs: SPURS job binaries are raw code+data blobs
 * the title loads from its own data files, so extract_spu_images.py cannot
 * find them. Each was captured at runtime with SPU_DUMP_MISS, then:
 *
 *   find_spu_functions.py --raw --base 0 --out funcs.json <dump>
 *   spu_lifter.py --base 0 --functions funcs.json --symbol-prefix <p> <dump>
 *
 * To regenerate: re-dump with SPU_DUMP_MISS and repeat. A fingerprint here
 * must match its dumped file exactly or the image will miss again.
 */
#include "spu_workload.h"

extern void spu_begin_image(int image_id);

extern void tj_socjobspu_func_00000000(spu_context*);
extern void tj_socjobspu_recomp_register(void);
extern void tj_dspjobspu_func_00000000(spu_context*);
extern void tj_dspjobspu_recomp_register(void);
extern void tj_j1B6BDAC375C5438Dspu_func_00000000(spu_context*);
extern void tj_j1B6BDAC375C5438Dspu_recomp_register(void);
extern void tj_j48AF2BC79A72CB50spu_func_00000000(spu_context*);
extern void tj_j48AF2BC79A72CB50spu_recomp_register(void);
extern void tj_j49C1D6C47E80AF43spu_func_00000000(spu_context*);
extern void tj_j49C1D6C47E80AF43spu_recomp_register(void);
extern void tj_j5DA195169669A5FFspu_func_00000000(spu_context*);
extern void tj_j5DA195169669A5FFspu_recomp_register(void);
extern void tj_j80435D36E623FAA0spu_func_00000000(spu_context*);
extern void tj_j80435D36E623FAA0spu_recomp_register(void);
extern void tj_j82D0BE145EC28AEEspu_func_00000000(spu_context*);
extern void tj_j82D0BE145EC28AEEspu_recomp_register(void);
extern void tj_j9804ABA84566F431spu_func_00000000(spu_context*);
extern void tj_j9804ABA84566F431spu_recomp_register(void);
extern void tj_jABD49FC58F53F368spu_func_00000000(spu_context*);
extern void tj_jABD49FC58F53F368spu_recomp_register(void);
extern void tj_jD58063CD2A0236B5spu_func_00000000(spu_context*);
extern void tj_jD58063CD2A0236B5spu_recomp_register(void);
extern void tj_jFE031AF67ED048DEspu_func_00000000(spu_context*);
extern void tj_jFE031AF67ED048DEspu_recomp_register(void);

void tj_spu_register_all(void)
{
    /* the SGX sound down-mix job chain */
    /* spujob_F523F26F69764538_64912.bin */
    spu_begin_image(1);
    tj_socjobspu_recomp_register();
    spu_workload_register_img(0xF523F26F69764538ULL, tj_socjobspu_func_00000000, 1, "socjob");

    /* the SGX sound DSP job (compressor / amp / peak) */
    /* spujob_FDFFAAC1BCD5352E_34704.bin */
    spu_begin_image(2);
    tj_dspjobspu_recomp_register();
    spu_workload_register_img(0xFDFFAAC1BCD5352EULL, tj_dspjobspu_func_00000000, 2, "dspjob");

    /* spujob_1B6BDAC375C5438D_13840.bin */
    spu_begin_image(3);
    tj_j1B6BDAC375C5438Dspu_recomp_register();
    spu_workload_register_img(0x1B6BDAC375C5438DULL, tj_j1B6BDAC375C5438Dspu_func_00000000, 3, "j1B6BDAC3");

    /* spujob_48AF2BC79A72CB50_25232.bin */
    spu_begin_image(4);
    tj_j48AF2BC79A72CB50spu_recomp_register();
    spu_workload_register_img(0x48AF2BC79A72CB50ULL, tj_j48AF2BC79A72CB50spu_func_00000000, 4, "j48AF2BC7");

    /* spujob_49C1D6C47E80AF43_24720.bin */
    spu_begin_image(5);
    tj_j49C1D6C47E80AF43spu_recomp_register();
    spu_workload_register_img(0x49C1D6C47E80AF43ULL, tj_j49C1D6C47E80AF43spu_func_00000000, 5, "j49C1D6C4");

    /* spujob_5DA195169669A5FF_72464.bin */
    spu_begin_image(6);
    tj_j5DA195169669A5FFspu_recomp_register();
    spu_workload_register_img(0x5DA195169669A5FFULL, tj_j5DA195169669A5FFspu_func_00000000, 6, "j5DA19516");

    /* spujob_80435D36E623FAA0_19488.bin */
    spu_begin_image(7);
    tj_j80435D36E623FAA0spu_recomp_register();
    spu_workload_register_img(0x80435D36E623FAA0ULL, tj_j80435D36E623FAA0spu_func_00000000, 7, "j80435D36");

    /* spujob_82D0BE145EC28AEE_17424.bin */
    spu_begin_image(8);
    tj_j82D0BE145EC28AEEspu_recomp_register();
    spu_workload_register_img(0x82D0BE145EC28AEEULL, tj_j82D0BE145EC28AEEspu_func_00000000, 8, "j82D0BE14");

    /* spujob_9804ABA84566F431_20624.bin */
    spu_begin_image(9);
    tj_j9804ABA84566F431spu_recomp_register();
    spu_workload_register_img(0x9804ABA84566F431ULL, tj_j9804ABA84566F431spu_func_00000000, 9, "j9804ABA8");

    /* spujob_ABD49FC58F53F368_9488.bin */
    spu_begin_image(10);
    tj_jABD49FC58F53F368spu_recomp_register();
    spu_workload_register_img(0xABD49FC58F53F368ULL, tj_jABD49FC58F53F368spu_func_00000000, 10, "jABD49FC5");

    /* spujob_D58063CD2A0236B5_53408.bin */
    spu_begin_image(11);
    tj_jD58063CD2A0236B5spu_recomp_register();
    spu_workload_register_img(0xD58063CD2A0236B5ULL, tj_jD58063CD2A0236B5spu_func_00000000, 11, "jD58063CD");

    /* spujob_FE031AF67ED048DE_46928.bin */
    spu_begin_image(12);
    tj_jFE031AF67ED048DEspu_recomp_register();
    spu_workload_register_img(0xFE031AF67ED048DEULL, tj_jFE031AF67ED048DEspu_func_00000000, 12, "jFE031AF6");

    spu_begin_image(0);
}

/* Register before the game creates any SPURS workload, task or job chain. */
__attribute__((constructor)) static void tj_spu_register_all_ctor(void)
{
    tj_spu_register_all();
}