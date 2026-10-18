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
extern void tj_socjob2spu_func_00000000(spu_context*);
extern void tj_socjob2spu_recomp_register(void);
extern void tj_socjob3spu_func_00000000(spu_context*);
extern void tj_socjob3spu_recomp_register(void);
extern void tj_socjob4spu_func_00000000(spu_context*);
extern void tj_socjob4spu_recomp_register(void);
extern void tj_socjob5spu_func_00000000(spu_context*);
extern void tj_socjob5spu_recomp_register(void);
extern void tj_socjob6spu_func_00000000(spu_context*);
extern void tj_socjob6spu_recomp_register(void);
extern void tj_socjob7spu_func_00000000(spu_context*);
extern void tj_socjob7spu_recomp_register(void);
extern void tj_socjob8spu_func_00000000(spu_context*);
extern void tj_socjob8spu_recomp_register(void);
extern void tj_socjob9spu_func_00000000(spu_context*);
extern void tj_socjob9spu_recomp_register(void);
extern void tj_socjob10spu_func_00000000(spu_context*);
extern void tj_socjob10spu_recomp_register(void);
extern void tj_socjob11spu_func_00000000(spu_context*);
extern void tj_socjob11spu_recomp_register(void);
extern void tj_socjob12spu_func_00000000(spu_context*);
extern void tj_socjob12spu_recomp_register(void);
extern void tj_socjob13spu_func_00000000(spu_context*);
extern void tj_socjob13spu_recomp_register(void);
extern void tj_animationjob16spu_func_00000000(spu_context*);
extern void tj_animationjob16spu_recomp_register(void);
extern void tj_geometryjob17spu_func_00000000(spu_context*);
extern void tj_geometryjob17spu_recomp_register(void);
extern void tj_survivaljob18spu_func_00000000(spu_context*);
extern void tj_survivaljob18spu_recomp_register(void);
extern void tj_task7000spu_func_00003050(spu_context*);
extern void tj_task7000spu_recomp_register(void);
extern void tj_task9A00spu_func_00003050(spu_context*);
extern void tj_task9A00spu_recomp_register(void);

/* fp/size are the dumped image's: spujob_F523F26F69764538_64912.bin */
#define TJ_SOCJOB_FP  0xF523F26F69764538ULL
#define TJ_SOCJOB2_FP 0xD58063CD2A0236B5ULL
#define TJ_SOCJOB3_FP 0xFDFFAAC1BCD5352EULL
#define TJ_SOCJOB4_FP 0xABD49FC58F53F368ULL
#define TJ_SOCJOB5_FP 0x1B6BDAC375C5438DULL
#define TJ_SOCJOB6_FP 0x80435D36E623FAA0ULL
#define TJ_SOCJOB7_FP 0x82D0BE145EC28AEEULL
#define TJ_SOCJOB8_FP 0x49C1D6C47E80AF43ULL
#define TJ_SOCJOB9_FP 0x5DA195169669A5FFULL
#define TJ_SOCJOB10_FP 0x9804ABA84566F431ULL
#define TJ_SOCJOB11_FP 0x48AF2BC79A72CB50ULL
#define TJ_SOCJOB12_FP 0xFE031AF67ED048DEULL
#define TJ_SOCJOB13_FP 0x78FEA84E1C23BB5CULL
#define TJ_ANIMATIONJOB16_FP 0xD8945A0BA2D3D054ULL
#define TJ_GEOMETRYJOB17_FP  0xDD6F7BFDDFCF7894ULL
#define TJ_SURVIVALJOB18_FP  0x463CEB5F46CA2F82ULL
#define TJ_TASK7000_FP 0x5D51D353BBB628ACULL
#define TJ_TASK9A00_FP 0x8D485835BE6E8738ULL

void tj_spu_register_all(void)
{
    spu_begin_image(1);
    tj_socjobspu_recomp_register();
    spu_workload_register_img(TJ_SOCJOB_FP, tj_socjobspu_func_00000000, 1,
                              "socjob");
    spu_begin_image(2);
    tj_socjob2spu_recomp_register();
    spu_workload_register_img(TJ_SOCJOB2_FP, tj_socjob2spu_func_00000000, 2,
                              "socjob2");
    spu_begin_image(3);
    tj_socjob3spu_recomp_register();
    spu_workload_register_img(TJ_SOCJOB3_FP, tj_socjob3spu_func_00000000, 3,
                              "socjob3");
    spu_begin_image(4);
    tj_socjob4spu_recomp_register();
    spu_workload_register_img(TJ_SOCJOB4_FP, tj_socjob4spu_func_00000000, 4,
                              "socjob4");
    spu_begin_image(5);
    tj_socjob5spu_recomp_register();
    spu_workload_register_img(TJ_SOCJOB5_FP, tj_socjob5spu_func_00000000, 5,
                              "socjob5");
    spu_begin_image(6);
    tj_socjob6spu_recomp_register();
    spu_workload_register_img(TJ_SOCJOB6_FP, tj_socjob6spu_func_00000000, 6,
                              "socjob6");
    spu_begin_image(7);
    tj_socjob7spu_recomp_register();
    spu_workload_register_img(TJ_SOCJOB7_FP, tj_socjob7spu_func_00000000, 7,
                              "socjob7");
    spu_begin_image(8);
    tj_socjob8spu_recomp_register();
    spu_workload_register_img(TJ_SOCJOB8_FP, tj_socjob8spu_func_00000000, 8,
                              "socjob8");
    spu_begin_image(9);
    tj_socjob9spu_recomp_register();
    spu_workload_register_img(TJ_SOCJOB9_FP, tj_socjob9spu_func_00000000, 9,
                              "socjob9");
    spu_begin_image(10);
    tj_socjob10spu_recomp_register();
    spu_workload_register_img(TJ_SOCJOB10_FP, tj_socjob10spu_func_00000000, 10,
                              "socjob10");
    spu_begin_image(11);
    tj_socjob11spu_recomp_register();
    spu_workload_register_img(TJ_SOCJOB11_FP, tj_socjob11spu_func_00000000, 11,
                              "socjob11");
    spu_begin_image(12);
    tj_socjob12spu_recomp_register();
    spu_workload_register_img(TJ_SOCJOB12_FP, tj_socjob12spu_func_00000000, 12,
                              "socjob12");
    spu_begin_image(13);
    tj_socjob13spu_recomp_register();
    spu_workload_register_img(TJ_SOCJOB13_FP, tj_socjob13spu_func_00000000, 13,
                              "socjob13");
    spu_begin_image(16);
    tj_animationjob16spu_recomp_register();
    spu_workload_register_img(TJ_ANIMATIONJOB16_FP,
                              tj_animationjob16spu_func_00000000, 16,
                              "animationjob16");
    spu_begin_image(17);
    tj_geometryjob17spu_recomp_register();
    spu_workload_register_img(TJ_GEOMETRYJOB17_FP,
                              tj_geometryjob17spu_func_00000000, 17,
                              "geometryjob17");
    spu_begin_image(18);
    tj_survivaljob18spu_recomp_register();
    spu_workload_register_img(TJ_SURVIVALJOB18_FP,
                              tj_survivaljob18spu_func_00000000, 18,
                              "survivaljob18");
    spu_begin_image(14);
    tj_task7000spu_recomp_register();
    spu_workload_register_img(TJ_TASK7000_FP, tj_task7000spu_func_00003050, 14,
                              "task7000");
    spu_begin_image(15);
    tj_task9A00spu_recomp_register();
    spu_workload_register_img(TJ_TASK9A00_FP, tj_task9A00spu_func_00003050, 15,
                              "task9A00");
    spu_begin_image(0);
}

/* Register before the game creates any SPURS workload, task or job chain. */
#if defined(_MSC_VER)
#pragma section(".CRT$XCU", read)
static void __cdecl tj_spu_register_all_ctor(void);
__declspec(allocate(".CRT$XCU"))
static void (__cdecl* const tj_spu_register_all_initializer)(void) =
    tj_spu_register_all_ctor;
static void __cdecl tj_spu_register_all_ctor(void)
#else
__attribute__((constructor)) static void tj_spu_register_all_ctor(void)
#endif
{
    tj_spu_register_all();
}
