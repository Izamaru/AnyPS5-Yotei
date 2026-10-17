"""Controlled AnyPS5 updates with verified backups and recoverable activation. GPL-2.0-only."""
from __future__ import annotations

import configparser
from contextlib import contextmanager
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import uuid

UPSTREAM = 'https://github.com/boykopovar/AnyPS5.git'
BRANCH = 'refs/heads/main'
SCHEMA = 1
BACKUP_LIMIT = 5
BACKUP_ID = re.compile(r'^\d{8}_\d{6}_[0-9a-f]{8}$')


def token():
    return datetime.now().strftime('%Y%m%d_%H%M%S') + '_' + uuid.uuid4().hex[:8]


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for part in iter(lambda: f.read(1024 * 1024), b''):
            h.update(part)
    return h.hexdigest()


def read(path, default=None):
    return json.loads(Path(path).read_text(encoding='utf-8')) if Path(path).is_file() else default


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with temp.open('w', encoding='utf-8') as f:
            json.dump(value, f, indent=2, ensure_ascii=False)
            f.write('\n'); f.flush(); os.fsync(f.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def checked(root, relative):
    rel = Path(relative)
    if rel.is_absolute() or '..' in rel.parts or '\\' in str(relative) or ':' in str(relative):
        raise RuntimeError(f'Caminho invalido no atualizador: {relative}')
    path = root / rel
    if path.resolve() == root.resolve() or not path.resolve().is_relative_to(root.resolve()):
        raise RuntimeError(f'Caminho fora do projeto: {relative}')
    current = path
    while current != root:
        if current.is_symlink() or (hasattr(current, 'is_junction') and current.is_junction()):
            raise RuntimeError(f'Links/juncoes nao suportados pelo atualizador: {current}')
        current = current.parent
    return path


def state(root):
    value = read(root / 'updates/state.json', {'schema': SCHEMA, 'active_base': 'base/AnyPS5', 'history': []})
    if value.get('schema') != SCHEMA:
        raise RuntimeError('Formato de estado do atualizador desconhecido.')
    return value


def active_base(root):
    value = state(root)['active_base']
    if value != 'base/AnyPS5' and not re.fullmatch(r'updates/(versions|restored)/[A-Za-z0-9_-]+/base/AnyPS5', value):
        raise RuntimeError('Diretorio da versao ativa invalido.')
    return checked(root, value)


@contextmanager
def operation_lock(root):
    directory = root / 'updates'
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / 'operation.lock').open('a+b') as f:
        f.seek(0, 2)
        if not f.tell():
            f.write(b'0'); f.flush()
        f.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError('Outro comando do projeto esta em uso. Feche o jogo ou aguarde a operacao atual.') from exc
        try:
            yield
        finally:
            f.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(f, fcntl.LOCK_UN)


def windows_runtime():
    return os.name == 'nt'


def require_game_closed():
    if os.name == 'nt':
        result = subprocess.run(['tasklist', '/FI', 'IMAGENAME eq GhostOfYotei.exe', '/FO', 'CSV', '/NH'],
                                capture_output=True, text=True, errors='replace', timeout=30, check=True)
        if 'ghostofyotei.exe' in result.stdout.lower():
            raise RuntimeError('Feche GhostOfYotei.exe antes de atualizar ou restaurar.')


def inventory(directory, source=False, excluded=()):
    directory = Path(directory)
    result = {}
    if not directory.exists():
        return result
    for folder, dirs, files in os.walk(directory, followlinks=False):
        here = Path(folder)
        for name in list(dirs):
            path = here / name
            rel = path.relative_to(directory).as_posix()
            skip = name in ('.git', '__pycache__') or (source and here == directory and name in ('build', 'build-relinker', 'shader_cache')) or rel in excluded
            if skip:
                dirs.remove(name); continue
            checked(directory, rel)
        for name in files:
            path = here / name
            rel = path.relative_to(directory).as_posix()
            if name == '.git' or name.endswith('.pyc'):
                continue
            checked(directory, rel)
            result[rel] = {'sha256': digest(path), 'bytes': path.stat().st_size}
    return result


def copy_inventory(source, destination, records):
    destination.mkdir(parents=True, exist_ok=True)
    for name, record in records.items():
        src, dst = checked(source, name), checked(destination, name)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        if digest(dst) != record['sha256']:
            raise RuntimeError(f'Copia divergente: {name}')


def verify_inventory(directory, records, source=False):
    if inventory(directory, source=source) != records:
        raise RuntimeError(f'Backup ou arquivos foram alterados/incompletos: {directory}')


def converted_fingerprint(p):
    manifest = read(p.RUNTIME / 'port_manifest.json', {})
    result = {}
    for name in manifest.get('generated_files', []):
        # Older Windows conversions serialized native separators. Normalize only
        # at this boundary; checked() still rejects escapes, drives and links.
        # Keep the original key so existing backup fingerprints remain valid.
        path = checked(p.RUNTIME, name.replace('\\', '/'))
        if not path.is_file():
            raise RuntimeError(f'Arquivo convertido ausente: {name}')
        result[name] = digest(path)
    if manifest and not manifest.get('convert_only') and not result:
        raise RuntimeError('Manifesto sem lista de arquivos convertidos; atualizacao automatica bloqueada.')
    return result


def converted_name(name):
    """Restrict transactional conversion changes to relinker-generated outputs."""
    name = name.replace('\\', '/')
    root_files = {'GhostOfYotei.exe', 'GhostOfYotei.registry.json', 'windows-diagnostics-imports.txt'}
    registry = re.fullmatch(r'GhostOfYotei\.[A-Za-z0-9_.+-]+\.prx\.guest\.prx\.registry\.json', name)
    module = re.fullmatch(r'app0/(?:sce_module|sce_modules|prx)/[A-Za-z0-9_.+-]+\.prx\.guest\.prx', name)
    if name not in root_files and not registry and not module:
        raise RuntimeError(f'Arquivo nao reconhecido como gerado pelo conversor: {name}')
    if '..' in Path(name).parts:
        raise RuntimeError(f'Caminho invalido de arquivo convertido: {name}')
    return name


def converted_inventory(runtime, manifest):
    records = {}
    seen = set()
    for original in (manifest or {}).get('generated_files', []):
        name = converted_name(original)
        if name.casefold() in seen:
            raise RuntimeError(f'Arquivo convertido duplicado no manifesto: {name}')
        seen.add(name.casefold())
        path = checked(runtime, name)
        if not path.is_file():
            raise RuntimeError(f'Arquivo convertido ausente: {name}')
        records[name] = {'sha256': digest(path), 'bytes': path.stat().st_size}
    return records


def make_backup(p, reason):
    base = active_base(p.PROJECT_ROOT)
    source = inventory(base, source=True)
    libraries = inventory(p.RUNTIME / 'libs')
    port = read(p.RUNTIME / 'port_manifest.json')
    converted = converted_inventory(p.RUNTIME, port)
    fingerprint = converted_fingerprint(p)
    size = sum(x['bytes'] for group in (source, libraries, converted) for x in group.values())
    if shutil.disk_usage(p.PROJECT_ROOT).free < size + 512 * 1024 * 1024:
        raise RuntimeError('Espaco insuficiente para o backup e margem de 512 MiB. Nada foi atualizado.')
    ident = token()
    folder = p.PROJECT_ROOT / 'updates/backups' / ident
    folder.mkdir(parents=True)
    p.announce(f'Backup {ident}: fontes, dependencias, bibliotecas e conversao ({size / 1048576:.0f} MiB).')
    copy_inventory(base, folder / 'source', source)
    copy_inventory(p.RUNTIME / 'libs', folder / 'libs', libraries)
    copy_inventory(p.RUNTIME, folder / 'converted', converted)
    metadata = {
        'schema': SCHEMA, 'id': ident, 'reason': reason, 'state': state(p.PROJECT_ROOT),
        'source_manifest': read(base.parent / 'source_manifest.json', {}),
        'dependency_manifest': read(base.parent / 'dependency_manifest.json'),
        'port_manifest': port,
        'source_files': source, 'library_files': libraries,
        'converted_files': fingerprint, 'converted_payload': converted,
        'had_libraries': (p.RUNTIME / 'libs').is_dir(), 'complete': True,
    }
    # Publish completeness only after all copies have been hashed successfully.
    verify_inventory(base, source, source=True)
    verify_inventory(p.RUNTIME / 'libs', libraries)
    if converted_fingerprint(p) != fingerprint or read(p.RUNTIME / 'port_manifest.json') != port:
        raise RuntimeError('A conversao mudou durante o backup; backup nao publicado.')
    atomic_json(folder / 'backup.json', metadata)
    p.announce(f'Backup verificado: {folder}')
    return folder, metadata


def load_backup(root, ident):
    if not re.fullmatch(r'[A-Za-z0-9_-]+', ident):
        raise RuntimeError('Identificador de backup invalido.')
    folder = checked(root, 'updates/backups/' + ident)
    meta = read(folder / 'backup.json', {})
    if meta.get('schema') != SCHEMA or not meta.get('complete') or meta.get('id') != ident:
        raise RuntimeError('Backup incompleto ou desconhecido; restauracao bloqueada.')
    verify_inventory(folder / 'source', meta['source_files'])
    verify_inventory(folder / 'libs', meta['library_files'])
    if 'converted_payload' in meta:
        records = meta['converted_payload']
        expected = {converted_name(name): value for name, value in meta['converted_files'].items()}
        if len(expected) != len(meta['converted_files']) or {name: row['sha256'] for name, row in records.items()} != expected:
            raise RuntimeError('Inventario da conversao diverge do backup.')
        for name in records:
            if converted_name(name) != name:
                raise RuntimeError('Caminho de conversao nao normalizado no backup.')
        verify_inventory(folder / 'converted', records)
    return folder, meta


def remove(path):
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


def recover(root, announce):
    pending = root / 'updates/pending.json'
    journal = read(pending)
    if not journal:
        return False
    if journal.get('schema') != SCHEMA:
        raise RuntimeError('Registro de recuperacao desconhecido; nao inicie o jogo.')
    if journal.get('status') == 'committed':
        pending.unlink()
        return False
    require_game_closed()
    actions = journal['actions']
    allowed = {'games/Ghost-of-Yotei/runtime/libs', 'games/Ghost-of-Yotei/runtime/port_manifest.json', 'updates/state.json'}
    for action in actions:
        if action['dest'] not in allowed:
            prefix = 'games/Ghost-of-Yotei/runtime/'
            if not action['dest'].startswith(prefix):
                raise RuntimeError('Destino invalido no registro de recuperacao.')
            converted_name(action['dest'][len(prefix):])
        checked(root, action['dest'])
        for key in ('new', 'old'):
            if not action[key].startswith('updates/transactions/' + journal['id'] + '/'):
                raise RuntimeError('Caminho invalido no registro de recuperacao.')
            checked(root, action[key])
    announce('Recuperando troca interrompida: restaurando o estado anterior...')
    for action in reversed(actions):
        dest, old, new = [checked(root, action[k]) for k in ('dest', 'old', 'new')]
        if old.exists():
            remove(dest)
            dest.parent.mkdir(parents=True, exist_ok=True)
            os.replace(old, dest)
        elif not action['existed'] and not new.exists():
            remove(dest)
    pending.unlink()
    announce('Estado anterior recuperado. Backup preservado.')
    return True


def activate(p, next_state, libs=None, manifest=None, converted=None):
    root = p.PROJECT_ROOT
    ident = token()
    transaction = root / 'updates/transactions' / ident
    transaction.mkdir(parents=True)
    plans = []
    removed = set()
    if converted is not None:
        if manifest is None:
            raise RuntimeError('Troca de conversao exige manifesto.')
        incoming = converted_inventory(Path(converted), manifest)
        current = converted_inventory(p.RUNTIME, read(p.RUNTIME / 'port_manifest.json'))
        combined = current.keys() | incoming.keys()
        if len({name.casefold() for name in combined}) != len(combined):
            raise RuntimeError('Nomes convertidos diferem apenas por maiusculas; revise a conversao.')
        copy_inventory(Path(converted), transaction / 'new/converted', incoming)
        for name in sorted(current.keys() | incoming.keys()):
            destination = 'games/Ghost-of-Yotei/runtime/' + name
            plans.append((destination, 'converted/' + name))
            if name not in incoming:
                removed.add(destination)
    if libs is not None:
        records = inventory(libs)
        copy_inventory(libs, transaction / 'new/libs', records)
        plans.append(('games/Ghost-of-Yotei/runtime/libs', 'libs'))
    if manifest is not None:
        atomic_json(transaction / 'new/port_manifest.json', manifest)
        plans.append(('games/Ghost-of-Yotei/runtime/port_manifest.json', 'port_manifest.json'))
    atomic_json(transaction / 'new/state.json', next_state)
    plans.append(('updates/state.json', 'state.json'))
    actions = []
    for dest, name in plans:
        destination = checked(root, dest)
        actions.append({'dest': dest, 'new': f'updates/transactions/{ident}/new/{name}',
                        'old': f'updates/transactions/{ident}/old/{name}', 'existed': destination.exists(),
                        'remove': dest in removed})
    journal = {'schema': SCHEMA, 'id': ident, 'status': 'installing', 'actions': actions}
    pending = root / 'updates/pending.json'
    atomic_json(pending, journal)
    try:
        for action in actions:
            dest, old, new = [checked(root, action[k]) for k in ('dest', 'old', 'new')]
            old.parent.mkdir(parents=True, exist_ok=True)
            dest.parent.mkdir(parents=True, exist_ok=True)
            if action['existed']:
                os.replace(dest, old)
            if not action['remove']:
                os.replace(new, dest)
        journal['status'] = 'committed'
        atomic_json(pending, journal)
    except BaseException:
        try:
            recover(root, p.announce)
        except Exception as exc:
            raise RuntimeError('Recuperacao pendente; feche o jogo e execute rollback. Nao apague updates/.') from exc
        raise
    pending.unlink(missing_ok=True)
    # Backups are durable; this is only redundant transaction data.
    shutil.rmtree(transaction, ignore_errors=True)


def git(args, cwd=None, timeout=180):
    result = subprocess.run(['git', '-c', 'core.autocrlf=false', *args], cwd=cwd, capture_output=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError('Git falhou: ' + result.stderr.decode('utf-8', 'replace')[-3000:])
    return result.stdout


def latest():
    lines = git(['ls-remote', UPSTREAM, BRANCH]).decode().splitlines()
    value = next((line.split()[0] for line in lines if line.endswith('\t' + BRANCH)), '')
    if not re.fullmatch(r'[0-9a-f]{40}', value):
        raise RuntimeError('Nao foi possivel identificar a revisao main do AnyPS5.')
    return value


def check(p):
    p.require_tool('git')
    revision = latest()
    current = read(active_base(p.PROJECT_ROOT).parent / 'source_manifest.json', {}).get('revision')
    result = {'current': current, 'available': revision, 'update_available': revision != current,
              'url': f'https://github.com/boykopovar/AnyPS5/compare/{current}...{revision}'}
    atomic_json(p.PROJECT_ROOT / 'updates/last_check.json', result)
    p.announce(f'Instalado: {current}\nDisponivel: {revision}')
    p.announce('Atualizacao disponivel. Use U para preparar, testar e atualizar.' if revision != current else 'A base ja esta na revisao main consultada; correcoes locais permanecem aplicadas.')
    p.announce('Mudancas: ' + result['url'])
    return result


def submodules(base):
    config = configparser.ConfigParser(interpolation=None)
    config.read(base / '.gitmodules', encoding='utf-8')
    result = []
    for section in config.sections():
        rel = config[section]['path']
        checked(base, rel)
        if not rel.startswith('3rdparty/'):
            raise RuntimeError('Submodulo fora de 3rdparty; integracao manual necessaria.')
        result.append(rel)
    return result


def extract_archive(archive, dest):
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive) as stream:
        for item in stream:
            path = checked(dest, item.name.rstrip('/'))
            if item.isdir():
                path.mkdir(parents=True, exist_ok=True)
            elif item.isfile():
                path.parent.mkdir(parents=True, exist_ok=True)
                with stream.extractfile(item) as src, path.open('wb') as dst:
                    shutil.copyfileobj(src, dst)
            else:
                raise RuntimeError('Link ou arquivo especial no codigo upstream; integracao manual necessaria.')


def merge_sources(original, local, incoming, report_path):
    excluded = set(submodules(original)) | set(submodules(local)) | set(submodules(incoming))
    inventories = [inventory(x, source=True, excluded=excluded) for x in (original, local, incoming)]
    old_files, local_files, new_files = inventories
    changed = sorted(name for name in old_files.keys() | local_files.keys()
                     if old_files.get(name) != local_files.get(name))
    report = {'merged': [], 'already_upstream': [], 'conflicts': [], 'local_files': changed}
    patches = []
    for name in changed:
        paths = [x / name for x in (original, local, incoming)]
        old, ours, theirs = [x.read_bytes() if x.is_file() else None for x in paths]
        if ours == theirs:
            report['already_upstream'].append(name); continue
        merged = ours
        if name == '.gitmodules':
            report['conflicts'].append(name); continue
        if theirs != old:
            if None in (old, ours, theirs) or any(b'\x00' in x for x in (old, ours, theirs)):
                report['conflicts'].append(name); continue
            result = subprocess.run(['git', 'merge-file', '-p', str(paths[1]), str(paths[0]), str(paths[2])], capture_output=True)
            if result.returncode:
                report['conflicts'].append(name); continue
            merged = result.stdout
        dest = paths[2]
        if merged is None:
            dest.unlink(missing_ok=True)
        else:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(merged)
        report['merged'].append(name)
        if merged is not None:
            patches.append({'file': name, 'reason': 'Preserved local changes by controlled updater',
                            'original_sha256': hashlib.sha256(theirs).hexdigest() if theirs is not None else None,
                            'modified_sha256': hashlib.sha256(merged).hexdigest()})
    atomic_json(report_path, report)
    if report['conflicts']:
        raise RuntimeError(f"Conflitos em {len(report['conflicts'])} arquivo(s). Versao ativa preservada. Relatorio: {report_path}")
    return patches, report


def require_conversion_compatible(p, incoming):
    if (p.RUNTIME / 'port_manifest.json').is_file():
        if inventory(p.BASE / 'core/relinker') != inventory(incoming / 'core/relinker'):
            raise RuntimeError('O conversor mudou. Esta atualizacao exige avaliar uma reconversao; ativacao automatica bloqueada. Versao atual preservada.')


def apply_lab_update(p, jobs=4):
    """Install the reviewed bundled delta into an isolated, rollback-capable slot."""
    require_game_closed()
    package = p.PROJECT_ROOT / 'docs/lab-update'
    release = read(package / 'manifest.json', {})
    if release.get('schema') != SCHEMA or not release.get('files'):
        raise RuntimeError('Pacote de correcao local ausente/incompleto em docs/lab-update.')
    current = read(p.BASE.parent / 'source_manifest.json', {})
    if current.get('revision') != release.get('revision'):
        raise RuntimeError('Esta correcao exige a revisao indicada no pacote. A base ativa e diferente; nada foi alterado.')
    for key, value in release.get('requires', {}).items():
        if current.get(key) != value:
            raise RuntimeError(f'Base incompativel com a correcao: {key}. Nada foi alterado.')
    pending = []
    for name, record in release['files'].items():
        source = checked(package / 'files', name)
        destination = checked(p.BASE, name)
        if not source.is_file() or digest(source) != record['after']:
            raise RuntimeError(f'Pacote incompleto ou alterado: {name}')
        actual = digest(destination) if destination.is_file() else None
        if actual not in (record['before'], record['after']):
            raise RuntimeError(f'Correcao conflita com a base ativa: {name}. Nada foi alterado.')
        if actual != record['after']:
            pending.append(name)
    if not pending:
        p.announce('A correcao deste pacote ja esta presente na base ativa.')
        return 0
    for tool in ('git', 'cmake', 'ninja', 'gcc', 'g++', 'ctest'):
        p.require_tool(tool)
    p.check_compiler()
    backup, saved = make_backup(p, 'before-lab-update-' + release['version'])
    slot = p.PROJECT_ROOT / 'updates/versions' / token()
    candidate = slot / 'base/AnyPS5'
    copy_inventory(backup / 'source', candidate, saved['source_files'])
    for name, record in release['files'].items():
        target = checked(candidate, name)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(checked(package / 'files', name), target)
        if digest(target) != record['after']:
            raise RuntimeError(f'Copia da correcao divergente: {name}')
    require_conversion_compatible(p, candidate)
    patches = {entry['file']: dict(entry) for entry in current.get('patches', [])}
    for name, record in release['files'].items():
        entry = patches.setdefault(name, {'file': name, 'original_sha256': record['before']})
        entry.update(modified_sha256=record['after'], reason='Bundled lab correction ' + release['version'],
                     additional_patch=release['patch'])
    manifest = {**current, **release.get('metadata', {}), 'patches': list(patches.values()),
                'source_modified': True, 'lab_version': release['version'], 'update_backup': saved['id']}
    atomic_json(candidate.parent / 'source_manifest.json', manifest)
    if saved['dependency_manifest'] is not None:
        atomic_json(candidate.parent / 'dependency_manifest.json', saved['dependency_manifest'])
    p.announce('Correcao preparada em pasta isolada. A versao atual continua ativa ate concluir todas as verificacoes.')
    return build_and_activate(p, candidate, slot, saved, current['revision'], jobs)


def update(p, jobs=4):
    require_game_closed()
    for tool in ('git', 'cmake', 'ninja', 'gcc', 'g++', 'ctest'):
        p.require_tool(tool)
    p.check_compiler()
    info = check(p)
    if not info['update_available']:
        return 0
    old_revision, revision = info['current'], info['available']
    if not re.fullmatch(r'[0-9a-f]{40}', old_revision or ''):
        raise RuntimeError('A base nao tem revisao upstream valida; atualizacao bloqueada.')
    backup, saved = make_backup(p, 'before-update')
    slot = p.PROJECT_ROOT / 'updates/versions' / token()
    candidate = slot / 'base/AnyPS5'
    candidate.mkdir(parents=True)
    p.announce('Baixando a revisao fixada em uma pasta isolada...')
    git(['init', str(candidate)])
    git(['config', 'core.autocrlf', 'false'], candidate)
    git(['remote', 'add', 'origin', UPSTREAM], candidate)
    git(['fetch', '--depth', '1', 'origin', revision, old_revision], candidate)
    git(['checkout', '--detach', revision], candidate)
    if git(['rev-parse', 'HEAD'], candidate).decode().strip() != revision:
        raise RuntimeError('Revisao baixada divergente.')
    archive = slot / 'original.tar'
    git(['archive', old_revision, '--output', str(archive)], candidate)
    original = slot / 'original'
    extract_archive(archive, original)
    p.announce('Comparando base original, correcoes locais e nova revisao...')
    patches, report = merge_sources(original, p.BASE, candidate, slot / 'merge_report.json')
    require_conversion_compatible(p, candidate)
    manifest = {**saved['source_manifest'], 'revision': revision, 'revision_origin': 'git fetch pinned upstream commit',
                'patches': patches, 'source_modified': bool(patches), 'lab_version': p.VERSION,
                'updated_from': old_revision, 'update_backup': saved['id']}
    atomic_json(candidate.parent / 'source_manifest.json', manifest)
    p.announce('Baixando dependencias fixadas da nova revisao...')
    p.log_command(['git', 'submodule', 'update', '--init', '--recursive'], cwd=candidate, label='update-deps')
    status = git(['submodule', 'status', '--recursive'], candidate).decode().splitlines()
    if any(line and line[0] != ' ' for line in status):
        raise RuntimeError('Dependencias divergentes da revisao solicitada.')
    atomic_json(candidate.parent / 'dependency_manifest.json', {'revision': revision, 'submodules': status})
    return build_and_activate(p, candidate, slot, saved, revision, jobs)


def build_and_activate(p, candidate, slot, saved, revision, jobs):
    for name in ('tools', 'tests'):
        shutil.copytree(p.PROJECT_ROOT / name, slot / name, ignore=shutil.ignore_patterns('__pycache__'), dirs_exist_ok=True)
    p.announce('Compilando a candidata e executando os testes. Isso pode demorar e usar bastante disco.')
    p.log_command([sys.executable, str(slot / 'tools/project.py'), 'build', '--tests', '--jobs', str(jobs)], cwd=slot, label='update-build')
    p.log_command([sys.executable, '-m', 'unittest', 'discover', '-s', 'tests', '-v'], cwd=slot, label='update-lab-tests')
    # The existing converted executable is never replaced by this workflow.
    port = read(p.RUNTIME / 'port_manifest.json')
    staged_libs = None
    if port and not port.get('convert_only'):
        if not windows_runtime():
            raise RuntimeError('Ativacao do runtime Windows exige o proprio Windows.')
        previous_base = p.BASE
        try:
            p.BASE = candidate
            inputs = p.library_inputs()
            staged_libs = slot / 'runtime-libs'
            staged_libs.mkdir()
            for source in inputs:
                shutil.copy2(source, staged_libs / source.name)
            # Audit the current registries/guest modules against the new host libraries.
            old_library_function = p.runtime_libraries
            try:
                p.runtime_libraries = lambda: staged_libs
                if p.cmd_audit(None):
                    raise RuntimeError('Auditoria bloqueou o novo runtime. Versao anterior preservada.')
            finally:
                p.runtime_libraries = old_library_function
        finally:
            p.BASE = previous_base
        port = {**port, 'library_revision': revision, 'library_lab_version': p.VERSION,
                'library_files': ['libs/' + x.name for x in inputs],
                'library_sha256': {x.name: digest(x) for x in staged_libs.iterdir()}, 'game_tested': False}
    require_game_closed()
    verify_inventory(p.BASE, saved['source_files'], source=True)
    verify_inventory(p.RUNTIME / 'libs', saved['library_files'])
    if converted_fingerprint(p) != saved['converted_files'] or read(p.RUNTIME / 'port_manifest.json') != saved['port_manifest']:
        raise RuntimeError('O runtime mudou durante a preparacao; ativacao bloqueada.')
    if state(p.PROJECT_ROOT) != saved['state'] or read(p.BASE.parent / 'source_manifest.json', {}) != saved['source_manifest']:
        raise RuntimeError('A versao ativa mudou durante a preparacao; ativacao bloqueada.')
    next_state = {**state(p.PROJECT_ROOT), 'active_base': candidate.relative_to(p.PROJECT_ROOT).as_posix(),
                  'history': [*state(p.PROJECT_ROOT)['history'], saved['id']]}
    activate(p, next_state, staged_libs, port)
    p.announce(f'Atualizacao ativada: {revision[:12]}. Backup para retorno: {saved["id"]}. Teste o jogo; compilacao nao comprova compatibilidade.')
    return 0


def rollback(p):
    require_game_closed()
    current = state(p.PROJECT_ROOT)
    if not current['history']:
        raise RuntimeError('Nenhuma atualizacao ativada para desfazer. Backups de tentativas ficam em updates/backups/.')
    folder, saved = load_backup(p.PROJECT_ROOT, current['history'][-1])
    if 'converted_payload' not in saved and converted_fingerprint(p) != saved['converted_files']:
        raise RuntimeError('Os arquivos convertidos mudaram desde o backup. Retorno automatico bloqueado para evitar misturar versoes.')
    make_backup(p, 'before-rollback')
    # Restore into a fresh location; never overwrite the user's source tree.
    restored = p.PROJECT_ROOT / 'updates/restored' / token() / 'base/AnyPS5'
    copy_inventory(folder / 'source', restored, saved['source_files'])
    atomic_json(restored.parent / 'source_manifest.json', saved['source_manifest'])
    if saved['dependency_manifest'] is not None:
        atomic_json(restored.parent / 'dependency_manifest.json', saved['dependency_manifest'])
    next_state = {**saved['state'], 'active_base': restored.relative_to(p.PROJECT_ROOT).as_posix(),
                  'history': retained_history(p.PROJECT_ROOT, saved['state']['history'])}
    activate(p, next_state, folder / 'libs' if saved['port_manifest'] is not None else None, saved['port_manifest'],
             converted=folder / 'converted' if 'converted_payload' in saved else None)
    p.announce('Versao anterior restaurada a partir do backup verificado, incluindo sua conversao quando disponivel.')
    p.announce('Para recompilar/converter novamente, use 4: caches de compilacao nao fazem parte do backup.')
    return 0


def backups(p):
    history = set(state(p.PROJECT_ROOT)['history'])
    paths = sorted((p.PROJECT_ROOT / 'updates/backups').glob('*/backup.json'))
    for path in paths:
        value = read(path, {})
        p.announce(f"{value.get('id')}: {value.get('reason')} | revisao {value.get('source_manifest', {}).get('revision')} | {'na cadeia de retorno' if value.get('id') in history else 'backup de seguranca/tentativa'}")
    if not paths:
        p.announce('Nenhum backup criado ainda.')
    return 0


def backup_catalog(root):
    """Only complete, managed backups; never follow a junction for retention."""
    directory = checked(root, 'updates/backups')
    if not directory.exists():
        return []
    records = []
    for folder in directory.iterdir():
        if not BACKUP_ID.fullmatch(folder.name):
            continue
        folder = checked(root, 'updates/backups/' + folder.name)
        path = checked(root, 'updates/backups/' + folder.name + '/backup.json')
        if not folder.is_dir() or not path.is_file():
            continue
        try:
            value = read(path)
        except (OSError, ValueError):
            continue
        if not isinstance(value, dict) or value.get('schema') != SCHEMA or value.get('id') != folder.name or not value.get('complete'):
            continue
        size = sum(item.get('bytes', 0) for key in ('source_files', 'library_files', 'converted_payload')
                   for item in value.get(key, {}).values())
        records.append({'id': folder.name, 'reason': value.get('reason', ''),
                        'revision': value.get('source_manifest', {}).get('revision'),
                        'lab_version': value.get('source_manifest', {}).get('lab_version'),
                        'bytes': size, 'published_ns': path.stat().st_mtime_ns})
    return sorted(records, key=lambda row: (row['id'][:15], row['published_ns'], row['id']), reverse=True)


def retained_history(root, history):
    available = {row['id'] for row in backup_catalog(root)}
    return [ident for ident in history if ident in available]


def prune_backups(p, keep=BACKUP_LIMIT):
    """Call under operation_lock, after the operation releases its backup inputs.

    A rename into backup-trash removes an obsolete copy from the catalog before
    deleting it. A locked/partially deleted tree is retried on the next call.
    Backup snapshots remain immutable; rollback filters their historical IDs.
    """
    if not isinstance(keep, int) or isinstance(keep, bool) or keep < 1:
        raise ValueError('O limite de backups deve ser positivo.')
    root = p.PROJECT_ROOT
    if (root / 'updates/pending.json').exists():
        p.announce('Limpeza de backups adiada ate concluir a recuperacao pendente.')
        return []
    catalog = backup_catalog(root)
    victims = catalog[keep:]
    # Once five complete copies exist, abandoned partial copies add no usable
    # restore point. We hold the operation lock and never clean during recovery,
    # so a copy still being created cannot be mistaken for an abandoned one.
    if len(catalog) >= keep:
        complete = {row['id'] for row in catalog}
        directory = checked(root, 'updates/backups')
        for folder in directory.iterdir():
            if not BACKUP_ID.fullmatch(folder.name) or folder.name in complete:
                continue
            folder = checked(root, 'updates/backups/' + folder.name)
            if not folder.is_dir() or not {item.name for item in folder.iterdir()} <= {'source', 'libs', 'converted', 'backup.json', 'project-configuration'}:
                continue
            metadata = checked(root, 'updates/backups/' + folder.name + '/backup.json')
            if metadata.exists():
                try:
                    value = read(metadata)
                except (OSError, ValueError):
                    continue
                if not isinstance(value, dict) or value.get('schema') != SCHEMA or value.get('id') != folder.name or value.get('complete') is not False:
                    continue
            victims.append({'id': folder.name, 'reason': 'obsolete-incomplete-backup', 'bytes': 0})
    # Preflight all absolute targets before any move, recursive removal or state write.
    trash = checked(root, 'updates/backup-trash')
    plans = [(row, checked(root, 'updates/backups/' + row['id']),
              checked(root, 'updates/backup-trash/' + row['id'])) for row in victims]
    pending = []
    if trash.exists():
        for item in trash.iterdir():
            if BACKUP_ID.fullmatch(item.name):
                pending.append(checked(root, 'updates/backup-trash/' + item.name))
    for target in pending:
        try:
            remove(target)
        except OSError:
            p.announce(f'Limpeza sera repetida quando o backup estiver liberado: {target.name}')
    survivors = {row['id'] for row in catalog[:keep]}
    current = state(root)
    history = [ident for ident in current['history'] if ident in survivors]
    if history != current['history']:
        atomic_json(root / 'updates/state.json', {**current, 'history': history})
    removed = []
    for row, source, destination in plans:
        try:
            trash.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                raise OSError('Limpeza anterior ainda pendente')
            os.replace(source, destination)
            removed.append(row)
            try:
                remove(destination)
            except OSError:
                p.announce(f'Backup antigo retirado; exclusao sera repetida: {row["id"]}')
            p.announce(f'Backup antigo removido pela retencao de {keep} copias: {row["id"]}')
        except OSError as exc:
            p.announce(f'Nao foi possivel limpar {row["id"]}; nova tentativa na proxima operacao: {exc}')
    if removed:
        previous = read(root / 'updates/backup-retention.json', {}).get('events', [])
        atomic_json(root / 'updates/backup-retention.json', {
            'schema': 1, 'keep': keep,
            'events': [*previous, {'time': datetime.now().isoformat(timespec='seconds'), 'removed': removed}][-100:]})
    return removed
