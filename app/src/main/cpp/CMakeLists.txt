cmake_minimum_required(VERSION 3.22.1)
project(rpytl_llama C CXX)

set(CMAKE_C_STANDARD 11)
set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

# llama.cpp (third_party/llama.cpp 서브모듈, 태그 고정) 를 정적으로 묶어 .so 하나로 만든다.
set(BUILD_SHARED_LIBS OFF CACHE BOOL "" FORCE)
set(LLAMA_BUILD_COMMON OFF CACHE BOOL "" FORCE)
set(LLAMA_BUILD_TESTS OFF CACHE BOOL "" FORCE)
set(LLAMA_BUILD_TOOLS OFF CACHE BOOL "" FORCE)
set(LLAMA_BUILD_EXAMPLES OFF CACHE BOOL "" FORCE)
set(LLAMA_BUILD_SERVER OFF CACHE BOOL "" FORCE)
set(LLAMA_BUILD_APP OFF CACHE BOOL "" FORCE)
set(LLAMA_OPENSSL OFF CACHE BOOL "" FORCE)
set(GGML_NATIVE OFF CACHE BOOL "" FORCE)        # 크로스 컴파일
set(GGML_OPENMP OFF CACHE BOOL "" FORCE)        # docs/android.md 권장
set(GGML_LLAMAFILE OFF CACHE BOOL "" FORCE)
set(GGML_BACKEND_DL OFF CACHE BOOL "" FORCE)
if(ANDROID_ABI STREQUAL "arm64-v8a")
    # 갤럭시 S24 (Cortex-X4/A720/A520) 기준: dotprod + i8mm + fp16. 2021 년 이전 기기에서는 동작 안 할 수 있음.
    set(GGML_CPU_ARM_ARCH "armv8.2-a+dotprod+i8mm+fp16" CACHE STRING "" FORCE)
endif()

add_subdirectory(${CMAKE_CURRENT_LIST_DIR}/../../../../third_party/llama.cpp llama-build)

add_library(rpytl_llama SHARED llama_jni.cpp)
target_link_libraries(rpytl_llama llama log android)
target_link_options(rpytl_llama PRIVATE "-Wl,-z,max-page-size=16384")
