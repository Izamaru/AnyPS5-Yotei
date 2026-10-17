"""Exercise backported function bodies with a mock display, without SDL/Vulkan."""
from pathlib import Path
import os
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
LIBS = ROOT / 'base/AnyPS5/core/libs'


class VrrBackportTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('g++'), 'requires g++')
    def test_output_bounds_modes_and_error_paths(self):
        source = (LIBS / 'prx/libSceVideoOut/src/Output.cpp').read_text()
        helper_start = source.index('static VideoOutOutputStatus outputStatus(')
        helper = source[helper_start:source.index('\n}', helper_start) + 2] + '\n'
        function = source[source.index('int APS5_VABI sceVideoOutGetOutputStatusWithVrr('):
                          source.index('int APS5_VABI sceVideoOutGetResolutionStatus(')]
        driver = (LIBS / 'prx/libSceVideoOut/include/VideoOutDriver.hpp').read_text()
        constants = '\n'.join(re.findall(r'^static constexpr (?:int|uint64_t) VIDEO_OUT_(?:ERROR_INVALID_ADDRESS|ERROR_INVALID_HANDLE|OUTPUT_MODE_119_88HZ|REFRESH_RATE_119_88HZ|REFRESH_RATE_59_94HZ) = .*;', driver, re.M))
        prefix = r'''
#include "SceTypes.hpp"
#include <array>
#include <cassert>
#include <cstring>
#include <mutex>
#include <cstdlib>
#if defined(_WIN32) && defined(__MINGW32__)
#include <unwind.h>
// The standalone mock has no runtime unwinder. Any unexpected exception must
// fail the test; guest exception propagation is covered by the native suite.
extern "C" _Unwind_Reason_Code __gxx_personality_v0(int, _Unwind_Action,
    unsigned long long, _Unwind_Exception*, _Unwind_Context*) { std::abort(); }
#endif
struct ProcessShutdown {};
[[noreturn]] void LibcAwaitExit_nid_postfix() { std::abort(); }
struct TestConfig {
    unsigned width = 1920, height = 1080;
    uint64_t outputMode = 0;
    std::mutex mutex;
    bool open = true;
    void Check() {}
} config;
struct VideoOutDriver {
    static VideoOutDriver& Get() { static VideoOutDriver driver; return driver; }
    bool HasConfig(int handle) { return handle == 1; }
    bool IsOpen(int handle) { return handle == 1 && config.open; }
    TestConfig* GetConfig(int) { return &config; }
};
'''
        cases = r'''
int main() {
    static_assert(sizeof(VideoOutOutputStatus) == 48);
    std::array<unsigned char, 98> buffer;
    for (bool high : {false, true}) {
        config.width = high ? 3840 : 1920;
        config.height = high ? 2160 : 1080;
        config.outputMode = high ? VIDEO_OUT_OUTPUT_MODE_119_88HZ : 0;
        buffer.fill(0xa5);
        assert(sceVideoOutGetOutputStatusWithVrr(1, buffer.data() + 1) == 0);
        assert(buffer[0] == 0xa5);
        for (int i = 81; i < 98; ++i) assert(buffer[i] == 0xa5);
        VideoOutOutputStatus result;
        std::memcpy(&result, buffer.data() + 1, sizeof(result));
        assert(result.resolution == (high ? 2u : 1u));
        assert(result.dynamicRange == 1);
        assert(result.refreshRate == (high ? 13u : 3u));
        for (int i = 17; i <= 80; ++i) assert(buffer[i] == 0);
    }
    buffer.fill(0xa5);
    for (int handle : {-1, 0, 2}) {
        assert(sceVideoOutGetOutputStatusWithVrr(handle, buffer.data()) == static_cast<int>(0x8029000Bu));
        for (auto byte : buffer) assert(byte == 0xa5);
    }
    assert(sceVideoOutGetOutputStatusWithVrr(-1, nullptr) == static_cast<int>(0x80290002u));
    assert(sceVideoOutGetOutputStatusWithVrr(1, nullptr) == static_cast<int>(0x80290002u));
    config.open = false;
    assert(sceVideoOutGetOutputStatusWithVrr(1, buffer.data()) == static_cast<int>(0x8029000Bu));
    for (auto byte : buffer) assert(byte == 0xa5);
}
'''
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            cpp = path / 'vrr.cpp'
            cpp.write_text(prefix + constants + '\n' + helper + function + cases)
            binary = path / 'vrr-test'
            # Use the same guest ABI unwind format as the native runtime.
            unwind = ['-fno-asynchronous-unwind-tables'] if os.name == 'nt' else []
            for defines in ([], ['-D_WIN32']):
                with self.subTest(defines=defines):
                    compiled = subprocess.run(['g++', '-std=c++20', '-O1', *unwind, *defines, '-I', str(LIBS),
                                               str(cpp), '-o', str(binary)], capture_output=True, text=True)
                    self.assertEqual(compiled.returncode, 0, compiled.stderr)
                    subprocess.run([str(binary)], check=True, capture_output=True)
