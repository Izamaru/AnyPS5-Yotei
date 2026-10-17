from contextlib import ExitStack
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tools import updater as u


class UpdaterTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack(); self.addCleanup(self.stack.close)
        self.temp = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.root = self.temp/'project'; self.root.mkdir()
        self.base = self.root/'base/AnyPS5'; self.base.mkdir(parents=True)
        self.runtime = self.root/'games/Ghost-of-Yotei/runtime'; self.runtime.mkdir(parents=True)
        (self.base/'core/relinker').mkdir(parents=True)
        (self.base/'core/relinker/main.cpp').write_text('converter v1')
        (self.base/'CMakeLists.txt').write_text('cmake')
        (self.base/'.gitmodules').write_text('')
        (self.base/'fix.cpp').write_text('local fix\n')
        u.atomic_json(self.base.parent/'source_manifest.json', {'revision': 'a'*40, 'patches': []})
        (self.runtime/'libs').mkdir(); (self.runtime/'libs/driver.prx').write_bytes(b'MZ old runtime')
        (self.runtime/'GhostOfYotei.exe').write_bytes(b'MZ converted unchanged')
        (self.runtime/'app0').mkdir(); (self.runtime/'app0/assets.bin').write_bytes(b'GAME DATA DO NOT MODIFY')
        self.port = {'generated_files': ['GhostOfYotei.exe'], 'registries': [], 'cpu': 'intel', 'convert_only': False}
        u.atomic_json(self.runtime/'port_manifest.json', self.port)
        self.p = SimpleNamespace(PROJECT_ROOT=self.root, BASE=self.base, RUNTIME=self.runtime, VERSION='0.3.8',
                                 announce=lambda x: None)
        self.stack.enter_context(patch.object(u, 'require_game_closed'))
        # Fixtures are tiny; test the disk-space policy explicitly below rather
        # than depending on the free space of the host's temporary volume.
        self.stack.enter_context(patch.object(u.shutil, 'disk_usage', return_value=SimpleNamespace(free=1 << 40)))

    def prepare_activation(self):
        libs = self.temp/'new-libs'; libs.mkdir(exist_ok=True)
        (libs/'driver.prx').write_bytes(b'MZ new runtime')
        (libs/'extra.prx').write_bytes(b'MZ added')
        candidate = self.root/'updates/versions/test/base/AnyPS5'; candidate.mkdir(parents=True, exist_ok=True)
        next_state = {'schema': 1, 'active_base': 'updates/versions/test/base/AnyPS5', 'history': []}
        return libs, next_state

    def assert_old_runtime(self):
        self.assertEqual((self.runtime/'libs/driver.prx').read_bytes(), b'MZ old runtime')
        self.assertFalse((self.runtime/'libs/extra.prx').exists())
        self.assertEqual(u.read(self.runtime/'port_manifest.json'), self.port)
        self.assertEqual(u.active_base(self.root), self.base)
        self.assertEqual((self.runtime/'app0/assets.bin').read_bytes(), b'GAME DATA DO NOT MODIFY')

    def test_backup_restores_exact_runtime_and_source_without_game_copy(self):
        folder, backup = u.make_backup(self.p, 'before-update')
        self.assertFalse((folder/'GhostOfYotei.exe').exists())
        self.assertFalse((folder/'app0').exists())
        libs, next_state = self.prepare_activation()
        next_state['history'] = [backup['id']]
        u.activate(self.p, next_state, libs, {**self.port, 'revision': 'new'})
        self.p.BASE = u.active_base(self.root)
        u.rollback(self.p)
        restored = u.active_base(self.root)
        self.assertEqual((restored/'fix.cpp').read_text(), 'local fix\n')
        self.assertEqual((self.runtime/'libs/driver.prx').read_bytes(), b'MZ old runtime')
        self.assertFalse((self.runtime/'libs/extra.prx').exists())
        self.assertEqual(u.read(self.runtime/'port_manifest.json'), self.port)
        self.assertEqual(u.state(self.root)['history'], [])
        self.assertEqual((self.runtime/'app0/assets.bin').read_bytes(), b'GAME DATA DO NOT MODIFY')
        self.assertEqual(len(list((self.root/'updates/backups').glob('*/backup.json'))), 2)

    def test_corrupt_backup_blocks_restore_before_changes(self):
        folder, meta = u.make_backup(self.p, 'before-update')
        (folder/'libs/driver.prx').write_bytes(b'corrupt')
        with self.assertRaisesRegex(RuntimeError, 'alterados/incompletos'):
            u.load_backup(self.root, meta['id'])
        self.assert_old_runtime()

    def test_backup_accepts_legacy_windows_generated_paths(self):
        guest = self.runtime / 'app0/sce_module/libc.prx.guest.prx'
        guest.parent.mkdir(parents=True)
        guest.write_bytes(b'MZ converted guest')
        name = r'app0\sce_module\libc.prx.guest.prx'
        self.port['generated_files'].append(name)
        u.atomic_json(self.runtime / 'port_manifest.json', self.port)
        folder, meta = u.make_backup(self.p, 'legacy-windows-manifest')
        self.assertEqual(meta['converted_files'][name], u.digest(guest))
        self.assertEqual(u.load_backup(self.root, meta['id'])[1], meta)
        self.assertEqual(u.read(self.runtime / 'port_manifest.json'), self.port)
        self.assertEqual(guest.read_bytes(), b'MZ converted guest')
        self.assertTrue((folder / 'backup.json').is_file())

    def test_legacy_windows_paths_cannot_escape_runtime(self):
        for name in (r'..\escape', r'app0\..\..\escape', r'C:\escape',
                     r'\escape', r'\\server\share\escape', r'app0\file:stream'):
            with self.subTest(name=name):
                u.atomic_json(self.runtime / 'port_manifest.json',
                              {**self.port, 'generated_files': [name]})
                with self.assertRaises(RuntimeError):
                    u.converted_fingerprint(self.p)

    def test_changed_conversion_blocks_legacy_backup_rollback(self):
        folder, meta = u.make_backup(self.p, 'before-update')
        meta.pop('converted_payload')
        u.atomic_json(folder / 'backup.json', meta)
        libs, next_state = self.prepare_activation(); next_state['history'] = [meta['id']]
        u.activate(self.p, next_state, libs, self.port)
        (self.runtime/'GhostOfYotei.exe').write_bytes(b'new conversion')
        with self.assertRaisesRegex(RuntimeError, 'convertidos mudaram'):
            u.rollback(self.p)
        self.assertEqual((self.runtime/'libs/driver.prx').read_bytes(), b'MZ new runtime')

    def test_backup_restores_changed_conversion_and_preserves_assets(self):
        folder, meta = u.make_backup(self.p, 'before-reconversion')
        self.assertEqual((folder / 'converted/GhostOfYotei.exe').read_bytes(), b'MZ converted unchanged')
        self.assertFalse((folder / 'converted/app0/assets.bin').exists())
        stage = self.temp / 'converted'; stage.mkdir()
        (stage / 'GhostOfYotei.exe').write_bytes(b'MZ new conversion')
        (stage / 'GhostOfYotei.registry.json').write_bytes(b'new registry')
        libs, next_state = self.prepare_activation()
        next_state['history'] = [meta['id']]
        port = {**self.port, 'generated_files': ['GhostOfYotei.exe', 'GhostOfYotei.registry.json']}
        u.activate(self.p, next_state, libs, port, converted=stage)
        self.assertEqual((self.runtime / 'GhostOfYotei.exe').read_bytes(), b'MZ new conversion')
        self.p.BASE = u.active_base(self.root)
        u.rollback(self.p)
        self.assertEqual((self.runtime / 'GhostOfYotei.exe').read_bytes(), b'MZ converted unchanged')
        self.assertFalse((self.runtime / 'GhostOfYotei.registry.json').exists())
        self.assertEqual(u.read(self.runtime / 'port_manifest.json'), self.port)
        self.assertEqual((self.runtime / 'app0/assets.bin').read_bytes(), b'GAME DATA DO NOT MODIFY')
        self.assertEqual((self.runtime / 'libs/driver.prx').read_bytes(), b'MZ old runtime')

    def test_conversion_transaction_restores_deleted_modules_after_late_failure(self):
        old_module = self.runtime / 'app0/sce_module/old.prx.guest.prx'
        old_module.parent.mkdir(parents=True)
        old_module.write_bytes(b'MZ old guest module')
        self.port['generated_files'].append(r'app0\sce_module\old.prx.guest.prx')
        u.atomic_json(self.runtime / 'port_manifest.json', self.port)
        stage = self.temp / 'converted'; stage.mkdir()
        (stage / 'GhostOfYotei.exe').write_bytes(b'MZ replacement')
        (stage / 'GhostOfYotei.registry.json').write_bytes(b'new registry')
        port = {**self.port, 'generated_files': ['GhostOfYotei.exe', 'GhostOfYotei.registry.json']}
        libs, next_state = self.prepare_activation()
        original = os.replace
        def fail_manifest(src, dst):
            if Path(src).as_posix().endswith('/new/port_manifest.json'):
                raise PermissionError('manifest locked after conversion changed')
            return original(src, dst)
        with patch.object(u.os, 'replace', side_effect=fail_manifest):
            with self.assertRaises(PermissionError):
                u.activate(self.p, next_state, libs, port, converted=stage)
        self.assert_old_runtime()
        self.assertEqual((self.runtime / 'GhostOfYotei.exe').read_bytes(), b'MZ converted unchanged')
        self.assertEqual(old_module.read_bytes(), b'MZ old guest module')
        self.assertFalse((self.runtime / 'GhostOfYotei.registry.json').exists())
        self.assertFalse((self.root / 'updates/pending.json').exists())

    def test_corrupt_converted_backup_blocks_restore(self):
        folder, meta = u.make_backup(self.p, 'before-reconversion')
        (folder / 'converted/GhostOfYotei.exe').write_bytes(b'corrupted')
        with self.assertRaisesRegex(RuntimeError, 'alterados/incompletos'):
            u.load_backup(self.root, meta['id'])
        self.assert_old_runtime()

    def test_conversion_cannot_replace_assets_or_duplicate_windows_paths(self):
        stage = self.temp / 'converted'
        (stage / 'app0/sce_module').mkdir(parents=True)
        (stage / 'app0/assets.bin').write_bytes(b'not a converted output')
        (stage / 'app0/sce_module/libc.prx.guest.prx').write_bytes(b'MZ module')
        for names in (['app0/assets.bin'], [r'app0\sce_module\libc.prx.guest.prx', 'app0/sce_module/LIBC.prx.guest.prx']):
            with self.subTest(names=names):
                with self.assertRaises(RuntimeError):
                    u.converted_inventory(stage, {'generated_files': names})
        self.assert_old_runtime()

    def test_recovery_rejects_game_asset_destination_before_writing(self):
        ident = 'asset-escape'
        transaction = self.root / 'updates/transactions' / ident
        (transaction / 'old').mkdir(parents=True)
        (transaction / 'old/payload').write_bytes(b'do not restore over an asset')
        u.atomic_json(self.root / 'updates/pending.json', {'schema': 1, 'id': ident, 'status': 'installing',
            'actions': [{'dest': 'games/Ghost-of-Yotei/runtime/app0/assets.bin',
                         'old': 'updates/transactions/' + ident + '/old/payload',
                         'new': 'updates/transactions/' + ident + '/new/payload', 'existed': True}]})
        with self.assertRaises(RuntimeError):
            u.recover(self.root, lambda _: None)
        self.assert_old_runtime()
        self.assertEqual((transaction / 'old/payload').read_bytes(), b'do not restore over an asset')

    def test_locked_dll_during_activation_rolls_back_all_targets(self):
        libs, next_state = self.prepare_activation()
        original = os.replace
        def locked(src, dst):
            if Path(src).as_posix().endswith('/new/port_manifest.json'):
                raise PermissionError('Windows file lock')
            return original(src, dst)
        with patch.object(u.os, 'replace', side_effect=locked):
            with self.assertRaises(PermissionError):
                u.activate(self.p, next_state, libs, {'new': True})
        self.assert_old_runtime()
        self.assertFalse((self.root/'updates/pending.json').exists())

    def test_process_death_at_every_rename_recovers_old_runtime(self):
        libs, next_state = self.prepare_activation()
        u.atomic_json(self.root/'updates/state.json', u.state(self.root))
        # A hard process exit bypasses exception handlers and finally blocks.
        program = '''
import os,sys,json
from pathlib import Path
from types import SimpleNamespace
from tools import updater as u
root=Path(sys.argv[1]); count=0; original=os.replace
p=SimpleNamespace(PROJECT_ROOT=root,announce=lambda x:None)
allowed={str(root/'games/Ghost-of-Yotei/runtime/libs'),str(root/'games/Ghost-of-Yotei/runtime/port_manifest.json'),str(root/'updates/state.json')}
def crash(src,dst):
    global count
    result=original(src,dst)
    if str(src) in allowed or (str(dst) in allowed and '/new/' in str(src).replace('\\\\','/')):
        count+=1
        if count==int(sys.argv[4]): os._exit(71)
    return result
os.replace=crash
u.activate(p,json.loads(sys.argv[3]),Path(sys.argv[2]),{'new':True})
'''
        for step in range(1, 7):
            result = subprocess.run([sys.executable, '-c', program, str(self.root), str(libs), json.dumps(next_state), str(step)], capture_output=True)
            self.assertEqual(result.returncode, 71, result.stderr)
            self.assertTrue(u.recover(self.root, lambda x: None))
            self.assert_old_runtime()
            self.assertFalse(u.recover(self.root, lambda x: None))

    def test_process_death_during_reconversion_recovers_exe_modules_and_libraries(self):
        old_module = self.runtime / 'app0/sce_module/old.prx.guest.prx'
        old_module.parent.mkdir(parents=True)
        old_module.write_bytes(b'MZ old guest module')
        self.port['generated_files'].append('app0/sce_module/old.prx.guest.prx')
        u.atomic_json(self.runtime / 'port_manifest.json', self.port)
        u.atomic_json(self.root / 'updates/state.json', u.state(self.root))
        libs, next_state = self.prepare_activation()
        stage = self.temp / 'converted'; stage.mkdir()
        (stage / 'GhostOfYotei.exe').write_bytes(b'MZ new exe')
        (stage / 'GhostOfYotei.registry.json').write_bytes(b'new registry')
        new_port = {**self.port, 'generated_files': ['GhostOfYotei.exe', 'GhostOfYotei.registry.json']}
        program = '''
import json,os,sys
from pathlib import Path
from types import SimpleNamespace
from tools import updater as u
root=Path(sys.argv[1]); runtime=root/'games/Ghost-of-Yotei/runtime'
p=SimpleNamespace(PROJECT_ROOT=root,RUNTIME=runtime,announce=lambda x:None)
allowed={str(root/'updates/state.json'),*(str(runtime/name) for name in ('GhostOfYotei.exe','GhostOfYotei.registry.json','app0/sce_module/old.prx.guest.prx','libs','port_manifest.json'))}
original=os.replace; count=0
def crash(src,dst):
    global count
    result=original(src,dst)
    if str(src) in allowed or (str(dst) in allowed and '/new/' in str(src).replace(chr(92),'/')):
        count+=1
        if count==int(sys.argv[6]): os._exit(71)
    return result
os.replace=crash
u.activate(p,json.loads(sys.argv[4]),Path(sys.argv[2]),json.loads(sys.argv[5]),converted=Path(sys.argv[3]))
'''
        for step in range(1, 11):
            with self.subTest(rename=step):
                result = subprocess.run([sys.executable, '-c', program, str(self.root), str(libs), str(stage),
                                         json.dumps(next_state), json.dumps(new_port), str(step)], capture_output=True)
                self.assertEqual(result.returncode, 71, result.stderr)
                self.assertTrue(u.recover(self.root, lambda _: None))
                self.assert_old_runtime()
                self.assertEqual((self.runtime / 'GhostOfYotei.exe').read_bytes(), b'MZ converted unchanged')
                self.assertEqual(old_module.read_bytes(), b'MZ old guest module')
                self.assertFalse((self.runtime / 'GhostOfYotei.registry.json').exists())
                self.assertFalse((self.root / 'updates/pending.json').exists())

    def test_committed_journal_is_not_rolled_back(self):
        u.atomic_json(self.root/'updates/pending.json', {'schema': 1, 'status': 'committed'})
        self.assertFalse(u.recover(self.root, lambda x: None))
        self.assertFalse((self.root/'updates/pending.json').exists())

    def test_low_disk_space_creates_no_complete_backup(self):
        with patch.object(u.shutil, 'disk_usage', return_value=SimpleNamespace(free=1)):
            with self.assertRaisesRegex(RuntimeError, 'Espaco insuficiente'):
                u.make_backup(self.p, 'test')
        self.assertFalse((self.root/'updates/backups').exists())

    def test_path_escape_and_symlink_are_rejected(self):
        for name in ('../escape', '/tmp/escape', 'C:/escape', 'foo\\bar'):
            with self.assertRaises(RuntimeError): u.checked(self.root, name)
        if os.name != 'nt':
            (self.base/'link').symlink_to(self.temp)
            with self.assertRaises(RuntimeError): u.inventory(self.base, source=True)

    def test_operation_lock_blocks_other_process(self):
        script = 'from tools import updater as u; from pathlib import Path; import sys;\nwith u.operation_lock(Path(sys.argv[1])): pass'
        with u.operation_lock(self.root):
            result = subprocess.run([sys.executable, '-c', script, str(self.root)], capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b'Outro comando', result.stderr)
        with u.operation_lock(self.root): pass

    def test_converter_change_blocks_old_executable(self):
        candidate = self.temp/'candidate'; shutil.copytree(self.base, candidate)
        (candidate/'core/relinker/main.cpp').write_text('new converter')
        with self.assertRaisesRegex(RuntimeError, 'conversor mudou'):
            u.require_conversion_compatible(self.p, candidate)
        self.assert_old_runtime()

    @unittest.skipUnless(shutil.which('git'), 'requires git')
    def test_three_way_merge_keeps_both_changes_and_detects_absorbed_fix(self):
        roots = [self.temp/x for x in ('original','local','incoming')]
        for root in roots:
            root.mkdir(); (root/'.gitmodules').write_text('')
        old, local, new = roots
        content = '\n'.join(f'line {n}' for n in range(20))+'\n'
        (old/'file.cpp').write_text(content)
        (local/'file.cpp').write_text(content.replace('line 1\n','local fix\n'))
        (new/'file.cpp').write_text(content.replace('line 18\n','upstream fix\n'))
        for root in roots: (root/'absorbed.cpp').write_text('upstream' if root==old else 'fixed')
        (local/'added.hpp').write_text('new local header')
        patches, report = u.merge_sources(old,local,new,self.temp/'report.json')
        self.assertIn('local fix', (new/'file.cpp').read_text())
        self.assertIn('upstream fix', (new/'file.cpp').read_text())
        self.assertEqual(report['already_upstream'], ['absorbed.cpp'])
        self.assertEqual((new/'added.hpp').read_text(),'new local header')
        self.assertEqual(len(patches),2)

    @unittest.skipUnless(shutil.which('git'), 'requires git')
    def test_merge_conflict_never_changes_local_source(self):
        roots = [self.temp/x for x in ('original','local','incoming')]
        for root, value in zip(roots, ('old','ours','theirs')):
            root.mkdir(); (root/'.gitmodules').write_text(''); (root/'file.cpp').write_text(value+'\n')
        with self.assertRaisesRegex(RuntimeError, 'Conflitos'):
            u.merge_sources(*roots,self.temp/'report.json')
        self.assertEqual((roots[1]/'file.cpp').read_text(),'ours\n')
        self.assertEqual(u.read(self.temp/'report.json')['conflicts'], ['file.cpp'])


@unittest.skipUnless(shutil.which('git'), 'requires git')
class UpdateFlowTests(unittest.TestCase):
    setUp = UpdaterTests.setUp
    assert_old_runtime = UpdaterTests.assert_old_runtime
    def init_upstream(self):
        repo = self.temp/'upstream'; shutil.copytree(self.base, repo)
        (repo/'fix.cpp').write_text('upstream original\n')
        u.git(['init', str(repo)]);u.git(['config','user.name','Test'],repo);u.git(['config','user.email','test@example.invalid'],repo)
        u.git(['add','.'],repo);u.git(['commit','-m','old'],repo)
        old = u.git(['rev-parse','HEAD'],repo).decode().strip()
        (repo/'new.cpp').write_text('new upstream feature\n')
        u.git(['add','.'],repo);u.git(['commit','-m','new'],repo)
        new = u.git(['rev-parse','HEAD'],repo).decode().strip()
        u.atomic_json(self.base.parent/'source_manifest.json',{'revision':old,'patches':[]})
        self.stack.enter_context(patch.object(u,'UPSTREAM',str(repo)))
        self.stack.enter_context(patch.object(u,'latest',return_value=new))
        self.stack.enter_context(patch.object(u,'windows_runtime',return_value=True))
        for name in ('tools','tests'): (self.root/name).mkdir()
        self.p.require_tool=lambda x:None;self.p.check_compiler=lambda:None
        self.p.runtime_libraries=lambda:self.runtime/'libs'
        self.p.library_inputs=lambda:list((self.p.BASE/'build/core/libs/libs').glob('*.prx'))
        self.audit_called=False
        def audit(_):
            self.audit_called=True
            self.assertNotEqual(self.p.BASE,self.base)
            self.assertEqual((self.p.runtime_libraries()/'new.prx').read_bytes(),b'MZ tested build')
            return 0
        self.p.cmd_audit=audit
        def command(args,**kwargs):
            if args[0]=='git':
                u.git(args[1:],kwargs.get('cwd'));return 0
            if 'build' in args:
                candidate=Path(args[1]).parents[1]/'base/AnyPS5/build/core/libs/libs'
                candidate.mkdir(parents=True);(candidate/'new.prx').write_bytes(b'MZ tested build')
            return 0
        self.p.log_command=command
        return old,new

    def test_full_update_flow_then_offline_rollback(self):
        old,new=self.init_upstream()
        self.assertEqual(u.update(self.p,2),0)
        self.assertTrue(self.audit_called)
        active=u.active_base(self.root)
        self.assertEqual((active/'fix.cpp').read_text(),'local fix\n')
        self.assertTrue((active/'new.cpp').exists())
        self.assertEqual(u.read(active.parent/'source_manifest.json')['revision'],new)
        self.assertFalse((self.runtime/'libs/driver.prx').exists())
        self.assertTrue((self.runtime/'libs/new.prx').exists())
        self.assertEqual(u.read(self.runtime/'port_manifest.json')['library_revision'],new)
        self.p.BASE=active
        with patch.object(u,'git',side_effect=RuntimeError('network unavailable')):
            self.assertEqual(u.rollback(self.p),0)
        self.assertEqual(u.read(u.active_base(self.root).parent/'source_manifest.json')['revision'],old)
        self.assertEqual((self.runtime/'libs/driver.prx').read_bytes(),b'MZ old runtime')
        self.assertFalse((self.runtime/'libs/new.prx').exists())

    def test_failed_build_preserves_active_and_backup(self):
        self.init_upstream();original=self.p.log_command
        def fail(args,**kwargs):
            if 'build' in args:raise RuntimeError('compiler failure')
            return original(args,**kwargs)
        self.p.log_command=fail
        with self.assertRaisesRegex(RuntimeError,'compiler failure'):u.update(self.p,2)
        self.assert_old_runtime()
        self.assertEqual(len(list((self.root/'updates/backups').glob('*/backup.json'))),1)

    def test_failed_tests_preserve_active(self):
        self.init_upstream();original=self.p.log_command
        def fail(args,**kwargs):
            if 'unittest' in args:raise RuntimeError('test failure')
            return original(args,**kwargs)
        self.p.log_command=fail
        with self.assertRaisesRegex(RuntimeError,'test failure'):u.update(self.p,2)
        self.assert_old_runtime()

    def test_failed_audit_preserves_active(self):
        self.init_upstream();self.p.cmd_audit=lambda _:1
        with self.assertRaisesRegex(RuntimeError,'Auditoria'):u.update(self.p,2)
        self.assert_old_runtime()

    def test_no_update_has_no_backup_or_build(self):
        old,_=self.init_upstream()
        with patch.object(u,'latest',return_value=old):self.assertEqual(u.update(self.p),0)
        self.assertFalse((self.root/'updates/backups').exists())
        self.assert_old_runtime()


class LabUpdateTests(unittest.TestCase):
    setUp = UpdaterTests.setUp
    assert_old_runtime = UpdaterTests.assert_old_runtime
    init_upstream = UpdateFlowTests.init_upstream

    def prepare_package(self):
        old, _ = self.init_upstream()
        self.p.VERSION = '0.3.9'
        package = self.root / 'docs/lab-update'
        (package / 'files').mkdir(parents=True)
        (package / 'files/fix.cpp').write_text('pointer table fix\n')
        release = {'schema': 1, 'version': '0.3.9', 'revision': old, 'requires': {},
                   'patch': 'docs/Shaders-0.3.9.patch', 'metadata': {'pointer_tables': 'tested'},
                   'files': {'fix.cpp': {'before': u.digest(self.base / 'fix.cpp'),
                                         'after': u.digest(package / 'files/fix.cpp')}}}
        u.atomic_json(package / 'manifest.json', release)
        return package, release

    def test_local_update_and_offline_return_preserve_dependencies_and_game(self):
        self.prepare_package()
        guest = self.runtime / 'app0/sce_module/libc.prx.guest.prx'
        guest.parent.mkdir(parents=True)
        guest.write_bytes(b'MZ converted guest')
        self.port['generated_files'].append(r'app0\sce_module\libc.prx.guest.prx')
        u.atomic_json(self.runtime / 'port_manifest.json', self.port)
        dependency = self.base / '3rdparty/example/data.txt'
        dependency.parent.mkdir(parents=True); dependency.write_text('pinned dependency')
        u.atomic_json(self.base.parent / 'dependency_manifest.json', {'submodules': ['pinned']})
        with patch.object(u, 'git', side_effect=RuntimeError('network unavailable')):
            self.assertEqual(u.apply_lab_update(self.p, 2), 0)
        self.assertTrue(self.audit_called)
        active = u.active_base(self.root)
        self.assertEqual((active / 'fix.cpp').read_text(), 'pointer table fix\n')
        self.assertEqual((active / '3rdparty/example/data.txt').read_text(), 'pinned dependency')
        self.assertEqual((self.base / 'fix.cpp').read_text(), 'local fix\n')
        self.assertEqual(u.read(active.parent / 'source_manifest.json')['pointer_tables'], 'tested')
        self.assertEqual(u.read(active.parent / 'dependency_manifest.json'), {'submodules': ['pinned']})
        self.p.BASE = active
        with patch.object(u, 'git', side_effect=RuntimeError('network unavailable')):
            self.assertEqual(u.rollback(self.p), 0)
        self.assertEqual((u.active_base(self.root) / 'fix.cpp').read_text(), 'local fix\n')
        self.assertEqual((self.runtime / 'libs/driver.prx').read_bytes(), b'MZ old runtime')
        self.assertEqual((self.runtime / 'app0/assets.bin').read_bytes(), b'GAME DATA DO NOT MODIFY')
        self.assertEqual(guest.read_bytes(), b'MZ converted guest')
        self.assertEqual(u.read(self.runtime / 'port_manifest.json'), self.port)

    def test_corrupted_package_is_rejected_before_backup(self):
        package, _ = self.prepare_package()
        (package / 'files/fix.cpp').write_text('corrupt')
        with self.assertRaisesRegex(RuntimeError, 'Pacote incompleto ou alterado'):
            u.apply_lab_update(self.p)
        self.assert_old_runtime()
        self.assertFalse((self.root / 'updates/backups').exists())

    def test_local_conflict_preserves_changes(self):
        self.prepare_package()
        (self.base / 'fix.cpp').write_text('user changes')
        with self.assertRaisesRegex(RuntimeError, 'conflita'):
            u.apply_lab_update(self.p)
        self.assertEqual((self.base / 'fix.cpp').read_text(), 'user changes')
        self.assert_old_runtime()

    def test_different_revision_is_rejected(self):
        package, release = self.prepare_package()
        release['revision'] = 'b' * 40
        u.atomic_json(package / 'manifest.json', release)
        with self.assertRaisesRegex(RuntimeError, 'base ativa e diferente'):
            u.apply_lab_update(self.p)
        self.assert_old_runtime()

    def test_failed_local_build_keeps_backup_and_current_version(self):
        self.prepare_package()
        self.p.log_command = lambda *a, **kw: (_ for _ in ()).throw(RuntimeError('build failed'))
        with self.assertRaisesRegex(RuntimeError, 'build failed'):
            u.apply_lab_update(self.p)
        self.assert_old_runtime()
        backups = list((self.root / 'updates/backups').glob('*/backup.json'))
        self.assertEqual(len(backups), 1)
        self.assertTrue(u.read(backups[0])['complete'])

    def test_retry_local_build_reuses_existing_candidate(self):
        self.prepare_package()
        command = self.p.log_command
        self.p.log_command = lambda *a, **kw: (_ for _ in ()).throw(RuntimeError('build failed'))
        with self.assertRaisesRegex(RuntimeError, 'build failed'):
            u.apply_lab_update(self.p)
        slot, = (self.root / 'updates/versions').iterdir()
        candidate = slot / 'base/AnyPS5'
        meta = u.read(candidate.parent / 'source_manifest.json')
        _, saved = u.load_backup(self.root, meta['update_backup'])
        self.p.log_command = command
        self.assertEqual(u.build_and_activate(self.p, candidate, slot, saved, meta['revision'], 2), 0)
        self.assertTrue(self.audit_called)
        self.assertEqual(u.active_base(self.root), candidate)

    def test_reapplying_is_noop(self):
        self.prepare_package()
        u.apply_lab_update(self.p)
        self.p.BASE = u.active_base(self.root)
        previous = u.state(self.root)
        with patch.object(u, 'make_backup', side_effect=AssertionError('must not back up')):
            self.assertEqual(u.apply_lab_update(self.p), 0)
        self.assertEqual(u.state(self.root), previous)

    def test_patch_cannot_change_converter_with_existing_game(self):
        package, release = self.prepare_package()
        name = 'core/relinker/main.cpp'
        target = package / 'files' / name
        target.parent.mkdir(parents=True); target.write_text('different converter')
        release['files'][name] = {'before': u.digest(self.base / name), 'after': u.digest(target)}
        u.atomic_json(package / 'manifest.json', release)
        with self.assertRaisesRegex(RuntimeError, 'conversor mudou'):
            u.apply_lab_update(self.p)
        self.assert_old_runtime()
