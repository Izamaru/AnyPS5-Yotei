"""Local AnyPS5 interface. Standard library only; no remote service or shell commands."""
from __future__ import annotations

import argparse
from collections import deque
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
import urllib.request
import webbrowser

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools import updater as u, resource_profiles, log_retention

DEFAULTS = {'profile': 'desempenho', 'cache_mib': 2048, 'jobs': min(os.cpu_count() or 2, 4),
            'cpu': 'intel', 'dump_shaders': False}


def validate_settings(value):
    if not isinstance(value, dict) or set(value) != set(DEFAULTS):
        raise ValueError('Configuracao incompleta ou desconhecida.')
    if value['profile'] not in ('desempenho', 'economico') or value['cpu'] not in ('intel', 'amd'):
        raise ValueError('Perfil ou CPU invalido.')
    for key, low, high in (('cache_mib', 1024, resource_profiles.MAX_CACHE_MIB), ('jobs', 1, 8)):
        if type(value[key]) is not int or not low <= value[key] <= high:
            raise ValueError(f'Valor invalido: {key}')
    if value['cache_mib'] not in resource_profiles.CACHE_STAGES_MIB or type(value['dump_shaders']) is not bool:
        raise ValueError('Cache ou captura invalida.')
    return dict(value)


def command_for(action, settings):
    settings = validate_settings(settings)
    actions = {
        'run': ['run', '--profile', settings['profile'], '--cache-mib', str(settings['cache_mib'])]
               + (['--dump-shaders'] if settings['dump_shaders'] else []),
        'status': ['status'], 'audit': ['audit'], 'check-updates': ['check-updates'],
        'setup': ['setup'], 'build': ['build', '--tests', '--jobs', str(settings['jobs'])],
        'build-relinker': ['build', '--relinker-only', '--tests', '--jobs', str(settings['jobs'])],
        'relink': ['relink', '--cpu', settings['cpu']],
        'apply-lab-update': ['apply-lab-update', '--jobs', str(settings['jobs'])],
        'rollback': ['rollback'], 'backup': ['backup'],
    }
    if action not in actions:
        raise ValueError('Acao desconhecida.')
    return actions[action]


class Controller:
    def __init__(self, root=ROOT):
        self.root = Path(root)
        self.lock = threading.RLock()
        self.settings_path = self.root / 'updates/gui-settings.json'
        try:
            saved = u.read(self.settings_path, dict(DEFAULTS))
            # Migrate both earlier scales without resetting CPU/jobs/captures.
            if isinstance(saved, dict) and type(saved.get('cache_mib')) is int and 256 <= saved['cache_mib'] <= 24576:
                saved = {**saved, 'cache_mib': min(resource_profiles.CACHE_STAGES_MIB,
                         key=lambda amount: (abs(amount - saved['cache_mib']), -amount))}
            self.settings = validate_settings(saved)
        except (OSError, ValueError):
            self.settings = dict(DEFAULTS)
        self.job = None
        self.process = None
        self.sequence = 0
        self.lines = deque(maxlen=500)
        self.frames = deque(maxlen=10)
        self.resource_path = None
        self.stopping = False
        self.closing = False

    def save_settings(self, value):
        value = validate_settings(value)
        with self.lock:
            u.atomic_json(self.settings_path, value)
            self.settings = value
        return value

    def log(self, message):
        with self.lock:
            self.sequence += 1
            self.lines.append({'id': self.sequence, 'text': message[:1800]})

    def start(self, action):
        with self.lock:
            if self.closing:
                raise RuntimeError('O painel esta encerrando.')
            if self.job and self.job['status'] == 'running':
                raise RuntimeError('Uma operacao ja esta em andamento.')
            args = command_for(action, self.settings)
            if action == 'rollback' and not u.state(self.root)['history']:
                raise ValueError('Nao ha uma versao anterior entre os backups disponiveis.')
            self.root.joinpath('logs').mkdir(exist_ok=True)
            path = self.root / 'logs' / ('interface_' + datetime.now().strftime('%Y%m%d_%H%M%S_%f') + '.log')
            self.job = {'action': action, 'status': 'running', 'started': time.time(),
                        'log': path.relative_to(self.root).as_posix(), 'message': 'Iniciando…', 'exit_code': None}
            self.lines.clear()
            self.frames.clear()
            self.resource_path = None
            self.stopping = False
            self.process = None
            threading.Thread(target=self.worker, args=(args, path), daemon=True).start()
            return dict(self.job)

    def worker(self, args, path):
        code = -1
        try:
            environment = dict(os.environ, PYTHONIOENCODING='utf-8', PYTHONUNBUFFERED='1')
            temporary = self.root / 'logs/interface-temp'
            temporary.mkdir(exist_ok=True)
            environment.update(TEMP=str(temporary), TMP=str(temporary))
            python = Path(sys.executable)
            if python.name.lower() == 'pythonw.exe':
                python = python.with_name('python.exe')
            command = [str(python), str(self.root / 'tools/project.py'), *args]
            with path.open('w', encoding='utf-8') as output:
                with subprocess.Popen(command, cwd=self.root, env=environment,
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                        encoding='utf-8', errors='replace',
                        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0)) as process:
                    with self.lock:
                        self.process = process
                    for line in process.stdout:
                        output.write(line)
                        output.flush()
                        line = line.rstrip()
                        match = re.match(r'\[texture-cache\].*\bframe (\d+)\s*$', line)
                        if match:
                            with self.lock:
                                self.frames.append((time.monotonic(), int(match[1])))
                        if '[YOTEI] Log: ' in line:
                            candidate = Path(line.split('[YOTEI] Log: ', 1)[1].strip())
                            if candidate.name.startswith('game_') and candidate.resolve().is_relative_to((self.root / 'logs').resolve()):
                                self.resource_path = candidate.with_suffix('.resources.jsonl')
                        important = line.startswith(('[YOTEI]', '[ERRO]', 'FATAL:', 'Traceback', 'ERROR:', 'Error:', 'FAILED:', 'Ran ', 'OK', 'Test #'))
                        if important:
                            with self.lock:
                                self.job['message'] = line[:300]
                        # Full raw output is on disk; keep verbose texture/dispatch diagnostics out of the panel.
                        if important or not line.startswith('['):
                            self.log(line)
                    code = process.wait()
        except Exception as exc:
            self.log(str(exc))
            with self.lock:
                self.job['message'] = str(exc)
        finally:
            with self.lock:
                self.process = None
                stopped = self.stopping
                self.job.update(status='stopped' if stopped else ('completed' if code == 0 else 'failed'),
                                exit_code=code, ended=time.time())
                if code == 0:
                    self.job['message'] = 'Operacao concluida.'
                elif stopped:
                    self.job['message'] = 'Teste encerrado por voce.'
                else:
                    self.job['message'] = 'A operacao terminou com erro. Consulte a atividade e o log completo.'
            try:
                with u.operation_lock(self.root):
                    log_retention.prune_logs(self.root, announce=self.log)
            except (OSError, ValueError, RuntimeError) as exc:
                self.log('Limpeza automatica de logs adiada: ' + str(exc))

    def stop(self):
        with self.lock:
            if not self.job or self.job['status'] != 'running' or self.job['action'] != 'run' or not self.process:
                raise ValueError('Apenas o teste iniciado pela interface pode ser encerrado aqui.')
            if self.process.poll() is not None:
                return
            self.stopping = True
            pid = self.process.pid
            try:
                if os.name == 'nt':
                    # PID comes from our Popen, not the request; target only this owned process tree.
                    subprocess.run(['taskkill', '/PID', str(pid), '/T', '/F'], capture_output=True,
                                   timeout=15, creationflags=subprocess.CREATE_NO_WINDOW, check=True)
                else:
                    self.process.terminate()
            except (OSError, subprocess.SubprocessError) as exc:
                self.stopping = False
                raise RuntimeError('Nao foi possivel encerrar este teste: ' + str(exc)) from exc

    def resources(self):
        path = self.resource_path
        if not path or not path.is_file():
            return None
        try:
            with path.open('rb') as stream:
                stream.seek(max(0, path.stat().st_size - 8192))
                lines = stream.read().decode('utf-8', errors='replace').splitlines()
            for line in reversed(lines):
                try:
                    row = json.loads(line)
                    if row.get('type') == 'sample':
                        return row
                except ValueError:
                    pass
        except OSError:
            pass
        return None

    def snapshot(self):
        current = u.state(self.root)
        source = u.read(u.active_base(self.root).parent / 'source_manifest.json', {})
        port = u.read(self.root / 'games/Ghost-of-Yotei/runtime/port_manifest.json', {})
        benchmark = u.read(self.root / 'logs/watch-investigation.json', {})
        if 'selected_present_rate' not in benchmark:
            benchmark = u.read(self.root / 'logs/cache4-investigation.json', {})
        if 'selected_present_rate' not in benchmark:
            benchmark = u.read(self.root / 'logs/fps-optimization-result.json', {})
        with self.lock:
            fps = None
            if len(self.frames) > 1:
                duration = self.frames[-1][0] - self.frames[0][0]
                if duration > 0:
                    fps = (self.frames[-1][1] - self.frames[0][1]) / duration
            return {'settings': dict(self.settings), 'job': dict(self.job) if self.job else None,
                    'fps': fps, 'resources': self.resources(), 'can_rollback': bool(current['history']),
                    'ready': not port.get('convert_only', True) and (self.root / 'games/Ghost-of-Yotei/runtime/GhostOfYotei.exe').is_file(),
                    'release': source.get('upstream_release', '—'), 'lab_version': source.get('lab_version', '—'),
                    'revision': source.get('revision', '')[:12], 'cpu': port.get('cpu', '—'),
                    'benchmark_fps': benchmark.get('selected_present_rate'),
                    'backup_limit': u.BACKUP_LIMIT, 'cache_stages_mib': resource_profiles.CACHE_STAGES_MIB}

    def backup_list(self):
        history = set(u.state(self.root)['history'])
        return [{**row, 'rollback_target': row['id'] in history,
                 'next_rollback': bool(history) and row['id'] == u.state(self.root)['history'][-1]}
                for row in u.backup_catalog(self.root)]


class Server(ThreadingHTTPServer):
    daemon_threads = True
    def __init__(self, address, controller):
        super().__init__(address, Handler)
        self.controller = controller
        self.token = secrets.token_urlsafe(32)
        self.instance = secrets.token_hex(16)
        self.origin = f'http://127.0.0.1:{self.server_port}'
        self.last_seen = time.monotonic()


class Handler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def log_message(self, *args):
        pass

    def respond(self, code, value, kind='application/json; charset=utf-8'):
        body = value.encode('utf-8') if isinstance(value, str) else json.dumps(value, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', kind)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; frame-ancestors 'none'; connect-src 'self'")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def trusted_origin(self):
        return self.headers.get('Host') == self.server.origin.removeprefix('http://') and self.headers.get('Origin', self.server.origin) == self.server.origin

    def do_GET(self):
        if not self.trusted_origin():
            return self.respond(403, {'error': 'Origem nao autorizada.'})
        self.server.last_seen = time.monotonic()
        try:
            if self.path == '/':
                html = (ROOT / 'ui/index.html').read_text(encoding='utf-8')
                return self.respond(200, html.replace('__SESSION_TOKEN__', self.server.token), 'text/html; charset=utf-8')
            if self.path in ('/app.js', '/style.css'):
                kind = 'text/javascript' if self.path.endswith('.js') else 'text/css'
                return self.respond(200, (ROOT / 'ui' / self.path[1:]).read_text(encoding='utf-8'), kind + '; charset=utf-8')
            if self.path == '/api/health':
                return self.respond(200, {'app': 'anyps5-local', 'instance': self.server.instance})
            if self.path == '/api/state':
                return self.respond(200, self.server.controller.snapshot())
            if self.path == '/api/backups':
                return self.respond(200, {'backups': self.server.controller.backup_list(), 'keep': u.BACKUP_LIMIT})
            if self.path.startswith('/api/log?after='):
                after = int(self.path.split('=', 1)[1])
                with self.server.controller.lock:
                    return self.respond(200, {'lines': [row for row in self.server.controller.lines if row['id'] > after]})
            return self.respond(404, {'error': 'Pagina inexistente.'})
        except (OSError, ValueError, RuntimeError) as exc:
            return self.respond(400, {'error': str(exc)})

    def do_POST(self):
        if not self.trusted_origin() or not hmac.compare_digest(self.headers.get('X-AnyPS5-Token', ''), self.server.token):
            # Consume a small rejected body before closing the connection.
            # Closing with unread request bytes can reset the socket on Windows
            # before the client receives the 403 response.
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if 0 <= length <= 8192:
                    self.rfile.read(length)
            except (OSError, ValueError):
                pass
            return self.respond(403, {'error': 'Sessao nao autorizada.'})
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 8192 or self.headers.get('Content-Type') != 'application/json':
                raise ValueError('Requisicao invalida.')
            value = json.loads(self.rfile.read(length))
            self.server.last_seen = time.monotonic()
            controller = self.server.controller
            if self.path == '/api/settings':
                return self.respond(200, controller.save_settings(value))
            if self.path == '/api/action' and isinstance(value, dict) and set(value) == {'action'}:
                return self.respond(200, controller.start(value['action']))
            if self.path == '/api/stop' and value == {}:
                controller.stop()
                return self.respond(200, {'ok': True})
            if self.path == '/api/shutdown' and value == {}:
                with controller.lock:
                    if controller.job and controller.job['status'] == 'running':
                        raise RuntimeError('Aguarde a operacao terminar antes de fechar o painel.')
                    controller.closing = True
                self.respond(200, {'ok': True})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
            return self.respond(404, {'error': 'Acao inexistente.'})
        except (ValueError, TypeError, KeyError) as exc:
            return self.respond(400, {'error': str(exc)})
        except RuntimeError as exc:
            return self.respond(409, {'error': str(exc)})
        except OSError as exc:
            return self.respond(500, {'error': str(exc)})


def main():
    parser = argparse.ArgumentParser(description='Painel visual local do AnyPS5')
    parser.add_argument('--no-browser', action='store_true')
    parser.add_argument('--port', type=int, default=0)
    args = parser.parse_args()
    session_path = ROOT / 'logs/gui-session.json'
    try:
        previous = u.read(session_path, {})
        if type(previous.get('port')) is int and 0 < previous['port'] < 65536:
            url = f'http://127.0.0.1:{previous["port"]}'
            with urllib.request.urlopen(url + '/api/health', timeout=0.8) as response:
                running = json.load(response)
            if running.get('app') == 'anyps5-local' and running.get('instance') == previous.get('instance'):
                if not args.no_browser:
                    webbrowser.open(url)
                print(url, flush=True)
                return 0
    except (OSError, ValueError):
        pass
    controller = Controller()
    try:
        with u.operation_lock(ROOT):
            u.recover(ROOT, controller.log)
            u.prune_backups(SimpleNamespace(PROJECT_ROOT=ROOT, announce=controller.log))
            log_retention.prune_logs(ROOT, announce=controller.log)
    except (OSError, ValueError, RuntimeError) as exc:
        controller.log('Limpeza inicial adiada: ' + str(exc))
    server = Server(('127.0.0.1', args.port), controller)
    u.atomic_json(session_path, {'pid': os.getpid(), 'port': server.server_port, 'instance': server.instance})
    print(server.origin, flush=True)
    if not args.no_browser:
        webbrowser.open(server.origin)
    def idle_exit():
        while not threading.Event().wait(20):
            with controller.lock:
                busy = controller.job and controller.job['status'] == 'running'
                idle = not busy and time.monotonic() - server.last_seen > 180
                if idle:
                    controller.closing = True
            if idle:
                server.shutdown()
                return
    threading.Thread(target=idle_exit, daemon=True).start()
    try:
        server.serve_forever(poll_interval=0.3)
    finally:
        server.server_close()
        if u.read(session_path, {}).get('instance') == server.instance:
            session_path.unlink(missing_ok=True)
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as exc:
        ROOT.joinpath('logs').mkdir(exist_ok=True)
        ROOT.joinpath('logs/interface-startup-error.txt').write_text(str(exc), encoding='utf-8')
        raise
