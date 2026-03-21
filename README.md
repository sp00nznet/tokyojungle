# Tokyo Jungle Recompiled

**A Pomeranian walks into post-apocalyptic Tokyo. No, seriously.**

Tokyo Jungle (2012) is the most wonderfully absurd game Sony ever published — a survival action game where you play as animals fighting for dominance in a Tokyo abandoned by humanity. One moment you're a tiny Pomeranian sneaking past lions in Shibuya, the next you're a dinosaur asserting territorial control over Yoyogi Park. It's brutal. It's beautiful. It has no business being as good as it is.

And it's about to disappear.

## Why This Exists

Tokyo Jungle was a PS3 exclusive. The Western release was **digital-only** — no disc, no physical backup. Both of its developers are gone (Japan Studio dissolved 2021, Crispy's dormant since 2015). The PS3 store is running on fumes. Sony's PS Plus cloud streaming for PS3 titles has been [broken for months](https://www.playstationlifestyle.net/2026/01/18/ps3-game-streaming-busted-ps-plus-premium/). Nobody is coming to save this game.

So we're doing it ourselves.

**Tokyo Jungle Recompiled** is a native PC port built using [ps3recomp](https://github.com/sp00nznet/ps3recomp) — a static recompilation toolchain that translates PS3 PowerPC binaries into native C code, then compiles them as x86-64 executables. No emulation at runtime. No compatibility layers. Just raw, recompiled native code.

## Status: Phase 6 — Game Init Completes, Main Loop Reached

> **This is the first 3D title to attempt ps3recomp.** The game's initialization runs to completion and execution enters the main game loop. No crashes. 35,210 functions lifted from the original binary.

| Milestone | Status |
|-----------|--------|
| Binary analysis & function discovery | **Done** — 35,210 functions lifted |
| PPU disassembly & code lifting | **Done** — Full convergence (iterative OPD discovery) |
| Project scaffold & build system | **Done** — 18MB native executable |
| ELF loading & VM setup | **Done** — 4GB address space, segments loaded |
| CRT initialization | **Done** — _start through __libc_start, all CRT stubs |
| LV2 syscall dispatch | **Done** — Full dispatch table, TTY, memory, threading |
| Import table resolution (PLT/NID) | **Done** — 318 NIDs, 61 HLE handlers, OPD patching |
| Game init (game_init) | **Done** — Reaches cellGcmInit and main loop |
| HLE module stubs (cellGcm, cellPad, etc.) | **In Progress** — Stubs return OK, need real impls |
| Graphics backend (RSX -> Vulkan/D3D12) | Not Started |
| Audio (cellAudio -> WASAPI/SDL) | Not Started |
| Input (cellPad -> XInput/SDL) | Not Started |
| SPU task handling | Not Started |
| Survival Mode playable | Not Started |
| Story Mode playable | Not Started |
| DLC species support | Not Started |

### What's Working

- **35,210 PPU functions** statically recompiled to native C and compiled to x86-64
- **CRT initialization** chain runs cleanly (argc/argv setup, TLS, heap, SPU stubs)
- **Import resolution** for all 24 PS3 modules (cellGcmSys, cellSysutil, cellPad, cellAudio, sysPrxForUser, etc.)
- **Indirect call dispatch** through function pointer tables and vtables (OPD fallback)
- **LV2 syscall handling** with dispatch table for threading, memory, filesystem, events
- **Game initialization** completes — subsystem init, cellGcmInit, enters main loop
- **No crashes** — game runs until vsync/flip wait (expected with null renderer)

### Current Blockers

- **Null renderer** — Game loop waits for vsync/flip events that never arrive
- **Stubbed subsystems** — Subsystem registration loop and memory pool init are stubbed
- **CRT vsnprintf** — Game's internal printf crashes on format strings (sprintf dispatcher stubbed)

## How It Works

```
PS3 ELF binary
    ↓ decrypt & parse
PPU PowerPC assembly
    ↓ disassemble & lift
Generated C source code (thousands of files)
    ↓ compile with MSVC/GCC/Clang
Native x86-64 executable
    + ps3recomp runtime (HLE OS services)
    + Graphics backend (RSX → Vulkan)
    + Audio/Input backends
    ↓
Tokyo Jungle on your PC
```

The ps3recomp toolchain handles the heavy lifting of translating PowerPC instructions to equivalent C code. We provide game-specific configuration, function stubs, and fixes on top.

## Building

### Prerequisites

- **CMake** 3.20+
- **C++20 compiler** — MSVC 2022 (19.35+), GCC 12+, or Clang 14+
- **Python** 3.10+ with packages: `pycryptodome`, `capstone`, `construct`, `tabulate`, `tomli`, `tqdm`
- **ps3recomp** — clone from [sp00nznet/ps3recomp](https://github.com/sp00nznet/ps3recomp)
- **A legitimate copy of Tokyo Jungle** (NPUA80523 / BCAS20219) — you must legally own the game
- **Ninja** (recommended build system)

### Steps

```bash
# Clone with submodule
git clone --recursive https://github.com/sp00nznet/tokyojungle.git
cd tokyojungle

# Install Python dependencies
pip install -r requirements.txt

# Set up your decrypted ELF (you must provide this yourself)
cp /path/to/your/EBOOT.ELF input/

# Run the analysis pipeline (function discovery + lifting)
python scripts/analyze.py

# Post-lift processing (patches, stubs, dispatch table)
python scripts/gen_stubs.py

# Configure and build
cmake -B build -G Ninja
cmake --build build --config Release

# Run
./build/Release/tokyojungle.exe
```

> **Note:** You must supply your own legally obtained game files. This project does not include, distribute, or link to any copyrighted game data.

## Game IDs

| Region | ID | Format |
|--------|----|--------|
| NA (Digital) | NPUA80523 | PSN |
| JP (Disc) | BCAS20219 | Blu-ray |
| EU (Digital) | NPEA00421 | PSN |

## Project Structure

```
tokyojungle/
├── config/
│   └── tokyojungle.toml       # ps3recomp configuration
├── src/
│   ├── main.cpp                # Entry point, VM init, ELF loading
│   ├── stubs.cpp               # Game-specific HLE overrides (trophy, network)
│   ├── recomp_bridge.h         # Memory access, indirect call dispatch, syscalls
│   ├── import_resolver.h       # PLT import table population & heap allocator
│   ├── hle_imports.h           # 61 HLE handlers (cellGcm, cellPad, malloc, etc.)
│   └── elf_loader.h            # ELF segment loader
├── scripts/
│   ├── analyze.py              # Binary analysis pipeline
│   ├── gen_stubs.py            # Post-lift processing (TOC fix, bctr, stubs)
│   └── add_traces.py           # Debug tracing for game functions
├── input/                      # Your game files go here (gitignored)
├── generated/                  # Recompiled C output (~150MB, gitignored)
│   ├── ppu_recomp.c            # 35,210 lifted PPU functions
│   ├── ppu_recomp.h            # Function declarations
│   ├── ppu_stubs.c             # Manual CRT overrides (_start, init stubs)
│   ├── dispatch_table.c        # Guest addr -> host func lookup table
│   └── functions.json          # Function boundary list for lifter
├── build/                      # Build output (gitignored)
└── docs/
    ├── PROGRESS.md             # Detailed progress log
    └── TECHNICAL.md            # Technical notes & findings
```

## Contributing

This is an ambitious preservation project and help is very welcome. The biggest areas of need:

- **RSX Graphics Reverse Engineering** — Understanding Tokyo Jungle's rendering pipeline and building the Vulkan translation layer
- **SPU Program Analysis** — Identifying and reimplementing SPU tasks (likely audio processing, physics, particle systems)
- **Game Logic Debugging** — Once code is lifted, tracking down crashes and behavioral bugs
- **Testing** — Comparing behavior against RPCS3 reference runs

If you've worked with PS3 internals, RPCS3, N64Recomp, or similar recompilation projects, your expertise would be invaluable.

## Legal

This project is a clean-room recompilation toolchain output. It does not contain any proprietary Sony or game code. You must provide your own legally obtained game files. This project exists solely for game preservation purposes.

**Tokyo Jungle** is a trademark of Sony Interactive Entertainment. This project is not affiliated with, endorsed by, or connected to Sony, Japan Studio, or Crispy's in any way.

## A Love Letter

Tokyo Jungle is one of those rare games that could only exist because someone at Sony said "sure, why not" to a pitch about a Pomeranian surviving the apocalypse. It's weird. It's wonderful. It sold over 500,000 copies. It got a perfect score from Famitsu. And in a few years, without intervention, it will exist only in YouTube videos and fond memories.

Games like this don't deserve to vanish just because the hardware they ran on becomes obsolete. If you've ever watched a golden retriever take down a crocodile in the streets of Shibuya, you know exactly why this project exists.

**Let's keep Tokyo Jungle alive.**

---

*Built with [ps3recomp](https://github.com/sp00nznet/ps3recomp) | Star this repo if you want to pet the Pomeranian*
