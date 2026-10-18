#include "cellSaveData.h"
#include "ps3emu/guest_call.h"
#include "ppu_context.h"

#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#ifdef _WIN32
#include <direct.h>
#include <windows.h>
#else
#include <unistd.h>
#define MAX_PATH 1024
#define _getcwd getcwd
#define _rmdir rmdir
#endif

#define VM_SIZE       0x04000000u
#define SETBUF_EA     0x00100000u
#define FILELIST_EA   0x00101000u
#define PARAM_EA      0x00102000u
#define NAME_EA       0x00103000u
#define SAVE_BUF_EA   0x00104000u
#define LOAD_BUF_EA   0x00105000u
#define DIR_EA        0x00106000u
#define BAD_DIR_EA    0x00106100u
#define STAT_OPD      0x00001000u
#define FILE_OPD      0x00002000u

uint8_t* vm_base = NULL;
uint32_t ppu_vm_size = 0;
int g_resv_store_active = 0;
ps3_guest_caller_fn g_ps3_guest_caller = NULL;

void ppu_resv_break_store(uint64_t ea, uint32_t width) { (void)ea; (void)width; }
void ps3_hle_register_ctx(uint32_t nid, const char* name,
                          void (*fn)(ppu_context*))
{
    (void)nid; (void)name; (void)fn;
}

static const uint8_t s_payload[] = {
    0x54, 0x4A, 0x2D, 0x53, 0x41, 0x56, 0x45, 0x2D,
    0x50, 0x52, 0x4F, 0x4F, 0x46
};
static int s_is_save;
static int s_expect_new;
static int s_file_phase;
static int s_failed;
static int s_rebind_userdata;
static uint32_t s_recreate_mode = CELL_SAVEDATA_RECREATE_NO_NOBROKEN;
static int s_custom_request;
static uint32_t s_operation, s_offset, s_size, s_buffer_size, s_buffer_ea;
static uint32_t s_expected_exc_size;
static int32_t s_stat_result = CELL_SAVEDATA_CBRESULT_OK_NEXT;
static uint32_t s_stat_param_ea = PARAM_EA;

static uint32_t read_be32(uint32_t ea)
{
    const uint8_t* p = vm_base + ea;
    return ((uint32_t)p[0] << 24) | ((uint32_t)p[1] << 16) |
           ((uint32_t)p[2] << 8) | p[3];
}

static void write_be32(uint32_t ea, uint32_t value)
{
    vm_base[ea + 0] = (uint8_t)(value >> 24);
    vm_base[ea + 1] = (uint8_t)(value >> 16);
    vm_base[ea + 2] = (uint8_t)(value >> 8);
    vm_base[ea + 3] = (uint8_t)value;
}

static void expect_u32(const char* name, uint32_t actual, uint32_t expected)
{
    if (actual != expected) {
        fprintf(stderr, "FAIL: %s actual=%u expected=%u\n",
                name, actual, expected);
        s_failed = 1;
    }
}

static void harness_guest_call(uint32_t opd, uint64_t arg0, uint64_t arg1,
                               uint64_t arg2, uint64_t arg3, uint64_t arg4,
                               uint64_t arg5, uint64_t arg6, uint64_t arg7)
{
    (void)arg3; (void)arg4; (void)arg5; (void)arg6; (void)arg7;
    uint32_t cb_ea = (uint32_t)arg0;
    uint32_t get_ea = (uint32_t)arg1;
    uint32_t set_ea = (uint32_t)arg2;

    if (opd == STAT_OPD) {
        expect_u32("callback userdata", read_be32(cb_ea + 16), 0x12345678u);
        if (s_is_save) {
            expect_u32("save isNewData", read_be32(get_ea + 4),
                       (uint32_t)s_expect_new);
            memset(vm_base + PARAM_EA, 0, 1552);
            memcpy(vm_base + PARAM_EA, "Tokyo Jungle Harness", 20);
            memcpy(vm_base + PARAM_EA + 1288, "TJTEST", 6);
            write_be32(set_ea + 0, s_stat_param_ea);
        } else {
            expect_u32("existing-save flag", read_be32(get_ea + 4), 0u);
            expect_u32("fileNum", read_be32(get_ea + 1628), 1u);
            expect_u32("fileListNum", read_be32(get_ea + 1632), 1u);
            expect_u32("fileList EA", read_be32(get_ea + 1636), FILELIST_EA);
            if (strcmp((const char*)(vm_base + get_ea + 64),
                       "Tokyo Jungle Harness") != 0) {
                fprintf(stderr, "FAIL: saved metadata title was not reloaded\n");
                s_failed = 1;
            }
            if (strcmp((const char*)(vm_base + FILELIST_EA + 40),
                       "DATA.BIN") != 0) {
                fprintf(stderr, "FAIL: DATA.BIN absent from guest file list\n");
                s_failed = 1;
            }
        }
        write_be32(set_ea + 4, s_recreate_mode);
        if (s_rebind_userdata)
            write_be32(cb_ea + 16, 0x89ABCDEFu);
        write_be32(cb_ea, (uint32_t)s_stat_result);
        return;
    }

    if (opd == FILE_OPD) {
        if (s_rebind_userdata) {
            uint32_t expected_userdata = s_file_phase == 0 ? 0x89ABCDEFu : 0u;
            uint32_t actual_userdata = read_be32(cb_ea + 16);
            expect_u32("callback-updated userdata", actual_userdata,
                       expected_userdata);
            if (actual_userdata != expected_userdata) {
                write_be32(cb_ea, CELL_SAVEDATA_CBRESULT_ERR_FAILURE);
                return;
            }
            /* A callback may also replace its state pointer with NULL. */
            write_be32(cb_ea + 16, 0u);
        }
        if (s_file_phase == 0) {
            expect_u32("initial excSize", read_be32(get_ea), 0u);
            write_be32(set_ea + 0, s_is_save ? CELL_SAVEDATA_FILEOP_WRITE
                                             : CELL_SAVEDATA_FILEOP_READ);
            write_be32(set_ea + 8, CELL_SAVEDATA_FILETYPE_NORMALFILE);
            write_be32(set_ea + 28, NAME_EA);
            write_be32(set_ea + 32, 0u);
            write_be32(set_ea + 36, (uint32_t)sizeof(s_payload));
            write_be32(set_ea + 40, (uint32_t)sizeof(s_payload));
            write_be32(set_ea + 44, s_is_save ? SAVE_BUF_EA : LOAD_BUF_EA);
            if (s_custom_request) {
                write_be32(set_ea + 0, s_operation);
                write_be32(set_ea + 32, s_offset);
                write_be32(set_ea + 36, s_size);
                write_be32(set_ea + 40, s_buffer_size);
                write_be32(set_ea + 44, s_buffer_ea);
            }
            write_be32(cb_ea, CELL_SAVEDATA_CBRESULT_OK_NEXT);
            s_file_phase++;
        } else {
            expect_u32("completed excSize", read_be32(get_ea),
                       s_custom_request ? s_expected_exc_size
                                        : (uint32_t)sizeof(s_payload));
            write_be32(cb_ea, CELL_SAVEDATA_CBRESULT_OK_LAST);
            s_file_phase++;
        }
        return;
    }

    fprintf(stderr, "FAIL: unexpected guest OPD 0x%08X\n", opd);
    s_failed = 1;
}

static int file_equals(const char* path, const uint8_t* expected, size_t size)
{
    FILE* fp = fopen(path, "rb");
    if (!fp)
        return 0;
    uint8_t actual[64];
    size_t got = fread(actual, 1, sizeof(actual), fp);
    fclose(fp);
    return got == size && memcmp(actual, expected, size) == 0;
}

static int file_exists(const char* path)
{
    FILE* fp = fopen(path, "rb");
    if (!fp) return 0;
    fclose(fp);
    return 1;
}

static int s_native_phase, s_native_result;
static void native_list(CellSaveDataCBResult* result, CellSaveDataListGet* get,
                         CellSaveDataListSet* set)
{
    (void)get;
    expect_u32("native list userdata", (uint32_t)(uintptr_t)result->userdata, 0x3456);
    result->userdata = (void*)(uintptr_t)0x1234;
    result->result = CELL_SAVEDATA_CBRESULT_OK_NEXT;
    set->focusDirName = "NPUA80523-AUTO-";
}

static void native_stat(CellSaveDataCBResult* result, CellSaveDataStatGet* get,
                         CellSaveDataStatSet* set)
{
    (void)get; (void)set;
    expect_u32("native stat userdata", (uint32_t)(uintptr_t)result->userdata, 0x1234);
    result->userdata = (void*)(uintptr_t)0x2345;
    result->result = CELL_SAVEDATA_CBRESULT_OK_NEXT;
}

static void native_file(CellSaveDataCBResult* result, CellSaveDataFileGet* get,
                         CellSaveDataFileSet* set)
{
    static uint8_t invalid_payload[sizeof(s_payload)] = {0};
    expect_u32("native file userdata", (uint32_t)(uintptr_t)result->userdata,
               s_native_phase ? 0 : 0x2345);
    expect_u32("native previous excSize", get->excSize, s_native_phase ? 3 : 0);
    result->userdata = NULL;
    set->fileName = "DATA.BIN";
    if (!s_native_phase++) {
        set->fileOperation = CELL_SAVEDATA_FILEOP_WRITE_NOTRUNC;
        set->fileSize = set->fileBufSize = 3;
        set->fileBuf = s_native_result ? invalid_payload : (void*)s_payload;
        result->result = s_native_result;
    } else {
        set->fileOperation = CELL_SAVEDATA_FILEOP_READ; /* zero bytes, NULL buffer */
        result->result = CELL_SAVEDATA_CBRESULT_OK_LAST;
    }
}

static void check_native_callbacks(const char* data_path)
{
    CellSaveDataSetBuf buf = {0};
    CellSaveDataSetList list = {0};
    list.dirNamePrefix = "NPUA80523";
    buf.dirListMax = 8;
    buf.fileListMax = 8;
    for (int mode = 0; mode < 3; ++mode) {
        s_native_phase = 0;
        s_native_result = mode == 0 ? CELL_SAVEDATA_CBRESULT_OK_NEXT
                          : mode == 1 ? CELL_SAVEDATA_CBRESULT_ERR_FAILURE : 3;
        s32 rc = cellSaveDataListSave2(0, &list, &buf, native_list,
                                      native_stat, native_file, 0,
                                      (void*)(uintptr_t)0x3456);
        expect_u32("native callback result", (uint32_t)rc,
                   mode ? (uint32_t)CELL_SAVEDATA_ERROR_CBRESULT : CELL_OK);
        expect_u32("native callback count", s_native_phase, mode ? 1 : 2);
        expect_u32("native callback preserves payload",
                   file_equals(data_path, s_payload, sizeof(s_payload)), 1);
    }
}

static void run_file_request(const char* label, uint32_t operation,
                             uint32_t offset, uint32_t size,
                             uint32_t buffer_size, uint32_t buffer_ea,
                             s32 expected_result, uint32_t expected_exc_size)
{
    s_custom_request = 1;
    s_operation = operation;
    s_offset = offset;
    s_size = size;
    s_buffer_size = buffer_size;
    s_buffer_ea = buffer_ea;
    s_expected_exc_size = expected_exc_size;
    s_is_save = operation != CELL_SAVEDATA_FILEOP_READ;
    s_expect_new = 0;
    s_file_phase = 0;
    s32 rc = s_is_save ? cellSaveDataAutoSave2(
        0, (const char*)(uintptr_t)DIR_EA, 0,
        (CellSaveDataSetBuf*)(uintptr_t)SETBUF_EA,
        (CellSaveDataStatCallback)(uintptr_t)STAT_OPD,
        (CellSaveDataFileCallback)(uintptr_t)FILE_OPD,
        0, (void*)(uintptr_t)0x12345678u) : cellSaveDataAutoLoad2(
        0, (const char*)(uintptr_t)DIR_EA, 0,
        (CellSaveDataSetBuf*)(uintptr_t)SETBUF_EA,
        (CellSaveDataStatCallback)(uintptr_t)STAT_OPD,
        (CellSaveDataFileCallback)(uintptr_t)FILE_OPD,
        0, (void*)(uintptr_t)0x12345678u);
    expect_u32(label, (uint32_t)rc, (uint32_t)expected_result);
    expect_u32("request callback count", (uint32_t)s_file_phase,
               expected_result == CELL_OK ? 2u : 1u);
}

static void check_file_requests(const char* data_path)
{
    uint8_t expected[32] = {0};
    memcpy(expected, s_payload, sizeof(s_payload));

    run_file_request("short WRITE buffer rejected", CELL_SAVEDATA_FILEOP_WRITE,
                     0, sizeof(s_payload), 4, SAVE_BUF_EA,
                     CELL_SAVEDATA_ERROR_PARAM, 0);
    expect_u32("bad WRITE preserves payload",
               file_equals(data_path, s_payload, sizeof(s_payload)), 1);
    run_file_request("short NOTRUNC buffer rejected",
                     CELL_SAVEDATA_FILEOP_WRITE_NOTRUNC,
                     4, sizeof(s_payload), 4, SAVE_BUF_EA,
                     CELL_SAVEDATA_ERROR_PARAM, 0);
    expect_u32("bad NOTRUNC preserves payload",
               file_equals(data_path, s_payload, sizeof(s_payload)), 1);
    run_file_request("null declared buffer rejected", CELL_SAVEDATA_FILEOP_WRITE,
                     0, 0, 1, 0, CELL_SAVEDATA_ERROR_PARAM, 0);
    expect_u32("bad null buffer preserves payload",
               file_equals(data_path, s_payload, sizeof(s_payload)), 1);

    run_file_request("guest WRITE past VM rejected", CELL_SAVEDATA_FILEOP_WRITE,
                     0, 16, 16, VM_SIZE - 8,
                     CELL_SAVEDATA_ERROR_ACCESS_ERROR, 0);
    expect_u32("out-of-range WRITE preserves payload",
               file_equals(data_path, s_payload, sizeof(s_payload)), 1);
    run_file_request("wrapped guest WRITE rejected", CELL_SAVEDATA_FILEOP_WRITE,
                     0, 16, 16, UINT32_MAX - 7,
                     CELL_SAVEDATA_ERROR_ACCESS_ERROR, 0);
    expect_u32("wrapped WRITE preserves payload",
               file_equals(data_path, s_payload, sizeof(s_payload)), 1);
    run_file_request("empty READ accepts null buffer", CELL_SAVEDATA_FILEOP_READ,
                     0, 0, 0, 0, CELL_OK, 0);

    run_file_request("NOTRUNC offset write", CELL_SAVEDATA_FILEOP_WRITE_NOTRUNC,
                     4, 3, 3, SAVE_BUF_EA, CELL_OK, 3);
    memcpy(expected + 4, s_payload, 3);
    expect_u32("NOTRUNC preserves prefix and suffix",
               file_equals(data_path, expected, sizeof(s_payload)), 1);
    run_file_request("restore full payload", CELL_SAVEDATA_FILEOP_WRITE,
                     0, sizeof(s_payload), sizeof(s_payload), SAVE_BUF_EA,
                     CELL_OK, sizeof(s_payload));
    run_file_request("WRITE offset truncates", CELL_SAVEDATA_FILEOP_WRITE,
                     4, 3, 3, SAVE_BUF_EA, CELL_OK, 3);
    expect_u32("WRITE removes stale suffix", file_equals(data_path, expected, 7), 1);

    run_file_request("empty WRITE truncates", CELL_SAVEDATA_FILEOP_WRITE,
                     5, 0, 0, 0, CELL_OK, 0);
    expect_u32("empty WRITE keeps prefix", file_equals(data_path, expected, 5), 1);
    run_file_request("empty WRITE extends", CELL_SAVEDATA_FILEOP_WRITE,
                     9, 0, 0, 0, CELL_OK, 0);
    memset(expected + 5, 0, sizeof(expected) - 5);
    expect_u32("extension is zero filled", file_equals(data_path, expected, 9), 1);

    memset(vm_base + LOAD_BUF_EA, 0xA5, sizeof(expected));
    run_file_request("offset READ ends at EOF", CELL_SAVEDATA_FILEOP_READ,
                     7, 9, 9, LOAD_BUF_EA, CELL_OK, 2);
    expect_u32("READ copied only available bytes",
               vm_base[LOAD_BUF_EA] == 0 && vm_base[LOAD_BUF_EA + 1] == 0 &&
               vm_base[LOAD_BUF_EA + 2] == 0xA5, 1);

    run_file_request("WRITE after gap", CELL_SAVEDATA_FILEOP_WRITE,
                     12, 3, 3, SAVE_BUF_EA, CELL_OK, 3);
    memcpy(expected + 12, s_payload, 3);
    expect_u32("gap and prefix preserved", file_equals(data_path, expected, 15), 1);
    run_file_request("empty NOTRUNC does not extend",
                     CELL_SAVEDATA_FILEOP_WRITE_NOTRUNC,
                     30, 0, 0, 0, CELL_OK, 0);
    expect_u32("empty NOTRUNC preserves file", file_equals(data_path, expected, 15), 1);
    s_custom_request = 0;
}

int main(int argc, char** argv)
{
    (void)argv;
    s_rebind_userdata = argc > 1;
    char cwd[MAX_PATH];
    char root[MAX_PATH];
    char save_dir[MAX_PATH];
    char data_path[MAX_PATH];
    char sfo_path[MAX_PATH];
    char stale_path[MAX_PATH];
    if (!_getcwd(cwd, sizeof(cwd)))
        return 1;
#ifdef _WIN32
    snprintf(root, sizeof(root), "%s\\savedata-io-harness-%lu-%llu", cwd,
             (unsigned long)GetCurrentProcessId(),
             (unsigned long long)GetTickCount64());
    if (_putenv_s("PS3_SAVEDATA_ROOT", root) != 0)
        return 1;
#else
    snprintf(root, sizeof(root), "%s/savedata-io-harness-XXXXXX", cwd);
    if (!mkdtemp(root) || setenv("PS3_SAVEDATA_ROOT", root, 1) != 0)
        return 1;
#endif
    snprintf(save_dir, sizeof(save_dir), "%s/NPUA80523-AUTO-", root);
    snprintf(data_path, sizeof(data_path), "%s/DATA.BIN", save_dir);
    snprintf(sfo_path, sizeof(sfo_path), "%s/PARAM.SFO", save_dir);
    snprintf(stale_path, sizeof(stale_path), "%s/STALE.BIN", save_dir);

    vm_base = (uint8_t*)calloc(1, VM_SIZE);
    if (!vm_base)
        return 1;
    ppu_vm_size = VM_SIZE;
    g_ps3_guest_caller = harness_guest_call;

    write_be32(SETBUF_EA + 4, 8u);
    write_be32(SETBUF_EA + 32, 8u * 56u);
    write_be32(SETBUF_EA + 36, FILELIST_EA);
    memcpy(vm_base + NAME_EA, "DATA.BIN", 9);
    memcpy(vm_base + DIR_EA, "NPUA80523-AUTO-", sizeof("NPUA80523-AUTO-"));
    memcpy(vm_base + BAD_DIR_EA, "../escape", 10);
    memcpy(vm_base + SAVE_BUF_EA, s_payload, sizeof(s_payload));

    s32 rc = cellSaveDataAutoSave2(
        0, (const char*)(uintptr_t)BAD_DIR_EA, 0,
        (CellSaveDataSetBuf*)(uintptr_t)SETBUF_EA,
        (CellSaveDataStatCallback)(uintptr_t)STAT_OPD,
        (CellSaveDataFileCallback)(uintptr_t)FILE_OPD,
        0, (void*)(uintptr_t)0x12345678u);
    expect_u32("traversal directory rejected", (uint32_t)rc,
               (uint32_t)CELL_SAVEDATA_ERROR_PARAM);

    s_is_save = 1;
    s_expect_new = 1;
    s_file_phase = 0;
    rc = cellSaveDataAutoSave2(
        0, (const char*)(uintptr_t)DIR_EA, 0,
        (CellSaveDataSetBuf*)(uintptr_t)SETBUF_EA,
        (CellSaveDataStatCallback)(uintptr_t)STAT_OPD,
        (CellSaveDataFileCallback)(uintptr_t)FILE_OPD,
        0, (void*)(uintptr_t)0x12345678u);
    expect_u32("AutoSave2 result", (uint32_t)rc, 0u);
    expect_u32("save callback count", (uint32_t)s_file_phase, 2u);
    if (!file_equals(data_path, s_payload, sizeof(s_payload))) {
        fprintf(stderr, "FAIL: save payload was not written\n");
        s_failed = 1;
    }
    if (!file_exists(sfo_path)) {
        fprintf(stderr, "FAIL: save metadata was not written\n");
        s_failed = 1;
    }

    FILE* stale = fopen(stale_path, "wb");
    int stale_ok = 0;
    if (stale) {
        stale_ok = fwrite("stale", 1, 5, stale) == 5;
        if (fclose(stale) != 0)
            stale_ok = 0;
    }
    if (!stale_ok) {
        fprintf(stderr, "FAIL: could not stage stale save file\n");
        s_failed = 1;
    } else {
        /* Invalid stat results/metadata must be rejected before recreation
         * can remove either an existing payload or an unrelated stale file. */
        s_recreate_mode = CELL_SAVEDATA_RECREATE_YES;
        s_is_save = 1;
        s_expect_new = 0;
        for (int invalid = 0; invalid < 3; ++invalid) {
            s_file_phase = 0;
            s_stat_result = invalid == 0 ? 3 : CELL_SAVEDATA_CBRESULT_OK_NEXT;
            s_stat_param_ea = invalid == 1 ? VM_SIZE - 16
                              : invalid == 2 ? UINT32_MAX - 15 : PARAM_EA;
            rc = cellSaveDataAutoSave2(
                0, (const char*)(uintptr_t)DIR_EA, 0,
                (CellSaveDataSetBuf*)(uintptr_t)SETBUF_EA,
                (CellSaveDataStatCallback)(uintptr_t)STAT_OPD,
                (CellSaveDataFileCallback)(uintptr_t)FILE_OPD,
                0, (void*)(uintptr_t)0x12345678u);
            expect_u32("invalid stat rejected", (uint32_t)rc,
                       (uint32_t)(invalid == 0 ? CELL_SAVEDATA_ERROR_CBRESULT
                                               : CELL_SAVEDATA_ERROR_PARAM));
            expect_u32("invalid stat skips file callback", s_file_phase, 0);
            expect_u32("invalid stat preserves payload",
                       file_equals(data_path, s_payload, sizeof(s_payload)), 1);
            expect_u32("invalid stat preserves stale file",
                       file_equals(stale_path, (const uint8_t*)"stale", 5), 1);
        }
        s_stat_result = CELL_SAVEDATA_CBRESULT_OK_NEXT;
        s_stat_param_ea = PARAM_EA;
        s_recreate_mode = CELL_SAVEDATA_RECREATE_YES;
        s_is_save = 1;
        s_expect_new = 0;
        s_file_phase = 0;
        rc = cellSaveDataAutoSave2(
            0, (const char*)(uintptr_t)DIR_EA, 0,
            (CellSaveDataSetBuf*)(uintptr_t)SETBUF_EA,
            (CellSaveDataStatCallback)(uintptr_t)STAT_OPD,
            (CellSaveDataFileCallback)(uintptr_t)FILE_OPD,
            0, (void*)(uintptr_t)0x12345678u);
        expect_u32("recreate save result", (uint32_t)rc, 0u);
        if (file_exists(stale_path)) {
            fprintf(stderr, "FAIL: recreate left stale save file\n");
            s_failed = 1;
        }
    }

    memset(vm_base + LOAD_BUF_EA, 0, sizeof(s_payload));
    s_recreate_mode = CELL_SAVEDATA_RECREATE_NO_NOBROKEN;
    s_is_save = 0;
    s_file_phase = 0;
    rc = cellSaveDataAutoLoad2(
        0, (const char*)(uintptr_t)DIR_EA, 0,
        (CellSaveDataSetBuf*)(uintptr_t)SETBUF_EA,
        (CellSaveDataStatCallback)(uintptr_t)STAT_OPD,
        (CellSaveDataFileCallback)(uintptr_t)FILE_OPD,
        0, (void*)(uintptr_t)0x12345678u);
    expect_u32("AutoLoad2 result", (uint32_t)rc, 0u);
    expect_u32("load callback count", (uint32_t)s_file_phase, 2u);
    if (memcmp(vm_base + LOAD_BUF_EA, s_payload, sizeof(s_payload)) != 0) {
        fprintf(stderr, "FAIL: reloaded payload differs\n");
        s_failed = 1;
    }

    check_native_callbacks(data_path);
    check_file_requests(data_path);

    free(vm_base);
    vm_base = NULL;
    remove(data_path);
    remove(sfo_path);
    remove(stale_path);
    _rmdir(save_dir);
    _rmdir(root);

    if (s_failed)
        return 1;
    puts("PASS: guest save/load round trip, offset writes, truncation and invalid buffers");
    return 0;
}
