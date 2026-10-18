#include "jobchain_joblist.h"

#include <stdio.h>
#include <string.h>

static int check(int condition, const char* message)
{
    if (condition) return 1;
    fprintf(stderr, "FAIL: %s\n", message);
    return 0;
}

static void write_be32(unsigned char* p, unsigned value)
{
    p[0] = (unsigned char)(value >> 24);
    p[1] = (unsigned char)(value >> 16);
    p[2] = (unsigned char)(value >> 8);
    p[3] = (unsigned char)value;
}

typedef struct capture {
    unsigned calls;
    unsigned ea[8];
    unsigned size[8];
    unsigned index[8];
} capture;

typedef struct header_source {
    unsigned ea;
    unsigned char bytes[JOBCHAIN_JOBLIST_HEADER_SIZE];
} header_source;

static int read_header(void* opaque, uint32_t ea,
                       uint8_t out[JOBCHAIN_JOBLIST_HEADER_SIZE])
{
    header_source* source = (header_source*)opaque;
    if (ea != source->ea) return 0;
    memcpy(out, source->bytes, sizeof(source->bytes));
    return 1;
}

static int capture_job(void* opaque, uint32_t ea, uint32_t size, uint32_t index)
{
    capture* result = (capture*)opaque;
    if (result->calls >= 8) return 1;
    result->ea[result->calls] = ea;
    result->size[result->calls] = size;
    result->index[result->calls] = index;
    ++result->calls;
    return 0;
}

typedef struct stop_after {
    unsigned checks;
    unsigned stop_on_check;
} stop_after;

static int should_stop(void* opaque)
{
    stop_after* state = (stop_after*)opaque;
    return ++state->checks >= state->stop_on_check;
}

int main(void)
{
    header_source source;
    jobchain_joblist list;
    capture seen = {0};
    uint32_t jobs = 0;

    memset(&source, 0, sizeof(source));
    source.ea = 0x4000;
    write_be32(source.bytes + 0, 2);
    write_be32(source.bytes + 4, 0x100);
    write_be32(source.bytes + 12, 0x2000);

    if (!check(jobchain_dispatch_command(0x1800, 0x80, 0x10000, &jobs,
                                         JOBCHAIN_JOBCHAIN_JOB_BUDGET,
                                         read_header, &source, NULL, NULL,
                                         capture_job, &seen) == JOBCHAIN_JOB_DISPATCHED,
               "direct JOB must remain dispatchable")) return 1;
    if (!check(jobchain_dispatch_command(0x4006, 0x80, 0x10000, &jobs,
                                         JOBCHAIN_JOBCHAIN_JOB_BUDGET,
                                         read_header, &source, NULL, NULL,
                                         capture_job, &seen) == JOBCHAIN_JOB_DISPATCHED,
               "two-descriptor JOBLIST must dispatch")) return 1;
    if (!check(seen.calls == 3 && seen.ea[0] == 0x1800 && seen.size[0] == 0x80 &&
               seen.index[0] == 0 && seen.ea[1] == 0x2000 && seen.size[1] == 0x100 &&
               seen.index[1] == 1 && seen.ea[2] == 0x2100 && seen.size[2] == 0x100 &&
               seen.index[2] == 2 && jobs == 3,
               "direct and list jobs must preserve ordered EA, size, and indices")) return 1;

    memset(source.bytes, 0, sizeof(source.bytes));
    source.ea = 0x4010;
    if (!check(jobchain_dispatch_command(0x4016, 0x80, 0x10000, &jobs,
                                         JOBCHAIN_JOBCHAIN_JOB_BUDGET,
                                         read_header, &source, NULL, NULL,
                                         capture_job, &seen) == JOBCHAIN_JOB_NOOP &&
               seen.calls == 3 && jobs == 3,
               "zero-count JOBLIST must be valid no-op without size or EA")) return 1;

    write_be32(source.bytes + 0, 2);
    write_be32(source.bytes + 4, 0x40);
    write_be32(source.bytes + 12, 0x2000);
    if (!check(jobchain_joblist_decode(source.bytes, 0x10000, &list),
               "0x40 descriptor size must be valid")) return 1;
    write_be32(source.bytes + 4, 0x400);
    if (!check(jobchain_joblist_decode(source.bytes, 0x10000, &list),
               "0x400 descriptor size must be valid")) return 1;
    write_be32(source.bytes + 4, 0xC0);
    if (!check(!jobchain_joblist_decode(source.bytes, 0x10000, &list),
               "non-0x80-multiple descriptor size must fail")) return 1;
    write_be32(source.bytes + 4, 0x480);
    if (!check(!jobchain_joblist_decode(source.bytes, 0x10000, &list),
               "oversized descriptor must fail")) return 1;
    write_be32(source.bytes + 4, 0x100);
    write_be32(source.bytes + 12, 0x2008);
    if (!check(!jobchain_joblist_decode(source.bytes, 0x10000, &list),
               "unaligned descriptor EA must fail")) return 1;
    write_be32(source.bytes + 12, 0x2000);
    if (!check(jobchain_dispatch_command(0x400E, 0x80, 0x10000, &jobs,
                                         JOBCHAIN_JOBCHAIN_JOB_BUDGET,
                                         read_header, &source, NULL, NULL,
                                         capture_job, &seen) == JOBCHAIN_JOB_INVALID,
               "unaligned header EA must fail")) return 1;

    jobs = JOBCHAIN_JOBCHAIN_JOB_BUDGET - 1;
    seen.calls = 0;
    write_be32(source.bytes + 0, 2);
    if (!check(jobchain_dispatch_command(0x4016, 0x80, 0x10000, &jobs,
                                         JOBCHAIN_JOBCHAIN_JOB_BUDGET,
                                         read_header, &source, NULL, NULL,
                                         capture_job, &seen) == JOBCHAIN_JOB_BUDGET_EXHAUSTED &&
               seen.calls == 1 && seen.index[0] == JOBCHAIN_JOBCHAIN_JOB_BUDGET - 1 &&
               jobs == JOBCHAIN_JOBCHAIN_JOB_BUDGET,
               "JOBLIST must charge each entry against aggregate budget")) return 1;

    {
        stop_after stop = {0, 2};
        jobs = 0;
        seen.calls = 0;
        if (!check(jobchain_dispatch_command(0x4016, 0x80, 0x10000, &jobs,
                                             JOBCHAIN_JOBCHAIN_JOB_BUDGET,
                                             read_header, &source, should_stop, &stop,
                                             capture_job, &seen) == JOBCHAIN_JOB_STOPPED &&
                   seen.calls == 1 && jobs == 1,
                   "JOBLIST must check shutdown before each descriptor")) return 1;
    }

    puts("jobchain JOBLIST regression passed");
    return 0;
}
