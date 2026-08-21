#!/usr/bin/env python3
"""Re-join C string literals that got split across two source lines.

A literal newline inside a C string is a syntax error. This happens when an
edit meant to emit an escaped newline emitted a real one instead. Detect a
line whose unescaped double-quotes are unbalanced, followed by a line that
starts with a quote, and splice them back together with an escaped newline.

Preserves CRLF if the file uses it.

    python scripts/fix_split_strings.py src/*.cpp src/*.h
"""
import io
import sys

BSL = chr(92)   # backslash
QUO = chr(34)   # double quote


def unescaped_quote_count(line):
    """Number of double-quotes not preceded by a backslash."""
    n = 0
    i = 0
    while i < len(line):
        if line[i] == BSL:
            i += 2
            continue
        if line[i] == QUO:
            n += 1
        i += 1
    return n


def fix(path):
    raw = io.open(path, "rb").read()
    crlf = raw.find(b"\r\n") >= 0
    text = raw.decode("utf-8")
    if crlf:
        text = text.replace("\r\n", "\n")
    lines = text.split("\n")

    out = []
    i = 0
    fixed = 0
    while i < len(lines):
        cur = lines[i]
        nxt = lines[i + 1] if i + 1 < len(lines) else None
        if (nxt is not None
                and unescaped_quote_count(cur) % 2 == 1
                and nxt.lstrip().startswith(QUO)):
            out.append(cur + BSL + "n" + nxt.lstrip())
            i += 2
            fixed += 1
            continue
        out.append(cur)
        i += 1

    if fixed:
        joined = "\n".join(out)
        if crlf:
            joined = joined.replace("\n", "\r\n")
        io.open(path, "wb").write(joined.encode("utf-8"))
    return fixed


def main():
    if len(sys.argv) < 2:
        print("usage: fix_split_strings.py FILE...", file=sys.stderr)
        return 2
    for path in sys.argv[1:]:
        print("%s: %d literal(s) rejoined" % (path, fix(path)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
