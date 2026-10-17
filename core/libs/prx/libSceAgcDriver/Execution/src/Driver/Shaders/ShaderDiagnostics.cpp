#include "prx/libSceAgcDriver/Execution/include/Driver/Driver.hpp"
#include "ControlFlow/RequestSerializer.hpp"
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <stdexcept>
#include <functional>
#include <string_view>

namespace AgcDriver::DriverDetail {

void Driver::reportSkip(const char* kind, const std::string& what) {
    static std::mutex reportedMutex;
    static std::set<std::size_t> reported;
    static std::map<std::string, int> requestDumps;

    static const bool traceSkips = std::getenv("APS5_TRACE_SKIPS") != nullptr;
    static const int dumpLimit = [] { const char* text = std::getenv("APS5_SKIP_REQUEST_DUMPS"); return text ? std::atoi(text) : 8; }();
    static constexpr std::string_view marker = "\nRecompileRequest:\n";
    std::lock_guard lock(reportedMutex);
    std::string line;
    static bool announced = false;
    if (!announced) {
        announced = true;
        char text[160];
        std::snprintf(text, sizeof(text), "[gpu] skip reports: one write each, RecompileRequest dumps capped at %d per reason (APS5_SKIP_REQUEST_DUMPS)\n", dumpLimit);
        line = text;
    }
    const std::string prefix = "[gpu] skipped " + std::string(kind);
    if (reported.insert(std::hash<std::string>{}(what)).second) {
        const auto request = what.find(marker);
        if (request == std::string::npos) {
            line += prefix + ": " + what + "\n";
        } else {
            const auto reason = what.substr(0, request);
            const auto suffix = what.find(' ', request + marker.size());
            auto& dumps = requestDumps[reason];
            if (dumpLimit < 0 || dumps < dumpLimit) {
                ++dumps;
                line += prefix + ": " + what + "\n";
            } else {
                if (dumps == dumpLimit) {
                    ++dumps;
                    line += prefix + ": further RecompileRequest dumps for \"" + reason + "\" are left out\n";
                }
                line += prefix + ": " + reason + " (request left out)" + (suffix == std::string::npos ? std::string() : what.substr(suffix)) + "\n";
            }
        }
    } else if (traceSkips) {
        line += prefix + " again: " + what.substr(0, 100) + "\n";
    }
    if (!line.empty()) std::fwrite(line.data(), 1, line.size(), stderr);
}

std::string Driver::dumpRequest(std::uint64_t address, const ShaderRecompiler::RecompileRequest& request, bool registration) {
    static std::mutex dumpMutex;
    static std::set<std::pair<std::uint64_t, bool>> dumped;
    char name[96];
    std::snprintf(name, sizeof(name), "shader_%llx%s.req", static_cast<unsigned long long>(address), registration ? "_registration" : "");
    std::filesystem::path path(name);
    if (const auto* directory = std::getenv("APS5_SHADER_DUMP_DIR"); directory != nullptr && *directory != '\0') path = std::filesystem::u8path(directory) / path;
    std::lock_guard lock(dumpMutex);
    const auto key = std::pair{address, registration};
    if (dumped.contains(key)) return path.string();
    try {
        const auto text = ShaderRecompiler::RequestSerializer{}.Serialize(request);
        std::ofstream file(path, std::ios::binary | std::ios::trunc);
        file.write(text.data(), static_cast<std::streamsize>(text.size()));
        file.close();
        if (!file) throw std::runtime_error("could not write shader capture");
        dumped.insert(key);
    } catch (const std::exception& error) {
        std::fprintf(stderr, "[gpu] could not serialize request for 0x%llx: %s\n", static_cast<unsigned long long>(address), error.what());
        return {};
    }
    return path.string();
}

}
