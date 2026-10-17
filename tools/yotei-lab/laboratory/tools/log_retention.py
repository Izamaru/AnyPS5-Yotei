"""Keep recent execution logs; never traverse investigation/build directories."""
from __future__ import annotations

from datetime import datetime
import os
from pathlib import Path
import stat

try:
    from . import updater
except ImportError:
    import updater

LOG_LIMIT = 50
COMPANIONS = ('.resources.jsonl', '.events.jsonl')


def _linked(path):
    info = path.lstat()
    return path.is_symlink() or bool(getattr(info, 'st_file_attributes', 0) &
                                   getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0))


def _file(directory, path):
    if path.parent != directory or path.resolve().parent != directory.resolve() or _linked(path):
        raise RuntimeError('Link ou caminho fora dos logs: ' + str(path))
    if not stat.S_ISREG(path.lstat().st_mode):
        raise RuntimeError('A retencao aceita apenas arquivos: ' + str(path))
    return path


def catalog(root):
    directory = Path(root) / 'logs'
    if not directory.exists():
        return []
    if _linked(directory) or directory.resolve().parent != Path(root).resolve():
        raise RuntimeError('A pasta logs deve pertencer diretamente ao projeto.')
    rows = []
    for path in directory.iterdir():
        if path.suffix.lower() != '.log' or _linked(path) or not path.is_file():
            continue
        rows.append({'path': path, 'mtime_ns': path.stat().st_mtime_ns})
    return sorted(rows, key=lambda row: (row['mtime_ns'], row['path'].name), reverse=True)


def prune_logs(root, keep=LOG_LIMIT, *, dry_run=False, announce=None):
    """Call under the project's operation_lock, after log streams are closed.

    Only direct *.log files and their two telemetry companions are managed.
    Captures, JSON summaries, scripts, runtimes, source and driver backups stay.
    Windows refuses deletion of open log files; those are retried next time.
    """
    if type(keep) is not int or keep < 1:
        raise ValueError('O limite de logs deve ser um inteiro positivo.')
    root = Path(root).resolve()
    directory = root / 'logs'
    rows = catalog(root)
    plans = []
    skipped = []
    # Resolve and validate every exact target before any deletion. No recursion.
    for row in rows[keep:]:
        log = row['path']
        try:
            targets = [_file(directory, log)]
            for suffix in COMPANIONS:
                companion = log.with_suffix(suffix)
                if os.path.lexists(companion):
                    targets.append(_file(directory, companion))
            plans.append({'log': log.name, 'files': targets,
                          'bytes': sum(path.stat().st_size for path in targets)})
        except (OSError, RuntimeError) as exc:
            skipped.append({'log': log.name, 'reason': str(exc)})
    result = {'keep': keep, 'before': len(rows), 'dry_run': dry_run,
              'planned_logs': len(plans), 'planned_bytes': sum(p['bytes'] for p in plans),
              'removed_logs': [], 'removed_files': 0, 'removed_bytes': 0, 'skipped': skipped}
    if dry_run:
        result['planned_names'] = [p['log'] for p in plans]
        return result
    for plan in plans:
        # Delete the log first: if Windows says it is open, preserve its telemetry.
        log, *companions = plan['files']
        try:
            _file(directory, log)
            size = log.stat().st_size
            log.unlink()
        except (OSError, RuntimeError) as exc:
            result['skipped'].append({'log': log.name, 'reason': str(exc)})
            continue
        result['removed_logs'].append(log.name)
        result['removed_files'] += 1
        result['removed_bytes'] += size
        for path in companions:
            try:
                _file(directory, path)
                size = path.stat().st_size
                path.unlink()
                result['removed_files'] += 1
                result['removed_bytes'] += size
            except (OSError, RuntimeError) as exc:
                result['skipped'].append({'log': log.name, 'file': path.name, 'reason': str(exc)})
    result['after'] = len(catalog(root))
    if result['removed_logs']:
        receipt = root / 'updates/log-retention.json'
        previous = updater.read(receipt, {}).get('events', [])
        updater.atomic_json(receipt, {'schema': 1, 'keep': keep,
            'scope': 'Direct execution logs and resources/events telemetry only; no recursive deletion.',
            'events': [*previous, {'time': datetime.now().astimezone().isoformat(timespec='seconds'),
                                 **result}][-30:]})
        if announce:
            announce(f'Retencao de logs: {len(result["removed_logs"])} antigos removidos; '
                     f'{result["after"]} mantidos (limite {keep}).')
    return result
