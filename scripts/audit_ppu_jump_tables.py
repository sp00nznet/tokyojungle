"""Read-only classification of generated .word placeholders against the exact ELF."""
import bisect
import hashlib
from pathlib import Path
import re
import struct

ROOT = Path(__file__).resolve().parents[1]
ELF = Path(__file__).resolve().parents[1] / "input" / "EBOOT.ELF"
# The original game identity, not the generated source identity.
ELF_SHA = "6d253f6429485b43b816fd6884611377626ee95db5dd9200ea58395c93a0812d"
WORD = rb"[ \t]*/\* TODO: \.word 0x[0-9A-Fa-f]{8} \*/;[ \t]*\r?\n"
RUN = re.compile(rb"(?m)^(?:" + WORD + rb")+")
VALUE = re.compile(rb"TODO: \.word 0x([0-9A-Fa-f]{8})")
FUNCTION = re.compile(rb"(?m)^void func_([0-9A-F]{8})\(ppu_context\* ctx\) \{")
CASE = re.compile(rb"case 0x([0-9A-F]{8})u: goto loc_([0-9A-F]{8});")
LABEL = re.compile(rb"(?m)^loc_([0-9A-F]{8}):")


def elf_reader(data):
    phoff = struct.unpack_from(">Q", data, 32)[0]
    entsize, count = struct.unpack_from(">HH", data, 54)
    segments = [struct.unpack_from(">IIQQQQQQ", data, phoff + i * entsize)
                for i in range(count)]

    def read(address, size):
        for kind, flags, offset, start, _, length, _, _ in segments:
            if kind == 1 and flags & 1 and start <= address and address + size <= start + length:
                return data[offset + address - start:offset + address - start + size]
        return None
    return read


def classify(previous, values, function, labels, read):
    if not previous.startswith(b"switch ((uint32_t)ctx->ctr)") or not previous.endswith(b"} return;"):
        return None, "no unconditional generated switch predecessor"
    pairs = CASE.findall(previous)
    if not pairs or any(target != label for target, label in pairs):
        return None, "invalid switch destinations"
    targets = {int(target, 16) for target, _ in pairs}
    if not targets <= labels:
        return None, "destination label missing from owning function"
    payload = struct.pack(">" + "I" * len(values), *values)
    candidates = []
    for target in targets:
        base = (target - values[0]) & 0xFFFFFFFF
        if base & 3 or base < function:
            continue
        if read(base, len(payload)) != payload or read(base - 4, 4) != b"\x4E\x80\x04\x20":
            continue
        if all(((base + value) & 0xFFFFFFFF) in targets for value in values):
            candidates.append(base)
    if len(candidates) != 1:
        return None, f"expected one original bctr/table match, found {len(candidates)}"
    return candidates[0], None


def classify_mixed(previous, values, function, end_address, labels, read):
    """Some negative table offsets decode as FP operations, splitting .word runs.

    The caller requires an unconditional switch and no intervening source label.
    Validate the whole original bctr-to-next-label span as offsets to real cases.
    """
    if not previous.startswith(b"switch ((uint32_t)ctx->ctr)") or not previous.endswith(b"} return;"):
        return None, "no unconditional generated switch predecessor"
    pairs = CASE.findall(previous)
    if not pairs or any(target != label for target, label in pairs):
        return None, "invalid switch destinations"
    targets = {int(target, 16) for target, _ in pairs}
    if not targets <= labels:
        return None, "destination label missing from owning function"
    code = read(function, end_address - function) if end_address > function else None
    if not code:
        return None, "original function-to-label bytes unavailable"
    candidates = []
    for offset in range(0, len(code) - 4, 4):
        if code[offset:offset + 4] != b"\x4E\x80\x04\x20":
            continue
        base = function + offset + 4
        table = code[offset + 4:]
        if len(table) % 4:
            continue
        original = [word[0] for word in struct.iter_unpack(">I", table)]
        if not all(((base + word) & 0xFFFFFFFF) in targets for word in original):
            continue
        position = 0
        for word in original:
            if position < len(values) and word == values[position]:
                position += 1
        if position == len(values):
            candidates.append(base)
    if len(candidates) != 1:
        return None, f"expected one complete original jump table, found {len(candidates)}"
    return candidates[0], None


def self_check():
    previous = (b"switch ((uint32_t)ctx->ctr) { case 0x00000140u: goto loc_00000140;"
                b"case 0x00000144u: goto loc_00000144; default: ps3_indirect_call(ctx); return; } return;")
    values = [0x20, 0x24, 0x20]
    memory = {0x11C: b"\x4E\x80\x04\x20", 0x120: struct.pack(">III", *values)}
    read = lambda address, size: memory.get(address, b"")[:size]
    labels = {0x140, 0x144}
    assert classify(previous, values, 0x100, labels, read) == (0x120, None)
    assert classify(previous, values, 0x100, {0x140}, read)[0] is None
    assert classify(previous, [0x20, 0x28, 0x20], 0x100, labels, read)[0] is None
    assert classify(previous.removesuffix(b" return;"), values, 0x100, labels, read)[0] is None
    memory[0x11C] = b"\x60\x00\x00\x00"
    assert classify(previous, values, 0x100, labels, read)[0] is None
    code = bytearray(b"\x60\x00\x00\x00" * 11)
    code[0x1C:0x20] = b"\x4E\x80\x04\x20"
    code[0x20:0x2C] = struct.pack(">III", *values)
    read_mixed = lambda address, size: code[address - 0x100:address - 0x100 + size]
    assert classify_mixed(previous, [0x24], 0x100, 0x12C, labels, read_mixed) == (0x120, None)
    assert classify_mixed(previous, [0x28], 0x100, 0x12C, labels, read_mixed)[0] is None
    code[0x28:0x2C] = struct.pack(">I", 0x28)
    assert classify_mixed(previous, [0x24], 0x100, 0x12C, labels, read_mixed)[0] is None


def main():
    self_check()
    elf = ELF.read_bytes()
    if hashlib.sha256(elf).hexdigest() != ELF_SHA:
        raise RuntimeError("Original ELF identity changed; audit refused")
    read = elf_reader(elf)
    total = covered = tables = 0
    unknown = []
    for path in sorted((ROOT / "generated").glob("ppu_recomp_*.cpp")):
        data = path.read_bytes()
        functions = list(FUNCTION.finditer(data))
        positions = [match.start() for match in functions]
        label_cache = {}
        mixed_cache = {}
        verified_tables = set()
        file_total = len(VALUE.findall(data))
        file_covered = file_tables = 0
        for run in RUN.finditer(data):
            values = [int(value, 16) for value in VALUE.findall(run[0])]
            owner = bisect.bisect_right(positions, run.start()) - 1
            if owner < 0:
                unknown.append((path.name, 0, len(values), "no owning function"))
                continue
            function = int(functions[owner][1], 16)
            if owner not in label_cache:
                end = positions[owner + 1] if owner + 1 < len(positions) else len(data)
                label_cache[owner] = {int(label, 16) for label in re.findall(
                    rb"(?m)^loc_([0-9A-F]{8}):", data[positions[owner]:end])}
            previous_start = data.rfind(b"\n", 0, run.start() - 1) + 1
            previous = data[previous_start:run.start()].strip()
            base, error = classify(previous, values, function, label_cache[owner], read)
            if error:
                switch = data.rfind(b"switch ((uint32_t)ctx->ctr)", positions[owner], run.start())
                end = positions[owner + 1] if owner + 1 < len(positions) else len(data)
                next_label = LABEL.search(data, switch, end) if switch >= 0 else None
                if next_label and next_label.start() >= run.end():
                    if switch not in mixed_cache:
                        line_end = data.find(b"\n", switch)
                        block_values = [int(value, 16) for value in VALUE.findall(
                            data[line_end:next_label.start()])]
                        mixed_cache[switch] = classify_mixed(
                            data[switch:line_end].strip(), block_values, function,
                            int(next_label[1], 16), label_cache[owner], read)
                    base, error = mixed_cache[switch]
            if error:
                unknown.append((path.name, function, len(values), error))
            else:
                file_covered += len(values)
                verified_tables.add(base)
        file_tables = len(verified_tables)
        print(f"{path.name}: {file_covered}/{file_total} .word entries confirmed as jump-table data; {file_tables} tables")
        total += file_total
        covered += file_covered
        tables += file_tables
    print(f"ELF-backed jump-table data: {covered}/{total} entries, {tables} tables; classifier checks:8 PASS")
    for path, function, count, error in unknown[:12]:
        print(f"UNCLASSIFIED {path} func_{function:08X} entries={count}: {error}")
    if covered != total:
        print("Unclassified does not mean missing instructions; inspect before changing source.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
