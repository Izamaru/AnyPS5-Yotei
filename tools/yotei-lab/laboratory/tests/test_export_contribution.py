"""Regression checks for contribution boundaries and integrity. GPL-2.0-only."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools import export_contribution as e


class ContributionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.base = self.root / 'base/AnyPS5'
        self.source = self.base / 'core/sample.cpp'
        self.source.parent.mkdir(parents=True)
        self.source.write_text('int sample() { return 1; }\n', encoding='utf-8')
        (self.base / 'LICENSE').write_text('GPL-2.0-only', encoding='utf-8')
        for name in ('LICENSE', 'LICENCA_E_CREDITOS.md'):
            (self.root / name).write_text('GPL-2.0-only', encoding='utf-8')
        self.manifest = {'revision': 'a' * 40, 'patches': [{
            'file': 'core/sample.cpp', 'original_sha256': 'b' * 64,
            'modified_sha256': e.sha256(self.source.read_bytes())}]}
        self.save()
        self.addCleanup(patch.stopall)
        patch.object(e, 'LAB_FILES', ()).start()

    def save(self):
        (self.base.parent / 'source_manifest.json').write_text(
            json.dumps(self.manifest), encoding='utf-8')

    def test_only_allowlisted_sources_export_and_all_hashes_match(self):
        for name in ('games/title/eboot.bin', 'logs/run.txt', 'updates/private.json',
                     'core/unknown.cpp', '.env', 'Diagnostico-Intel/dump.bin'):
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'PRIVATE UNSELECTED INPUT')
        report = e.export(self.root, 'publishing/review')
        folder = self.root / 'publishing/review'
        actual = {p.relative_to(folder).as_posix() for p in folder.rglob('*') if p.is_file()}
        self.assertEqual(actual, {item['path'] for item in report['files']} | {'manifest.json'})
        for item in report['files']:
            self.assertEqual(e.sha256((folder / item['path']).read_bytes()), item['sha256'])
        self.assertNotIn(str(self.root), (folder / 'manifest.json').read_text())

    def test_audit_creates_no_publication_directory(self):
        e.export(self.root)
        self.assertFalse((self.root / 'publishing').exists())

    def test_changed_source_blocks_export_before_any_output(self):
        self.source.write_text('changed', encoding='utf-8')
        with self.assertRaisesRegex(RuntimeError, 'recorded hash'):
            e.export(self.root, 'publishing/review')
        self.assertFalse((self.root / 'publishing').exists())

    def test_game_path_in_manifest_is_rejected(self):
        self.manifest['patches'][0]['file'] = 'games/eboot.cpp'
        self.save()
        with self.assertRaisesRegex(RuntimeError, 'Unapproved'):
            e.export(self.root, 'publishing/review')

    def test_traversal_and_output_overwrite_are_rejected(self):
        for name in ('../escape', '/absolute', 'C:/escape', 'publishing/../games',
                     'publishing\\escape', 'games/review', 'publishing//review'):
            with self.subTest(name=name), self.assertRaises(RuntimeError):
                e.export(self.root, name)
        output = self.root / 'publishing/review'
        output.mkdir(parents=True)
        with self.assertRaisesRegex(RuntimeError, 'already exists'):
            e.export(self.root, 'publishing/review')

    def test_binary_and_non_utf8_sources_are_rejected(self):
        for data in (b'\x00game', b'\xff\xfe', b'MZ\x00'):
            with self.subTest(data=data), self.assertRaises(RuntimeError):
                e.check_text('core/file.cpp', data)

    def test_credential_and_personal_path_checks_do_not_print_values(self):
        samples = ('gh' + 'p_' + 'x' * 36,
                   'AK' + 'IA' + 'A' * 16,
                   '-----BEGIN ' + 'PRIVATE KEY-----',
                   'C:/' + 'Users/' + 'private_name/file',
                   'https://' + 'someone:password@example.org/file',
                   'api_key = ' + '"sensitive-example-value"')
        for value in samples:
            with self.subTest(kind=value[:4]), self.assertRaises(RuntimeError) as ctx:
                e.check_text('sample.cpp', value.encode())
            self.assertNotIn(value, str(ctx.exception))

    def test_duplicate_native_paths_are_rejected(self):
        self.manifest['patches'].append(dict(self.manifest['patches'][0]))
        self.save()
        with self.assertRaisesRegex(RuntimeError, 'duplicate'):
            e.export(self.root)

    def test_source_symlink_is_rejected(self):
        target = self.root / 'private.cpp'
        target.write_text('private', encoding='utf-8')
        self.source.unlink()
        try:
            self.source.symlink_to(target)
        except OSError:
            self.skipTest('Host does not permit creating symlinks')
        with self.assertRaises(RuntimeError):
            e.export(self.root)


if __name__ == '__main__':
    unittest.main()
