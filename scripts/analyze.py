#!/usr/bin/env python3
"""
Tokyo Jungle Recompiled — Binary Analysis Pipeline

This script orchestrates the ps3recomp toolchain to:
1. Parse the decrypted ELF binary
2. Resolve NID imports against the database
3. Discover function boundaries
4. Disassemble PPU code
5. Lift to C source code
6. Generate the function table

Prerequisites:
- Decrypted EBOOT.ELF in input/
- ps3recomp tools accessible (set PS3RECOMP_TOOLS env var or use --tools-dir)
- Python packages: pycryptodome, capstone, construct, tabulate, tomli, tqdm
"""

import argparse
import os
import subprocess
import sys
from pathlib import Path

# Project paths
PROJECT_ROOT = Path(__file__).parent.parent
INPUT_DIR = PROJECT_ROOT / "input"
OUTPUT_DIR = PROJECT_ROOT / "generated"
CONFIG_FILE = PROJECT_ROOT / "config" / "tokyojungle.toml"


def find_tools_dir() -> Path:
    """Locate the ps3recomp tools directory."""
    # Check environment variable first
    env_dir = os.environ.get("PS3RECOMP_TOOLS")
    if env_dir:
        return Path(env_dir)

    # Check sibling directory (common layout)
    sibling = PROJECT_ROOT.parent / "ps3recomp" / "tools"
    if sibling.exists():
        return sibling

    print("ERROR: Cannot find ps3recomp tools directory.")
    print("Set PS3RECOMP_TOOLS environment variable or use --tools-dir")
    sys.exit(1)


def run_tool(tools_dir: Path, script: str, args: list[str], desc: str) -> bool:
    """Run a Python tool. `script` may be a bare filename (relative to
    `tools_dir`, e.g. ps3recomp tools) or an absolute path (e.g. our local
    scripts/scan_opds.py)."""
    sp = Path(script)
    script_path = sp if sp.is_absolute() else tools_dir / script
    if not script_path.exists():
        print(f"ERROR: Tool not found: {script_path}")
        return False

    print(f"\n{'='*60}")
    print(f"  {desc}")
    print(f"{'='*60}")

    cmd = [sys.executable, str(script_path)] + args
    print(f"Running: {' '.join(cmd)}\n")

    result = subprocess.run(cmd, cwd=str(PROJECT_ROOT))
    if result.returncode != 0:
        print(f"\nERROR: {script} failed with exit code {result.returncode}")
        return False

    return True


def check_prerequisites() -> bool:
    """Verify all prerequisites are met."""
    elf_path = INPUT_DIR / "EBOOT.ELF"

    if not elf_path.exists():
        print("ERROR: EBOOT.ELF not found in input/ directory")
        print()
        print("You must provide a decrypted ELF from your own copy of Tokyo Jungle.")
        print("Use RPCS3's built-in decryption or another tool to decrypt EBOOT.BIN -> EBOOT.ELF")
        print()
        print("Expected game IDs:")
        print("  NPUA80523 (NA Digital)")
        print("  BCAS20219 (JP Disc)")
        print("  NPEA00421 (EU Digital)")
        return False

    # Check file size sanity (PS3 ELFs are typically several MB)
    size_mb = elf_path.stat().st_size / (1024 * 1024)
    print(f"Found EBOOT.ELF ({size_mb:.1f} MB)")

    if size_mb < 1:
        print("WARNING: ELF file seems unusually small. Is it properly decrypted?")
    if size_mb > 100:
        print("WARNING: ELF file seems unusually large. Verify it's the correct file.")

    return True


def main():
    parser = argparse.ArgumentParser(description="Tokyo Jungle binary analysis pipeline")
    parser.add_argument("--tools-dir", type=Path, help="Path to ps3recomp tools/ directory")
    parser.add_argument("--skip-to", choices=["functions", "lift"],
                        help="Skip to a specific pipeline stage")
    parser.add_argument("--dry-run", action="store_true", help="Print commands without executing")
    args = parser.parse_args()

    print("=" * 60)
    print("  Tokyo Jungle Recompiled — Analysis Pipeline")
    print("=" * 60)

    # Prerequisites
    if not check_prerequisites():
        return 1

    tools_dir = args.tools_dir or find_tools_dir()
    print(f"Using tools from: {tools_dir}")

    # Create output directory
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    elf = str(INPUT_DIR / "EBOOT.ELF")
    output = str(OUTPUT_DIR)

    # The upstream lifter (v0.5.1+) folds ELF parsing, NID resolution and
    # disassembly into the lifter itself; we only need find_functions plus
    # an OPD-scan pass (the new lifter doesn't discover vtable-only
    # functions on its own; we recover them by walking the data segment for
    # OPD entries pointing into the code segment).
    stages = [
        ("functions", "find_functions.py",
         [elf, "--output", f"{output}/functions.json"],
         "Phase 1: Discovering function boundaries"),

        ("opds", str(PROJECT_ROOT / "scripts" / "scan_opds.py"),
         ["--in",  f"{output}/functions.json",
          "--out", f"{output}/functions_with_opds.json",
          "--elf", elf],
         "Phase 2: Recovering vtable-only functions via OPD scan"),

        ("lift", "ppu_lifter.py",
         [elf, "--functions", f"{output}/functions_with_opds.json",
          "--output", output,
          "--header-name", "ppu_recomp.h",
          "--source-name", "ppu_recomp.c"],
         "Phase 3: Lifting PPU to C++"),
    ]

    # Skip stages if requested
    skip = True if args.skip_to else False
    for stage_name, script, stage_args, desc in stages:
        if args.skip_to and stage_name == args.skip_to:
            skip = False
        if skip:
            print(f"\nSkipping: {desc}")
            continue

        if args.dry_run:
            print(f"\n[DRY RUN] Would run: {script} {' '.join(stage_args)}")
            continue

        if not run_tool(tools_dir, script, stage_args, desc):
            print(f"\nPipeline failed at stage: {stage_name}")
            print("Fix the issue and re-run with --skip-to to resume.")
            return 1

    print("\n" + "=" * 60)
    print("  Pipeline complete!")
    print("=" * 60)
    print(f"\nGenerated files are in: {OUTPUT_DIR}")
    print("Next step: cmake -B build -G Ninja && cmake --build build")

    return 0


if __name__ == "__main__":
    sys.exit(main())
