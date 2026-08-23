# Tokyo Jungle Recompiled

**A Pomeranian walks into post-apocalyptic Tokyo. No, seriously.**

Tokyo Jungle (2012) is the most wonderfully absurd game Sony ever published — a survival action game where you play as animals fighting for dominance in a Tokyo abandoned by humanity. One moment you're a tiny Pomeranian sneaking past lions in Shibuya, the next you're a dinosaur asserting territorial control over Yoyogi Park. It's brutal. It's beautiful. It has no business being as good as it is.

And it's about to disappear.

## Why This Exists

Tokyo Jungle was a PS3 exclusive. The Western release was **digital-only** — no disc, no physical backup. Both of its developers are gone (Japan Studio dissolved 2021, Crispy's dormant since 2015). The PS3 store is running on fumes. Sony's PS Plus cloud streaming for PS3 titles has been [broken for months](https://www.playstationlifestyle.net/2026/01/18/ps3-game-streaming-busted-ps-plus-premium/). Nobody is coming to save this game.

So we're doing it ourselves.

**Tokyo Jungle Recompiled** is a native PC port built using [ps3recomp](https://github.com/sp00nznet/ps3recomp) — a static recompilation toolchain that translates PS3 PowerPC binaries into native C code, then compiles them as x86-64 executables. No emulation at runtime. No compatibility layers. Just raw, recompiled native code.

## Status: Phase 8 — Boots to a Window, Audio Initialises, Geometry Not Yet Drawing

> **This is the first 3D title to attempt ps3recomp.** The game boots end-to-end,
> opens a D3D12 window, runs its PSN data-install flow, brings up SPURS and the
> SPU job manager, and completes audio initialisation. Its own clear colour
> reaches the screen. It does not yet draw geometry.

**A note on the function count.** Earlier builds advertised *35,208 lifted
functions*. That number was wrong, and the current one is lower on purpose:
`find_functions` was treating intra-function basic blocks as separate function
entries, inventing tens of thousands of phantoms. It now covers those blocks
properly. The fix was checked against ground truth on a *different* title that
ships an unstripped debug build (+90 real functions recovered, 0 phantoms
introduced) — Tokyo Jungle's own binary is stripped, so its count cannot be
verified directly. The honest figure is **7,924**. A count going down was progress.

| Milestone | Status |
|-----------|--------|
| Binary analysis & function discovery | **Done** — 7,924 functions, ground-truth verified |
| PPU disassembly & code lifting | **Done** — 7 body overrides (heap, abort, pure-virtual) |
| Project scaffold & build system | **Done** — clang-cl + Ninja |
| ELF loading & VM setup | **Done** — 4 GB space, 256 MB VRAM, callback stacks, GCM page |
| CRT initialization | **Done** — argv, TLS (`r13`), guest heap redirected |
| LV2 syscall dispatch | **Done** — plus `PS3_SCTRACE` with guest-caller resolution |
| Import table resolution (PLT/NID) | **Done** — slots derived from the module descriptors |
| Guest threads | **Done** — thread entry trampoline (every thread was a silent no-op) |
| Graphics backend (RSX → D3D12) | **Partial** — window opens, clears present, no geometry |
| PSN data-install flow | **Done** — DataCheck / CreateGameData handshake completes |
| SPU job manager (SPURS) | **Partial** — chains walk, job guards work, 1 of 12 images lifted |
| Audio (cellAudio) | **Partial** — init / PortOpen / PortStart succeed, no output device yet |
| Input (cellPad → XInput/SDL) | Not Started |
| Survival Mode playable | Not Started |
| Story Mode playable | Not Started |
| DLC species support | Not Started |

### What's Working

- **7,924 PPU functions** recompiled to native C++ and compiled to x86-64
- **A window with the game's own pixels in it** — `CELL_GCM` `CLEAR_SURFACE` reaches
  the D3D12 backend and presents; the frame colour is the guest's, not ours
- **The PS3 data-install flow** — the title's installer runs to completion, which
  needed all three of ps3recomp's independent path roots configured (`sys_fs`
  syscalls, the `cellFs` HLE prefix table, and `cellGame`'s content path)
- **Guest threads actually run** — `ppu_install_thread_trampoline`; without it every
  `sys_ppu_thread_create` thread spawned, reported FINISHED, and executed nothing
- **SPURS job chains** — the walker handles JOB / SYNC / NEXT / GUARD, and job
  guards are implemented, so a chain waits for its notify instead of running
  against parameters the game has not filled in yet
- **One SPU image lifted and dispatched** — the "soc-job" sound down-mix chain,
  captured with `SPU_DUMP_MISS` and matched by content fingerprint
- **Audio initialisation** — `cellAudioInit`, `cellAudioPortOpen`,
  `cellAudioSetNotifyEventQueue` and `cellAudioPortStart` all complete
- **Diagnostics that earn their keep** — `TJ_GENMAP` (stub address → import name),
  `TJ_OPD_GUARD` (write-protect the OPD arena), `PS3_SCTRACE`, `TJ_IMPTRACE`,
  and a watchdog that resolves a wedged thread back to a guest function

### Current Blockers

- **No geometry** — the frame clears and presents, but nothing is drawn into it.
  This is the headline problem and the next thing to solve.
- **11 of 12 SPU images are unlifted** — the title dispatches ~100 SPU jobs across
  12 distinct images per run and only the sound job is implemented; the rest log
  `dispatch MISS` and return without doing anything
- **A crash shortly after `cellGcmMapMainMemory`**, i.e. once audio init stops
  blocking the boot and the game reaches render setup
- **`cellFont` is held back** on the title's own stubs (`TJ_KEEP='cellFont*'`), so
  no UI text renders

### Recently Fixed

Two bugs worth naming, because both spent a long time wearing a convincing disguise:

- **`cellAudioPortOpen` was allocating its audio buffer on top of the HLE OPD arena.**
  It used a hardcoded `0x01000000`, commented "free window" — which is exactly where
  this port keeps its import descriptor table. Opening an audio port `memset` 128 KB
  of it to zero, after which every import resolved through those descriptors
  dispatched to a null address. A null `bctr` returns *with r3 untouched*, so the
  guest reads its own first argument back as a status code. The middleware duly
  reported `failed to set notify queue (23A0)` — and `0x23A0` was not an error code
  at all, it was the event-queue key it had just passed in.
- **SPURS never signalled the application.** `cellSpursAttachLv2EventQueue` discarded
  the queue id, so a thread blocked in `sys_event_queue_receive` waiting for job
  completion could never be woken. The title sat there for its entire boot.

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
    + Graphics backend (RSX → D3D12)
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
│   ├── ppu_recomp_0NN.cpp      # 7,924 lifted PPU functions
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

- **RSX Graphics Reverse Engineering** — Working out why the game's draw calls never reach the D3D12 backend. The frame clears and presents correctly, so the path is open; something upstream is not submitting geometry.
- **SPU Program Analysis** — The title dispatches around 100 SPU jobs per run across
  12 distinct images, and only one ("soc-job", the sound down-mix) is lifted. The
  others are raw SPURS job binaries loaded from the game's own data files, so they
  have to be captured at runtime with `SPU_DUMP_MISS` before they can be lifted.
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
