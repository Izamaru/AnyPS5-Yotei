from pathlib import Path
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from tools import project as p

class ShaderDiagnosticsTests(unittest.TestCase):
    def test_capture_directory_is_per_run_and_child_environment_only(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(p, 'PROJECT_ROOT', Path(temp)):
            env = dict(os.environ)
            code = 'import os,json; print(json.dumps({k:os.environ[k] for k in ("APS5_DUMP_SHADERS","APS5_SHADER_DUMP_DIR")}))'
            for token in ('one', 'two'):
                with patch.object(p, 'stamp', return_value=token):
                    self.assertEqual(p.log_command([sys.executable, '-c', code], label='game', env=env, capture_shaders=True), 0)
                lines = (Path(temp)/'logs'/f'game_{token}.log').read_text().splitlines()
                data = next(json.loads(x) for x in lines if x.startswith('{'))
                self.assertEqual(data['APS5_DUMP_SHADERS'], '1')
                self.assertEqual(Path(data['APS5_SHADER_DUMP_DIR']), Path(temp)/'logs'/f'game_{token}.shaders')
                self.assertTrue(Path(data['APS5_SHADER_DUMP_DIR']).is_dir())
            self.assertEqual(env, dict(os.environ))

    @unittest.skipUnless(shutil.which('g++'), 'requires g++')
    def test_registration_does_not_hide_dispatch_and_failed_write_can_retry(self):
        path = p.BASE/'core/libs/prx/libSceAgcDriver/Execution/src/Driver/Shaders/ShaderDiagnostics.cpp'
        text = path.read_text()
        body = text[text.index('std::string Driver::dumpRequest('):text.rfind('\n}')]
        source = r'''
#include <filesystem>
#include <fstream>
#include <string>
#include <set>
#include <mutex>
#include <cstdlib>
#include <cstdio>
#include <cstdint>
#include <cassert>
#include <stdexcept>
namespace ShaderRecompiler {
struct RecompileRequest { std::string value; };
struct RequestSerializer { std::string Serialize(const RecompileRequest& r) { return r.value; } };
}
struct Driver { static std::string dumpRequest(std::uint64_t, const ShaderRecompiler::RecompileRequest&, bool); };
'''+body+r'''
std::string read(const std::string& p) { std::ifstream f(p); return std::string(std::istreambuf_iterator<char>(f), {}); }
int main() {
#ifdef _WIN32
    _putenv_s("APS5_SHADER_DUMP_DIR", "captures");
#else
    setenv("APS5_SHADER_DUMP_DIR", "captures", 1);
#endif
    assert(Driver::dumpRequest(0x123, {"failed"}, false).empty());
    std::filesystem::create_directory("captures");
    const auto registration = Driver::dumpRequest(0x123, {"registration"}, true);
    const auto dispatch = Driver::dumpRequest(0x123, {"dispatch"}, false);
    assert(!registration.empty() && !dispatch.empty() && registration != dispatch);
    assert(read(registration) == "registration");
    assert(read(dispatch) == "dispatch");
    assert(Driver::dumpRequest(0x123, {"duplicate"}, false) == dispatch);
    assert(read(dispatch) == "dispatch");
}
'''
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); cpp = root/'test.cpp'; exe = root/'test.exe'; cpp.write_text(source)
            subprocess.run(['g++', '-std=c++20', str(cpp), '-o', str(exe)], check=True, capture_output=True)
            subprocess.run([str(exe)], cwd=root, check=True, capture_output=True)
