import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools import log_retention as retention


class LogRetentionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.logs = self.root / 'logs'
        self.logs.mkdir()

    def log(self, name, rank):
        path = self.logs / name
        path.write_text(name, encoding='utf-8')
        os.utime(path, ns=(rank * 10**9, rank * 10**9))
        return path

    def test_keeps_50_newest_by_time_with_telemetry_and_preserves_research(self):
        for n in range(55):
            path = self.log(f'run-{n:02}.log', n + 1)
            path.with_suffix('.resources.jsonl').write_text('telemetry')
            path.with_suffix('.events.jsonl').write_text('events')
            path.with_suffix('.shaders').mkdir()
            (path.with_suffix('.shaders') / 'capture.req').write_text('replay input')
        (self.logs / 'summary.json').write_text('summary')
        (self.logs / 'candidate').mkdir()
        (self.logs / 'candidate/native.log').write_text('nested evidence')
        result = retention.prune_logs(self.root)
        self.assertEqual(result['after'], 50)
        self.assertEqual(len(result['removed_logs']), 5)
        self.assertEqual(result['removed_files'], 15)
        self.assertFalse((self.logs / 'run-04.log').exists())
        self.assertTrue((self.logs / 'run-05.log').exists())
        self.assertTrue((self.logs / 'run-00.shaders/capture.req').exists())
        self.assertTrue((self.logs / 'candidate/native.log').exists())
        self.assertTrue((self.logs / 'summary.json').exists())
        self.assertEqual(retention.prune_logs(self.root)['removed_logs'], [])

    def test_dry_run_does_not_change_files_or_create_receipt(self):
        self.log('old.log', 1)
        self.log('new.log', 2)
        result = retention.prune_logs(self.root, keep=1, dry_run=True)
        self.assertEqual(result['planned_names'], ['old.log'])
        self.assertTrue((self.logs / 'old.log').exists())
        self.assertFalse((self.root / 'updates').exists())

    def test_locked_log_preserves_its_telemetry_for_retry(self):
        old = self.log('old.log', 1)
        companion = old.with_suffix('.resources.jsonl')
        companion.write_text('keep until log closes')
        self.log('new.log', 2)
        unlink = Path.unlink
        def locked(path, *args, **kwargs):
            if path == old:
                raise PermissionError('open by another process')
            return unlink(path, *args, **kwargs)
        with patch.object(Path, 'unlink', locked):
            result = retention.prune_logs(self.root, keep=1)
        self.assertEqual(result['removed_logs'], [])
        self.assertTrue(companion.exists())
        self.assertEqual(result['after'], 2)
        self.assertEqual(retention.prune_logs(self.root, keep=1)['after'], 1)

    def test_companion_link_never_deletes_external_target_or_its_log(self):
        old = self.log('old.log', 1)
        self.log('new.log', 2)
        outside = self.root / 'outside.jsonl'
        outside.write_text('untouched')
        companion = old.with_suffix('.events.jsonl')
        try:
            companion.symlink_to(outside)
        except OSError:
            self.skipTest('Symlinks unavailable for this account')
        result = retention.prune_logs(self.root, keep=1)
        self.assertEqual(result['removed_logs'], [])
        self.assertTrue(old.exists())
        self.assertEqual(outside.read_text(), 'untouched')

    @unittest.skipUnless(os.name == 'nt', 'Windows file sharing behavior')
    def test_real_open_windows_log_is_preserved_until_closed(self):
        old = self.log('old.log', 1)
        self.log('new.log', 2)
        with old.open('a'):
            result = retention.prune_logs(self.root, keep=1)
        self.assertEqual(result['removed_logs'], [])
        self.assertEqual(result['after'], 2)
        self.assertEqual(retention.prune_logs(self.root, keep=1)['after'], 1)

    def test_directory_in_place_of_telemetry_preserves_entire_group(self):
        old = self.log('old.log', 1)
        self.log('new.log', 2)
        directory = old.with_suffix('.events.jsonl')
        directory.mkdir()
        (directory / 'evidence').write_text('preserve')
        result = retention.prune_logs(self.root, keep=1)
        self.assertEqual(result['removed_logs'], [])
        self.assertEqual(len(result['skipped']), 1)
        self.assertTrue(old.exists())
        self.assertTrue((directory / 'evidence').exists())

    def test_directory_named_log_is_not_a_log(self):
        (self.logs / 'source.log').mkdir()
        self.assertEqual(retention.catalog(self.root), [])
        for invalid in (0, -1, True, 1.5):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                retention.prune_logs(self.root, keep=invalid)


if __name__ == '__main__':
    unittest.main()
