#pragma once

#include <charconv>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <string_view>

namespace AgcDriver::Graphics {

inline std::uint64_t ResourceBudget(const char* variable, std::uint64_t fallback) {
    const char* raw = std::getenv(variable);
    if (raw == nullptr) return fallback;
    const std::string_view text(raw);
    std::uint64_t mib = 0;
    const auto result = std::from_chars(text.data(), text.data() + text.size(), mib);
    if (text.empty() || result.ec != std::errc{} || result.ptr != text.data() + text.size() || mib > 16384) {
        std::fprintf(stderr, "[resources] invalid %s; using default\n", variable);
        return fallback;
    }
    return mib << 20u;
}

}
