# About the project

## Why This Exists

Tokyo Jungle was a PS3 exclusive. The Western release was **digital-only** — no disc, no physical backup. Both of its developers are gone (Japan Studio dissolved 2021, Crispy's dormant since 2015). The PS3 store is running on fumes. Sony's PS Plus cloud streaming for PS3 titles has been [broken for months](https://www.playstationlifestyle.net/2026/01/18/ps3-game-streaming-busted-ps-plus-premium/). Nobody is coming to save this game.

So we're doing it ourselves.

**Tokyo Jungle Recompiled** is a native PC port built using [ps3recomp](https://github.com/sp00nznet/ps3recomp) — a static recompilation toolchain that translates PS3 PowerPC binaries into native C code, then compiles them as x86-64 executables. No emulation at runtime. No compatibility layers. Just raw, recompiled native code.

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

## Project Structure

```
tokyojungle/
├── src/
│   ├── main.cpp                # Entry point, VM init, ELF loading, HLE registration
│   ├── tj_present.cpp          # Present loop: vblank cadence, flips, FIFO drain, clean exit
│   ├── tj_crt_overrides.cpp    # Game-specific overrides (MLAA skip, SPU task signals, ...)
│   ├── tj_pamf_media.cpp       # PAMF movie playback
│   ├── tj_gcm_interrupt.cpp    # GCM interrupt worker
│   ├── stubs.cpp, recomp_bridge.h, import_resolver.h, hle_imports.h, elf_loader.h
│   └── gen/                    # HLE NID table, SPU workload registry
├── scripts/
│   ├── analyze.py, gen_stubs.py  # Lifting pipeline
│   ├── post_lift.py              # Post-lift repairs and hooks
│   └── launch_tokyojungle.ps1    # Launcher with the -Recommended preset
├── patches/ps3recomp/          # Runtime changes as one patch against ps3recomp
├── tests/                      # Native and Python unit tests
├── input/                      # Your game files go here (gitignored)
├── generated/                  # Recompiled C++ output (gitignored)
└── docs/                       # Progress notes and development history
```

## A Love Letter

Tokyo Jungle is one of those rare games that could only exist because someone at Sony said "sure, why not" to a pitch about a Pomeranian surviving the apocalypse. It's weird. It's wonderful. It sold over 500,000 copies. It got a perfect score from Famitsu. And in a few years, without intervention, it will exist only in YouTube videos and fond memories.

Games like this don't deserve to vanish just because the hardware they ran on becomes obsolete. If you've ever watched a golden retriever take down a crocodile in the streets of Shibuya, you know exactly why this project exists.

**Let's keep Tokyo Jungle alive.**

---

*Built with [ps3recomp](https://github.com/sp00nznet/ps3recomp) | Star this repo if you want to pet the Pomeranian*
