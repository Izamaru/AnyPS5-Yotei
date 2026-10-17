from pathlib import Path
import tempfile
import unittest
import json
from unittest.mock import patch
from tools import ffmpeg_snapshot as f


class FFmpegSnapshotTests(unittest.TestCase):
    def test_snapshot_lf_crlf_and_repeated_preparation(self):
        original = (Path(__file__).parent / 'fixtures/ffmpeg-CMakeLists.txt').read_bytes()
        for data in (original, original.replace(b'\n', b'\r\n')):
            with self.subTest(crlf=b'\r\n' in data), tempfile.TemporaryDirectory() as tmp:
                base = Path(tmp)
                p = base / '3rdparty/ffmpeg-core/CMakeLists.txt'
                p.parent.mkdir(parents=True)
                p.write_bytes(data)
                f.prepare(base, lambda _: None)
                patched = p.read_bytes()
                self.assertNotIn(b'COMMAND git rev-parse', patched)
                self.assertIn(b'set(FFMPEG_GIT_SHA "9ac4cfd195f1")', patched)
                self.assertEqual(p.with_name('CMakeLists.txt.pc-lab-original').read_bytes(), data)
                f.prepare(base, lambda _: None)
                self.assertEqual(p.read_bytes(), patched)

    def test_changed_recipe_is_preserved_and_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / '3rdparty/ffmpeg-core/CMakeLists.txt'
            p.parent.mkdir(parents=True)
            data = b'# custom recipe\n'
            p.write_bytes(data)
            with self.assertRaises(RuntimeError):
                f.prepare(Path(tmp), lambda _: None)
            self.assertEqual(p.read_bytes(), data)
            self.assertFalse(p.with_name('CMakeLists.txt.pc-lab-original').exists())

    def test_restored_snapshot_uses_its_own_dependency_revision(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / 'AnyPS5'
            p = base / '3rdparty/ffmpeg-core/CMakeLists.txt'
            p.parent.mkdir(parents=True)
            p.write_bytes((Path(__file__).parent / 'fixtures/ffmpeg-CMakeLists.txt').read_bytes())
            (base.parent / 'dependency_manifest.json').write_text(json.dumps({'submodules': [' ' + 'b'*40 + ' 3rdparty/ffmpeg-core (heads/test)']}))
            f.prepare(base, lambda _: None)
            self.assertIn('set(FFMPEG_GIT_SHA "bbbbbbbbbbbb")', p.read_text())

    def test_verified_git_checkout_keeps_new_upstream_recipe(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            p = base / '3rdparty/ffmpeg-core/CMakeLists.txt'
            p.parent.mkdir(parents=True)
            (base / '.git').mkdir(); (p.parent / '.git').write_text('gitdir: test')
            p.write_text('# new upstream recipe')
            with patch.object(f.subprocess, 'check_output', side_effect=['160000 commit ' + 'c'*40 + '\t3rdparty/ffmpeg-core\n', 'c'*40 + '\n']):
                f.prepare(base, lambda _: None)
            self.assertEqual(p.read_text(), '# new upstream recipe')

    def test_mismatched_git_dependency_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            p = base / '3rdparty/ffmpeg-core/CMakeLists.txt'
            p.parent.mkdir(parents=True)
            (base / '.git').mkdir(); (p.parent / '.git').write_text('gitdir: test')
            p.write_text('# new upstream recipe')
            with patch.object(f.subprocess, 'check_output', side_effect=['160000 commit ' + 'c'*40 + '\t3rdparty/ffmpeg-core\n', 'd'*40 + '\n']):
                with self.assertRaisesRegex(RuntimeError, 'diverge'): f.prepare(base, lambda _: None)
