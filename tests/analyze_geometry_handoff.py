"""Compare paired SPU output and actual indexed uploads without launching the game.

This reports byte/position provenance, not visual or gameplay correctness.
Requires the opt-in before/after-pack, output, index and vertex captures.
"""

import argparse
import json
from pathlib import Path
import re
import struct


def analyze(probe: Path) -> dict:
    log = (probe / "tokyojungle.stderr.log").read_text(errors="replace")
    jobs = []
    for output in sorted(probe.glob("geometry-output-phase2-hit*-ea*.bin")):
        match = re.fullmatch(r"geometry-output-phase2-hit(\d+)-ea([0-9A-F]+)\.bin", output.name)
        hit, ea = int(match[1]), int(match[2], 16)
        before = (probe / f"geometry-before-pack-phase2-hit{hit}.bin").read_bytes()
        if len(before) != 2048 + 262144:
            raise ValueError(f"Truncated SPU snapshot: {hit}")
        obj = struct.unpack_from("<I", before, 82 * 16)[0]
        count = struct.unpack_from("<I", before, 81 * 16)[0]
        header = struct.unpack_from(">32I", before, 2048 + obj)
        data = output.read_bytes()
        if count * 2 > len(data):
            raise ValueError(f"Index prefix exceeds allocation: {hit}")
        jobs.append({"hit": hit, "ea": ea, "indices": count,
                     "vertices": header[2] >> 16, "data": data})

    draws = []
    for indices_path in sorted((probe / "indexed-geometry").glob("indices_*_fp*.bin")):
        match = re.fullmatch(r"indices_(\d+)_fp([0-9A-F]+)\.bin", indices_path.name)
        tag, fp = int(match[1]), match[2]
        prefix = rf"\[TJ-IDX#{tag}\] "
        source = re.search(prefix + r"index .* ea=([0-9A-F]+) type=(u16be|u32be) source_count=(\d+)", log)
        attribute = re.search(prefix + r"a0 en=(\d+) stride=(\d+) type=(\d+) size=(\d+) off=([0-9A-F]+) freq=(\d+)", log)
        sample = re.search(prefix + r"a0\[0\] index=(\d+) elem=(\d+) ea=([0-9A-F]+)", log)
        entry = re.search(prefix + r"entry prim=(\d+) first=(\d+) count=(\d+)", log)
        record = {"draw": tag, "fp": fp}
        draws.append(record)
        if not all((source, attribute, sample, entry)):
            record["unsupported"] = "Missing source or attribute metadata"
            continue
        enabled, stride, kind, size = map(int, attribute.groups()[:4])
        if int(entry[1]) != 5 or not enabled or kind != 2 or size != 3 or int(attribute[6]) > 1:
            record["unsupported"] = "Only uninstanced F32x3 indexed triangle captures are compared"
            continue
        index_bytes = indices_path.read_bytes()
        width = 2 if source[2] == "u16be" else 4
        count = int(source[3])
        if len(index_bytes) != count * width:
            raise ValueError(f"Truncated index capture: {tag}")
        indices = struct.unpack(f">{count}{'H' if width == 2 else 'I'}", index_bytes)
        base_index = int(sample[1]) - indices[0]
        position_ea = int(sample[3], 16) - int(sample[2]) * stride
        vertex_bytes = (probe / "indexed-geometry" / f"vertices_{tag}_fp{fp}.bin").read_bytes()
        if len(vertex_bytes) != count * 256:
            record["unsupported"] = "Uploaded vertex count differs from source count"
            continue
        uploaded = [struct.unpack_from("<3f", vertex_bytes, i * 256) for i in range(count)]
        record.update({"indices": count, "index_ea": source[1],
                       "position_ea": f"{position_ea:08X}", "base_index": base_index,
                       "matches": []})
        for job in jobs:
            data, ea = job["data"], job["ea"]
            index_offset = int(source[1], 16) + int(entry[2]) * width - ea
            same_indices = (0 <= index_offset <= len(data) - len(index_bytes) and
                            data[index_offset:index_offset + len(index_bytes)] == index_bytes)
            at_bound_address = at_packed_position = readable = 0
            # Format FF in the captured animal jobs uses a 16-byte vertex
            # stride and an index prefix rounded to 16 bytes. Report this
            # candidate separately from the actual guest-address comparison.
            packed_offset = (job["indices"] * 2 + 15) & ~15
            for i, raw_index in enumerate(indices):
                vi = raw_index + base_index
                off = position_ea - ea + vi * stride
                if 0 <= off <= len(data) - 12:
                    readable += 1
                    at_bound_address += struct.unpack_from(">3f", data, off) == uploaded[i]
                off = packed_offset + vi * 16
                if 0 <= vi < job["vertices"] and off <= len(data) - 12:
                    at_packed_position += struct.unpack_from(">3f", data, off) == uploaded[i]
            record["matches"].append({"producer_hit": job["hit"],
                                      "same_index_bytes": same_indices,
                                      "readable_at_bound_address": readable,
                                      "positions_at_bound_address": at_bound_address,
                                      "positions_at_candidate_packed_offset": at_packed_position})
    return {"proof": "offline captured-byte provenance only",
            "jobs": [{k: v for k, v in j.items() if k != "data"} for j in jobs],
            "draws": draws}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("probe", type=Path)
    args = parser.parse_args()
    print(json.dumps(analyze(args.probe), indent=2))
