"""Audit/repair only proven non-r0 computed branches emitted as link returns.

Uses pinned local image bytes and existing function bounds. Never relifts a
function or touches unrelated source bytes. Default is read-only.
"""
from pathlib import Path
import argparse
import hashlib
import json
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
TOOLS = Path(__file__).resolve().parents[2] / "ps3recomp" / "tools"
sys.path.insert(0, str(TOOLS))
from spu_disasm import disassemble_spu
from build_spu_workloads import fnv1a64
from spu_lifter import SPULifter, LiftedFunction, compute_link_returns, _bi_target_reg


def audit(name, fingerprint):
    folder = ROOT / "generated/spu_lifted" / name
    matches = list((ROOT / "generated/spu_images").glob(f"spujob_{fingerprint}_*.bin"))
    if len(matches) != 1:
        return None
    image = matches[0].read_bytes()
    if fnv1a64(image) != int(fingerprint, 16):
        raise RuntimeError("image fingerprint mismatch: " + name)
    entries = json.loads((folder / "functions.json").read_text())
    bounds = [(int(str(e["start"]), 0), int(str(e["end"]), 0)) for e in entries]
    decoded = disassemble_spu(image, 0)
    lifter = SPULifter(prefix=f"tj_{name}")
    lifter.link_return = compute_link_returns(decoded, bounds)
    source = folder / "spu_recomp.c"
    before = source.read_bytes()
    result = before
    changes, skipped = [], []
    for start, end in bounds:
        branches = [i for i in decoded if start <= i.addr < end and _bi_target_reg(i) is not None]
        for branch in branches:
            target = _bi_target_reg(branch)
            if target == "0" or branch.addr in lifter.link_return:
                continue
            pattern = (rb"^void \w+spu_func_%08X\(spu_context\* ctx\) \{\r?\n.*?^}\r?\n" % start)
            blocks = list(re.finditer(pattern, result, re.M | re.S))
            if len(blocks) != 1:
                skipped.append(f"{branch.addr:05X}: function shape")
                continue
            block = blocks[0]
            body = block.group()
            newline = b"\r\n" if b"\r\n" in body else b"\n"
            fn = LiftedFunction(start_addr=start, end_addr=end)
            new = b"        " + lifter._translate(branch, fn).encode() + newline
            if new in body:
                continue
            lifter.link_return.add(branch.addr)
            old = b"        " + lifter._translate(branch, fn).encode() + newline
            lifter.link_return.remove(branch.addr)
            if old not in body:
                skipped.append(f"{branch.addr:05X}: no matching old/new branch")
                continue
            if len(branches) != 1 or body.count(old) != 1:
                skipped.append(f"{branch.addr:05X}: ambiguous branch association")
                continue
            rewritten = body.replace(old, new, 1)
            result = result[:block.start()] + rewritten + result[block.end():]
            changes.append({"pc": f"0x{branch.addr:05X}", "raw": f"0x{branch.raw:08X}",
                            "register": target, "old": old.decode().strip(), "new": new.decode().strip()})
    return source, before, result, {"image": name, "fingerprint": fingerprint,
        "image_sha256": hashlib.sha256(image).hexdigest(), "changes": changes,
        "skipped": skipped, "before_sha256": hashlib.sha256(before).hexdigest(),
        "after_sha256": hashlib.sha256(result).hexdigest(), "added_bytes": len(result)-len(before)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--image", action="append", help="limit to named lifted image")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    registry = (ROOT / "src/gen/spu_workloads.c").read_text()
    fingerprints = dict(re.findall(r"#define TJ_(\w+)_FP\s+0x([0-9A-F]+)ULL", registry))
    results, edits, missing = [], [], []
    for folder in sorted((ROOT / "generated/spu_lifted").iterdir()):
        name = folder.name
        if not folder.is_dir() or (args.image and name not in args.image):
            continue
        fp = fingerprints.get(name.upper())
        item = audit(name, fp) if fp else None
        if item is None:
            missing.append(name)
            continue
        source, before, after, report = item
        results.append(report)
        if before != after:
            edits.append((source, before, after))
        print(f"{name}: {len(report['changes'])} repairs, {len(report['skipped'])} unmodified/unresolved")
        for change in report["changes"]:
            print(f"  {change['pc']} {change['raw']} bi r{change['register']}")
        for skipped in report["skipped"]:
            print("  SKIP " + skipped)
    if args.apply:
        for source, before, after in edits:
            if source.read_bytes() != before:
                raise RuntimeError("source changed during audit: " + str(source))
            source.write_bytes(after)
    if args.report:
        args.report.write_text(json.dumps({"applied": args.apply, "images": results,
                                          "missing_images": missing}, indent=2) + "\n")
    print(f"{'Applied' if args.apply else 'Read-only'}: {len(edits)} changed files; missing original image mapping: {missing}")


if __name__ == "__main__":
    main()
