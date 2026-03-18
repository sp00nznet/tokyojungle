# Technical Notes

## Game Information

- **Title:** Tokyo Jungle
- **Game IDs:** NPUA80523 (NA), BCAS20219 (JP), NPEA00421 (EU)
- **Content ID:** UP9000-NPUA80523_00-TOKYOJUNGLE00001
- **Developer:** Crispy's / Japan Studio
- **Engine:** Unknown / proprietary
- **Native Resolution:** 720p (1280x720)
- **Frame Rate:** 30fps target
- **SDK Version:** 3.70
- **License Type:** NPDRM LOCAL (requires RAP)

## Binary Analysis Results

### ELF Structure

| Property | Value |
|----------|-------|
| Format | ELF64 Big-Endian PPC64 |
| Entry Point | 0x3428F0 (function descriptor in .data) |
| Code Segment | 0x10000 - 0x336268 (~3.1 MB, RX) |
| Data Segment | 0x340000 - 0x3BED80 (~520 KB file, ~9.2 MB virtual) |
| BSS | 0x3BED80 - 0xC0E208 (~8.5 MB) |
| Section Names | Stripped (all zeros in string table) |

### Code Statistics

- **24,372 functions** discovered by aggressive function finder
- **2,882 functions** detected by simple boundary analysis
- **825,498 lines** of disassembly
- **2,255,297 lines** of generated C code (135 MB)
- **318 NID imports** across 24 PS3 modules

### Imported Modules (24 total, 318 functions)

#### Critical for Boot
| Module | Functions | Purpose |
|--------|-----------|---------|
| sysPrxForUser | 17 | Core system: threads, mutexes, memory, printf |
| cellSysmodule | 4 | Module loading/unloading |
| cellSysutil | 21 | System callbacks, video output config |
| cellGame | 7 | Game data/save management, boot check |
| sys_fs | 17 | Filesystem operations |

#### Graphics (THE BIG CHALLENGE)
| Module | Functions | Purpose |
|--------|-----------|---------|
| cellGcmSys | 33 | RSX command buffers, display, tiling, memory mapping |
| cellResc | 15 | Resolution scaling engine |
| cellFont | 30 | Font rendering (UI text, menus) |
| cellFontFT | 1 | FreeType backend init |

Key GCM functions used:
- `cellGcmInit`, `cellGcmSetDisplayBuffer`, `cellGcmSetFlip` — display setup
- `cellGcmMapMainMemory`, `cellGcmMapEaIoAddress` — memory mapping
- `cellGcmSetTile`, `cellGcmBindTile`, `cellGcmSetZcull` — tiled rendering
- `cellGcmGetControlRegister`, `cellGcmSetPrepareFlip` — command buffer control

#### Audio
| Module | Functions | Purpose |
|--------|-----------|---------|
| cellAudio | 11 | Audio port management, mixing |

All 11 functions resolved: Init, Quit, PortOpen/Close/Start/Stop, GetPortConfig, SetPortLevel, event queues.

#### Input
| Module | Functions | Purpose |
|--------|-----------|---------|
| sys_io | 16 | Controller (cellPad) and keyboard input |

#### Media Playback
| Module | Functions | Purpose |
|--------|-----------|---------|
| cellSail | 32 | Video/media player (cutscenes, intros) |
| cellPamf | 2 | Media format parsing |

**Note:** cellSail is a high-level media player (not cellVdec/cellAdec as initially expected). This is actually easier to HLE since we can just decode with FFmpeg.

#### SPU
| Module | Functions | Purpose |
|--------|-----------|---------|
| cellSpurs | 32 | SPU task scheduling and management |

32 SPURS functions = heavy SPU usage. Likely for:
- Audio mixing/effects
- Physics/collision
- Particle systems
- AI pathfinding

#### Network/PSN (Can be stubbed)
| Module | Functions | Purpose |
|--------|-----------|---------|
| sceNp | 32 | PlayStation Network |
| sceNpTrophy | 9 | Trophy system |
| sceNpCommerce2 | 16 | PS Store (DLC purchases) |
| cellNetCtl | 4 | Network control |
| cellHttp | 6 | HTTP client |
| cellSsl | 3 | SSL/TLS |
| sys_net | 2 | Network sockets |
| cellSysutilNpEula | 1 | EULA dialog |

73 functions total that can be safely stubbed to return "no network" / "success".

#### Misc
| Module | Functions | Purpose |
|--------|-----------|---------|
| cellRtc | 5 | Real-time clock (in-game time progression) |
| cellL10n | 2 | Localization (character encoding) |

## Game Asset Structure

```
USRDIR/
├── EBOOT.BIN              (3.7 MB - encrypted game executable)
├── data/
│   ├── edge/              (Animal models & textures - ~400 files)
│   │   ├── em_*.pac       (Model data - beagle, bear, cat, deer, etc.)
│   │   ├── em_*.tex       (Texture data - 340KB each)
│   │   └── ee_*.pad       (Common effect data)
│   ├── map/               (Level geometry)
│   │   ├── shibuya*.bd    (Shibuya district maps)
│   │   ├── sewer*.bd      (Sewer level maps)
│   │   └── menu/          (Menu backgrounds)
│   ├── snd/               (Sound/music - .sgd format)
│   │   ├── *_tj_stage*.sgd  (Stage BGM)
│   │   ├── *_tj_battle*.sgd (Battle music)
│   │   ├── *_tj_menu*.sgd   (Menu music)
│   │   └── *_tj_ending.sgd  (Ending theme)
│   ├── lang/              (Localization)
│   │   ├── EN/            (English)
│   │   ├── FC/            (French Canadian?)
│   │   ├── PU/            (Portuguese?)
│   │   └── SU/            (Spanish?)
│   ├── region/            (Region-specific data)
│   ├── save/              (Save data templates)
│   ├── *.pac              (Packed archives)
│   ├── *.bin              (Binary data)
│   ├── *.dat              (Data tables)
│   └── load_*.pam         (Loading screen videos - PAMF format)
├── MANUAL/                (Digital manual - DDS textures)
├── PARAM.SFO              (Game metadata)
├── ICON0.PNG              (Game icon)
└── PIC1.PNG               (Background image)
```

### Observations
- ~50+ animal species as separate model files in `edge/`
- PAMF format for video (load screens) — handled by cellSail
- SGD format for audio — Sony's proprietary sound format
- Custom `.pac` archive format — may need reverse engineering
- 4 language directories suggest multi-language support

## RPCS3 Compatibility Notes

RPCS3 wiki status: **Playable**

Known issues under emulation:
- Washed-out graphics on Vulkan (OpenGL works) — RSX command handling differences
- Map text rendering issues — font/texture coordinate problems
- Occasional audio cutouts — cellAudio timing issues

## Architecture Notes

### Entry Point Analysis
The ELF entry (0x3428F0) is a PPC64 function descriptor table in the data segment:
- Contains pairs of (function_address, TOC_pointer)
- The actual code entry is loaded from the descriptor
- TOC (Table of Contents) at 0x359220 — used for global data access

### Stack Convention
Functions follow standard PPC64 ABI:
- `stdu r1, -N(r1)` — create stack frame
- `mflr r0; std r0, N(r1)` — save return address
- `ld r0, N(r1); mtlr r0; blr` — restore and return
