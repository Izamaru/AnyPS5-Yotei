import hashlib
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch
import urllib.error
import zipfile
from tools import setup_windows as s
from tools import project as p

class Response(io.BytesIO):
    def geturl(self):
        return 'https://release-assets.githubusercontent.com/fixture'

class SetupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.messages = []
        self.say = self.messages.append

    def package(self, data):
        return dict(s.PACKAGES['ninja'], size=len(data), sha256=hashlib.sha256(data).hexdigest())

    def zip(self, files):
        path = self.root / 'fixture.zip'
        with zipfile.ZipFile(path, 'w') as archive:
            for name, data in files.items():
                archive.writestr(name, data)
        return path

    def test_verified_download_is_reused_without_network(self):
        data = b'official-test-archive'; package = self.package(data)
        with patch.object(s.urllib.request, 'urlopen', return_value=Response(data)) as network:
            path = s.download(package, self.root, self.say)
            self.assertEqual(s.download(package, self.root, self.say), path)
        network.assert_called_once()
        self.assertEqual(path.read_bytes(), data)
        self.assertFalse(list(self.root.glob('*.part')))

    def test_hash_mismatch_is_never_installed(self):
        package = self.package(b'correct')
        with patch.object(s.urllib.request, 'urlopen', return_value=Response(b'incorre')):
            with self.assertRaisesRegex(RuntimeError, 'SHA-256'):
                s.download(package, self.root, self.say)
        self.assertFalse((self.root / package['asset']).exists())
        self.assertFalse(list(self.root.glob('*.part')))

    def test_network_failure_is_retried(self):
        data = b'zip'; package = self.package(data)
        with patch.object(s.urllib.request, 'urlopen', side_effect=[urllib.error.URLError('timeout'), Response(data)]) as network:
            path = s.download(package, self.root, self.say)
        self.assertEqual(network.call_count, 2)
        self.assertEqual(path.read_bytes(), data)

    def test_untrusted_paths_rejected_before_extraction(self):
        for malicious in ('../outside', 'C:/outside', 'a/file:stream', '/absolute'):
            archive = self.zip({'valid.txt': 'ok', malicious: 'bad'})
            target = self.root / 'target'; target.mkdir(exist_ok=True)
            with self.assertRaises(RuntimeError):
                s.unpack(archive, target)
            self.assertFalse((target / 'valid.txt').exists())

    def test_archive_symlink_is_rejected(self):
        path = self.root / 'link.zip'
        with zipfile.ZipFile(path, 'w') as archive:
            info = zipfile.ZipInfo('link'); info.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(info, '../outside')
        with self.assertRaises(RuntimeError):
            s.unpack(path, self.root)

    def test_invalid_new_layout_preserves_existing_install(self):
        existing = self.root / '.toolchain/ninja'; existing.mkdir(parents=True)
        (existing / 'ninja.exe').write_bytes(b'previous')
        archive = self.zip({'wrong.exe': 'new'})
        with patch.object(s, 'download', return_value=archive):
            with self.assertRaisesRegex(RuntimeError, 'Layout'):
                s.install_package('ninja', self.root, self.say)
        self.assertEqual((existing / 'ninja.exe').read_bytes(), b'previous')

    def test_install_promotes_complete_package_and_records_origin(self):
        archive = self.zip({'ninja.exe': 'synthetic binary', 'LICENSE': 'fixture'})
        with patch.object(s, 'download', return_value=archive), patch.object(s, 'output', return_value='1.13.2'), patch.object(s, 'probe', return_value=(True, 'fixture')):
            s.install_package('ninja', self.root, self.say)
        folder = self.root / '.toolchain/ninja'
        self.assertEqual((folder / 'ninja.exe').read_text(), 'synthetic binary')
        self.assertEqual(json.loads((folder / '.install.json').read_text())['sha256'], s.PACKAGES['ninja']['sha256'])
        self.assertFalse(list((self.root / '.toolchain').glob('unpack-*')))

    def test_valid_tools_do_not_trigger_installs(self):
        with patch.object(s, 'windows_host', return_value=True), patch.object(s, 'activate'), patch.object(s, 'probe', return_value=(True, 'already installed')), patch.object(s, 'install_package') as install:
            s.install_tools(self.root, self.say)
        install.assert_not_called()

    def test_missing_tools_are_installed_before_submodules(self):
        events = []
        with patch.object(p, 'PROJECT_ROOT', self.root), patch.object(p, 'require_base'), patch.object(s, 'install_tools', side_effect=lambda *args: events.append('tools')), patch.object(p, 'cmd_deps', side_effect=lambda *args: events.append('deps')):
            p.cmd_setup(p.make_parser().parse_args(['setup']))
        self.assertEqual(events, ['tools', 'deps'])
        self.assertEqual(len(list((self.root / 'logs').glob('setup_*.log'))), 1)

    def test_failed_tools_prevent_submodule_step(self):
        with patch.object(p, 'PROJECT_ROOT', self.root), patch.object(p, 'require_base'), patch.object(s, 'install_tools', side_effect=RuntimeError('download failed')), patch.object(p, 'cmd_deps') as deps:
            with self.assertRaisesRegex(RuntimeError, 'download failed'):
                p.cmd_setup(p.make_parser().parse_args(['setup']))
        deps.assert_not_called()
        log = next((self.root / 'logs').glob('setup_*.log'))
        self.assertIn('download failed', log.read_text())

    def test_tools_only_never_clones(self):
        with patch.object(p, 'PROJECT_ROOT', self.root), patch.object(p, 'require_base'), patch.object(s, 'install_tools'), patch.object(p, 'cmd_deps') as deps:
            p.cmd_setup(p.make_parser().parse_args(['setup', '--tools-only']))
        deps.assert_not_called()

    def test_environment_is_idempotent_and_prefers_local_tools(self):
        local = self.root / '.toolchain/ninja'; local.mkdir(parents=True)
        (local / 'ninja.exe').touch(); (local / '.install.json').write_text('{}')
        with patch.object(s, 'windows_host', return_value=True), patch.dict(os.environ, {'PATH': '/existing', 'SSL_CERT_FILE': 'user-ca'}):
            s.activate(self.root); first = os.environ['PATH']; s.activate(self.root)
            self.assertEqual(os.environ['PATH'], first)
            self.assertTrue(first.startswith(str(local)))
            self.assertEqual(os.environ['SSL_CERT_FILE'], 'user-ca')

    def test_unsupported_host_does_not_download(self):
        with patch.object(s, 'windows_host', return_value=False), patch.object(s, 'download') as download:
            with self.assertRaisesRegex(RuntimeError, 'Windows'):
                s.install_tools(self.root, self.say)
        download.assert_not_called()

if __name__ == '__main__':
    unittest.main()
