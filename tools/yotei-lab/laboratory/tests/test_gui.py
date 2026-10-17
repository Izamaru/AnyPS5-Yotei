import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

from tools import gui


class GuiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.controller = gui.Controller(self.root)

    def test_only_known_commands_and_validated_options_can_be_executed(self):
        settings = dict(gui.DEFAULTS, cache_mib=1024, dump_shaders=True)
        self.assertEqual(gui.command_for('run', settings), ['run', '--profile', 'desempenho', '--cache-mib', '1024', '--dump-shaders'])
        self.assertIn('--tests', gui.command_for('build', settings))
        for value in ({**settings, 'jobs': '4 & anything'}, {**settings, 'cache_mib': True},
                      {**settings, 'cache_mib': 999999}, {**settings, 'profile': 'external'},
                      {**settings, 'cpu': 'intel & anything'}, {**settings, 'extra': 'unexpected'}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                gui.validate_settings(value)
        with self.assertRaises(ValueError):
            gui.command_for('update', settings)
        with self.assertRaises(ValueError):
            gui.command_for('anything.exe', settings)

    def test_settings_persist_without_changing_other_project_state(self):
        wanted = dict(gui.DEFAULTS, cache_mib=1024, jobs=2)
        self.controller.save_settings(wanted)
        self.assertEqual(gui.Controller(self.root).settings, wanted)
        self.assertFalse((self.root / 'updates/state.json').exists())

    def test_four_cache_stages_are_persisted_and_forwarded_exactly(self):
        expected = (1024, 2048, 3072, 4096)
        self.assertEqual(gui.resource_profiles.CACHE_STAGES_MIB, expected)
        for amount in expected:
            with self.subTest(mib=amount):
                settings = dict(gui.DEFAULTS, cache_mib=amount)
                self.controller.save_settings(settings)
                self.assertEqual(gui.Controller(self.root).settings['cache_mib'], amount)
                command = gui.command_for('run', settings)
                self.assertEqual(command[command.index('--cache-mib') + 1], str(amount))
        for amount in (256, 512, 1536, 4097, 6144, 16384, 24576):
            with self.subTest(invalid=amount), self.assertRaises(ValueError):
                gui.validate_settings(dict(gui.DEFAULTS, cache_mib=amount))

    def test_old_cache_scale_migrates_without_resetting_other_preferences(self):
        for old, new in ((256, 1024), (2048, 2048), (3072, 3072), (6144, 4096), (24576, 4096)):
            with self.subTest(old=old):
                original = dict(gui.DEFAULTS, cache_mib=old, jobs=3, cpu='amd', dump_shaders=True)
                gui.u.atomic_json(self.controller.settings_path, original)
                self.assertEqual(gui.Controller(self.root).settings, {**original, 'cache_mib': new})

    def test_one_operation_at_a_time_and_shutdown_prevents_new_jobs(self):
        with patch.object(threading.Thread, 'start'):
            self.controller.start('status')
        with self.assertRaises(RuntimeError):
            self.controller.start('audit')
        self.controller.job['status'] = 'completed'
        self.controller.closing = True
        with self.assertRaises(RuntimeError):
            self.controller.start('status')

    def test_worker_collects_output_and_frames_from_only_its_process(self):
        tools = self.root / 'tools'
        tools.mkdir()
        (tools / 'project.py').write_text(
            "import time\nprint('[YOTEI] fixture', flush=True)\n"
            "print('[texture-cache] frame 10', flush=True)\ntime.sleep(.04)\n"
            "print('[texture-cache] frame 20', flush=True)\n", encoding='utf-8')
        self.controller.start('status')
        deadline = time.monotonic() + 5
        while self.controller.job['status'] == 'running' and time.monotonic() < deadline:
            time.sleep(.02)
        self.assertEqual(self.controller.job['status'], 'completed')
        self.assertEqual(self.controller.job['exit_code'], 0)
        self.assertEqual([row[1] for row in self.controller.frames], [10, 20])
        raw = (self.root / self.controller.job['log']).read_text(encoding='utf-8')
        self.assertIn('frame 20', raw)
        self.assertFalse(any('texture-cache' in row['text'] for row in self.controller.lines))
        self.assertIsNone(self.controller.process)

    def test_stop_is_restricted_to_an_owned_running_game_test(self):
        with self.assertRaises(ValueError):
            self.controller.stop()
        self.controller.process = SimpleNamespace(pid=12345, poll=lambda: None)
        self.controller.job = {'status': 'running', 'action': 'build'}
        with self.assertRaises(ValueError):
            self.controller.stop()


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.controller = gui.Controller(Path(self.temp.name))
        self.server = gui.Server(('127.0.0.1', 0), self.controller)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def request(self, path, body=None, headers=None):
        extra = dict(headers or {})
        if body is not None:
            extra.setdefault('Content-Type', 'application/json')
        request = urllib.request.Request(self.server.origin + path,
            data=json.dumps(body).encode() if body is not None else None, headers=extra)
        try:
            response = urllib.request.urlopen(request, timeout=3)
        except urllib.error.HTTPError as response:
            return response.code, json.load(response)
        with response:
            return response.status, json.load(response)

    def test_loopback_host_and_origin_are_checked(self):
        self.assertEqual(self.request('/api/health')[0], 200)
        self.assertEqual(self.request('/api/state', headers={'Host': 'unexpected.test'})[0], 403)
        self.assertEqual(self.request('/api/state', headers={'Origin': 'https://unexpected.test'})[0], 403)
        self.assertEqual(self.request('/tools/project.py')[0], 404)

    def test_mutations_need_session_token_and_valid_body(self):
        settings = dict(gui.DEFAULTS, cache_mib=3072)
        self.assertEqual(self.request('/api/settings', settings)[0], 403)
        headers = {'X-AnyPS5-Token': self.server.token}
        self.assertEqual(self.request('/api/settings', settings, headers)[0], 200)
        self.assertEqual(self.request('/api/state')[1]['settings'], settings)
        with patch.object(self.controller, 'start') as start:
            self.assertEqual(self.request('/api/action', {'action': 'run', 'command': 'bad'}, headers)[0], 404)
            start.assert_not_called()
        self.assertEqual(self.request('/api/action', {'action': 'unknown'}, headers)[0], 400)
        self.assertEqual(self.request('/api/settings', {**settings, 'jobs': 'bad'}, headers)[0], 400)

    def test_shutdown_is_blocked_during_an_operation(self):
        self.controller.job = {'status': 'running', 'action': 'build'}
        self.assertEqual(self.request('/api/shutdown', {}, {'X-AnyPS5-Token': self.server.token})[0], 409)
        self.assertFalse(self.controller.closing)


if __name__ == '__main__':
    unittest.main()
