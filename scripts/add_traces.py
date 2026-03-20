#!/usr/bin/env python3
"""Add trace calls to func_001F468C (game_init body)."""
import re
import sys

with open('generated/ppu_recomp.c', 'rb') as f:
    data = f.read()

marker = b'void func_001F468C(ppu_context* ctx) {'
idx = data.find(marker)
if idx == -1:
    print("ERROR: function not found")
    sys.exit(1)

# Find matching close brace
depth = 0
i = idx + len(marker)
while i < len(data):
    if data[i:i+1] == b'{':
        depth += 1
    elif data[i:i+1] == b'}':
        if depth == 0:
            break
        depth -= 1
    i += 1

body = data[idx + len(marker):i]

# Find all function call sites
pattern = rb'([ \t]+)(func_[0-9A-Fa-f]+)\(ctx\);'
calls = list(re.finditer(pattern, body))
print(f"Found {len(calls)} calls in func_001F468C")

# Insert traces (work backwards)
new_body = bytearray(body)
for n, m in enumerate(reversed(calls)):
    num = len(calls) - 1 - n
    fn = m.group(2).decode('ascii')
    # Build trace line as raw bytes: fprintf(stderr, "[TRACE] #N funcname\n");
    # The \n must be literal backslash + n (0x5c, 0x6e) in the C source
    trace = b'        fprintf(stderr, "[TRACE] #' + str(num).encode() + b' ' + fn.encode()
    trace += bytes([0x5c, 0x6e])  # literal \n for C
    trace += b'");\r\n'
    new_body[m.start():m.start()] = trace

out = data[:idx + len(marker)] + bytes(new_body) + data[i:]
with open('generated/ppu_recomp.c', 'wb') as f:
    f.write(out)

print(f"Added {len(calls)} traces")
