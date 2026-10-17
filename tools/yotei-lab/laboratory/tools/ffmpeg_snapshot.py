"""Adapt the pinned FFmpeg recipe for a source snapshot without Git metadata."""
import hashlib
import json
import re
import subprocess
from pathlib import Path

REVISION = '9ac4cfd195f192ed8b08566f49c28dbb48d08341'
ORIGINAL_SHA256 = '8fe1d93545485fd951adb7c4de707c9fc90b3d692506720bd9f48d67ff837e04'
GIT_BLOCK = '''execute_process(
\tCOMMAND git rev-parse --short=12 HEAD
\tWORKING_DIRECTORY ${CMAKE_CURRENT_SOURCE_DIR}
\tOUTPUT_VARIABLE FFMPEG_GIT_SHA
\tOUTPUT_STRIP_TRAILING_WHITESPACE)'''
SNAPSHOT_BLOCK = ('# PC Lab: exact gitlink from the bundled AnyPS5 revision.\n'
                  'set(FFMPEG_GIT_SHA "' + REVISION[:12] + '")')


def pinned_revision(base):
    manifest = Path(base).parent / 'dependency_manifest.json'
    if manifest.is_file():
        rows = json.loads(manifest.read_text(encoding='utf-8')).get('submodules', [])
        for row in rows:
            match = re.match(r'^ ([0-9a-f]{40}) 3rdparty/ffmpeg-core(?: |$)', row)
            if match:
                return match[1]
        raise RuntimeError('Manifesto de dependencias sem revisao valida do FFmpeg.')
    return REVISION


def prepare(base, announce):
    base = Path(base)
    path = base / '3rdparty/ffmpeg-core/CMakeLists.txt'
    if not path.is_file():
        raise RuntimeError('FFmpeg ausente; execute a opcao 2 para preparar dependencias.')
    data = path.read_bytes()
    text = data.decode('utf-8').replace('\r\n', '\n')
    if (base / '.git').exists() and (path.parent / '.git').exists():
        entry = subprocess.check_output(['git', '-C', str(base), 'ls-tree', 'HEAD', '--', '3rdparty/ffmpeg-core'], text=True).split()
        actual = subprocess.check_output(['git', '-C', str(path.parent), 'rev-parse', 'HEAD'], text=True).strip()
        if len(entry) < 4 or entry[0] != '160000' or entry[2] != actual:
            raise RuntimeError('FFmpeg diverge do gitlink da versao ativa.')
        if '# PC Lab: exact gitlink' not in text:
            announce('FFmpeg: submodulo Git verificado; receita upstream preservada.')
            return
    revision = pinned_revision(base)
    snapshot_block = ('# PC Lab: exact gitlink from the bundled AnyPS5 revision.\n'
                      'set(FFMPEG_GIT_SHA "' + revision[:12] + '")')
    if text.count(snapshot_block) == 1:
        restored = text.replace(snapshot_block, GIT_BLOCK, 1)
        if hashlib.sha256(restored.encode()).hexdigest() == ORIGINAL_SHA256:
            return
    if hashlib.sha256(text.encode()).hexdigest() != ORIGINAL_SHA256 or text.count(GIT_BLOCK) != 1:
        raise RuntimeError('Receita FFmpeg diferente da revisao fixada; nenhum arquivo alterado. Envie 3rdparty/ffmpeg-core/CMakeLists.txt para verificacao.')
    backup = path.with_name('CMakeLists.txt.pc-lab-original')
    if backup.exists() and backup.read_bytes().replace(b'\r\n', b'\n') != text.encode():
        raise RuntimeError('Backup FFmpeg diverge; nenhum arquivo alterado.')
    if not backup.exists():
        backup.write_bytes(data)
    temporary = path.with_name('CMakeLists.txt.pc-lab-tmp')
    temporary.write_bytes(text.replace(GIT_BLOCK, snapshot_block, 1).encode())
    temporary.replace(path)
    announce('FFmpeg: revisao fixada ' + revision[:12] + '; download independe de metadados Git.')
