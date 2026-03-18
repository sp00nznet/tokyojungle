#!/usr/bin/env python3
"""
PS3 PKG Extractor for Tokyo Jungle Recompiled

Extracts PS3 .pkg files to retrieve game contents (EBOOT.BIN, assets, etc.)
PS3 PKG format: big-endian header + encrypted/plaintext file entries

References:
- https://www.psdevwiki.com/ps3/PKG_files
- RPCS3 source (rpcs3/Crypto/unpkg.cpp)
"""

import hashlib
import hmac
import os
import struct
import sys
from pathlib import Path
from Crypto.Cipher import AES

# PS3 PKG constants
PKG_MAGIC = 0x7F504B47  # \x7FPKG
PKG_HEADER_SIZE = 0xC0

# Known PS3 PKG AES keys (public knowledge, documented on psdevwiki)
PKG_AES_KEY_RETAIL = bytes.fromhex("2E7B71D7C9C9A14EA3221F188828B8F8")
PKG_AES_KEY_DEBUG  = bytes.fromhex("00000000000000000000000000000000")
PKG_AES_KEY_PSP    = bytes.fromhex("07F2C68290B50D2C33818D709B60E62B")

# PS3 content keys
PS3_CONTENT_KEY = bytes.fromhex("E31A70C9CE1DD72BF3C0622963F2ECCB")


def read_be32(data, offset):
    return struct.unpack('>I', data[offset:offset+4])[0]

def read_be64(data, offset):
    return struct.unpack('>Q', data[offset:offset+8])[0]

def read_be16(data, offset):
    return struct.unpack('>H', data[offset:offset+2])[0]


class PKGEntry:
    def __init__(self, name_offset, name_size, data_offset, data_size, flags, padding):
        self.name_offset = name_offset
        self.name_size = name_size
        self.data_offset = data_offset
        self.data_size = data_size
        self.flags = flags
        self.padding = padding
        self.name = ""

    @property
    def is_directory(self):
        return (self.flags & 0xFF) == 0x04

    @property
    def is_file(self):
        return (self.flags & 0xFF) == 0x01 or (self.flags & 0xFF) == 0x03


class PKGExtractor:
    def __init__(self, pkg_path: str):
        self.pkg_path = pkg_path
        self.entries = []

    def _derive_key(self, header_data):
        """Derive the decryption key from the PKG header."""
        # The key is derived from the PKG content ID and the retail key
        # Using the first 16 bytes of pkg_data_riv from the header
        pkg_data_riv = header_data[0x70:0x80]
        return pkg_data_riv

    def _decrypt_data(self, key, iv_base, data, offset):
        """Decrypt PKG data using AES-CTR mode."""
        result = bytearray(len(data))
        block_size = 16

        for i in range(0, len(data), block_size):
            # Calculate counter value
            counter = offset + i
            block_num = counter // block_size

            # Build IV: base IV incremented by block number
            iv_int = int.from_bytes(iv_base, 'big') + block_num
            iv = iv_int.to_bytes(16, 'big')

            cipher = AES.new(PKG_AES_KEY_RETAIL, AES.MODE_ECB)
            keystream = cipher.encrypt(iv)

            chunk = data[i:i+block_size]
            for j in range(len(chunk)):
                result[i+j] = chunk[j] ^ keystream[j]

        return bytes(result)

    def extract(self, output_dir: str):
        """Extract all files from the PKG."""
        output_path = Path(output_dir)

        with open(self.pkg_path, 'rb') as f:
            # Read header
            header = f.read(PKG_HEADER_SIZE)

            magic = read_be32(header, 0)
            if magic != PKG_MAGIC:
                print(f"ERROR: Not a valid PKG file (magic: 0x{magic:08X}, expected 0x{PKG_MAGIC:08X})")
                return False

            pkg_revision = read_be16(header, 4)
            pkg_type = read_be16(header, 6)
            metadata_offset = read_be32(header, 8)
            metadata_count = read_be32(header, 12)
            metadata_size = read_be32(header, 16)

            item_count = read_be32(header, 20)
            total_size = read_be64(header, 24)
            data_offset = read_be64(header, 32)
            data_size = read_be64(header, 40)

            content_id = header[0x30:0x30+0x30].split(b'\x00')[0].decode('ascii', errors='replace')

            print(f"PKG Info:")
            print(f"  Revision: 0x{pkg_revision:04X}")
            print(f"  Type: 0x{pkg_type:04X}")
            print(f"  Content ID: {content_id}")
            print(f"  Items: {item_count}")
            print(f"  Data offset: 0x{data_offset:X}")
            print(f"  Data size: {data_size / (1024*1024):.1f} MB")
            print(f"  Total size: {total_size / (1024*1024):.1f} MB")
            print()

            # Derive decryption key
            pkg_data_riv = header[0x70:0x80]

            # Read file table (at data_offset)
            entry_size = 32  # Each entry is 32 bytes
            table_size = item_count * entry_size

            f.seek(data_offset)
            encrypted_table = f.read(table_size)

            # Decrypt file table
            table_data = self._decrypt_data(PKG_AES_KEY_RETAIL, pkg_data_riv,
                                           encrypted_table, 0)

            # Parse entries
            for i in range(item_count):
                off = i * entry_size
                name_offset = read_be32(table_data, off + 0)
                name_size = read_be32(table_data, off + 4)
                entry_data_offset = read_be64(table_data, off + 8)
                entry_data_size = read_be64(table_data, off + 16)
                flags = read_be32(table_data, off + 24)
                padding = read_be32(table_data, off + 28)

                entry = PKGEntry(name_offset, name_size, entry_data_offset,
                               entry_data_size, flags, padding)
                self.entries.append(entry)

            # Read and decrypt names
            for entry in self.entries:
                f.seek(data_offset + entry.name_offset)
                enc_name = f.read(entry.name_size)
                dec_name = self._decrypt_data(PKG_AES_KEY_RETAIL, pkg_data_riv,
                                             enc_name, entry.name_offset)
                entry.name = dec_name.decode('utf-8', errors='replace').rstrip('\x00')

            # Extract files
            extracted = 0
            skipped = 0

            for entry in self.entries:
                if not entry.name:
                    continue

                target = output_path / entry.name

                if entry.is_directory:
                    target.mkdir(parents=True, exist_ok=True)
                    continue

                if entry.is_file and entry.data_size > 0:
                    target.parent.mkdir(parents=True, exist_ok=True)

                    size_mb = entry.data_size / (1024 * 1024)
                    print(f"  Extracting: {entry.name} ({size_mb:.2f} MB)")

                    # Read and decrypt file data
                    f.seek(data_offset + entry.data_offset)

                    with open(target, 'wb') as out:
                        remaining = entry.data_size
                        chunk_size = 1024 * 1024  # 1MB chunks
                        file_offset = entry.data_offset

                        while remaining > 0:
                            read_size = min(chunk_size, remaining)
                            encrypted_chunk = f.read(read_size)
                            if not encrypted_chunk:
                                break
                            decrypted = self._decrypt_data(
                                PKG_AES_KEY_RETAIL, pkg_data_riv,
                                encrypted_chunk, file_offset
                            )
                            out.write(decrypted[:read_size])
                            remaining -= read_size
                            file_offset += read_size

                    extracted += 1
                else:
                    skipped += 1

            print(f"\nExtracted {extracted} files ({skipped} skipped)")
            return True


def main():
    if len(sys.argv) < 2:
        # Auto-find PKG in input directory
        input_dir = Path(__file__).parent.parent / "input"
        pkg_files = list(input_dir.glob("*.pkg"))
        if not pkg_files:
            print("Usage: extract_pkg.py <pkg_file> [output_dir]")
            print("Or place a .pkg file in the input/ directory")
            return 1
        pkg_path = str(pkg_files[0])
        print(f"Auto-found PKG: {pkg_path}")
    else:
        pkg_path = sys.argv[1]

    output_dir = sys.argv[2] if len(sys.argv) > 2 else str(Path(pkg_path).parent)

    print(f"Extracting: {pkg_path}")
    print(f"Output to:  {output_dir}")
    print()

    extractor = PKGExtractor(pkg_path)
    if extractor.extract(output_dir):
        print("\nDone! Look for USRDIR/EBOOT.BIN in the output.")
        print("Next step: decrypt EBOOT.BIN -> EBOOT.ELF")
    else:
        print("\nExtraction failed.")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
