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

file(GLOB_RECURSE GENERATED_SOURCES "${GENERATED_DIR}/*.c")

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
add_executable(tokyojungle
    src/main.cpp
    src/stubs.cpp
    ${GENERATED_SOURCES}
)

target_include_directories(tokyojungle PRIVATE
    # Our bridge header and game-specific code
    ${CMAKE_SOURCE_DIR}/src
    # Generated code directory (ppu_recomp.h lives here)
    ${CMAKE_SOURCE_DIR}/generated
    # ps3recomp public headers (ps3emu/*.h)
    ${PS3RECOMP_DIR}/include
    # ps3recomp runtime internal headers (ppu_context.h, ppu_memory.h, etc.)
    ${PS3RECOMP_DIR}/runtime/ppu
    ${PS3RECOMP_DIR}/runtime/memory
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
