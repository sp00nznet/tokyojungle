# Testing and performance

Every result below comes from hands-on play unless it says otherwise.

## Play-tested

Every result here comes from someone actually playing the game, not from a script:

| Date | Session | Result |
|------|---------|--------|
| 2026-10-11 | Survival, busy areas | Map labels and text render correctly |
| 2026-10-12 | Survival, several runs | Menus and HUD fixed (they used to flicker or vanish) |
| 2026-10-13 | ~13 min Survival | 29-30 FPS in busy areas, no HUD loss, game saved normally |
| 2026-10-13 | ~22 min Survival | Exposed a rare command-ring deadlock (freeze), since fixed |
| 2026-10-13 | ~14 min full run | No freeze; game saved during play |
| **2026-10-14** | **2 h 10 min, 102 in-game years** | **30.0 FPS average, 99.7% of frames on time, no crash or freeze** |

Between the hands-on runs, every build was also checked with an automated hidden
run: boot, menus, into Survival, frame rate and error checks.

## Frame pacing (2-hour run, 2026-10-14)

| Measure | Result |
|---------|--------|
| Gameplay frames | 219,510 over 7,310 s |
| Average frame rate | **30.0 FPS** (the game's native rate) |
| Frames on time (37 ms or less) | **99.7%** |
| Frames over 45 ms | 21 in two hours (first-sight shaders, area loads) |
| Median / 99th percentile frame time | 33.3 ms / 35.7 ms |

The day before, the same measurement gave 27 FPS with 26-29% of frames late.

## How it got here (September → October 2026)

- **Renderer.** Prefix replay with GPU ownership, plus early texture-label publication with a batch cap.
- **HUD.** 384 texture slots; the HUD used to vanish when 96 ran out.
- **Long-session freeze.** A 4096-entry vertex-shader cache with a hash index replaced a cache that filled up after about 13 minutes and froze the game.
- **Anti-aliasing.** The PS3's SPU MLAA job, the biggest single slowdown, is skipped and replaced by a host FXAA pass.
- **Shader hitches.** An on-disk shader cache and background shader compiling cut the worst first-sight hitch from 678 ms to 161 ms. Vertex conversion runs on 8 threads.
- **Command ring.** A libgcm-style FIFO recycle fixed a deadlock in very large frames.
- **Guest hot paths.** Environment checks that ran thousands of times a second are now cached. That removed almost all of the remaining late frames.
- **Window.** Alt no longer freezes the game, and closing the window ends the game cleanly, waiting for any save in progress.
- **PC controls.** An in-game Controls screen (F1) rebinds every key, mouse button and controller button.

The August-September development notes are kept in
[docs/HISTORY-2026-09.md](docs/HISTORY-2026-09.md). They cover the boot, the stalled
game-clock counter and the first frames.
