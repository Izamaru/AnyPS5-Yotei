"""Compile the actual pool against a tiny Vulkan mock, without a GPU/SDK."""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
LIBS = ROOT / "base/AnyPS5/core/libs"
GRAPHICS = Path("prx/libSceAgcDriver/Graphics")

ENVIRONMENT = r'''
#include <cstdlib>
int TestSetEnv(const char* name, const char* value, int overwrite) {
#ifdef _WIN32
    if (!overwrite && std::getenv(name) != nullptr) return 0;
    return _putenv_s(name, value);
#else
    return setenv(name, value, overwrite);
#endif
}
int TestUnsetEnv(const char* name) {
#ifdef _WIN32
    return _putenv_s(name, "");
#else
    return unsetenv(name);
#endif
}
'''

CONTEXT = r'''
#pragma once
#include <cstddef>
#include <cstdint>
#include <memory>
#include <stdexcept>
using VkDevice = void*;
using VkBuffer = std::uintptr_t;
using VkDeviceMemory = std::uintptr_t;
using VkDeviceAddress = std::uint64_t;
using VkDeviceSize = std::uint64_t;
using VkBufferUsageFlags = std::uint32_t;
using VkMemoryPropertyFlags = std::uint32_t;
using VkResult = int;
constexpr auto VK_NULL_HANDLE = 0;
constexpr auto VK_SUCCESS = 0, VK_ERROR_OUT_OF_DEVICE_MEMORY = -2, VK_ERROR_OUT_OF_HOST_MEMORY = -1;
constexpr auto VK_BUFFER_USAGE_STORAGE_BUFFER_BIT = 1, VK_BUFFER_USAGE_TRANSFER_SRC_BIT = 2, VK_BUFFER_USAGE_TRANSFER_DST_BIT = 4;
constexpr auto VK_MEMORY_PROPERTY_DEVICE_LOCAL_BIT = 1, VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT = 2, VK_MEMORY_PROPERTY_HOST_COHERENT_BIT = 4;
constexpr auto VK_MEMORY_HEAP_DEVICE_LOCAL_BIT = 1, VK_MAX_MEMORY_HEAPS = 16;
constexpr auto VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO = 1, VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_FLAGS_INFO = 2, VK_MEMORY_ALLOCATE_DEVICE_ADDRESS_BIT = 1;
constexpr auto VK_WHOLE_SIZE = ~std::uint64_t{0};
struct VkMemoryAllocateInfo { int sType; const void* pNext = nullptr; VkDeviceSize allocationSize = 0; std::uint32_t memoryTypeIndex = 0; };
struct VkMemoryAllocateFlagsInfo { int sType; const void* pNext; unsigned flags; unsigned deviceMask; };
struct VkPhysicalDeviceMemoryProperties {
    unsigned memoryHeapCount = 1;
    struct Heap { VkDeviceSize size = 4ull << 30; unsigned flags = 1; } memoryHeaps[16];
    struct Type { unsigned propertyFlags = 6; } memoryTypes[32];
};
using PFN_vkUnmapMemory = void(*)(VkDevice,VkDeviceMemory);
using PFN_vkDestroyBuffer = void(*)(VkDevice,VkBuffer,const void*);
using PFN_vkFreeMemory = void(*)(VkDevice,VkDeviceMemory,const void*);
using PFN_vkAllocateMemory = VkResult(*)(VkDevice,const VkMemoryAllocateInfo*,const void*,VkDeviceMemory*);
using PFN_vkMapMemory = VkResult(*)(VkDevice,VkDeviceMemory,VkDeviceSize,VkDeviceSize,unsigned,void**);
void* MockFunction(const char*);
namespace AgcDriver::Graphics {
class BufferPool;
inline void Check(VkResult result, const char* message) { if (result != VK_SUCCESS) throw std::runtime_error(message); }
struct Context {
    VkDevice device = nullptr;
    VkPhysicalDeviceMemoryProperties memory;
    struct Limits { unsigned maxMemoryAllocationCount = 24576; } limits;
    mutable std::shared_ptr<BufferPool> bufferPool;
    template<class T> T Function(const char* name) const { return reinterpret_cast<T>(MockFunction(name)); }
};
}
'''

CASES = r'''
#include "prx/libSceAgcDriver/Graphics/include/BufferPool.hpp"
#include "prx/libSceAgcDriver/Graphics/include/ResourceBudget.hpp"
#include <cassert>
#include <cstring>
#include <set>
using namespace AgcDriver::Graphics;
constexpr std::size_t MiB = 1048576;
std::set<VkDeviceMemory> freed;
void unmap(VkDevice, VkDeviceMemory) {}
void destroy(VkDevice, VkBuffer, const void*) {}
void release(VkDevice, VkDeviceMemory memory, const void*) { assert(freed.insert(memory).second); }
VkResult allocate(VkDevice, const VkMemoryAllocateInfo*, const void*, VkDeviceMemory*) { return -2; }
VkResult map(VkDevice,VkDeviceMemory,VkDeviceSize,VkDeviceSize,unsigned,void**) { return -2; }
void* MockFunction(const char* name) {
    if (!strcmp(name,"vkUnmapMemory")) return reinterpret_cast<void*>(unmap);
    if (!strcmp(name,"vkDestroyBuffer")) return reinterpret_cast<void*>(destroy);
    if (!strcmp(name,"vkFreeMemory")) return reinterpret_cast<void*>(release);
    if (!strcmp(name,"vkAllocateMemory")) return reinterpret_cast<void*>(allocate);
    if (!strcmp(name,"vkMapMemory")) return reinterpret_cast<void*>(map);
    assert(false); return nullptr;
}
BufferAllocation allocation(unsigned id, std::size_t size = 16 * MiB) {
    return {id,id,nullptr,0,size,size,1,6,0,false,size};
}
int main() {
    TestUnsetEnv("APS5_HOST_POOL_MIB");
    assert(ResourceBudget("APS5_HOST_POOL_MIB", 123) == 123);
    for (auto bad : {"", "-1", "-0", "1x", " 1", "16385", "9999999999999999999999999"}) {
        TestSetEnv("APS5_HOST_POOL_MIB",bad,1);
        assert(ResourceBudget("APS5_HOST_POOL_MIB",123) == 123);
    }
    TestSetEnv("APS5_HOST_POOL_MIB","32",1);
    assert(ResourceBudget("APS5_HOST_POOL_MIB",123) == 32 * MiB);
    {
        Context context;
        BufferPool pool(context);
        pool.Put(allocation(1));
        pool.Put(allocation(2));
        auto recent = pool.Take(16 * MiB,1);
        assert(recent && recent->memory == 2);
        pool.Put(*recent);
        pool.Put(allocation(3));
        assert(freed.contains(1)); // Least recent free buffer, not a live buffer.
        auto live = pool.Take(16 * MiB,1);
        assert(live && live->memory == 3);
        pool.Put(allocation(4));
        pool.Put(allocation(5));
        assert(!freed.contains(3)); // A checked-out allocation is never evicted.
        pool.Put(*live);
        pool.Put(allocation(6,64 * MiB));
        assert(freed.contains(6)); // Too large to retain, freed immediately.
    }
    assert(freed.size() == 6);
    TestSetEnv("APS5_HOST_POOL_MIB","0",1);
    {
        Context context;
        BufferPool pool(context);
        pool.Put(allocation(7));
        assert(!pool.Take(16 * MiB,1));
    }
    assert(freed.contains(7));
}
'''


class NativeBudgets(unittest.TestCase):
    @unittest.skipUnless(shutil.which("g++"), "requires g++")
    def test_direct_backing_counts_failure_and_64bit_request(self):
        source = (LIBS / "prx/libkernel/DirectMemory/DirectMemory.cpp").read_text()
        begin = source.index("#ifdef _WIN32\nstd::atomic<std::uint64_t> directBackingLive")
        body = source[begin:source.index("struct PhysicalPage", begin)]
        prefix = r'''
#include <atomic>
#include <cstddef>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cassert>
#include <system_error>
#undef __linux__
#define _WIN32 1
using DWORD = std::uint32_t;
using HANDLE = void*;
struct MEMORYSTATUSEX { DWORD dwLength; std::uint64_t ullAvailPhys = 0, ullAvailPageFile = 0; };
bool GlobalMemoryStatusEx(MEMORYSTATUSEX* state) { state->ullAvailPhys = 123; state->ullAvailPageFile = 456; return true; }
constexpr auto INVALID_HANDLE_VALUE = nullptr;
constexpr auto PAGE_EXECUTE_READWRITE = 64;
constexpr auto SEC_RESERVE = 0x04000000;
bool fail = false;
unsigned closes = 0;
unsigned forgotten = 0;
int creationFlags = 0;
std::uint64_t requested = 0;
HANDLE CreateFileMappingW(HANDLE, void*, int flags, DWORD hi, DWORD lo, void*) {
    creationFlags = flags;
    requested = (std::uint64_t{hi} << 32u) | lo;
    return fail ? nullptr : reinterpret_cast<void*>(1);
}
unsigned long GetLastError() { return 1450; }
void CloseHandle(HANDLE) { ++closes; }
int WinProtFromPosix(int prot) { return prot; }
namespace GuestArena {
void GuestArenaMap_nid_postfix(void*, std::size_t, HANDLE, std::uint64_t, int) {}
void GuestArenaMapDirect_nid_postfix(void*, std::size_t, HANDLE, std::uint64_t, int) {}
void GuestArenaForgetSection_nid_postfix(HANDLE section) { assert(section); ++forgotten; }
}
'''
        cases = r'''
int main() {
    TestSetEnv("APS5_RESOURCE_TRACE", "1", 1);
    constexpr std::uint64_t size = 0x125800000ull;
    {
        PhysicalBacking backing(size, 0, 0);
        assert(requested == size);
        assert(creationFlags == EXPECTED_FLAGS);
        assert(directBackingLive == size && directBackingPeak == size);
        fail = true;
        try { PhysicalBacking refused(size, 0, 0); assert(false); }
        catch (const std::system_error& error) { assert(error.code().value() == 1450); }
        assert(directBackingLive == size && closes == 0);
    }
    assert(directBackingLive == 0 && directBackingPeak == size && closes == 1);
    assert(forgotten == EXPECTED_FORGOTTEN);
}
'''
        with tempfile.TemporaryDirectory() as tmp:
            cpp, binary = Path(tmp) / "direct.cpp", Path(tmp) / "direct-test"
            expected_flags = 'PAGE_EXECUTE_READWRITE | SEC_RESERVE' if 'SEC_RESERVE' in body else 'PAGE_EXECUTE_READWRITE'
            expected_forgotten = int('GuestArenaForgetSection_nid_postfix' in body)
            definitions = f'\nconstexpr auto EXPECTED_FLAGS = {expected_flags};\nconstexpr unsigned EXPECTED_FORGOTTEN = {expected_forgotten};\n'
            cpp.write_text(ENVIRONMENT + prefix + definitions + body + cases)
            compiled = subprocess.run(["g++", "-std=c++20", str(cpp), "-o", str(binary)], capture_output=True, text=True)
            self.assertEqual(compiled.returncode, 0, compiled.stderr)
            ran = subprocess.run([str(binary)], capture_output=True, text=True)
            self.assertEqual(ran.returncode, 0, ran.stderr)
            self.assertIn("win_error=1450", ran.stderr)
            self.assertIn("released", ran.stderr)

    @unittest.skipUnless(shutil.which("g++"), "requires g++")
    def test_actual_pool_eviction_reuse_live_buffers_and_parsing(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            include = directory / GRAPHICS / "include"
            include.mkdir(parents=True)
            (include / "Context.hpp").write_text(CONTEXT)
            profile = directory / "prx/libSceAgcDriver/Execution/include/ProfileOutput.hpp"
            profile.parent.mkdir(parents=True)
            profile.write_text("#pragma once\nnamespace AgcDriver { inline void ProfilePrint_nid_no_patch(const char*, ...) {} }\n")
            main = directory / "main.cpp"
            main.write_text(ENVIRONMENT + CASES)
            output = directory / "test-pool"
            process = subprocess.run(["g++", "-std=c++20", "-O1", "-pthread", "-I", str(directory), "-I", str(LIBS),
                str(main), str(LIBS / GRAPHICS / "src/BufferPool.cpp"), "-o", str(output)], capture_output=True, text=True)
            self.assertEqual(process.returncode, 0, process.stderr)
            result = subprocess.run([str(output)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
