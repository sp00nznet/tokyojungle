cmake_minimum_required(VERSION 3.20)
project(tokyojungle_recompiled
    VERSION 0.1.0
    DESCRIPTION "Tokyo Jungle - Static Recompilation for PC"
    LANGUAGES C CXX
)

set(CMAKE_C_STANDARD 17)
set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

# =============================================================================
# ps3recomp runtime
# =============================================================================
set(PS3RECOMP_DIR "${CMAKE_SOURCE_DIR}/../ps3recomp" CACHE PATH "Path to ps3recomp")

if(NOT EXISTS "${PS3RECOMP_DIR}/CMakeLists.txt")
    message(FATAL_ERROR
        "ps3recomp not found at ${PS3RECOMP_DIR}\n"
        "Clone it from https://github.com/sp00nznet/ps3recomp\n"
        "Or set -DPS3RECOMP_DIR=/path/to/ps3recomp"
    )
endif()

add_subdirectory(${PS3RECOMP_DIR} ps3recomp EXCLUDE_FROM_ALL)

# =============================================================================
# Generated recompiled code
# =============================================================================
set(GENERATED_DIR "${CMAKE_SOURCE_DIR}/generated")

file(GLOB_RECURSE GENERATED_SOURCES CONFIGURE_DEPENDS
    "${GENERATED_DIR}/*.c"
    "${GENERATED_DIR}/*.cpp")

if(NOT GENERATED_SOURCES)
    message(WARNING
        "No generated source files found in ${GENERATED_DIR}/\n"
        "Run the analysis pipeline first: python scripts/analyze.py"
    )
    set(GENERATED_SOURCES "")
endif()

# =============================================================================
# Game executable
# =============================================================================
# The runtime library no longer bundles the PPU loader/HLE/VFS translation
# units -- current ps3recomp expects each title to compile them, so the game
# can supply its own function_table and boot harness. Tokyo Jungle was built
# against a runtime that still bundled them, which is why linking against the
# current one left ppu_vm_size, vm_read32, ppu_resv_* and friends unresolved.
set(PS3RECOMP_PPU_SOURCES
    "${PS3RECOMP_DIR}/runtime/ppu/ppu_loader.cpp"   # vm + ELF loader + run
    "${PS3RECOMP_DIR}/runtime/ppu/ppu_hle.cpp"      # NID -> handler dispatch
    "${PS3RECOMP_DIR}/runtime/ppu/ppu_sysprx.cpp"   # boot-critical CRT
    "${PS3RECOMP_DIR}/runtime/ppu/ppu_fs.cpp"       # cellFs VFS
)

add_executable(tokyojungle
    src/main.cpp
    src/stubs.cpp
    src/dispatch_glue.cpp
    ${PS3RECOMP_PPU_SOURCES}
    ${GENERATED_SOURCES}
)

target_include_directories(tokyojungle PRIVATE
    # Our bridge header and game-specific code
    ${CMAKE_SOURCE_DIR}/src
    ${CMAKE_SOURCE_DIR}/src/compat   # <dirent.h>/<unistd.h> Win32 shims for ppu_fs.cpp
    # Generated code directory (ppu_recomp.h lives here)
    ${CMAKE_SOURCE_DIR}/generated
    # ps3recomp public headers (ps3emu/*.h)
    ${PS3RECOMP_DIR}/include
    # ps3recomp runtime internal headers (ppu_context.h, ppu_memory.h, etc.)
    ${PS3RECOMP_DIR}/runtime/ppu
    ${PS3RECOMP_DIR}/runtime/memory
    # LV2 syscall dispatch
    ${PS3RECOMP_DIR}/runtime/syscalls
)

target_link_libraries(tokyojungle PRIVATE
    ps3recomp_runtime
)

# Platform-specific link libraries
if(WIN32)
    target_link_libraries(tokyojungle PRIVATE
        ws2_32
        xinput
        ole32
        bcrypt
    )
else()
    target_link_libraries(tokyojungle PRIVATE
        pthread
        m
    )
endif()

# =============================================================================
# Compiler settings
# =============================================================================
if(MSVC)
    target_compile_options(tokyojungle PRIVATE
        /W3
        /MP          # Parallel compilation (essential for 135MB generated file)
        /bigobj      # Generated files can be huge
        /utf-8
    )
    # Disable warnings common in generated code
    target_compile_options(tokyojungle PRIVATE
        /wd4244      # conversion warnings (uint64 -> uint32 in addr truncation)
        /wd4267      # size_t to uint32 conversion
        /wd4146      # unary minus on unsigned
        /wd4305      # truncation from double to float
        /wd4018      # signed/unsigned mismatch
    )
else()
    target_compile_options(tokyojungle PRIVATE
        -Wall
        -Wno-unused-variable
        -Wno-unused-function
        -Wno-sign-compare
        -Wno-parentheses
        -Wno-unused-but-set-variable
    )
endif()

# =============================================================================
# Install
# =============================================================================
install(TARGETS tokyojungle RUNTIME DESTINATION bin)
