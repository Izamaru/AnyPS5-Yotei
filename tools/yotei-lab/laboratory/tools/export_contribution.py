"""Prepare a local source review bundle. GPL-2.0-only. Never uploads files."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import sys

try:
    from . import updater
except ImportError:
    import updater

LAB_FILES = (
    'tools/__init__.py', 'tools/project.py', 'tools/updater.py',
    'tools/gui.py', 'tools/setup_windows.py', 'tools/ffmpeg_snapshot.py',
    'tools/resource_monitor.py', 'tools/resource_profiles.py',
    'tools/log_retention.py', 'tests/test_log_retention.py',
    'tools/start_gui.vbs', 'tools/export_contribution.py',
    'tests/test_backup_retention.py', 'tests/test_ffmpeg_snapshot.py',
    'tests/test_gui.py', 'tests/test_project.py',
    'tests/test_resource_budgets_cpp.py', 'tests/test_resources.py',
    'tests/test_setup_windows.py', 'tests/test_shader_diagnostics.py',
    'tests/test_updater.py', 'tests/test_vrr_backport.py',
    'tests/test_export_contribution.py',
    'ui/app.js', 'ui/index.html', 'ui/style.css',
    'INICIAR_AQUI.bat', 'MENU_TERMINAL.bat',
)
DEPENDENCY = '3rdparty/libusb-cmake/libusb/build-aux/gen-describe.ps1'
TEXT_SUFFIXES = {'.cpp', '.hpp', '.h', '.c', '.py', '.ps1', '.cmake', '.md'}
SECRET_PATTERNS = (
    ('GitHub token', re.compile(r'\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{30,})\b')),
    ('AWS access key', re.compile(r'\b(?:AKIA|ASIA)[A-Z0-9]{16}\b')),
    ('private key', re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----')),
    ('OpenAI key', re.compile(r'\bsk-(?:proj-)?[A-Za-z0-9_-]{30,}\b')),
    ('personal path', re.compile(r'(?i)(?:[A-Z]:[\\/]+Users[\\/]+[^\\/\s"\']+|/(?:home|Users)/[^/\s"\']+)')),
    ('URL credentials', re.compile(r'https?://[^\s/:@]+:[^\s/@]+@')),
    ('literal credential', re.compile(r'''(?im)^\s*["']?(?:password|passwd|api_key|access_token|auth_token|client_secret)["']?\s*[:=]\s*["'][^"'\r\n]{8,}["']''')),
)
NOTICE = '''# AnyPS5 Yotei source review bundle

Local review material, not an installable release. Nothing was uploaded.

native/ contains modified files relative to the pinned revision in manifest.json.
Do not copy this overlay over current main: port one change at a time and test it.
dependency-overrides/ contains a nested libusb source adjustment; review it in
the dependency repository separately and preserve its license.
laboratory/ contains the controller, interface and tests for integration review.
It needs a base checkout, source manifests and lab-update package before use;
it is not a self-contained laboratory installation.

Only explicitly selected UTF-8 source files are included. Games, runtime,
builds, logs, captures, dumps, caches, credentials and local state are excluded
by selection. Pattern checks are heuristic; manually review every file before
publication, including tests and embedded shader data. No original game code,
extracted shaders, assets or identifiers of private accounts may be published.

Keep GPL-2.0-only and attribution. Existing upstream changes are not new local
contributions. Follow current CONTRIBUTING.md, check overlapping PRs, use one
branch per topic, report tests and AI assistance. Menu/gameplay remain unverified.
'''


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def source_path(root, name):
    rel = PurePosixPath(name)
    if (not name or rel.is_absolute() or '..' in rel.parts or
            '\\' in name or ':' in name or str(rel) != name):
        raise RuntimeError(f'Invalid source path: {name}')
    return updater.checked(root, name)


def check_text(name, data):
    if len(data) > 4 * 1024 * 1024 or b'\0' in data:
        raise RuntimeError(f'Binary/oversized input blocked: {name}')
    try:
        content = data.decode('utf-8')
    except UnicodeDecodeError as exc:
        raise RuntimeError(f'Non-UTF-8 input blocked: {name}') from exc
    for label, pattern in SECRET_PATTERNS:
        match = pattern.search(content)
        if match:
            line = content[:match.start()].count('\n') + 1
            raise RuntimeError(f'Review required: {label} in {name}:{line}')


def native_allowed(name):
    rel = PurePosixPath(name)
    return (name == DEPENDENCY or
            (rel.parts[0] in {'core', 'tools', 'docs', 'cmake'} and
             (rel.suffix in TEXT_SUFFIXES or rel.name == 'CMakeLists.txt') and
             not any(part.startswith('.') for part in rel.parts)))


def collect(root):
    root = Path(root).resolve()
    base = updater.active_base(root)
    manifest_path = source_path(root, (base.parent / 'source_manifest.json').relative_to(root).as_posix())
    source = json.loads(manifest_path.read_text(encoding='utf-8'))
    revision = source.get('revision', '')
    if not re.fullmatch(r'[0-9a-f]{40}', revision):
        raise RuntimeError('Missing pinned upstream revision')
    if not source.get('patches'):
        raise RuntimeError('No recorded source modifications')
    files, records, seen = {}, [], set()

    def add(path, destination):
        data = path.read_bytes()
        check_text(destination, data)
        if destination in files:
            raise RuntimeError(f'Duplicate destination: {destination}')
        files[destination] = data
        return sha256(data)

    for entry in source['patches']:
        name = entry['file']
        path = source_path(base, name)
        if name.casefold() in seen or not native_allowed(name):
            raise RuntimeError(f'Unapproved/duplicate native path: {name}')
        seen.add(name.casefold())
        before, after = entry.get('original_sha256'), entry.get('modified_sha256')
        if (not isinstance(after, str) or not re.fullmatch(r'[0-9a-f]{64}', after) or
                (before is not None and not re.fullmatch(r'[0-9a-f]{64}', before))):
            raise RuntimeError(f'Invalid source hashes: {name}')
        prefix = 'dependency-overrides/' if name == DEPENDENCY else 'native/'
        destination = prefix + name
        actual = add(path, destination)
        if actual != after:
            raise RuntimeError(f'Source differs from recorded hash: {name}')
        records.append({'path': destination, 'upstream_path': name,
                        'before_sha256': before, 'sha256': actual})

    for name in LAB_FILES:
        actual = add(source_path(root, name), 'laboratory/' + name)
        records.append({'path': 'laboratory/' + name, 'sha256': actual})
    for name in ('LICENSE', 'LICENCA_E_CREDITOS.md'):
        actual = add(source_path(root, name), name)
        records.append({'path': name, 'sha256': actual})
    actual = add(source_path(base, 'LICENSE'), 'native/LICENSE')
    records.append({'path': 'native/LICENSE', 'sha256': actual})
    files['README.md'] = NOTICE.encode('utf-8')
    records.append({'path': 'README.md', 'sha256': sha256(files['README.md'])})
    report = {'schema': 1, 'purpose': 'local-source-review',
              'fork': 'https://github.com/Izamaru/AnyPS5-Yotei',
              'upstream': updater.UPSTREAM, 'base_revision': revision,
              'native_modifications': len(seen), 'gameplay_verified': False,
              'requires_manual_review': True,
              'files': sorted(records, key=lambda item: item['path'])}
    files['manifest.json'] = (json.dumps(report, indent=2, ensure_ascii=False) + '\n').encode('utf-8')
    return files, report


def export(root, output=None):
    root = Path(root).resolve()
    destination = None
    if output is not None:
        destination = source_path(root, output)
        if not output.startswith('publishing/'):
            raise RuntimeError('Output must be inside publishing/')
        if destination.exists():
            raise RuntimeError('Output already exists; choose a new directory')
    files, report = collect(root)
    if destination is not None:
        destination.mkdir(parents=True, exist_ok=False)
        for name, data in sorted(files.items(), key=lambda item: (item[0] == 'manifest.json', item[0])):
            path = destination / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', help='New directory under publishing/; omit to audit only')
    args = parser.parse_args()
    try:
        report = export(Path(__file__).resolve().parents[1], args.output)
    except (RuntimeError, OSError, ValueError, KeyError, TypeError) as exc:
        print(f'Export blocked: {exc}', file=sys.stderr)
        return 1
    print(json.dumps({'mode': 'export' if args.output else 'audit',
                      'native_modifications': report['native_modifications'],
                      'selected_files': len(report['files']) + 1,
                      'base_revision': report['base_revision'],
                      'requires_manual_review': True}, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
