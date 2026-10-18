#include "cellSaveData.h"
#include "savedata_guest_abi.h"

#include <stdint.h>
#include <stdio.h>
#include <string.h>

static void put_be32(uint8_t* p, uint32_t value)
{
    p[0] = (uint8_t)(value >> 24);
    p[1] = (uint8_t)(value >> 16);
    p[2] = (uint8_t)(value >> 8);
    p[3] = (uint8_t)value;
}

static int expect_u32(const char* name, uint32_t actual, uint32_t expected)
{
    if (actual == expected)
        return 0;
    fprintf(stderr, "FAIL: %s actual=0x%08X expected=0x%08X\n",
            name, actual, expected);
    return 1;
}

int main(void)
{
    uint8_t raw[PS3_SAVEDATA_FILESET_SIZE];
    memset(raw, 0, sizeof(raw));
    put_be32(raw + PS3_SAVEDATA_FILESET_OPERATION, CELL_SAVEDATA_FILEOP_WRITE);
    put_be32(raw + PS3_SAVEDATA_FILESET_TYPE, CELL_SAVEDATA_FILETYPE_NORMALFILE);
    put_be32(raw + PS3_SAVEDATA_FILESET_NAME, 0x01234560u);
    put_be32(raw + PS3_SAVEDATA_FILESET_OFFSET, 0x00000120u);
    put_be32(raw + PS3_SAVEDATA_FILESET_SIZE_BYTES, 0x00002000u);
    put_be32(raw + PS3_SAVEDATA_FILESET_BUFFER_SIZE, 0x00004000u);
    put_be32(raw + PS3_SAVEDATA_FILESET_BUFFER, 0x03000000u);

    Ps3SaveDataGuestFileRequest request = ps3_savedata_decode_file_request(raw);
    int failed = 0;
    failed |= expect_u32("OK_NEXT", CELL_SAVEDATA_CBRESULT_OK_NEXT, 0u);
    failed |= expect_u32("OK_LAST", CELL_SAVEDATA_CBRESULT_OK_LAST, 1u);
    failed |= expect_u32("OK_LAST_NOCONFIRM",
                         CELL_SAVEDATA_CBRESULT_OK_LAST_NOCONFIRM, 2u);
    failed |= expect_u32("CBResult ABI size", PS3_SAVEDATA_CBRESULT_SIZE, 20u);
    failed |= expect_u32("StatGet ABI size", PS3_SAVEDATA_STATGET_SIZE, 1704u);
    failed |= expect_u32("FileSet ABI size", PS3_SAVEDATA_FILESET_SIZE, 48u);
    failed |= expect_u32("SetBuf ABI size", PS3_SAVEDATA_SETBUF_SIZE, 40u);
    failed |= expect_u32("FileStat ABI size", PS3_SAVEDATA_FILESTAT_SIZE, 56u);
    failed |= expect_u32("operation", request.operation, CELL_SAVEDATA_FILEOP_WRITE);
    failed |= expect_u32("file type", request.file_type,
                         CELL_SAVEDATA_FILETYPE_NORMALFILE);
    failed |= expect_u32("name EA", request.name_ea, 0x01234560u);
    failed |= expect_u32("offset", request.offset, 0x00000120u);
    failed |= expect_u32("size", request.size, 0x00002000u);
    failed |= expect_u32("buffer size", request.buffer_size, 0x00004000u);
    failed |= expect_u32("buffer EA", request.buffer_ea, 0x03000000u);

    uint8_t set_buf_raw[PS3_SAVEDATA_SETBUF_SIZE];
    memset(set_buf_raw, 0, sizeof(set_buf_raw));
    put_be32(set_buf_raw + PS3_SAVEDATA_SETBUF_FILE_LIST_MAX, 64u);
    put_be32(set_buf_raw + PS3_SAVEDATA_SETBUF_BUFFER_SIZE, 0x2000u);
    put_be32(set_buf_raw + PS3_SAVEDATA_SETBUF_BUFFER, 0x04000000u);
    Ps3SaveDataGuestSetBuf set_buf = ps3_savedata_decode_set_buf(set_buf_raw);
    failed |= expect_u32("fileListMax", set_buf.file_list_max, 64u);
    failed |= expect_u32("set buffer size", set_buf.buffer_size, 0x2000u);
    failed |= expect_u32("set buffer EA", set_buf.buffer_ea, 0x04000000u);

    if (failed)
        return 1;
    puts("PASS: PS3 savedata callback constants and 32-bit guest ABI");
    return 0;
}
