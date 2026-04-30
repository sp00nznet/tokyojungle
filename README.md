# Tokyo Jungle Recompiled

**A Pomeranian walks into post-apocalyptic Tokyo. No, seriously.**

Tokyo Jungle (2012) is the most wonderfully absurd game Sony ever published — a survival action game where you play as animals fighting for dominance in a Tokyo abandoned by humanity. One moment you're a tiny Pomeranian sneaking past lions in Shibuya, the next you're a dinosaur asserting territorial control over Yoyogi Park. It's brutal. It's beautiful. It has no business being as good as it is.

And it's about to disappear.

## Why This Exists

Tokyo Jungle was a PS3 exclusive. The Western release was **digital-only** — no disc, no physical backup. Both of its developers are gone (Japan Studio dissolved 2021, Crispy's dormant since 2015). The PS3 store is running on fumes. Sony's PS Plus cloud streaming for PS3 titles has been [broken for months](https://www.playstationlifestyle.net/2026/01/18/ps3-game-streaming-busted-ps-plus-premium/). Nobody is coming to save this game.

So we're doing it ourselves.

**Tokyo Jungle Recompiled** is a native PC port built using [ps3recomp](https://github.com/sp00nznet/ps3recomp) — a static recompilation toolchain that translates PS3 PowerPC binaries into native C code, then compiles them as x86-64 executables. No emulation at runtime. No compatibility layers. Just raw, recompiled native code.

## Status: Phase 7 — Game Runs Through Main, Iterating Game Loop

> **This is the first 3D title to attempt ps3recomp.** The game now runs end-to-end through CRT init, _cellGcmInitBody, and into the main game loop, alive indefinitely. 35,208 PPU functions lifted, 1 unmapped indirect-call site remaining. Adapted to ps3recomp v0.5.1.

| Milestone | Status |
|-----------|--------|
| Binary analysis & function discovery | **Done** — 35,208 functions (find_functions + OPD scan) |
| PPU disassembly & code lifting | **Done** — ps3recomp v0.5.1 lifter, 4 body overrides |
| Project scaffold & build system | **Done** — 32MB native exe, MSVC + Visual Studio 2022 |
| ELF loading & VM setup | **Done** — 4GB address space, segments loaded |
| CRT initialization | **Done** — _start through __libc_start, 11 CRT stubs |
| LV2 syscall dispatch | **Done** — Full dispatch table, TTY, memory, threading |
| Import table resolution (PLT/NID) | **Done** — 318 NIDs, 61 HLE handlers, OPD patching |
| Vtable / OPD-only function discovery | **Done** — scripts/scan_opds.py recovers 1,897 fns |
| Guest callback dispatch hook | **Done** — `g_ps3_guest_caller` → tj_guest_caller |
| Game init (game_init) | **Done** — Past _cellGcmInitBody, into main loop |
| RIP-resolving watchdog | **Done** — `tj_install_watchdog` for diagnosing hangs |
| HLE module stubs (cellGcm, cellPad, etc.) | **In Progress** — silent-ok for many, real impls needed |
| Graphics backend (RSX → D3D12) | **In Progress** — upstream has D3D12 backend, not wired in |
| Audio (cellAudio → WASAPI/SDL) | Not Started |
| Input (cellPad → XInput/SDL) | Not Started |
| SPU task handling | Not Started |
| Survival Mode playable | Not Started |
| Story Mode playable | Not Started |
| DLC species support | Not Started |

### What's Working

- **35,208 PPU functions** statically recompiled to native C++ and compiled to x86-64
- **ps3recomp v0.5.1 runtime** — D3D12 backend available, RSX command processor, FIFO watchdog, real `cellSaveData` / `cellSysutil` / `cellGcmSys` implementations
- **OPD-scan pass** (`scripts/scan_opds.py`) recovers vtable-only functions the lifter doesn't discover statically
- **Guest callback dispatch** (`g_ps3_guest_caller`) — HLE bridges can fire guest callbacks for sysutil events / vblank / save-data completion
- **CRT initialization** chain runs cleanly (argc/argv setup, TLS, heap, SPU stubs)
- **Import resolution** for all 24 PS3 modules
- **Indirect call dispatch** through function pointer tables and vtables (OPD fallback + linear HLE search + binary-search dispatch table)
- **LV2 syscall handling** for threading, memory, filesystem, events
- **Game initialization** completes — subsystem init, _cellGcmInitBody, main loop entered
- **Watchdog tooling** — RIP-to-guest-function resolver and call-rate sampling for diagnosing guest spins

### Current Blockers

- **No renderer** — RSX commands aren't being submitted; game runs the loop but draws nothing (upstream D3D12 backend is built but not yet wired into TJ)
- **Many NULL indirect calls** — late-init phase iterates a vtable region (`0x003CCxxx`) where the vtable hasn't been populated. Guarded (returns r3=0) but indicates a missing constructor or HLE bridge
- **One unmapped call** at `0x00041920` — only remaining static-discovery gap
- **No input/audio/graphics output** — backends not wired

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
