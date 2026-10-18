"""Insert bounded loader-result diagnostics without decoding/reformatting PPU bytes."""
from pathlib import Path

SITES = {
    "001EF9C0": (
        ("001EF9FC", "000BBCB0"), ("001EFA18", "000D01C0"),
        ("001EFA2C", "00091550"), ("001EFA50", "0019C470"),
        ("001EFAD0", "000F8CF0"), ("001EFAE8", "00085988"),
        ("001EFB0C", "001E1898"),
    ),
    "001EFC5C": (
        ("001EFCA8", "001E1854"), ("001EFCC4", "000D01C0"),
        ("001EFD08", "0019C4C8"), ("001EFD30", "00091584"),
        ("001EFD8C", "000F8CF0"), ("001EFDB8", "00085AF0"),
        ("001EFDE8", "000C9F20"),
    ),
}


def injection(lr, callee, eol):
    return ("        { extern void tj_survival_loader_result_trace(ppu_context*, uint32_t, uint32_t); "
            "tj_survival_loader_result_trace(ctx, 0x%s, 0x%s); } /* tj-loader-result */"
            % (lr, callee)).encode("ascii") + eol


def patch_bytes(original):
    patched = original
    inserted = []
    for owner, sites in SITES.items():
        signature = ("void func_%s(ppu_context* ctx) {" % owner).encode("ascii")
        if signature not in patched:
            continue
        if patched.count(signature) != 1:
            raise ValueError("ambiguous loader function " + owner)
        start = patched.index(signature)
        end = patched.find(b"\nvoid func_", start + len(signature))
        if end < 0:
            end = len(patched)
        block = patched[start:end]
        for lr, callee in sites:
            call = ("        ctx->lr = 0x%s; func_%s(ctx); DRAIN_TRAMPOLINE(ctx);"
                    % (lr, callee)).encode("ascii")
            if block.count(call) != 1:
                raise ValueError("expected one original call at " + lr)
            at = block.index(call) + len(call)
            eol = b"\r\n" if block[at:at + 2] == b"\r\n" else b"\n"
            if block[at:at + len(eol)] != eol:
                raise ValueError("unexpected call suffix at " + lr)
            add = injection(lr, callee, eol)
            at += len(eol)
            if block[at:at + len(add)] == add:
                continue
            if add in block:
                raise ValueError("misplaced loader trace at " + lr)
            block = block[:at] + add + block[at:]
            inserted.append(add)
        patched = patched[:start] + block + patched[end:]
    restored = patched
    for add in inserted:
        if restored.count(add) != 1:
            raise ValueError("ambiguous inserted diagnostic")
        restored = restored.replace(add, b"", 1)
    if restored != original:
        raise ValueError("loader patch modified bytes outside its insertions")
    return patched, len(inserted)


def repair(path, apply=False):
    path = Path(path)
    original = path.read_bytes()
    patched, count = patch_bytes(original)
    if count and apply:
        path.write_bytes(patched)
    return count
