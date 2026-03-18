# Progress Log

## Phase 0: Project Setup — COMPLETE

- [x] Project scaffold created
- [x] CMakeLists.txt with ps3recomp integration
- [x] Game configuration (config/tokyojungle.toml)
- [x] Entry point and runtime bootstrap (src/main.cpp)
- [x] Initial game-specific stubs (src/stubs.cpp)
- [x] Analysis pipeline script (scripts/analyze.py)
- [x] PKG extraction script (scripts/extract_pkg.py)
- [x] SELF decryption script (scripts/decrypt_self.py)

## Phase 1: Binary Analysis — COMPLETE

- [x] Extract game files from PKG (1,117 files, 2.4 GB)
- [x] Decrypt EBOOT.BIN → EBOOT.ELF (via RPCS3)
- [x] Parse ELF structure (ELF64 BE PPC64, 3.7 MB)
- [x] Discover function boundaries (24,372 functions found)
- [x] Identify NID imports (318 functions across 24 modules)
- [x] Map NID hashes to function names (93/318 resolved so far)
- [x] Catalog game asset structure
- [x] Document findings in TECHNICAL.md

## Phase 2: Code Generation — COMPLETE

- [x] Full PPU disassembly (825,498 lines)
- [x] Lift to C source code (2,255,297 lines, 135 MB)
- [x] Generate function table & header (26,217 declarations)
- [ ] Initial compilation attempt (expect errors)
- [ ] Fix compilation errors in generated code

## Phase 3: Boot Sequence — NOT STARTED

- [ ] Set up virtual filesystem mapping
- [ ] Implement missing NID stubs for remaining 225 unresolved functions
- [ ] Get past CRT initialization
- [ ] Successfully load all required HLE modules
- [ ] Handle game-specific init quirks
- [ ] Reach the game's main loop

## Phase 4: Graphics — NOT STARTED

- [ ] Catalog RSX commands used (33 cellGcmSys functions)
- [ ] Implement cellResc (15 functions) for resolution scaling
- [ ] Build Vulkan/D3D12 backend (or contribute to ps3recomp's)
- [ ] Shader translation (RSX vertex/fragment → HLSL/SPIR-V)
- [ ] cellFont + cellFontFT rendering (31 functions)
- [ ] First rendered frame

## Phase 5: Audio & Media — NOT STARTED

- [ ] Verify cellAudio integration (11 functions, all resolved)
- [ ] Handle cellSail media playback (32 functions — cutscenes)
- [ ] cellPamf format support (2 functions)
- [ ] SGD audio format decoding

## Phase 6: Input — NOT STARTED

- [ ] sys_io / cellPad controller mapping (16 functions)
- [ ] XInput / SDL integration

## Phase 7: SPU — NOT STARTED

- [ ] Analyze cellSpurs usage (32 functions)
- [ ] Identify SPU task types from binary
- [ ] HLE critical SPU tasks

## Phase 8: Network Stubs — MOSTLY DONE

- [x] Trophy system stubs (src/stubs.cpp)
- [x] Network stubs (src/stubs.cpp)
- [ ] sceNpCommerce2 stubs (DLC — 16 functions)
- [ ] Remaining PSN stubs

## Phase 9: Gameplay — NOT STARTED

- [ ] Title screen loads
- [ ] Survival Mode playable
- [ ] Story Mode playable
- [ ] Save/Load working
- [ ] Local co-op functional

## Phase 10: Polish — NOT STARTED

- [ ] Performance optimization
- [ ] Resolution scaling beyond 720p
- [ ] DLC species support
- [ ] Widescreen/ultrawide support
- [ ] Steam Deck compatibility
