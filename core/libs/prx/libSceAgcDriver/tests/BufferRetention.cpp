#include "prx/libSceAgcDriver/Graphics/include/BufferPool.hpp"
#include <algorithm>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <set>
#include <stdexcept>
#include <string>

using namespace AgcDriver::Graphics;

namespace {

constexpr VkDeviceSize MiB = VkDeviceSize{1} << 20u;
constexpr VkBufferUsageFlags Usage = VK_BUFFER_USAGE_STORAGE_BUFFER_BIT;
constexpr VkMemoryPropertyFlags Properties = VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT | VK_MEMORY_PROPERTY_HOST_COHERENT_BIT;
std::uintptr_t nextHandle = 1;
std::set<VkDeviceMemory> liveMemory;
std::set<VkBuffer> liveBuffers;
bool invalidDestruction = false;

void CheckTest(bool condition, const char* message) {
    if (!condition) throw std::runtime_error(message);
}

VKAPI_ATTR void VKAPI_CALL Unmap(VkDevice, VkDeviceMemory) {}

VKAPI_ATTR void VKAPI_CALL Destroy(VkDevice, VkBuffer buffer, const VkAllocationCallbacks*) {
    if (liveBuffers.erase(buffer) != 1) invalidDestruction = true;
}

VKAPI_ATTR void VKAPI_CALL Free(VkDevice, VkDeviceMemory memory, const VkAllocationCallbacks*) {
    if (liveMemory.erase(memory) != 1) invalidDestruction = true;
}

VKAPI_ATTR VkResult VKAPI_CALL Allocate(VkDevice, const VkMemoryAllocateInfo*, const VkAllocationCallbacks*, VkDeviceMemory*) {
    throw std::runtime_error("retention test unexpectedly requested Vulkan memory");
}

VKAPI_ATTR VkResult VKAPI_CALL Map(VkDevice, VkDeviceMemory, VkDeviceSize, VkDeviceSize, VkMemoryMapFlags, void**) {
    throw std::runtime_error("retention test unexpectedly mapped Vulkan memory");
}

PFN_vkVoidFunction VKAPI_CALL Resolve(VkDevice, const char* name) {
    if (std::strcmp(name, "vkUnmapMemory") == 0) return reinterpret_cast<PFN_vkVoidFunction>(Unmap);
    if (std::strcmp(name, "vkDestroyBuffer") == 0) return reinterpret_cast<PFN_vkVoidFunction>(Destroy);
    if (std::strcmp(name, "vkFreeMemory") == 0) return reinterpret_cast<PFN_vkVoidFunction>(Free);
    if (std::strcmp(name, "vkAllocateMemory") == 0) return reinterpret_cast<PFN_vkVoidFunction>(Allocate);
    if (std::strcmp(name, "vkMapMemory") == 0) return reinterpret_cast<PFN_vkVoidFunction>(Map);
    throw std::runtime_error("unexpected Vulkan function in retention test");
}

Context TestContext() {
    Context context{};
    context.deviceProc = Resolve;
    context.limits.maxMemoryAllocationCount = 1u << 20u;
    return context;
}

BufferAllocation Allocation(std::size_t bytes, VkDeviceSize allocated, VkBufferUsageFlags usage = Usage) {
    const auto buffer = reinterpret_cast<VkBuffer>(nextHandle++);
    const auto memory = reinterpret_cast<VkDeviceMemory>(nextHandle++);
    liveBuffers.insert(buffer);
    liveMemory.insert(memory);
    return {buffer, memory, nullptr, 0, allocated, bytes, usage, Properties, 0, false, bytes};
}

void CheckTier(std::size_t bytes, VkDeviceSize budget) {
    {
        BufferPool pool(TestContext());
        const auto allocated = std::max<VkDeviceSize>(bytes, budget / 2);
        const auto first = Allocation(bytes, allocated);
        pool.Put(first);
        auto live = pool.Take(bytes, Usage, Properties);
        if (budget < allocated) {
            CheckTest(!live && !liveMemory.contains(first.memory), "a disabled tier retained a returned allocation");
            return;
        }
        CheckTest(live && live->memory == first.memory, "a permitted allocation was not reused");
        const auto capacity = budget / allocated;
        const auto oldest = Allocation(bytes, allocated);
        pool.Put(oldest);
        BufferAllocation newest{};
        for (VkDeviceSize index = 0; index < capacity; ++index) {
            newest = Allocation(bytes, allocated);
            pool.Put(newest);
        }
        CheckTest(!liveMemory.contains(oldest.memory), "the configured byte limit did not evict the oldest idle allocation");
        CheckTest(liveMemory.contains(live->memory), "budget eviction destroyed a checked-out allocation");
        const auto reused = pool.Take(bytes, Usage, Properties);
        CheckTest(reused && reused->memory == newest.memory, "the newest cached allocation was not reused");
        pool.Put(*reused);
        pool.Put(*live);
        const auto oversized = Allocation(bytes, budget + 1, Usage | VK_BUFFER_USAGE_TRANSFER_SRC_BIT);
        pool.Put(oversized);
        CheckTest(!liveMemory.contains(oversized.memory), "an allocation larger than the byte limit was retained");
    }
    CheckTest(liveMemory.empty() && liveBuffers.empty(), "pool destruction leaked a retained allocation");
}

void CheckIndependentTiers(VkDeviceSize hostBudget, VkDeviceSize smallBudget) {
    if (hostBudget == 0 || smallBudget == 0) return;
    {
        BufferPool pool(TestContext());
        const auto small = Allocation(512, std::min<VkDeviceSize>(smallBudget, 512));
        pool.Put(small);
        const auto hostBytes = std::max<VkDeviceSize>(MiB, hostBudget / 2);
        for (unsigned index = 0; index < 4; ++index) pool.Put(Allocation(MiB, hostBytes));
        const auto reused = pool.Take(512, Usage, Properties);
        CheckTest(reused && reused->memory == small.memory, "host-tier eviction discarded an idle small-tier allocation");
        pool.Put(*reused);
    }
    CheckTest(liveMemory.empty() && liveBuffers.empty(), "tier-isolation test leaked an allocation");
}

}

int main(int argc, char** argv) {
    try {
        CheckTest(argc == 3, "expected host and small budgets in MiB");
        const auto hostBudget = std::stoull(argv[1]) * MiB;
        const auto smallBudget = std::stoull(argv[2]) * MiB;
        CheckTier(MiB, hostBudget);
        CheckTier(512, smallBudget);
        CheckIndependentTiers(hostBudget, smallBudget);
        CheckTest(!invalidDestruction && liveMemory.empty() && liveBuffers.empty(), "a Vulkan resource was leaked or destroyed twice");
        std::puts("buffer retention budgets, eviction, reuse and live-allocation checks passed");
        return 0;
    } catch (const std::exception& error) {
        std::fprintf(stderr, "%s\n", error.what());
        return 1;
    }
}
