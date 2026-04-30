#!/usr/bin/env python3
"""
Scan the ELF data segment for OPD entries and add their code targets to
the function list.

PS3 OPDs are 12-byte function descriptors stored in `.opd` (or in the
data segment). Layout:
    [0..4]   uint32_t function_address (PPC code entry, big-endian)
    [4..8]   uint32_t toc (constant 0x00359220 for single-module TJ)
    [8..12]  uint32_t env (usually 0)

The lifter's static call-graph analysis discovers most functions via direct
`bl` calls, but functions only reachable through vtables / function pointer
tables are missed. They show up at runtime as "indirect call to unmapped
address 0x000XXXXX" warnings (ppc_indirect_call falls through the dispatch
search).

This pass walks the data segment, finds every OPD-shaped entry pointing
into the code segment, and merges those code addresses into functions.json
so the next re-lift can generate mid-entry wrappers (or whole new function
bodies) for them.

Usage:
    python scripts/scan_opds.py
        [--in  generated/functions_final.json]
        [--out generated/functions_with_opds.json]
        [--elf input/EBOOT.ELF]
        [--toc 0x00359220]
        [--code-start 0x10000]
        [--code-end 0x336268]
        [--data-start 0x340000]
        [--data-size 0x8CE208]
"""
import argparse
import json
import struct
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent


def read_segment(elf_path: Path, vaddr: int, size: int) -> bytes:
    """Find the program header containing `vaddr` and return `size` bytes
    starting at that vaddr."""
    with open(elf_path, "rb") as f:
        data = f.read()
    if data[:4] != b"\x7fELF":
        raise RuntimeError(f"{elf_path} is not an ELF")
    is64 = data[4] == 2
    big = data[5] == 2
    end = ">" if big else "<"
    if is64:
        e_phoff   = struct.unpack(end + "Q", data[32:40])[0]
        e_phentsz = struct.unpack(end + "H", data[54:56])[0]
        e_phnum   = struct.unpack(end + "H", data[56:58])[0]
    else:
        e_phoff   = struct.unpack(end + "I", data[28:32])[0]
        e_phentsz = struct.unpack(end + "H", data[42:44])[0]
        e_phnum   = struct.unpack(end + "H", data[44:46])[0]

    for i in range(e_phnum):
        off = e_phoff + i * e_phentsz
        if is64:
            p_type, p_flags, p_offset, p_vaddr = struct.unpack(
                end + "IIQQ", data[off:off+24])
            p_filesz = struct.unpack(end + "Q", data[off+32:off+40])[0]
        else:
            p_type, p_offset, p_vaddr, _paddr, p_filesz = struct.unpack(
                end + "IIIII", data[off:off+20])
        if p_type != 1:  # PT_LOAD
            continue
        if p_vaddr <= vaddr < p_vaddr + p_filesz:
            file_off = p_offset + (vaddr - p_vaddr)
            avail = p_filesz - (vaddr - p_vaddr)
            return data[file_off:file_off + min(size, avail)]
    raise RuntimeError(f"vaddr 0x{vaddr:X} not found in any PT_LOAD segment")


def scan_opds(seg: bytes, base: int,
              code_start: int, code_end: int,
              toc_value: int) -> set[int]:
    """Find OPD entries in `seg`. An OPD looks like:
       <code_addr in [code_start, code_end)> <toc> <env>
       all big-endian uint32. Returns the set of unique code addresses.
    """
    found: set[int] = set()
    # OPDs are 12-byte aligned, so step by 4 (some land at non-12 boundaries)
    end = len(seg) - 8
    for off in range(0, end, 4):
        func_addr = int.from_bytes(seg[off:off+4], "big")
        toc       = int.from_bytes(seg[off+4:off+8], "big")
        if toc != toc_value:
            continue
        if not (code_start <= func_addr < code_end):
            continue
        if func_addr & 0x3:  # PPC instructions are 4-byte aligned
            continue
        found.add(func_addr)
    return found


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--in",  dest="inp",  default=str(PROJECT / "generated/functions_final.json"))
    p.add_argument("--out",              default=str(PROJECT / "generated/functions_with_opds.json"))
    p.add_argument("--elf",              default=str(PROJECT / "input/EBOOT.ELF"))
    p.add_argument("--toc",        type=lambda s: int(s, 0), default=0x00359220)
    p.add_argument("--code-start", type=lambda s: int(s, 0), default=0x10000)
    p.add_argument("--code-end",   type=lambda s: int(s, 0), default=0x336268)
    p.add_argument("--data-start", type=lambda s: int(s, 0), default=0x340000)
    p.add_argument("--data-size",  type=lambda s: int(s, 0), default=0x8CE208)
    args = p.parse_args()

    print(f"Reading data segment 0x{args.data_start:X}..0x{args.data_start + args.data_size:X}")
    seg = read_segment(Path(args.elf), args.data_start, args.data_size)
    print(f"  read {len(seg)} bytes")

    print(f"Scanning for OPD entries (code 0x{args.code_start:X}..0x{args.code_end:X}, toc 0x{args.toc:X})")
    opd_targets = scan_opds(seg, args.data_start,
                            args.code_start, args.code_end, args.toc)
    print(f"  found {len(opd_targets)} unique OPD code targets")

    with open(args.inp) as f:
        fns = json.load(f)
    existing = {int(str(e["start"]), 0) for e in fns}
    print(f"  existing functions: {len(existing)}")

    new = sorted(opd_targets - existing)
    print(f"  new (not in existing): {len(new)}")

    # Build address -> end map for finding parent ranges
    by_start = {}
    sorted_starts = sorted(existing)
    for e in fns:
        s = int(str(e["start"]), 0)
        by_start[s] = int(str(e["end"]), 0)

    # Classify new targets
    inside_count = 0
    standalone = 0
    new_entries = []
    import bisect
    for addr in new:
        i = bisect.bisect_right(sorted_starts, addr)
        if i == 0:
            new_entries.append({"start": f"0x{addr:08X}", "end": f"0x{addr+4:08X}"})
            standalone += 1
            continue
        parent = sorted_starts[i-1]
        parent_end = by_start[parent]
        if addr < parent_end:
            # Inside existing function — emit a tiny entry so the lifter
            # generates a tail-entry wrapper.
            new_entries.append({"start": f"0x{addr:08X}", "end": f"0x{addr+4:08X}"})
            inside_count += 1
        else:
            # Between functions — likely a standalone function whose extent
            # we don't know. Use a 4-byte stub; the lifter will scan from
            # there until it hits a return or the next function start.
            next_start = sorted_starts[i] if i < len(sorted_starts) else args.code_end
            new_entries.append({"start": f"0x{addr:08X}", "end": f"0x{min(addr+0x100, next_start):08X}"})
            standalone += 1

    print(f"  new entries: {inside_count} mid-function, {standalone} standalone")

    merged = sorted(fns + new_entries, key=lambda e: int(str(e["start"]), 0))
    print(f"  total: {len(merged)} entries")

    with open(args.out, "w") as f:
        json.dump(merged, f, indent=2)
    print(f"Wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
