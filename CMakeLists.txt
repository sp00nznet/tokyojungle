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
# The optional image17 DMA callback exists only inside this title runtime.
# Standalone generated-SPU/test targets include spu_dma.h without this define.
target_compile_definitions(ps3recomp_runtime PRIVATE
    PS3RECOMP_SPU_DMA_IMAGE17_CALLBACK
    PS3RECOMP_SPU_MAIN_WRITE_GATE)
# PC key prompts: key-cap art (libs/input/pad_prompt_art.c) and the UI-atlas
# patcher (libs/video/rsx_prompt_atlas.c) compile only with this define; the
# D3D12 backend then swaps the known button atlases at record time. Runtime
# switch: TJ_PROMPTS=0 shows the original PS3 prompts.
target_compile_definitions(ps3recomp_runtime PRIVATE
    PS3RECOMP_PROMPT_ART)

# =============================================================================
# Generated recompiled code
# =============================================================================
set(GENERATED_DIR "${CMAKE_SOURCE_DIR}/generated")
option(TJ_ENABLE_GENERATED_VM_FAST_ACCESS
    "Use direct VM primitives in generated PPU translation units" ON)

file(GLOB_RECURSE GENERATED_SOURCES CONFIGURE_DEPENDS
    "${GENERATED_DIR}/*.c"
    "${GENERATED_DIR}/*.cpp")

# Keep direct VM access local to generated PPU chunks. Runtime/HLE and lifted
# SPU sources retain the external VM accessors and their instrumentation. Set
# TJ_ENABLE_GENERATED_VM_FAST_ACCESS=OFF to retain runtime VM diagnostic hooks.
file(GLOB GENERATED_PPU_SOURCES CONFIGURE_DEPENDS
    "${GENERATED_DIR}/ppu_recomp_*.cpp")
if(TJ_ENABLE_GENERATED_VM_FAST_ACCESS AND WIN32 AND GENERATED_PPU_SOURCES)
    set_source_files_properties(${GENERATED_PPU_SOURCES} PROPERTIES
        COMPILE_DEFINITIONS TJ_GENERATED_VM_FAST_ACCESS)
    if(CMAKE_CXX_COMPILER_ID STREQUAL "Clang")
        if(MSVC)
            set(TJ_GENERATED_PPU_COMPILE_OPTIONS
                /clang:-fno-strict-aliasing
                /Zc:tlsGuards-)
        else()
            set(TJ_GENERATED_PPU_COMPILE_OPTIONS -fno-strict-aliasing)
        endif()
        set_property(SOURCE ${GENERATED_PPU_SOURCES} APPEND PROPERTY
            COMPILE_OPTIONS ${TJ_GENERATED_PPU_COMPILE_OPTIONS})
    endif()
    message(STATUS "generated PPU VM fast access: ON")
else()
    message(STATUS "generated PPU VM fast access: OFF")
endif()

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

# Lifted SPU images + workload registration. The runtime's SPURS job dispatcher
# matches a guest SPU image by content fingerprint and runs the lifted entry;
# without a match it logs "[spurs-job] dispatch MISS" and runs nothing, and the
# title blocks waiting for SPU work that never starts.
#
# These are NOT embedded SPU ELFs -- SPURS job binaries are raw code+data blobs
# loaded from the game's data files. See src/gen/spu_workloads.c for how the
# image was captured (SPU_DUMP_MISS) and lifted.
option(TJ_ENABLE_SPU "Compile + register the title's lifted SPU images" ON)
option(TJ_ENABLE_SPU_TRACE
    "Keep per-instruction trace calls in lifted SPU images (very slow)" OFF)
option(TJ_ENABLE_SPU_LS_DIAGNOSTICS
    "Keep LS watch/WWS/SMC diagnostic checks in lifted SPU images" OFF)
option(TJ_DIAG_GEOMETRY_FAST_DRAIN
    "DIAGNOSTIC ONLY: bypass normally inert image-17 transfer hooks" OFF)
option(TJ_DIAG_SPU_DIRECT_TAIL
    "DIAGNOSTIC ONLY: use direct musttail transfers for lifted image 17" OFF)
option(TJ_DIAG_TASK9A00_DIRECT_TAIL
    "DIAGNOSTIC ONLY: use direct musttail transfers for task9A00/image15" OFF)
option(TJ_DIAG_TASK9A00_HOT_LOOP_TAIL
    "LEGACY: direct-tail only task9A00 0x7F48 <-> 0x8000" OFF)
option(TJ_TASK9A00_HOT_LOOP_TAIL
    "Use direct musttail for the measured task9A00 0x7F48 <-> 0x8000 hot loop" OFF)
if(TJ_DIAG_GEOMETRY_FAST_DRAIN)
    target_compile_definitions(ps3recomp_runtime PRIVATE
        TJ_DIAG_GEOMETRY_FAST_DRAIN)
endif()
if(TJ_ENABLE_SPU)
    file(GLOB TJ_SPU_LIFTED CONFIGURE_DEPENDS
        "${CMAKE_SOURCE_DIR}/generated/spu_lifted/*/spu_recomp.c")
    if(TJ_DIAG_SPU_DIRECT_TAIL)
        set(TJ_GEOMETRY_SPU_SOURCE
            "${CMAKE_SOURCE_DIR}/generated/spu_lifted/geometryjob17/spu_recomp.c")
        set(TJ_GEOMETRY_SPU_TAIL_SOURCE
            "${CMAKE_SOURCE_DIR}/diagnostics/spu_tail/geometryjob17_spu_recomp.c")
        if(NOT EXISTS "${TJ_GEOMETRY_SPU_TAIL_SOURCE}")
            message(FATAL_ERROR
                "Missing ${TJ_GEOMETRY_SPU_TAIL_SOURCE}; run work/make_geometry_tail_variant.py")
        endif()
        # The variant must come from the current lifted source (which may carry
        # the fuse_spu_loops.py loop fusion; the variant is always unfused).
        set_property(DIRECTORY APPEND PROPERTY CMAKE_CONFIGURE_DEPENDS
            "${TJ_GEOMETRY_SPU_SOURCE}" "${TJ_GEOMETRY_SPU_TAIL_SOURCE}")
        file(SHA256 "${TJ_GEOMETRY_SPU_SOURCE}" TJ_GEOMETRY_SOURCE_SHA256)
        file(STRINGS "${TJ_GEOMETRY_SPU_TAIL_SOURCE}" TJ_GEOMETRY_VARIANT_HASH_LINES
             REGEX "TJ_DIAG_SOURCE_SHA256=")
        list(LENGTH TJ_GEOMETRY_VARIANT_HASH_LINES TJ_GEOMETRY_VARIANT_HASH_LINE_COUNT)
        set(TJ_GEOMETRY_VARIANT_SOURCE_SHA256 "")
        if(TJ_GEOMETRY_VARIANT_HASH_LINE_COUNT EQUAL 1)
            list(GET TJ_GEOMETRY_VARIANT_HASH_LINES 0 TJ_GEOMETRY_VARIANT_HASH_LINE)
            if(TJ_GEOMETRY_VARIANT_HASH_LINE MATCHES "TJ_DIAG_SOURCE_SHA256=([0-9A-Fa-f]+)")
                string(TOLOWER "${CMAKE_MATCH_1}" TJ_GEOMETRY_VARIANT_SOURCE_SHA256)
            endif()
        endif()
        string(TOLOWER "${TJ_GEOMETRY_SOURCE_SHA256}" TJ_GEOMETRY_SOURCE_SHA256)
        if(NOT TJ_GEOMETRY_VARIANT_SOURCE_SHA256 STREQUAL TJ_GEOMETRY_SOURCE_SHA256)
            message(FATAL_ERROR
                "image-17 direct-tail variant is stale or unstamped (needs exactly one "
                "TJ_DIAG_SOURCE_SHA256=${TJ_GEOMETRY_SOURCE_SHA256}); rerun "
                "work/make_geometry_tail_variant.py")
        endif()
        message(STATUS
            "TJ_DIAG_SPU_DIRECT_TAIL: image 17 uses the UNFUSED direct-tail variant "
            "(fuse_spu_loops.py 0x412C<->0x415C fusion not in this build)")
        list(REMOVE_ITEM TJ_SPU_LIFTED "${TJ_GEOMETRY_SPU_SOURCE}")
        # GENERATED_SOURCES independently globbed the same original file.
        # Remove it there too so the diagnostic variant is the sole owner of
        # the image-17 symbols.
        list(REMOVE_ITEM GENERATED_SOURCES "${TJ_GEOMETRY_SPU_SOURCE}")
        list(APPEND TJ_SPU_LIFTED "${TJ_GEOMETRY_SPU_TAIL_SOURCE}")
    endif()
    if(TJ_DIAG_TASK9A00_DIRECT_TAIL AND
       (TJ_TASK9A00_HOT_LOOP_TAIL OR TJ_DIAG_TASK9A00_HOT_LOOP_TAIL))
        message(FATAL_ERROR
            "Full task9A00 direct-tail and hot-loop-tail modes are mutually exclusive")
    endif()
    if(TJ_DIAG_TASK9A00_DIRECT_TAIL OR
       TJ_TASK9A00_HOT_LOOP_TAIL OR
       TJ_DIAG_TASK9A00_HOT_LOOP_TAIL)
        if(NOT CMAKE_C_COMPILER_ID MATCHES "Clang")
            message(FATAL_ERROR
                "task9A00 tail transfers require a Clang C compiler for musttail")
        endif()
        set(TJ_TASK9A00_SPU_SOURCE
            "${CMAKE_SOURCE_DIR}/generated/spu_lifted/task9A00/spu_recomp.c")
        set(TJ_TASK9A00_SPU_TAIL_SOURCE
            "${CMAKE_SOURCE_DIR}/diagnostics/spu_tail/task9A00_spu_recomp.c")
        if(NOT EXISTS "${TJ_TASK9A00_SPU_TAIL_SOURCE}")
            message(FATAL_ERROR
                "Missing ${TJ_TASK9A00_SPU_TAIL_SOURCE}; run work/make_task9A00_tail_variant.py")
        endif()
        file(SHA256 "${TJ_TASK9A00_SPU_SOURCE}" TJ_TASK9A00_SOURCE_SHA256)
        file(STRINGS "${TJ_TASK9A00_SPU_TAIL_SOURCE}"
             TJ_TASK9A00_VARIANT_HASH_LINES
             REGEX "TJ_DIAG_SOURCE_SHA256=")
        list(LENGTH TJ_TASK9A00_VARIANT_HASH_LINES
             TJ_TASK9A00_VARIANT_HASH_LINE_COUNT)
        if(NOT TJ_TASK9A00_VARIANT_HASH_LINE_COUNT EQUAL 1)
            message(FATAL_ERROR
                "task9A00 tail variant needs exactly one TJ_DIAG_SOURCE_SHA256=<64 hex> marker")
        endif()
        list(GET TJ_TASK9A00_VARIANT_HASH_LINES 0 TJ_TASK9A00_VARIANT_HASH_LINE)
        string(REGEX MATCH "TJ_DIAG_SOURCE_SHA256=([0-9A-Fa-f]+)"
               TJ_TASK9A00_VARIANT_HASH_MATCH
               "${TJ_TASK9A00_VARIANT_HASH_LINE}")
        if(NOT TJ_TASK9A00_VARIANT_HASH_MATCH)
            message(FATAL_ERROR
                "task9A00 tail variant has a malformed source hash marker")
        endif()
        set(TJ_TASK9A00_VARIANT_SOURCE_SHA256 "${CMAKE_MATCH_1}")
        string(LENGTH "${TJ_TASK9A00_VARIANT_SOURCE_SHA256}"
               TJ_TASK9A00_VARIANT_SOURCE_SHA256_LENGTH)
        if(NOT TJ_TASK9A00_VARIANT_SOURCE_SHA256_LENGTH EQUAL 64)
            message(FATAL_ERROR
                "task9A00 tail variant source hash must contain 64 hex digits")
        endif()
        string(TOLOWER "${TJ_TASK9A00_VARIANT_SOURCE_SHA256}"
               TJ_TASK9A00_VARIANT_SOURCE_SHA256)
        string(TOLOWER "${TJ_TASK9A00_SOURCE_SHA256}"
               TJ_TASK9A00_SOURCE_SHA256)
        if(NOT TJ_TASK9A00_VARIANT_SOURCE_SHA256 STREQUAL
           TJ_TASK9A00_SOURCE_SHA256)
            message(FATAL_ERROR
                "task9A00 source/variant SHA256 mismatch; regenerate work/make_task9A00_tail_variant.py")
        endif()
        list(REMOVE_ITEM TJ_SPU_LIFTED "${TJ_TASK9A00_SPU_SOURCE}")
        list(REMOVE_ITEM GENERATED_SOURCES "${TJ_TASK9A00_SPU_SOURCE}")
        list(APPEND TJ_SPU_LIFTED "${TJ_TASK9A00_SPU_TAIL_SOURCE}")
    endif()
    list(LENGTH TJ_SPU_LIFTED N_TJ_SPU)
    message(STATUS "lifted SPU images: ${N_TJ_SPU}")
    # Computed guest branches must tail-dispatch. The full runtime resolver is
    # compiled into a static library and cannot guarantee a tail call; repeated
    # guest loops then grow the host stack and become extremely slow. Compile
    # the Clang musttail shim in this title and route every lifted image to it.
    set(TJ_SPU_DISPATCH "${PS3RECOMP_DIR}/lbp/spu_dispatch_mt.c")
    set(TJ_SPU_SOURCES "${CMAKE_SOURCE_DIR}/src/gen/spu_workloads.c"
        ${TJ_SPU_LIFTED} "${TJ_SPU_DISPATCH}")
    # The lifted sources include "spu_context.h" relative to the runtime; scope
    # the include to just these TUs so the huge PPU chunks keep their cached
    # compile command.
    set_source_files_properties(${TJ_SPU_SOURCES} PROPERTIES
        INCLUDE_DIRECTORIES "${PS3RECOMP_DIR}/runtime/spu")
    set_source_files_properties(${TJ_SPU_LIFTED} PROPERTIES
        COMPILE_DEFINITIONS SPU_USE_DISPATCH_MT)
    if(TJ_TASK9A00_HOT_LOOP_TAIL OR TJ_DIAG_TASK9A00_HOT_LOOP_TAIL)
        set_property(SOURCE "${TJ_TASK9A00_SPU_TAIL_SOURCE}" APPEND PROPERTY
            COMPILE_DEFINITIONS TJ_DIAG_TASK9A00_HOT_LOOP_TAIL)
    endif()
    if(TJ_DIAG_GEOMETRY_FAST_DRAIN)
        set_property(SOURCE ${TJ_SPU_LIFTED} APPEND PROPERTY
            COMPILE_DEFINITIONS TJ_DIAG_GEOMETRY_FAST_DRAIN)
    endif()
    if(CMAKE_C_COMPILER_ID MATCHES "Clang|GNU" AND
       CMAKE_SYSTEM_PROCESSOR MATCHES "AMD64|amd64|x86_64|i.86")
        # Match spu_shufb's isolated target feature in lifted SPU translation
        # units so Clang can inline the hot helper instead of calling it for
        # every guest instruction. This does not change PPU/runtime codegen.
        if(MSVC)
            set(TJ_SSSE3_FLAG /clang:-mssse3)
        else()
            set(TJ_SSSE3_FLAG -mssse3)
        endif()
        set_property(SOURCE ${TJ_SPU_LIFTED} APPEND PROPERTY
            COMPILE_OPTIONS ${TJ_SSSE3_FLAG})
        if(MSVC)
            # Lifted guest code: no stack-cookie checks on every function
            # (1,500+ jobs per frame), matching the generated PPU options.
            set_property(SOURCE ${TJ_SPU_LIFTED} APPEND PROPERTY
                COMPILE_OPTIONS /GS- /Zc:tlsGuards-)
        endif()
    endif()
    if(NOT TJ_ENABLE_SPU_TRACE)
        set_property(SOURCE ${TJ_SPU_LIFTED} APPEND PROPERTY
            COMPILE_DEFINITIONS SPU_DISABLE_TRACE)
    endif()
    if(NOT TJ_ENABLE_SPU_LS_DIAGNOSTICS)
        set_property(SOURCE ${TJ_SPU_LIFTED} APPEND PROPERTY
            COMPILE_DEFINITIONS SPU_DISABLE_LS_DIAGNOSTICS)
    endif()
else()
    set(TJ_SPU_SOURCES "")
endif()

add_executable(tokyojungle
    src/main.cpp
    src/tj_rsx_reference.cpp
    src/stubs.cpp
    src/dispatch_glue.cpp
    src/tj_crt_overrides.cpp
    src/tj_prompt_text.cpp        # PC key names in game text (post_lift hook)
    src/tj_pamf_media.cpp
    src/tj_present.cpp
    src/tj_gcm_interrupt.cpp
    ${TJ_SPU_SOURCES}
    src/gen/ppu_hle_nids.cpp   # generated: NID -> ps3recomp lib handler
    ${PS3RECOMP_PPU_SOURCES}
    ${GENERATED_SOURCES}
)

# Window/taskbar/exe icon from your own game files: input/tokyojungle.ico (not in
# the repo; e.g. converted from the title's ICON0.PNG). Skipped when absent.
if(WIN32 AND EXISTS "${CMAKE_SOURCE_DIR}/input/tokyojungle.ico")
    enable_language(RC)
    file(WRITE "${CMAKE_BINARY_DIR}/tokyojungle_icon.rc"
         "1 ICON \"${CMAKE_SOURCE_DIR}/input/tokyojungle.ico\"\n")
    target_sources(tokyojungle PRIVATE "${CMAKE_BINARY_DIR}/tokyojungle_icon.rc")
endif()

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
target_compile_definitions(tokyojungle PRIVATE PS3RECOMP_SPU_MAIN_WRITE_GATE)

if(PS3RECOMP_BUILD_TESTS)
    include(CTest)
    add_executable(tj_import_data_test tests/import_data/main.cpp)
    target_include_directories(tj_import_data_test PRIVATE
        ${CMAKE_SOURCE_DIR}/src
        ${PS3RECOMP_DIR}/include
        ${PS3RECOMP_DIR}/runtime/ppu
        ${PS3RECOMP_DIR}/runtime/memory
    )
    if(BUILD_TESTING)
        add_test(NAME tj_import_data COMMAND tj_import_data_test)
    endif()

    add_executable(tj_rsx_scaled_blit_test
        tests/rsx_scaled_blit/main.c
    )
    target_include_directories(tj_rsx_scaled_blit_test PRIVATE
        ${PS3RECOMP_DIR}/libs/video
    )
    if(MSVC)
        target_compile_options(tj_rsx_scaled_blit_test PRIVATE /W3 /Gy)
        target_link_options(tj_rsx_scaled_blit_test PRIVATE /OPT:REF)
    endif()

    add_executable(tj_rsx_boundary_policy_test
        tests/rsx_boundary_policy/main.c
    )
    target_include_directories(tj_rsx_boundary_policy_test PRIVATE
        ${PS3RECOMP_DIR}/libs/video
    )
    if(MSVC)
        target_compile_options(tj_rsx_boundary_policy_test PRIVATE /W3 /Gy)
        target_link_options(tj_rsx_boundary_policy_test PRIVATE /OPT:REF)
    endif()
    if(BUILD_TESTING)
        add_test(NAME tj_rsx_boundary_policy COMMAND tj_rsx_boundary_policy_test)
    endif()

    add_executable(tj_rsx_stream_hash_test
        tests/rsx_stream_hash/main.c
        ${PS3RECOMP_DIR}/libs/video/rsx_stream_hash.c
    )
    target_include_directories(tj_rsx_stream_hash_test PRIVATE
        ${PS3RECOMP_DIR}/libs/video
    )
    if(MSVC)
        target_compile_options(tj_rsx_stream_hash_test PRIVATE /W3 /Gy)
        target_link_options(tj_rsx_stream_hash_test PRIVATE /OPT:REF)
    endif()
    if(BUILD_TESTING)
        add_test(NAME tj_rsx_stream_hash COMMAND tj_rsx_stream_hash_test)
    endif()

    add_executable(tj_guest_world_rendezvous_test
        tests/guest_world_rendezvous/main.c
        ${PS3RECOMP_DIR}/runtime/memory/guest_world_rendezvous.c
    )
    target_compile_definitions(tj_guest_world_rendezvous_test PRIVATE
        GUEST_WORLD_RENDEZVOUS_TESTING)
    target_include_directories(tj_guest_world_rendezvous_test PRIVATE
        ${PS3RECOMP_DIR}/runtime/memory
        ${PS3RECOMP_DIR}/runtime/spu
    )
    if(BUILD_TESTING)
        add_test(NAME tj_guest_world_rendezvous COMMAND tj_guest_world_rendezvous_test)
    endif()

    add_executable(tj_rsx_texture_pitch_test
        tests/rsx_texture_pitch/main.c
        ${PS3RECOMP_DIR}/libs/video/rsx_commands.c
    )
    target_include_directories(tj_rsx_texture_pitch_test PRIVATE
        ${PS3RECOMP_DIR}/include
        ${PS3RECOMP_DIR}/libs/video
    )
    if(MSVC)
        target_compile_options(tj_rsx_texture_pitch_test PRIVATE /W3 /Gy)
        target_link_options(tj_rsx_texture_pitch_test PRIVATE /OPT:REF)
    endif()

    add_executable(tj_spu_smc_ops_test
        tests/spu_smc_ops/main.c
    )
    target_include_directories(tj_spu_smc_ops_test PRIVATE
        ${PS3RECOMP_DIR}/runtime/spu
    )
    if(MSVC)
        target_compile_options(tj_spu_smc_ops_test PRIVATE /W3 /Gy)
        target_link_options(tj_spu_smc_ops_test PRIVATE /OPT:REF)
    endif()
    if(BUILD_TESTING)
        add_test(NAME tj_spu_smc_ops COMMAND tj_spu_smc_ops_test)
    endif()

    add_executable(tj_spu_main_write_gate_test
        tests/spu_main_write_gate/main.c
        ${PS3RECOMP_DIR}/runtime/spu/spu_main_write_gate.c
    )
    target_include_directories(tj_spu_main_write_gate_test PRIVATE
        ${PS3RECOMP_DIR}/runtime/spu
    )
    target_compile_definitions(tj_spu_main_write_gate_test PRIVATE
        SPU_MAIN_WRITE_GATE_TESTING)
    if(MSVC)
        target_compile_options(tj_spu_main_write_gate_test PRIVATE /W3 /Gy)
        target_link_options(tj_spu_main_write_gate_test PRIVATE /OPT:REF)
    endif()
    if(BUILD_TESTING)
        add_test(NAME tj_spu_main_write_gate COMMAND tj_spu_main_write_gate_test)
    endif()

    add_executable(tj_spu_ls_access_test
        tests/spu_ls_access/main.c
    )
    target_include_directories(tj_spu_ls_access_test PRIVATE
        ${PS3RECOMP_DIR}/runtime/spu
    )
    if(MSVC)
        target_compile_options(tj_spu_ls_access_test PRIVATE /W3 /Gy)
        target_link_options(tj_spu_ls_access_test PRIVATE /OPT:REF)
    endif()
    if(DEFINED TJ_SSSE3_FLAG)
        target_compile_options(tj_spu_ls_access_test PRIVATE ${TJ_SSSE3_FLAG})
    endif()
    if(BUILD_TESTING)
        add_test(NAME tj_spu_ls_access COMMAND tj_spu_ls_access_test)
    endif()

    add_executable(tj_spu_helpers_scalar_test
        ${PS3RECOMP_DIR}/runtime/spu/tests/test_spu_helpers.c
    )
    target_include_directories(tj_spu_helpers_scalar_test PRIVATE
        ${PS3RECOMP_DIR}/runtime/spu
    )
    add_executable(tj_spu_helpers_ssse3_test
        ${PS3RECOMP_DIR}/runtime/spu/tests/test_spu_helpers.c
    )
    target_include_directories(tj_spu_helpers_ssse3_test PRIVATE
        ${PS3RECOMP_DIR}/runtime/spu
    )
    if(MSVC)
        target_compile_options(tj_spu_helpers_scalar_test PRIVATE /W3 /Gy)
        target_compile_options(tj_spu_helpers_ssse3_test PRIVATE /W3 /Gy)
        target_link_options(tj_spu_helpers_scalar_test PRIVATE /OPT:REF)
        target_link_options(tj_spu_helpers_ssse3_test PRIVATE /OPT:REF)
    endif()
    if(DEFINED TJ_SSSE3_FLAG)
        target_compile_options(tj_spu_helpers_ssse3_test PRIVATE ${TJ_SSSE3_FLAG})
    endif()
    if(BUILD_TESTING)
        add_test(NAME tj_spu_helpers_scalar COMMAND tj_spu_helpers_scalar_test)
        add_test(NAME tj_spu_helpers_ssse3 COMMAND tj_spu_helpers_ssse3_test)
    endif()

    add_executable(tj_spu_dispatch_mt_test
        tests/spu_dispatch_mt/main.c
        ${PS3RECOMP_DIR}/lbp/spu_dispatch_mt.c
    )
    target_include_directories(tj_spu_dispatch_mt_test PRIVATE
        ${PS3RECOMP_DIR}/runtime/spu
    )
    if(MSVC)
        target_compile_options(tj_spu_dispatch_mt_test PRIVATE /W3 /Gy)
        target_link_options(tj_spu_dispatch_mt_test PRIVATE /OPT:REF)
    endif()

    add_executable(tj_font_scale_test
        tests/font_scale/main.c
        ${PS3RECOMP_DIR}/libs/font/cellFont.c
    )
    target_include_directories(tj_font_scale_test PRIVATE
        ${PS3RECOMP_DIR}/include
        ${PS3RECOMP_DIR}/libs/font
    )
    if(MSVC)
        target_compile_options(tj_font_scale_test PRIVATE /W3 /Gy)
        target_link_options(tj_font_scale_test PRIVATE /OPT:REF)
    endif()

    add_executable(tj_startup_imports_test
        tests/startup_imports/main.c
        ${PS3RECOMP_DIR}/libs/network/cellHttp.c
        ${PS3RECOMP_DIR}/libs/network/sceNp.c
        ${PS3RECOMP_DIR}/libs/system/cellSysmodule.c
        ${PS3RECOMP_DIR}/libs/system/sysPrxForUser.c
    )
    target_include_directories(tj_startup_imports_test PRIVATE
        ${PS3RECOMP_DIR}/include
        ${PS3RECOMP_DIR}/libs/network
        ${PS3RECOMP_DIR}/libs/system
        ${PS3RECOMP_DIR}/runtime/ppu
    )
    if(WIN32)
        target_link_libraries(tj_startup_imports_test PRIVATE ws2_32 bcrypt)
    endif()
    if(MSVC)
        target_compile_options(tj_startup_imports_test PRIVATE /W3 /Gy)
        target_link_options(tj_startup_imports_test PRIVATE /OPT:REF)
    endif()

    add_executable(tj_audio_pacing_test
        tests/audio_pacing/main.c
    )
    target_include_directories(tj_audio_pacing_test PRIVATE
        ${PS3RECOMP_DIR}/include
        ${PS3RECOMP_DIR}/libs/audio
    )
    if(MSVC)
        target_compile_options(tj_audio_pacing_test PRIVATE /W3 /Gy)
        target_link_options(tj_audio_pacing_test PRIVATE /OPT:REF)
    endif()

    add_executable(tj_pamf_timing_test
        tests/pamf_timing/main.cpp
    )
    target_include_directories(tj_pamf_timing_test PRIVATE
        ${CMAKE_SOURCE_DIR}/src
    )
    if(MSVC)
        target_compile_options(tj_pamf_timing_test PRIVATE /W3 /Gy)
        target_link_options(tj_pamf_timing_test PRIVATE /OPT:REF)
    endif()
    if(BUILD_TESTING)
        add_test(NAME tj_pamf_timing COMMAND tj_pamf_timing_test)
    endif()

    add_executable(tj_jobchain_run_gate_test
        tests/jobchain_run_gate/main.c
    )
    target_include_directories(tj_jobchain_run_gate_test PRIVATE
        ${PS3RECOMP_DIR}/libs/spurs
    )
    if(MSVC)
        target_compile_options(tj_jobchain_run_gate_test PRIVATE /W3 /Gy)
        target_link_options(tj_jobchain_run_gate_test PRIVATE /OPT:REF)
    endif()
    if(BUILD_TESTING)
        add_test(NAME tj_jobchain_run_gate COMMAND tj_jobchain_run_gate_test)
    endif()

    add_executable(tj_jobchain_joblist_test
        tests/jobchain_joblist/main.c
    )
    target_include_directories(tj_jobchain_joblist_test PRIVATE
        ${PS3RECOMP_DIR}/libs/spurs
    )
    if(MSVC)
        target_compile_options(tj_jobchain_joblist_test PRIVATE /W3 /Gy)
        target_link_options(tj_jobchain_joblist_test PRIVATE /OPT:REF)
    endif()
    if(BUILD_TESTING)
        add_test(NAME tj_jobchain_joblist COMMAND tj_jobchain_joblist_test)
    endif()

    add_executable(tj_savedata_guest_abi_test
        tests/savedata_guest_abi/main.c
    )
    target_include_directories(tj_savedata_guest_abi_test PRIVATE
        ${PS3RECOMP_DIR}/include
        ${PS3RECOMP_DIR}/libs/system
    )
    if(MSVC)
        target_compile_options(tj_savedata_guest_abi_test PRIVATE /W3 /Gy)
        target_link_options(tj_savedata_guest_abi_test PRIVATE /OPT:REF)
    endif()

    add_executable(tj_savedata_guest_io_test
        tests/savedata_guest_io/main.c
        ${PS3RECOMP_DIR}/libs/system/cellSaveData.c
    )
    target_include_directories(tj_savedata_guest_io_test PRIVATE
        ${PS3RECOMP_DIR}/include
        ${PS3RECOMP_DIR}/libs/system
        ${PS3RECOMP_DIR}/runtime/ppu
        ${PS3RECOMP_DIR}/runtime/memory
    )
    if(MSVC)
        target_compile_options(tj_savedata_guest_io_test PRIVATE /W3 /Gy)
        target_link_options(tj_savedata_guest_io_test PRIVATE /OPT:REF)
    endif()

    add_executable(tj_memory_container_test
        tests/memory_container/main.c
        ${PS3RECOMP_DIR}/runtime/syscalls/sys_memory.c
    )
    target_include_directories(tj_memory_container_test PRIVATE
        ${PS3RECOMP_DIR}/include
        ${PS3RECOMP_DIR}/runtime/memory
        ${PS3RECOMP_DIR}/runtime/ppu
        ${PS3RECOMP_DIR}/runtime/syscalls
    )
    if(MSVC)
        target_compile_options(tj_memory_container_test PRIVATE /W3 /Gy)
        target_link_options(tj_memory_container_test PRIVATE /OPT:REF)
    endif()

    add_executable(tj_gpio_test
        tests/gpio/main.c
        ${PS3RECOMP_DIR}/runtime/syscalls/sys_gpio.c
    )
    target_include_directories(tj_gpio_test PRIVATE
        ${PS3RECOMP_DIR}/include
        ${PS3RECOMP_DIR}/runtime/memory
        ${PS3RECOMP_DIR}/runtime/ppu
        ${PS3RECOMP_DIR}/runtime/syscalls
    )
    if(MSVC)
        target_compile_options(tj_gpio_test PRIVATE /W3 /Gy)
        target_link_options(tj_gpio_test PRIVATE /OPT:REF)
    endif()

    add_executable(tj_socjob_event_flag_test
        tests/socjob_event_flag/main.c
        generated/spu_lifted/socjob13/spu_recomp.c
    )
    set_source_files_properties(
        generated/spu_lifted/socjob13/spu_recomp.c
        PROPERTIES INCLUDE_DIRECTORIES "${PS3RECOMP_DIR}/runtime/spu")
    target_include_directories(tj_socjob_event_flag_test PRIVATE
        ${PS3RECOMP_DIR}/runtime/spu
        ${CMAKE_SOURCE_DIR}/generated/spu_lifted/socjob13
    )
    if(MSVC)
        target_compile_options(tj_socjob_event_flag_test PRIVATE /W3 /Gy)
        target_link_options(tj_socjob_event_flag_test PRIVATE /OPT:REF)
    endif()
endif()

# Platform-specific link libraries
if(WIN32)
    target_link_libraries(tokyojungle PRIVATE
        ws2_32
        xinput
        ole32
        bcrypt
        # The D3D12 RSX backend is linked now that src/tj_present.cpp drives
        # it; dxguid supplies IID_ID3D12Device / IID_IDXGIAdapter1.
        d3d12
        dxgi
        dxguid
        d3dcompiler
        winmm
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
    # Resolve opt-in runtime samples without changing optimization or codegen.
    target_link_options(tokyojungle PRIVATE /MAP)
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
