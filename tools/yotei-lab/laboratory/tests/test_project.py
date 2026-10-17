import argparse
from contextlib import ExitStack
import io
import json
import os
from pathlib import Path
import struct
import subprocess
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

from tools import project as p


def elf_header(machine=62, endian=1):
    header = bytearray(64)
    header[:7] = b'\x7fELF\x02' + bytes([endian, 1])
    struct.pack_into('<H', header, 18, machine)
    struct.pack_into('<I', header, 20, 1)
    struct.pack_into('<H', header, 52, 64)
    return header


class ProjectTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.base = self.root / 'base/AnyPS5'
        self.game = self.root / 'games/Ghost-of-Yotei'
        self.runtime = self.game / 'runtime'
        self.source = self.game / 'source/app0/eboot.bin'
        self.base.mkdir(parents=True)
        (self.base / 'core/relinker').mkdir(parents=True)
        (self.base / 'core/relinker/main.cpp').write_text('// fixture')
        (self.base / 'CMakeLists.txt').write_text('# fixture')
        (self.base / '.gitmodules').write_bytes(b'[submodule "SDL"]\npath = 3rdparty/SDL2\nurl = https://example.invalid/SDL\n')
        self.source.parent.mkdir(parents=True)
        (self.source.parent / 'sce_module').mkdir()
        self.source.write_bytes(elf_header())
        for name, value in [('PROJECT_ROOT', self.root), ('BASE', self.base), ('GAME', self.game),
                            ('SOURCE_EBOOT', self.source), ('RUNTIME', self.runtime)]:
            self.stack.enter_context(patch.object(p, name, value))

    def args(self, **values):
        return argparse.Namespace(input=None, cpu='amd', convert_only=True, **values)

    def test_main_prunes_logs_after_failed_command_even_if_backup_cleanup_fails(self):
        logs = self.root / 'logs'
        logs.mkdir()
        for n in range(51):
            path = logs / f'run-{n:02}.log'
            path.write_text('old run')
            os.utime(path, ns=(n * 10**9, n * 10**9))
        def failed(args):
            with (logs / 'run-50.log').open('a') as output:
                output.write('command failed')
            raise RuntimeError('test command failure')
        args = argparse.Namespace(command='status', func=failed)
        with patch.object(p.setup_windows, 'activate'), \
             patch.object(p, 'make_parser') as parser, \
             patch.object(p.updater, 'recover', return_value=False), \
             patch.object(p.updater, 'prune_backups', side_effect=RuntimeError('backup busy')), \
             patch.object(p, 'announce'), patch.object(p, 'error', return_value=1):
            parser.return_value.parse_args.return_value = args
            self.assertEqual(p.main(), 1)
        self.assertEqual(len(list(logs.glob('*.log'))), 50)
        self.assertFalse((logs / 'run-00.log').exists())
        self.assertTrue((logs / 'run-50.log').exists())

    def test_unicode_child_output_preserves_utf8_log_on_legacy_console(self):
        buffer = io.BytesIO()
        console = io.TextIOWrapper(buffer, encoding='cp1252', write_through=True)
        command = [sys.executable, '-c', "import sys;sys.stdout.buffer.write(bytes.fromhex('e298baefbfbd0a'))"]
        with patch.object(p.sys, 'stdout', console):
            self.assertEqual(p.log_command(command, label='encoding-test'), 0)
        self.assertIn(b'\\u263a\\ufffd', buffer.getvalue())
        log = next((self.root / 'logs').glob('encoding-test_*.log'))
        self.assertIn('\u263a\ufffd', log.read_text(encoding='utf-8'))
        console.close()

    def test_snapshot_without_git_is_accepted(self):
        p.require_base()
        self.assertEqual(p.missing_dependencies(), [str(Path('3rdparty/SDL2'))])

    def test_reject_arm_big_endian_and_truncated(self):
        for content in [elf_header(183), elf_header(endian=2), elf_header()[:20], b'SELF' + bytes(60)]:
            self.source.write_bytes(content)
            self.assertFalse(p.elf_is_64bit(self.source))
        self.source.write_bytes(elf_header())
        self.assertTrue(p.elf_is_64bit(self.source))

    def test_reject_conflicting_or_self_modules(self):
        (self.source.parent / 'sce_modules').mkdir()
        with self.assertRaisesRegex(RuntimeError, 'coexistem'):
            p.validate_modules(self.source)
        (self.source.parent / 'sce_modules').rmdir()
        (self.source.parent / 'sce_module/libc.prx').write_bytes(b'SELF')
        with self.assertRaisesRegex(RuntimeError, 'Modulo'):
            p.validate_modules(self.source)

    def test_failed_conversion_preserves_previous_runtime(self):
        self.runtime.mkdir()
        output = self.runtime / 'GhostOfYotei.exe'
        output.write_bytes(b'previous build')
        with patch.object(p, 'locate_relinker', return_value=self.source), \
             patch.object(p, 'log_command', side_effect=RuntimeError('conversion failed')):
            with self.assertRaises(RuntimeError):
                p.cmd_relink(self.args())
        self.assertEqual(output.read_bytes(), b'previous build')
        self.assertFalse(list(self.game.glob('yotei-convert-*')))

    def test_relink_success_manifest_ignores_stale_registries(self):
        self.runtime.mkdir()
        (self.runtime / 'old.registry.json').write_text('[]')
        (self.runtime / 'user-resource.dat').write_text('preserve')
        p.write_json(self.runtime / 'port_manifest.json', {'generated_files': ['old.registry.json']})
        def convert(command, cwd=None, **kwargs):
            Path(command[-1]).write_bytes(b'MZ synthetic')
            (cwd / 'GhostOfYotei.registry.json').write_text('[]')
            return 0
        with patch.object(p, 'locate_relinker', return_value=self.source), \
             patch.object(p, 'log_command', side_effect=convert):
            self.assertEqual(p.cmd_relink(self.args()), 0)
        manifest = json.loads((self.runtime / 'port_manifest.json').read_text())
        self.assertEqual(manifest['registries'], ['GhostOfYotei.registry.json'])
        self.assertTrue(manifest['convert_only'])
        self.assertEqual(manifest['source_sha256'], p.sha256(self.source))
        self.assertFalse((self.runtime / 'old.registry.json').exists())
        self.assertEqual((self.runtime / 'user-resource.dat').read_text(), 'preserve')

    def test_manifest_paths_cannot_escape_runtime(self):
        self.runtime.mkdir()
        with self.assertRaisesRegex(RuntimeError, 'fora do runtime'):
            p.runtime_path('../source/app0/eboot.bin')

    def test_dependencies_use_exact_revision_without_replacing_source(self):
        revision = 'a' * 40
        gitmodules = (self.base / '.gitmodules').read_bytes()
        def git(command, cwd=None, **kwargs):
            if command[:2] == ['git', 'checkout']:
                (cwd / '.gitmodules').write_bytes(gitmodules.replace(b'\n', b'\r\n'))
            if command[:3] == ['git', 'submodule', 'update']:
                dep = cwd / '3rdparty/SDL2'
                dep.mkdir(parents=True)
                (dep / 'CMakeLists.txt').write_text('# fixed dependency')
                (dep / '.git').write_text('temporary checkout link')
        with patch.object(p, 'require_tool'), patch.object(p, 'git_commit', return_value=revision), \
             patch.object(p, 'log_command', side_effect=git) as run, \
             patch.object(p.subprocess, 'check_output', side_effect=[revision, ' ' + 'b' * 40 + ' 3rdparty/SDL2\n']):
            self.assertEqual(p.cmd_deps(argparse.Namespace()), 0)
        commands = [call.args[0] for call in run.call_args_list]
        self.assertIn(['git', 'fetch', '--depth', '1', 'origin', revision], commands)
        self.assertEqual((self.base / 'core/relinker/main.cpp').read_text(), '// fixture')
        self.assertTrue((self.base / '3rdparty/SDL2/CMakeLists.txt').exists())
        self.assertFalse((self.base / '3rdparty/SDL2/.git').exists())
        self.assertFalse(list(self.base.parent.glob('anyps5-deps-*')))

    def test_gitmodules_accepts_crlf_on_either_side(self):
        checkout = self.root / 'checkout'; checkout.mkdir()
        content = (self.base / '.gitmodules').read_bytes()
        for source, fetched in ((content, content.replace(b'\n', b'\r\n')),
                                (content.replace(b'\n', b'\r\n'), content)):
            (self.base / '.gitmodules').write_bytes(source)
            (checkout / '.gitmodules').write_bytes(fetched)
            p.validate_gitmodules(checkout)

    def test_gitmodules_still_rejects_changed_url_path_and_extra_settings(self):
        checkout = self.root / 'checkout'; checkout.mkdir()
        content = (self.base / '.gitmodules').read_bytes()
        for changed in (content.replace(b'example.invalid', b'other.invalid'),
                        content.replace(b'3rdparty/SDL2', b'3rdparty/other'),
                        content + b'branch = other\n'):
            (checkout / '.gitmodules').write_bytes(changed.replace(b'\n', b'\r\n'))
            with self.assertRaisesRegex(RuntimeError, 'mesmo apos normalizar'):
                p.validate_gitmodules(checkout)

    @unittest.skipUnless(shutil.which('git'), 'Requires Git')
    def test_real_git_autocrlf_checkout_is_accepted(self):
        checkout = self.root / 'real-checkout'; checkout.mkdir()
        def git(*args):
            return subprocess.run(['git', '-C', str(checkout), *args],
                                  check=True, capture_output=True)
        git('init', '-q')
        git('config', 'user.name', 'Regression')
        git('config', 'user.email', 'test@example.invalid')
        git('config', 'core.autocrlf', 'false')
        content = (self.base / '.gitmodules').read_bytes()
        target = checkout / '.gitmodules'; target.write_bytes(content)
        git('add', '.gitmodules'); git('-c', 'commit.gpgsign=false', 'commit', '-qm', 'fixture')
        git('config', 'core.autocrlf', 'true'); target.unlink()
        git('checkout', 'HEAD', '--', '.gitmodules')
        self.assertNotEqual(target.read_bytes(), content)
        self.assertIn(b'\r\n', target.read_bytes())
        p.validate_gitmodules(checkout)

    def test_full_build_stops_before_cmake_when_deps_missing(self):
        with patch.object(p, 'require_tool'), patch.object(p, 'check_compiler'), patch.object(p, 'log_command') as run:
            with self.assertRaisesRegex(RuntimeError, 'Dependencias ausentes'):
                p.cmd_build(argparse.Namespace(relinker_only=False, tests=False, jobs=2))
            run.assert_not_called()

    def test_build_modes_are_isolated_and_bounded(self):
        with patch.object(p, 'require_tool'), patch.object(p, 'check_compiler'), patch.object(p, 'log_command') as run:
            p.cmd_build(argparse.Namespace(relinker_only=True, tests=True, jobs=2))
        calls = [call.args[0] for call in run.call_args_list]
        self.assertIn(str(self.base / 'build-relinker'), calls[0])
        self.assertIn('-DANYPS5_RELINKER_ONLY=ON', calls[0])
        self.assertEqual(calls[1][-2:], ['--parallel', '2'])
        self.assertEqual(calls[-1][0], 'ctest')

    def test_failed_tests_keep_compilation_hashes_without_syncing_runtime(self):
        source = self.base / 'core/relinker/main.cpp'
        manifest = {'patches': [{'file': 'core/relinker/main.cpp'}]}
        build = self.base / 'build'
        def command(args, **kwargs):
            build.mkdir(exist_ok=True)
            if args[0] == 'ctest':
                raise RuntimeError('test failure')
        with patch.object(p, 'require_tool'), patch.object(p, 'check_compiler'), \
             patch.object(p, 'missing_dependencies', return_value=[]), \
             patch.object(p.ffmpeg_snapshot, 'prepare'), \
             patch.object(p, 'source_manifest', return_value=manifest), \
             patch.object(p, 'log_command', side_effect=command), \
             patch.object(p, 'cmd_sync_runtime') as sync, patch.object(p.os, 'utime') as touch:
            for _ in range(2):
                with self.assertRaisesRegex(RuntimeError, 'test failure'):
                    p.cmd_build(argparse.Namespace(relinker_only=False, tests=True, jobs=2))
            self.assertEqual(touch.call_count, 1)
            sync.assert_not_called()
        self.assertEqual(json.loads((build / 'pc_lab_source_hashes.json').read_text()),
                         {'core/relinker/main.cpp': p.sha256(source)})

    def test_failed_native_build_does_not_publish_compilation_hashes(self):
        with patch.object(p, 'require_tool'), patch.object(p, 'check_compiler'), \
             patch.object(p, 'missing_dependencies', return_value=[]), \
             patch.object(p.ffmpeg_snapshot, 'prepare'), \
             patch.object(p, 'log_command', side_effect=RuntimeError('compiler failure')):
            with self.assertRaisesRegex(RuntimeError, 'compiler failure'):
                p.cmd_build(argparse.Namespace(relinker_only=False, tests=True, jobs=2))
        self.assertFalse((self.base / 'build/pc_lab_source_hashes.json').exists())

    def test_linux_libraries_cannot_be_used_as_windows_runtime(self):
        if os.name != 'nt':
            with self.assertRaisesRegex(RuntimeError, 'Windows'):
                p.library_inputs()

    def test_cpu_must_be_explicit(self):
        with self.assertRaises(SystemExit):
            p.make_parser().parse_args(['relink'])

    def test_update_routes_through_controlled_updater(self):
        with patch.object(p.updater, 'update', return_value=0) as update:
            self.assertEqual(p.cmd_update(argparse.Namespace(jobs=2)), 0)
            update.assert_called_once_with(p, 2)
        self.assertTrue((self.base / 'core/relinker/main.cpp').exists())

    def test_audit_uses_all_current_registries_and_exit_code(self):
        self.runtime.mkdir()
        names = ['GhostOfYotei.registry.json', 'GhostOfYotei.libc.prx.guest.prx.registry.json']
        for name in names:
            (self.runtime / name).write_text('[]')
        p.write_json(self.runtime / 'port_manifest.json', {'registries': names})
        with patch.object(p, 'log_command', return_value=1) as run:
            self.assertEqual(p.cmd_audit(argparse.Namespace()), 1)
        for name in names:
            self.assertIn(str(self.runtime / name), run.call_args.args[0])


class RealRelinkerTest(unittest.TestCase):
    @unittest.skipUnless(os.getenv('ANYPS5_TEST_RELINKER'), 'Set ANYPS5_TEST_RELINKER to a built relinker')
    def test_real_elf_to_windows_with_source_preservation(self):
        upstream_tests = p.BASE / 'core/relinker/relinker/tests'
        sys.path.insert(0, str(upstream_tests))
        self.addCleanup(sys.path.remove, str(upstream_tests))
        from test_optional_plt import fixture
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / 'source/eboot.bin'
            source.parent.mkdir()
            (source.parent / 'sce_module').mkdir()
            original = fixture()
            source.write_bytes(original)
            runtime = root / 'runtime'
            relinker = Path(os.environ['ANYPS5_TEST_RELINKER']).resolve()
            with patch.object(p, 'PROJECT_ROOT', root), patch.object(p, 'GAME', root), \
                 patch.object(p, 'RUNTIME', runtime), patch.object(p, 'locate_relinker', return_value=relinker):
                for cpu in ('amd', 'intel'):
                    self.assertEqual(p.cmd_relink(argparse.Namespace(input=str(source), cpu=cpu, convert_only=True)), 0)
                    self.assertEqual((runtime / 'GhostOfYotei.exe').read_bytes()[:2], b'MZ')
                    self.assertEqual(source.read_bytes(), original)
                    manifest = json.loads((runtime / 'port_manifest.json').read_text())
                    self.assertTrue(manifest['registries'])
                    self.assertEqual(manifest['cpu'], cpu)


if __name__ == '__main__':
    unittest.main()
