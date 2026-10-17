from contextlib import ExitStack
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from tools import updater as u


class RetentionTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory())) / 'project'
        self.root.mkdir()
        self.p = SimpleNamespace(PROJECT_ROOT=self.root, announce=lambda message: None)
        self.ids = [f'20261014_10000{i}_{i:08x}' for i in range(7)]
        for ident in self.ids:
            folder = self.root / 'updates/backups' / ident
            folder.mkdir(parents=True)
            (folder / 'payload').write_text('keep backup contents ' + ident)
            u.atomic_json(folder / 'backup.json', {
                'schema': 1, 'id': ident, 'complete': True,
                'state': {'schema': 1, 'history': list(self.ids[:2])}, 'source_files': {}})
        u.atomic_json(self.root / 'updates/state.json', {
            'schema': 1, 'active_base': 'base/AnyPS5', 'history': self.ids.copy()})
        (self.root / 'game.dat').write_text('untouched game')

    def test_keeps_five_newest_and_repairs_history_without_changing_snapshots(self):
        snapshot = (self.root / 'updates/backups' / self.ids[-1] / 'backup.json').read_bytes()
        removed = u.prune_backups(self.p)
        self.assertEqual({row['id'] for row in removed}, set(self.ids[:2]))
        self.assertEqual({row['id'] for row in u.backup_catalog(self.root)}, set(self.ids[2:]))
        self.assertEqual(u.state(self.root)['history'], self.ids[2:])
        self.assertEqual((self.root / 'updates/backups' / self.ids[-1] / 'backup.json').read_bytes(), snapshot)
        self.assertEqual(u.retained_history(self.root, self.ids), self.ids[2:])
        self.assertEqual((self.root / 'game.dat').read_text(), 'untouched game')
        self.assertEqual(u.prune_backups(self.p), [])

    def test_pending_transaction_defers_all_deletion(self):
        u.atomic_json(self.root / 'updates/pending.json', {'status': 'installing'})
        self.assertEqual(u.prune_backups(self.p), [])
        self.assertEqual(len(u.backup_catalog(self.root)), 7)
        self.assertEqual(u.state(self.root)['history'], self.ids)

    def test_locked_deletion_is_staged_and_retried(self):
        real_remove = u.remove
        with patch.object(u, 'remove', side_effect=PermissionError('locked')):
            u.prune_backups(self.p)
        self.assertEqual(len(u.backup_catalog(self.root)), 5)
        self.assertEqual(len(list((self.root / 'updates/backup-trash').iterdir())), 2)
        u.prune_backups(self.p)
        self.assertEqual(list((self.root / 'updates/backup-trash').iterdir()), [])

    def test_move_failure_never_deletes_the_backup_in_place(self):
        replace = u.os.replace
        def locked(source, destination):
            if Path(destination).parent.name == 'backup-trash':
                raise PermissionError('locked backup')
            return replace(source, destination)
        with patch.object(u.os, 'replace', side_effect=locked):
            self.assertEqual(u.prune_backups(self.p), [])
        self.assertEqual(len(u.backup_catalog(self.root)), 7)
        u.prune_backups(self.p)
        self.assertEqual(len(u.backup_catalog(self.root)), 5)

    def test_unmanaged_files_stay_but_abandoned_partial_does_not_accumulate(self):
        other = self.root / 'updates/backups/user-files'
        other.mkdir()
        (other / 'precious').write_text('keep')
        incomplete = self.root / 'updates/backups/20261015_000000_12345678'
        incomplete.mkdir()
        u.atomic_json(incomplete / 'backup.json', {'schema': 1, 'id': incomplete.name, 'complete': False})
        u.prune_backups(self.p)
        self.assertTrue((other / 'precious').exists())
        self.assertFalse(incomplete.exists())

    def test_partial_copies_are_preserved_when_fewer_than_five_complete_exist(self):
        for ident in self.ids[:3]:
            (self.root / 'updates/backups' / ident / 'backup.json').unlink()
        incomplete = self.root / 'updates/backups/20261015_000000_12345678'
        (incomplete / 'source').mkdir(parents=True)
        u.prune_backups(self.p)
        self.assertTrue(incomplete.exists())
        self.assertEqual(len(u.backup_catalog(self.root)), 4)

    def test_abandoned_partial_older_than_five_complete_backups_is_removed(self):
        incomplete = self.root / 'updates/backups/20261013_000000_12345678'
        (incomplete / 'source').mkdir(parents=True)
        (incomplete / 'source/partial').write_text('obsolete partial copy')
        removed = u.prune_backups(self.p)
        self.assertIn(incomplete.name, {row['id'] for row in removed})
        self.assertFalse(incomplete.exists())
        self.assertEqual(len(u.backup_catalog(self.root)), 5)

    def test_linked_target_is_rejected_before_mutation(self):
        outside = self.root.parent / 'outside'
        outside.mkdir()
        (outside / 'important').write_text('keep')
        link = self.root / 'updates/backups/20261014_000000_00000000'
        try:
            link.symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest('Creating symbolic links requires Windows developer mode')
        with self.assertRaises(RuntimeError):
            u.prune_backups(self.p)
        self.assertTrue((outside / 'important').exists())
        self.assertEqual(u.state(self.root)['history'], self.ids)


if __name__ == '__main__':
    unittest.main()
