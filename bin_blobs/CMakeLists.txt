cmake_minimum_required(VERSION 3.28)

set(CMAKE_TOOLCHAIN_FILE ${CMAKE_CURRENT_SOURCE_DIR}/arm-toolchain.cmake)

project(bin_blobs C CXX ASM)

set(CMAKE_C_STANDARD 23)
set(CMAKE_C_STANDARD_REQUIRED TRUE)
set(CMAKE_CXX_STANDARD 23)
set(CMAKE_CXX_STANDARD_REQUIRED TRUE)

find_program(CMAKE_OBJCOPY ${TOOLCHAIN_PREFIX}objcopy)

function(add_bin TARGET SOURCES)
    add_executable(${TARGET})

    target_sources(${TARGET} PRIVATE ${SOURCES})

    set(EXTRA_ARGS ${ARGN})
    list(LENGTH EXTRA_ARGS NUM_EXTRA_ARGS)
    if (${NUM_EXTRA_ARGS} GREATER 0)
        list(GET EXTRA_ARGS 0 LINK_SCRIPT_NAME)
    else()
        set(LINK_SCRIPT_NAME ${TARGET}.ld)
    endif()
    set(LINK_SCRIPT ${CMAKE_CURRENT_SOURCE_DIR}/${LINK_SCRIPT_NAME})
    if(EXISTS ${LINK_SCRIPT})
        set_target_properties(${TARGET} PROPERTIES
            LINK_FLAGS -T${LINK_SCRIPT}
            LINK_DEPENDS ${LINK_SCRIPT}
            )
    endif()

    set(BIN_NAME ${TARGET}.bin)
    add_custom_command(
        TARGET ${TARGET} POST_BUILD
        COMMAND ${CMAKE_OBJCOPY} -O binary ${TARGET} ${BIN_NAME}
        )

    install(FILES ${CMAKE_BINARY_DIR}/${BIN_NAME} TYPE BIN)
endfunction()

function(add_bin_emc TARGET SOURCES)
    add_bin(${TARGET} ${SOURCES} ${ARGN})
    target_compile_definitions(${TARGET} PRIVATE CPU_EMC)
    target_compile_options(${TARGET} PRIVATE -mthumb -march=armv7-m)
endfunction()

function(add_bin_efc TARGET SOURCES)
    add_bin(${TARGET} ${SOURCES} ${ARGN})
    target_compile_definitions(${TARGET} PRIVATE CPU_EFC)
    target_compile_options(${TARGET} PRIVATE -marm -mcpu=cortex-r5)
endfunction()

function(add_bin_eap TARGET SOURCES)
    add_bin(${TARGET} ${SOURCES} ${ARGN})
    target_compile_definitions(${TARGET} PRIVATE CPU_EAP)
    target_compile_options(${TARGET} PRIVATE -marm -mcpu=cortex-a7)
endfunction()

add_bin_emc(emc_dled_hook emc_dled_hook.cpp)
add_bin_emc(emc_cmd_handler emc_cmd_handler.cpp)

add_bin_efc(efc_thunk efc_thunk.S)
add_bin_efc(efc_stcm_counter efc_stcm_counter.cpp)

add_bin_efc(efc_uart_shell uart_shell.cpp uart_shell.ld)
add_bin_eap(eap_uart_shell uart_shell.cpp uart_shell.ld)

add_bin_efc(efc_uart_shell_rom uart_shell.cpp uart_shell_rom.ld)
target_compile_definitions(efc_uart_shell_rom PRIVATE ROM_ENV)
