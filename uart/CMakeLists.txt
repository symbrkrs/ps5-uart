cmake_minimum_required(VERSION 3.28)

# Pull in SDK (must be before project)
set(PICO_SDK_PATH ${CMAKE_CURRENT_SOURCE_DIR}/../../pico-sdk)
if(NOT EXISTS ${PICO_SDK_PATH})
    include(FetchContent)
    FetchContent_Declare(
        pico_sdk
        GIT_REPOSITORY https://github.com/raspberrypi/pico-sdk
        GIT_TAG master
        GIT_SUBMODULES_RECURSE FALSE
        )
    message("Downloading Raspberry Pi Pico SDK")
    FetchContent_Populate(pico_sdk)
    set(PICO_SDK_PATH ${pico_sdk_SOURCE_DIR})
endif()
include(${PICO_SDK_PATH}/pico_sdk_init.cmake)

set(PICO_BOARD "pico")

project(uart C CXX ASM)
set(CMAKE_C_STANDARD 23)
set(CMAKE_C_STANDARD_REQUIRED TRUE)
set(CMAKE_CXX_STANDARD 23)
set(CMAKE_CXX_STANDARD_REQUIRED TRUE)

# Initialize the SDK
pico_sdk_init()

option(ENABLE_DEBUG_STDIO
    "Creates additional cdc interface (0) for stdout/stdin; enables some debug spew")

add_executable(uart)

# so tusb_config.h can be found
target_include_directories(uart PRIVATE
    ${CMAKE_CURRENT_LIST_DIR}/config
    )

pico_generate_pio_header(uart ${CMAKE_CURRENT_LIST_DIR}/pio_uart.pio)

target_sources(uart PRIVATE
    ps5_uart.cpp
    )

target_compile_options(uart PRIVATE
    -Wall
    -Werror
    # silences cryptic gcc warning:
    # note: parameter passing for argument of type 'std::format_args'
    #   {aka 'std::basic_format_args<std::basic_format_context<std::__format::_Sink_iter<char>, char> >'}
    #   changed in GCC 9.1
    # https://gcc.gnu.org/gcc-9/changes.html
    # https://gcc.gnu.org/bugzilla/show_bug.cgi?id=88469
    -Wno-psabi
    )

target_link_libraries(uart PRIVATE
    pico_runtime
    tinyusb_device
    hardware_pio
    )

pico_enable_stdio_uart(uart DISABLED)

if(ENABLE_DEBUG_STDIO)
target_compile_definitions(uart PRIVATE ENABLE_DEBUG_STDIO)
target_link_libraries(uart PRIVATE pico_stdlib)
pico_enable_stdio_usb(uart ENABLED)
endif()

pico_add_extra_outputs(uart)

install(FILES ${CMAKE_BINARY_DIR}/uart.uf2 TYPE BIN)
