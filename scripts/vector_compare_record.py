"""Restore VMX floating compare Rc effects without relifting game code.

The manifest is derived from the verified original ELF. Every comparison,
including non-record forms, must retain its function, order, and operands.
Only the missing CR6 side effect is appended; vector results are unchanged.
"""
import argparse
import bisect
import collections
import hashlib
import json
from pathlib import Path
import re
import struct

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = Path(__file__).with_name("vector_compare_record.json")
ELF_SHA256 = "6d253f6429485b43b816fd6884611377626ee95db5dd9200ea58395c93a0812d"
EXPECTED = {"==": 423, ">=": 19, ">": 136}
EXPECTED_RECORD = {"==": 48, ">=": 19, ">": 105}
FUNC_RE = re.compile(rb"(?m)^void func_([0-9A-F]{8})\(ppu_context\* ctx\) \{")
CMP_RE = re.compile(
    rb"(?m)^[ \t]*(?P<body>\{ float a\[4\],b\[4\]; "
    rb"ppu_vldf4\(&ctx->vr\[(?P<a>\d+)\],a\); "
    rb"ppu_vldf4\(&ctx->vr\[(?P<b>\d+)\],b\); "
    rb"uint32_t\* d=\(uint32_t\*\)&ctx->vr\[(?P<d>\d+)\]; "
    rb"for\(int i=0;i<4;i\+\+\) d\[i\]=a\[i\](?P<op>==|>=|>)b\[i\]\?~0u:0; \})"
    rb"(?P<tail>[^\r\n]*)"
)


def collect_sources(files):
    sources, sites = {}, []
    for path in sorted(map(Path, files)):
        data = path.read_bytes()
        sources[path] = data
        functions = list(FUNC_RE.finditer(data))
        positions = [match.start() for match in functions]
        ordinal = collections.Counter()
        for match in CMP_RE.finditer(data):
            index = bisect.bisect_right(positions, match.start()) - 1
            if index < 0:
                raise ValueError("vector compare outside generated function")
            function = functions[index][1].decode()
            signature = [function, ordinal[function], match["op"].decode(),
                         int(match["d"]), int(match["a"]), int(match["b"])]
            ordinal[function] += 1
            sites.append((path, match, signature))
    return sources, sites


def record_suffix(vd, pc):
    return (
        " { const uint32_t* m=(const uint32_t*)&ctx->vr[%d]; "
        "uint32_t all=m[0]&m[1]&m[2]&m[3]; "
        "uint32_t any=m[0]|m[1]|m[2]|m[3]; "
        "ctx->cr=(ctx->cr&~0xF0u)|(all==0xFFFFFFFFu?0x80u:0u)|(any==0?0x20u:0u); "
        "} /* tj-ppu-vcmp-record pc=%08X */" % (vd, pc)
    ).encode()


def original_comparisons(elf_path):
    data = Path(elf_path).read_bytes()
    if hashlib.sha256(data).hexdigest() != ELF_SHA256:
        raise ValueError("original EBOOT.ELF SHA256 mismatch")
    if data[:6] != b"\x7fELF\x02\x02":
        raise ValueError("expected ELF64 big-endian image")
    phoff = struct.unpack_from(">Q", data, 32)[0]
    entsize, count = struct.unpack_from(">HH", data, 54)
    segments = []
    for index in range(count):
        kind, flags, offset, address, _, size, _, _ = struct.unpack_from(
            ">IIQQQQQQ", data, phoff + index * entsize)
        if kind == 1 and flags & 1:
            segments.append((address, size, offset))
    functions = json.loads((ROOT / "generated/EBOOT.functions.json").read_text())
    instructions = {}
    opcodes = {198: "==", 454: ">=", 710: ">"}
    for function in functions:
        start, end = int(function["start"], 16), int(function["end"], 16)
        for address, size, offset in segments:
            if address <= start < end <= address + size:
                for pc in range(start, end, 4):
                    word = struct.unpack_from(">I", data, offset + pc - address)[0]
                    if word >> 26 == 4 and (word & 1023) in opcodes:
                        instructions[pc] = [opcodes[word & 1023],
                                            (word >> 21) & 31,
                                            (word >> 16) & 31,
                                            (word >> 11) & 31,
                                            bool(word & 1024)]
                break
    return [(pc, *instructions[pc]) for pc in sorted(instructions)]


def make_manifest(files, elf_path):
    sources, sites = collect_sources(files)
    function_starts = sorted({int(m[1], 16) for data in sources.values()
                              for m in FUNC_RE.finditer(data)})
    instructions = original_comparisons(elf_path)
    if len(sites) != len(instructions) or len(sites) != 578:
        raise ValueError("original/generated float compare count mismatch: %d/%d" %
                         (len(instructions), len(sites)))
    entries = []
    for (_, match, signature), (pc, op, vd, va, vb, record) in zip(sites, instructions):
        if signature[2:] != [op, vd, va, vb]:
            raise ValueError("original/generated compare mismatch at %08X" % pc)
        if pc < int(signature[0], 16):
            raise ValueError("original compare precedes generated owning function")
        next_function = bisect.bisect_right(function_starts, int(signature[0], 16))
        if next_function < len(function_starts) and pc >= function_starts[next_function]:
            raise ValueError("original compare exceeds generated owning function")
        if match["tail"] not in (b"", record_suffix(vd, pc) if record else b""):
            raise ValueError("unexpected comparison suffix")
        entries.append({"signature": signature, "pc": "%08X" % pc, "record": record})
    result = {"elf_sha256": ELF_SHA256, "entries": entries}
    validate_manifest(result)
    return result


def validate_manifest(manifest):
    if manifest.get("elf_sha256") != ELF_SHA256:
        raise ValueError("vector compare manifest provenance mismatch")
    entries = manifest["entries"]
    if dict(collections.Counter(e["signature"][2] for e in entries)) != EXPECTED:
        raise ValueError("vector compare manifest cohort mismatch")
    if dict(collections.Counter(e["signature"][2] for e in entries if e["record"])) != EXPECTED_RECORD:
        raise ValueError("vector compare record cohort mismatch")
    sentinel = [e for e in entries if e["pc"] == "00133B64"]
    if len(sentinel) != 1 or not sentinel[0]["record"] or sentinel[0]["signature"][2:] != ["==", 0, 0, 28]:
        raise ValueError("FLT_MAX sentinel guard mapping mismatch")


def prepare_patches(files, manifest=None):
    manifest = manifest or json.loads(MANIFEST.read_text())
    validate_manifest(manifest)
    sources, sites = collect_sources(files)
    entries = manifest["entries"]
    if len(sites) != len(entries):
        raise ValueError("generated compare count changed; refusing partial repair")
    changes = collections.defaultdict(list)
    for (path, match, signature), entry in zip(sites, entries):
        if signature != entry["signature"]:
            raise ValueError("generated compare signature changed at %s" % entry["pc"])
        suffix = record_suffix(signature[3], int(entry["pc"], 16)) if entry["record"] else b""
        if match["tail"] == suffix:
            continue
        if match["tail"] != b"" or not entry["record"]:
            raise ValueError("unexpected generated compare modification at %s" % entry["pc"])
        changes[path].append((match.end("body"), suffix))
    prepared = {}
    for path, edits in changes.items():
        data = sources[path]
        chunks, cursor = [], 0
        for position, suffix in edits:
            chunks.extend((data[cursor:position], suffix))
            cursor = position
        chunks.append(data[cursor:])
        prepared[path] = b"".join(chunks)
    return prepared, sum(map(len, changes.values()))


def patch_files(files):
    # Validate every file before the first write. A partially applied repair is
    # safe to rerun; already marked instructions must match exactly.
    prepared, count = prepare_patches(files)
    for path, data in prepared.items():
        path.write_bytes(data)
    return count


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--make-manifest", metavar="ELF")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    files = sorted((ROOT / "generated").glob("ppu_recomp_*.cpp"))
    if args.make_manifest:
        if MANIFEST.exists():
            raise ValueError("manifest already exists; refusing overwrite")
        manifest = make_manifest(files, args.make_manifest)
        # Derived instruction metadata, not regenerated game source.
        MANIFEST.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        print("vector compare manifest: 578 mapped, 172 record instructions")
    if args.apply:
        print("vector compare record: %d patches" % patch_files(files))
    else:
        _, count = prepare_patches(files)
        print("vector compare record audit: %d patches pending" % count)


if __name__ == "__main__":
    main()
