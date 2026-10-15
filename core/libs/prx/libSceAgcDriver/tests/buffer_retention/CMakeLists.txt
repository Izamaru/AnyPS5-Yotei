cmake_minimum_required(VERSION 3.22)

get_filename_component(agcRetentionDriverRoot "${CMAKE_CURRENT_SOURCE_DIR}/../.." ABSOLUTE)
get_filename_component(agcRetentionLibsRoot "${agcRetentionDriverRoot}/../.." ABSOLUTE)
if(CMAKE_SOURCE_DIR STREQUAL CMAKE_CURRENT_SOURCE_DIR)
    project(BufferRetentionTests LANGUAGES CXX)
    enable_testing()
    set(ANYPS5_TEST_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/tests")
    set(ANYPS5_VULKAN_HEADERS_INCLUDE_DIR "${agcRetentionLibsRoot}/../../3rdparty/Vulkan-Headers/include")
endif()

find_package(Threads REQUIRED)
add_executable(agc_buffer_retention_tests
    ../BufferRetention.cpp
    "${agcRetentionDriverRoot}/Graphics/src/BufferPool.cpp")
target_include_directories(agc_buffer_retention_tests PRIVATE
    "${agcRetentionLibsRoot}" "${ANYPS5_VULKAN_HEADERS_INCLUDE_DIR}")
target_compile_features(agc_buffer_retention_tests PRIVATE cxx_std_20)
target_link_libraries(agc_buffer_retention_tests PRIVATE Threads::Threads)
set_target_properties(agc_buffer_retention_tests PROPERTIES CXX_EXTENSIONS OFF
    RUNTIME_OUTPUT_DIRECTORY "$<1:${ANYPS5_TEST_OUTPUT_DIRECTORY}>")
if(MINGW)
    target_compile_options(agc_buffer_retention_tests PRIVATE -fasynchronous-unwind-tables)
    target_link_options(agc_buffer_retention_tests PRIVATE -static -static-libgcc -static-libstdc++)
endif()

function(add_retention_case name host small expectedHost expectedSmall)
    add_test(NAME agc_buffer_retention_${name}
        COMMAND agc_buffer_retention_tests ${expectedHost} ${expectedSmall})
    set_tests_properties(agc_buffer_retention_${name} PROPERTIES TIMEOUT 20
        ENVIRONMENT "APS5_HOST_POOL_MIB=${host};APS5_SMALL_POOL_MIB=${small};APS5_BUFFER_POOL_SLOTS=4096;APS5_STAGING_POOL_MIB=0"
        ENVIRONMENT_MODIFICATION "APS5_BUFFER_POOL_SHARED=unset:;APS5_PROFILE_DRAW=unset:;APS5_NO_BUFFER_CLASSES=unset:")
endfunction()

add_retention_case(zero 0 0 0 0)
add_retention_case(bounded 32 1 32 1)
add_retention_case(host_disabled 0 1 0 1)
add_retention_case(small_disabled 32 0 32 0)
add_retention_case(host_invalid bad 1 512 1)
add_retention_case(small_invalid 32 bad 32 64)
add_retention_case(minimum 1 1 1 1)
add_retention_case(maximum 16384 16384 16384 16384)
add_retention_case(negative -1 -1 512 64)
add_retention_case(plus +1 +1 512 64)
add_retention_case(suffix 1x 1x 512 64)
add_retention_case(leading_space " 1" " 1" 512 64)
add_retention_case(trailing_space "1 " "1 " 512 64)
add_retention_case(over_limit 16385 16385 512 64)
add_retention_case(overflow 18446744073709551616 18446744073709551616 512 64)
add_retention_case(empty "" "" 512 64)
add_test(NAME agc_buffer_retention_defaults COMMAND agc_buffer_retention_tests 512 64)
set_tests_properties(agc_buffer_retention_defaults PROPERTIES TIMEOUT 20
    ENVIRONMENT "APS5_BUFFER_POOL_SLOTS=4096;APS5_STAGING_POOL_MIB=0"
    ENVIRONMENT_MODIFICATION "APS5_HOST_POOL_MIB=unset:;APS5_SMALL_POOL_MIB=unset:;APS5_BUFFER_POOL_SHARED=unset:;APS5_PROFILE_DRAW=unset:;APS5_NO_BUFFER_CLASSES=unset:")
