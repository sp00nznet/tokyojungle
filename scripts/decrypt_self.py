#!/usr/bin/env python3
"""
PS3 SELF Decryptor — Complete Implementation

Decrypts retail PS3 SELF (Signed ELF) files to plain ELF.
Handles NPDRM content with RAP-based klicensee derivation.

Algorithm (based on psdevwiki Certified File spec + RPCS3 unself.cpp):
  1. RAP → klicensee (content license key)
  2. klicensee → NPDRM decryption keys (erk + riv)
  3. Decrypt Encryption Root Header → metadata_key + metadata_iv
  4. Decrypt Certification Header + Segment Certs → per-segment keys
  5. Decrypt each data segment → reassemble plain ELF

References:
  - https://www.psdevwiki.com/ps3/Certified_File
  - https://www.psdevwiki.com/ps3/SELF_-_SPRX
  - https://www.psdevwiki.com/ps3/Keys
  - https://github.com/RPCS3/rpcs3/blob/master/rpcs3/Crypto/unself.cpp
"""

import struct
import sys
import zlib
from pathlib import Path
from Crypto.Cipher import AES


# ============================================================================
# Utility
# ============================================================================

def be16(d, o): return struct.unpack('>H', d[o:o+2])[0]
def be32(d, o): return struct.unpack('>I', d[o:o+4])[0]
def be64(d, o): return struct.unpack('>Q', d[o:o+8])[0]


def aes_cbc_dec(key, iv, data):
    return AES.new(key, AES.MODE_CBC, iv).decrypt(data)


def aes_ecb_dec(key, data):
    return AES.new(key, AES.MODE_ECB).decrypt(data)


def aes_ecb_enc(key, data):
    return AES.new(key, AES.MODE_ECB).encrypt(data)


def aes_ctr_dec(key, iv, data):
    """AES-128-CTR decryption (big-endian counter, PS3 style)."""
    out = bytearray(len(data))
    ctr = int.from_bytes(iv, 'big')
    for i in range(0, len(data), 16):
        ctr_bytes = ctr.to_bytes(16, 'big')
        keystream = AES.new(key, AES.MODE_ECB).encrypt(ctr_bytes)
        chunk = data[i:i+16]
        for j in range(len(chunk)):
            out[i+j] = chunk[j] ^ keystream[j]
        ctr += 1
    return bytes(out)


# ============================================================================
# PS3 Keys (publicly documented — psdevwiki.com/ps3/Keys, fail0verflow 2011)
# ============================================================================

# RAP conversion constants
RAP_KEY  = bytes.fromhex("8640B926C96C5E1F685BB66758E0EFFF")
RAP_PBOX = [0x0C, 0x03, 0x06, 0x04, 0x01, 0x0B, 0x0F, 0x08,
            0x02, 0x07, 0x00, 0x05, 0x0A, 0x0E, 0x0D, 0x09]
RAP_E1   = [0xA9, 0x3E, 0x1F, 0xD6, 0x7C, 0x55, 0xA3, 0x29,
            0xB7, 0x5F, 0xDD, 0xA6, 0x2A, 0x95, 0xC7, 0xA5]
RAP_E2   = [0x67, 0xD4, 0x5D, 0xA3, 0x29, 0x6D, 0x00, 0x6A,
            0x4E, 0x7C, 0x53, 0x7B, 0xF5, 0x53, 0x8C, 0x74]

# NPDRM keys
NP_KLIC_KEY  = bytes.fromhex("72F990788F9CFF745725F08E4C128387")
NP_RIF_KEY   = bytes.fromhex("B22095315FAAD9DF0B729D4A1124BBE0")
NP_KLIC_FREE = bytes(16)

# Loader keys for game executables (app_type=1, key_revision varied)
# Format: {key_rev: (erk_256, riv_128)}
# These are the published keys for retail SELF decryption
SELF_KEYS = {
    # Key revision 0x04 (older titles)
    0x04: (
        bytes.fromhex("A402D6B369C5D5A3804CF03F28C694A3AD5C285CBC947C77EE98A3B4B3CC0FA8"),
        bytes.fromhex("B3B6538BE3053AC07FEAC8D56E0B5311"),
    ),
    # Key revision 0x0A
    0x0A: (
        bytes.fromhex("A6ACD184F1E0B3670D8B44F76E8C1A08ABAC8A3ECCD8508DBF5E5E5F8D29B56E"),
        bytes.fromhex("CACF18AF8E3F1FCBE8B13D649CC81E93"),
    ),
    # Key revision 0x16 (most common for PSN titles ~2012-2013)
    0x16: (
        bytes.fromhex("7D50B8B0F4C4A67721BF80A5C233439304A2D96B4E06EF1A8C0FDB4B3DCA69A0"),
        bytes.fromhex("21D0FDA2CE4F71C04CFE1F2E5CC25B33"),
    ),
}

# Fallback: well-known key set for key_rev 0x16, app_type 1
DEFAULT_ERK = bytes.fromhex("7D50B8B0F4C4A67721BF80A5C233439304A2D96B4E06EF1A8C0FDB4B3DCA69A0")
DEFAULT_RIV = bytes.fromhex("21D0FDA2CE4F71C04CFE1F2E5CC25B33")


# ============================================================================
# RAP → Klicensee
# ============================================================================

def rap_to_klicensee(rap_bytes):
    """Convert 16-byte RAP file to klicensee."""
    key = bytearray(aes_ecb_dec(RAP_KEY, rap_bytes))

    for round_num in range(5):
        for i in range(16):
            p = RAP_PBOX[i]
            key[p] ^= RAP_E1[p]

        for i in range(15, 0, -1):
            key[i] ^= key[i - 1]

        tmp = bytearray(16)
        for i in range(16):
            tmp[RAP_PBOX[i]] = key[i]
        key = tmp

        for i in range(16):
            key[i] ^= RAP_E2[i]

    return bytes(key)


# ============================================================================
# SELF Parser & Decryptor
# ============================================================================

class SELFFile:
    def __init__(self, data):
        self.data = data
        self._parse()

    def _parse(self):
        d = self.data

        # --- SCE Header (0x20 bytes) ---
        self.magic      = be32(d, 0)
        self.version    = be32(d, 4)
        self.key_type   = be16(d, 8)
        self.category   = be16(d, 10)
        self.meta_off   = be32(d, 12)
        self.hdr_size   = be64(d, 16)
        self.data_size  = be64(d, 24)

        assert self.magic == 0x53434500, f"Not a SELF: 0x{self.magic:08X}"

        # --- Extended Header (SELF-specific, at 0x20) ---
        self.ext_hdr_ver = be64(d, 0x20)  # extended header version
        self.app_info_off = be64(d, 0x28)
        self.elf_off      = be64(d, 0x30)
        self.phdr_off     = be64(d, 0x38)
        self.shdr_off     = be64(d, 0x40)
        self.sec_info_off = be64(d, 0x48)
        self.sce_ver_off  = be64(d, 0x50)
        self.ctrl_info_off= be64(d, 0x58)
        self.ctrl_info_sz = be64(d, 0x60)
        # padding to 0x70

        # --- App Info (at app_info_off) ---
        ai = self.app_info_off
        self.auth_id    = be64(d, ai)
        self.vendor_id  = be32(d, ai + 8)
        self.app_type   = be32(d, ai + 12)
        self.app_ver    = be64(d, ai + 16)

        # --- ELF Header (at elf_off) ---
        eo = self.elf_off
        assert d[eo:eo+4] == b'\x7fELF', "ELF magic not found"
        self.elf_class  = d[eo + 4]  # 2 = 64-bit
        self.elf_data   = d[eo + 5]  # 2 = big-endian
        self.e_type     = be16(d, eo + 16)
        self.e_machine  = be16(d, eo + 18)
        self.e_entry    = be64(d, eo + 24)
        self.e_phoff    = be64(d, eo + 32)
        self.e_shoff    = be64(d, eo + 40)
        self.e_phnum    = be16(d, eo + 56)
        self.e_phentsize= be16(d, eo + 54)
        self.e_shnum    = be16(d, eo + 60)
        self.e_shentsize= be16(d, eo + 58)
        self.e_shstrndx = be16(d, eo + 62)

        # --- Program Headers (at phdr_off) ---
        self.phdrs = []
        for i in range(self.e_phnum):
            o = self.phdr_off + i * 0x38
            ph = {
                'p_type':   be32(d, o),
                'p_flags':  be32(d, o + 4),
                'p_offset': be64(d, o + 8),
                'p_vaddr':  be64(d, o + 16),
                'p_paddr':  be64(d, o + 24),
                'p_filesz': be64(d, o + 32),
                'p_memsz':  be64(d, o + 40),
                'p_align':  be64(d, o + 48),
            }
            self.phdrs.append(ph)

        # --- Section Info entries (at sec_info_off, one per phdr) ---
        self.sec_infos = []
        for i in range(self.e_phnum):
            o = self.sec_info_off + i * 0x20
            si = {
                'offset':     be64(d, o),
                'size':       be64(d, o + 8),
                'compressed': be32(d, o + 16),  # 1=none, 2=zlib
                'unknown1':   be32(d, o + 20),
                'unknown2':   be64(d, o + 24),
            }
            self.sec_infos.append(si)

        # --- Control Info (NPDRM info lives here) ---
        self.npdrm_riv = None
        self._parse_control_info()

    def _parse_control_info(self):
        """Parse control info entries looking for NPDRM info (type 3)."""
        d = self.data
        off = self.ctrl_info_off
        end = off + self.ctrl_info_sz

        while off < end and off + 8 <= len(d):
            ci_type = be32(d, off)
            ci_size = be32(d, off + 4)
            if ci_size == 0:
                break

            if ci_type == 3:  # NPDRM info
                # NPDRM control info:
                # +0x10: content_id (0x30 bytes)
                # +0x40: digest (0x10 bytes)
                # +0x50: npdrm_riv (0x10 bytes) — this is what we need
                # +0x60: encrypted klicensee (0x10 bytes)
                if off + 0x70 <= len(d):
                    self.npdrm_content_id = d[off+0x10:off+0x40].split(b'\x00')[0].decode('ascii', errors='replace')
                    self.npdrm_digest = d[off+0x40:off+0x50]
                    self.npdrm_riv = d[off+0x50:off+0x60]
                    self.npdrm_enc_klic = d[off+0x60:off+0x70]
                    print(f"  NPDRM Content ID: {self.npdrm_content_id}")
                    print(f"  NPDRM RIV: {self.npdrm_riv.hex()}")

            off += ci_size

    def print_info(self):
        print(f"  SCE: version={self.version} key_type=0x{self.key_type:04X} category=0x{self.category:04X}")
        print(f"  App: auth=0x{self.auth_id:016X} vendor=0x{self.vendor_id:08X} type={self.app_type}")
        print(f"  ELF: {'64' if self.elf_class == 2 else '32'}-bit {'BE' if self.elf_data == 2 else 'LE'} entry=0x{self.e_entry:X}")
        print(f"  Segments: {self.e_phnum} program headers, {self.e_shnum} section headers")
        print(f"  Key offsets: meta=0x{self.meta_off:X} sec_info=0x{self.sec_info_off:X} ctrl=0x{self.ctrl_info_off:X}")
        for i, (ph, si) in enumerate(zip(self.phdrs, self.sec_infos)):
            pt = {1: "LOAD", 4: "NOTE", 7: "TLS"}.get(ph['p_type'], f"0x{ph['p_type']:X}")
            comp = {1: "", 2: " (zlib)"}.get(si['compressed'], f" (comp={si['compressed']})")
            if ph['p_filesz'] > 0:
                print(f"    [{i}] {pt}: vaddr=0x{ph['p_vaddr']:X} file=0x{ph['p_filesz']:X} "
                      f"mem=0x{ph['p_memsz']:X} -> SELF[0x{si['offset']:X}:0x{si['size']:X}]{comp}")
            else:
                print(f"    [{i}] {pt}: vaddr=0x{ph['p_vaddr']:X} (no file data, mem=0x{ph['p_memsz']:X})")


def decrypt_self(self_data, klicensee=None):
    """
    Decrypt a PS3 SELF file and return a plain ELF.

    Full algorithm:
    1. Parse SELF structure
    2. Derive decryption keys from klicensee (NPDRM) or static keys
    3. Decrypt metadata → get per-segment keys
    4. Decrypt each segment
    5. Rebuild ELF
    """
    sf = SELFFile(self_data)

    print(f"\n  SELF Structure:")
    sf.print_info()

    # --- Step 1: Determine the decryption keys ---
    print(f"\n  Deriving decryption keys...")

    # For NPDRM content, we use the klicensee
    if klicensee and sf.npdrm_riv:
        print(f"  Using NPDRM klicensee: {klicensee.hex()}")
        # The metadata key is derived from the klicensee
        # Decrypt klicensee with NP_KLIC_KEY to get the actual content key
        content_key = aes_ecb_dec(NP_KLIC_KEY, klicensee)
        print(f"  Content key: {content_key.hex()}")
    else:
        content_key = None
        print(f"  No NPDRM info, using static keys")

    # --- Step 2: Decrypt metadata ---
    # The metadata starts at meta_off + 0x20 (after SCE header)
    meta_start = sf.meta_off + 0x20
    print(f"\n  Metadata at file offset 0x{meta_start:X}")

    # First, decrypt the Encryption Root Header (0x40 bytes)
    enc_root = self_data[meta_start:meta_start + 0x40]

    # Try NPDRM-derived keys first, then static keys
    metadata_key = None
    metadata_iv = None

    # For NPDRM, the encryption root header is decrypted with:
    # key = AES-ECB-ENC(NP_KLIC_KEY, klicensee) used as both key derivation input
    # But the actual approach from RPCS3:
    # 1. Get the erk/riv from the key table based on key_revision
    # 2. For NPDRM, XOR the erk with the content key

    key_rev = sf.key_type
    print(f"  Key revision: 0x{key_rev:04X}")

    # Try all known key sets
    keys_to_try = []
    if key_rev in SELF_KEYS:
        keys_to_try.append(("matched", SELF_KEYS[key_rev]))
    # Add all keys as fallback
    for kr, kv in SELF_KEYS.items():
        if kr != key_rev:
            keys_to_try.append((f"fallback-0x{kr:02X}", kv))

    for label, (erk, riv) in keys_to_try:
        # For NPDRM content, XOR first 16 bytes of erk with content key
        actual_erk = bytearray(erk)
        if content_key:
            for i in range(16):
                actual_erk[i] ^= content_key[i]
        actual_erk = bytes(actual_erk)

        # Use NPDRM riv if available, otherwise the key set riv
        actual_riv = sf.npdrm_riv if sf.npdrm_riv else riv

        # Decrypt the Encryption Root Header with AES-256-CBC
        try:
            dec_root = aes_cbc_dec(actual_erk, actual_riv, enc_root)
        except Exception:
            continue

        # The decrypted root header should contain:
        # [0:16]  = data encryption key
        # [16:32] = key padding (should be all zeros or known pattern)
        # [32:48] = data encryption IV
        # [48:64] = IV padding
        mk = dec_root[0:16]
        mk_pad = dec_root[16:32]
        mi = dec_root[32:48]
        mi_pad = dec_root[48:64]

        # Sanity check: padding bytes should be reasonable
        # In practice, the key pad and iv pad should be 0x00
        pad_zeros = sum(1 for b in mk_pad + mi_pad if b == 0)
        print(f"  Key set {label}: pad_zeros={pad_zeros}/32 mk={mk.hex()[:16]}... mi={mi.hex()[:16]}...")

        if pad_zeros >= 16:  # Good sign — at least half the padding is zeros
            metadata_key = mk
            metadata_iv = mi
            print(f"  -> Likely correct! (pad_zeros={pad_zeros})")
            break

    if not metadata_key:
        # Even if padding check fails, try the matched key anyway
        if key_rev in SELF_KEYS:
            erk, riv = SELF_KEYS[key_rev]
            actual_erk = bytearray(erk)
            if content_key:
                for i in range(16):
                    actual_erk[i] ^= content_key[i]
            actual_riv = sf.npdrm_riv if sf.npdrm_riv else riv
            dec_root = aes_cbc_dec(bytes(actual_erk), actual_riv, enc_root)
            metadata_key = dec_root[0:16]
            metadata_iv = dec_root[32:48]
            print(f"  Using key_rev 0x{key_rev:02X} despite uncertain padding")

    if not metadata_key:
        print("\n  ERROR: Could not derive metadata decryption keys.")
        print("  The SELF encryption keys for this title may not be in our key set.")
        print("  Please use RPCS3 'Utilities > Decrypt PS3 Binaries' instead.")
        return None

    # --- Step 3: Decrypt certification header + segment certs ---
    # These follow the encryption root header, encrypted with AES-128-CTR
    cert_start = meta_start + 0x40
    # Read enough for certification header (0x20) + segment entries
    cert_size = 0x20 + sf.e_phnum * 0x30 + 0x100  # generous overread
    cert_size = min(cert_size, len(self_data) - cert_start)

    enc_cert = self_data[cert_start:cert_start + cert_size]
    dec_cert = aes_ctr_dec(metadata_key, metadata_iv, enc_cert)

    # Certification Header (0x10 or 0x20 bytes):
    # +0: sign_input_length (u64)
    # +8: unknown (u32)
    # +12: section_count (u32)
    # +16: key_count (u32)
    # +20: opt_header_size (u32)
    # +24: unknown (u32)
    # +28: unknown (u32)
    sign_input_len = be64(dec_cert, 0)
    section_count = be32(dec_cert, 12)
    key_count = be32(dec_cert, 16)
    opt_hdr_size = be32(dec_cert, 20)

    print(f"\n  Certification Header:")
    print(f"    Sections: {section_count}")
    print(f"    Keys: {key_count}")
    print(f"    Opt header: {opt_hdr_size}")

    if section_count > 100 or key_count > 100:
        print("  WARNING: Unreasonable section/key count — metadata decryption may have failed")
        print("  Falling back to raw segment extraction...")
        return build_elf_raw(sf, self_data)

    # Segment Certification entries (0x20 each, after cert header at +0x20)
    seg_certs = []
    seg_cert_off = 0x20  # after certification header
    for i in range(section_count):
        o = seg_cert_off + i * 0x20
        sc = {
            'seg_offset':    be64(dec_cert, o),
            'seg_size':      be64(dec_cert, o + 8),
            'comp_type':     be32(dec_cert, o + 16),  # 1=none, 2=zlib
            'unknown':       be32(dec_cert, o + 20),
            'enc_type':      be32(dec_cert, o + 24),  # 0=none, 1=AES-128-CTR, 2=unknown
            'key_idx':       be32(dec_cert, o + 28),
        }
        seg_certs.append(sc)
        enc_name = {0: "plain", 1: "AES-CTR", 2: "enc-2"}.get(sc['enc_type'], f"enc-{sc['enc_type']}")
        comp_name = {1: "raw", 2: "zlib"}.get(sc['comp_type'], f"comp-{sc['comp_type']}")
        print(f"    [{i}] offset=0x{sc['seg_offset']:X} size=0x{sc['seg_size']:X} "
              f"{enc_name} {comp_name} key_idx={sc['key_idx']}")

    # Key table (16 bytes per key, follows segment certs)
    key_table_off = seg_cert_off + section_count * 0x20
    keys = []
    for i in range(key_count):
        o = key_table_off + i * 16
        k = dec_cert[o:o+16]
        keys.append(k)
        print(f"    Key[{i}]: {k.hex()}")

    # --- Step 4: Decrypt segments ---
    print(f"\n  Decrypting segments...")
    decrypted_segments = {}

    for i, sc in enumerate(seg_certs):
        if i >= len(sf.phdrs):
            break  # More cert entries than program headers (padding segments)

        ph = sf.phdrs[i]
        if ph['p_filesz'] == 0:
            continue

        seg_data = self_data[sc['seg_offset']:sc['seg_offset'] + sc['seg_size']]

        if sc['enc_type'] == 1:  # AES-128-CTR encrypted
            key_idx = sc['key_idx']
            if key_idx + 1 < len(keys):
                seg_key = keys[key_idx]
                seg_iv = keys[key_idx + 1]
                seg_data = aes_ctr_dec(seg_key, seg_iv, seg_data)
                print(f"    [{i}] Decrypted 0x{len(seg_data):X} bytes with key[{key_idx}]")
            else:
                print(f"    [{i}] WARNING: key_idx {key_idx} out of range")
        elif sc['enc_type'] == 0:
            print(f"    [{i}] Plain (no encryption)")
        else:
            print(f"    [{i}] Unknown encryption type {sc['enc_type']}")

        if sc['comp_type'] == 2:  # zlib compressed
            try:
                seg_data = zlib.decompress(seg_data)
                print(f"    [{i}] Decompressed to 0x{len(seg_data):X} bytes")
            except zlib.error as e:
                print(f"    [{i}] WARNING: zlib decompress failed: {e}")

        decrypted_segments[i] = seg_data

    # --- Step 5: Rebuild ELF ---
    return build_elf(sf, decrypted_segments)


def build_elf_raw(sf, self_data):
    """Build ELF from raw segment data (no metadata decryption)."""
    segments = {}
    for i, (ph, si) in enumerate(zip(sf.phdrs, sf.sec_infos)):
        if ph['p_filesz'] > 0 and si['size'] > 0:
            segments[i] = self_data[si['offset']:si['offset'] + si['size']]
    return build_elf(sf, segments)


def build_elf(sf, segments):
    """Reconstruct a plain ELF from parsed SELF and decrypted segments."""
    print(f"\n  Rebuilding ELF...")

    # Layout: ELF header (0x40) + PHDRs + padding + segment data
    elf_hdr_size = 0x40
    phdr_size = sf.e_phnum * 0x38

    # Calculate segment positions
    data_start = (elf_hdr_size + phdr_size + 0xFFFF) & ~0xFFFF  # Align to 64K
    current_off = data_start
    seg_layout = {}

    for i, ph in enumerate(sf.phdrs):
        if i in segments and ph['p_filesz'] > 0:
            # Align to segment alignment (at least 16)
            align = max(ph['p_align'], 16)
            if align > 1:
                current_off = (current_off + align - 1) & ~(align - 1)
            seg_layout[i] = current_off
            current_off += len(segments[i])
        else:
            seg_layout[i] = 0

    total_size = current_off

    # Build output
    out = bytearray(total_size)

    # ELF header
    elf_hdr = bytearray(sf.data[sf.elf_off:sf.elf_off + 0x40])
    struct.pack_into('>Q', elf_hdr, 32, 0x40)  # e_phoff = right after header
    struct.pack_into('>Q', elf_hdr, 40, 0)     # e_shoff = 0 (no section headers)
    struct.pack_into('>H', elf_hdr, 60, 0)     # e_shnum = 0
    struct.pack_into('>H', elf_hdr, 62, 0)     # e_shstrndx = 0
    out[0:0x40] = elf_hdr

    # Program headers
    for i, ph in enumerate(sf.phdrs):
        o = 0x40 + i * 0x38
        struct.pack_into('>I', out, o + 0,  ph['p_type'])
        struct.pack_into('>I', out, o + 4,  ph['p_flags'])
        struct.pack_into('>Q', out, o + 8,  seg_layout[i])
        struct.pack_into('>Q', out, o + 16, ph['p_vaddr'])
        struct.pack_into('>Q', out, o + 24, ph['p_paddr'])
        struct.pack_into('>Q', out, o + 32, ph['p_filesz'])
        struct.pack_into('>Q', out, o + 40, ph['p_memsz'])
        struct.pack_into('>Q', out, o + 48, ph['p_align'])

    # Segment data
    for i, data in segments.items():
        off = seg_layout[i]
        if off > 0:
            out[off:off + len(data)] = data
            print(f"    Segment [{i}] -> offset 0x{off:X} (0x{len(data):X} bytes)")

    # Validate: check if first loadable segment looks like PPC code
    for i, ph in enumerate(sf.phdrs):
        if ph['p_type'] == 1 and ph['p_filesz'] > 0 and i in segments:
            first_word = struct.unpack('>I', segments[i][:4])[0]
            opcode = (first_word >> 26) & 0x3F
            print(f"\n  Validation: first code word = 0x{first_word:08X} (opcode={opcode})")
            if opcode == 0 and first_word == 0:
                print("  WARNING: First word is 0x00000000 — data may still be encrypted")
            elif opcode in (14, 15, 18, 19, 31, 32, 33, 36, 37, 38, 44, 46, 47):
                print("  GOOD: Looks like valid PPC64 code!")
            else:
                print(f"  UNCERTAIN: opcode {opcode} is valid PPC but uncommon for entry")
            break

    return bytes(out)


def main():
    input_dir = Path(__file__).parent.parent / "input"

    if len(sys.argv) < 2:
        eboot = input_dir / "USRDIR" / "EBOOT.BIN"
    else:
        eboot = Path(sys.argv[1])

    if not eboot.exists():
        print(f"ERROR: {eboot} not found")
        return 1

    output = input_dir / "EBOOT.ELF"

    # Find RAP
    rap_path = None
    rap_files = list(input_dir.glob("*.rap"))
    if rap_files:
        rap_path = rap_files[0]

    # Load RAP and derive klicensee
    klicensee = None
    if rap_path:
        with open(rap_path, 'rb') as f:
            rap_data = f.read()
        klicensee = rap_to_klicensee(rap_data)
        print(f"RAP: {rap_path.name}")
        print(f"Klicensee: {klicensee.hex()}")
    else:
        print("WARNING: No RAP file found. NPDRM decryption may fail.")

    # Load SELF
    print(f"\nLoading: {eboot}")
    with open(eboot, 'rb') as f:
        self_data = f.read()
    print(f"Size: {len(self_data)} bytes ({len(self_data)/(1024*1024):.1f} MB)")

    # Decrypt
    elf_data = decrypt_self(self_data, klicensee)

    if elf_data is None:
        print("\nDecryption failed. Use RPCS3 instead.")
        return 1

    # Write output
    with open(output, 'wb') as f:
        f.write(elf_data)
    print(f"\nOutput: {output} ({len(elf_data)/(1024*1024):.1f} MB)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
