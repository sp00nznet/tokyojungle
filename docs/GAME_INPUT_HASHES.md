# Game files this port was built and tested with

A version string (`APP_VER 01.00`) is not an identity, so here are the hashes. SHA-256 is the one to check.

## Source disc

The game came from the Redump dump of **Best of PlayStation Network Vol. 1 (USA)**. No ISO was used directly:
the copy used was a PS3 game folder made from that ISO with PS3 Disc Dumper. Redump publishes CRC, MD5 and
SHA-1 for the ISO, not SHA-256:

| Redump ISO | Value |
|---|---|
| Name | `Best of PlayStation Network Vol. 1 (USA).iso` |
| CRC32 | `b30adaa1` |
| MD5 | `fff76b4b34a67166c84297a34cfaa289` |
| SHA-1 | `fdf804def4770be721e737c5cdf501ea3a9186a0` |

Tokyo Jungle is the package in `PS3_EXTRA/D002` of that disc. PARAM.SFO: `TITLE_ID NPUA80523`,
`APP_VER 01.00`, `VERSION 01.00`, `PS3_SYSTEM_VER 03.7000`. No game update applied.

## Files

| File | Bytes | SHA-256 |
|---|---:|---|
| `PS3_EXTRA/D002/DATA000.PKG` (the Tokyo Jungle package) | 2,577,268,768 | `f3b75c548079f75165e7a43f6478d0cc7f86502b782728c38b2cc1975b9ae07c` |
| `USRDIR/EBOOT.BIN` (encrypted) | 3,879,392 | `a37e53c916d1320e0ef7c74dab5d83ca8828bab4c20d5c2fc131b256220172fd` |
| `EBOOT.ELF` (decrypted; the binary that was recompiled) | 3,876,648 | `6d253f6429485b43b816fd6884611377626ee95db5dd9200ea58395c93a0812d` |
| `PARAM.SFO` | 1,040 | `ef125c5e6f49b51f9f33ca9a7050c4ff9622e6d90e8132da9c45b4ffe56af97b` |

More hashes for the same files (SHA-1 / MD5 / CRC32):

| File | SHA-1 | MD5 | CRC32 |
|---|---|---|---|
| `DATA000.PKG` | `6254ff1db5d6948dbde19303cc26799ad0e08a29` | `e23c699f9a624377d578521132a1208a` | `5FD5E02C` |
| `EBOOT.BIN` | `8d4639de5a177796aba0440f3c1ec30a3be675f1` | `904605afc0853849a31d3c395709812b` | `5187DEC6` |
| `EBOOT.ELF` | `78bdfaecc1c27f831ef990e1b6e10dfb8bf978d7` | `69e7337fc2b006a5b9c2f20395ab034c` | `4164D2B1` |
| `PARAM.SFO` | `37a5ec2980dce48aef5930396044ff5966c120e1` | `5ea5a8ce0ab35de1e71556f2509477c7` | `24535EC8` |

## Whole game folder

[GAME_INPUT_usa-extracted.sha256](GAME_INPUT_usa-extracted.sha256) lists the SHA-256 of all 1,118 files in
the extracted game folder (the one passed as `-GameData`). Check your copy from inside that folder:

```bash
sha256sum -c path/to/docs/GAME_INPUT_usa-extracted.sha256
```

The list file itself has SHA-256 `5da0aa53192ffa401593385ab8bcc415d6215c384fe659b65934ade811fa256f`.
