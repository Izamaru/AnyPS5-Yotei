"""Portable Windows build tools, pinned to official release assets. GPL-2.0-only."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import stat
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import zipfile

PACKAGES = {
    'git': {
        'repo': 'git-for-windows/git', 'tag': 'v2.56.0.windows.2',
        'asset': 'MinGit-2.56.0.2-64-bit.zip',
        'sha256': 'da35e72aa21c005a5a0d298cfbae110bc1609a815730ea0dde84b01a1b3cd3be',
        'size': 39806486, 'bin': 'cmd', 'exe': 'git.exe', 'args': ['--version'],
    },
    'gcc': {
        'repo': 'brechtsanders/winlibs_mingw', 'tag': '15.2.0posix-14.0.0-ucrt-r7',
        'asset': 'winlibs-x86_64-posix-seh-gcc-15.2.0-mingw-w64ucrt-14.0.0-r7.zip',
        'sha256': 'cb2fbad6162540cdf5e1facdce08d4dac359e8cf64f7f696a99274291763b815',
        'size': 267593703, 'bin': 'mingw64/bin', 'exe': 'g++.exe', 'args': ['-dumpfullversion'],
    },
    'cmake': {
        'repo': 'Kitware/CMake', 'tag': 'v4.4.4',
        'asset': 'cmake-4.4.4-windows-x86_64.zip',
        'sha256': 'bace36e94b31c68ab6fa295f26dfa11219e0701cf7c94b0284a7d1cb13dac536',
        'size': 54400562, 'bin': 'cmake-4.4.4-windows-x86_64/bin', 'exe': 'cmake.exe', 'args': ['--version'],
    },
    'ninja': {
        'repo': 'ninja-build/ninja', 'tag': 'v1.13.2', 'asset': 'ninja-win.zip',
        'sha256': '07fc8261b42b20e71d1720b39068c2e14ffcee6396b76fb7a795fb460b78dc65',
        'size': 291570, 'bin': '.', 'exe': 'ninja.exe', 'args': ['--version'],
    },
}


def windows_host():
    return os.name == 'nt' and platform.machine().lower() in ('amd64', 'x86_64')


def fingerprint(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def activate(root):
    """Change only this process and its children; never write the system PATH."""
    if not windows_host():
        return
    paths = []
    for key in ('cmake', 'ninja', 'gcc', 'git'):
        package = PACKAGES[key]
        directory = root / '.toolchain' / key
        if (directory / '.install.json').is_file() and (directory / package['bin'] / package['exe']).is_file():
            paths.append(str(directory / package['bin']))
    existing = os.environ.get('PATH', '').split(os.pathsep)
    os.environ['PATH'] = os.pathsep.join(dict.fromkeys(paths + existing))
    # WinLibs CMake can need an explicit trusted CA bundle to download FFmpeg.
    # Respect an existing user configuration. MinGit changed mingw64 to ucrt64.
    if 'SSL_CERT_FILE' not in os.environ:
        git = shutil.which('git')
        if git:
            git_root = Path(git).resolve().parent.parent
            for directory in ('mingw64', 'ucrt64', 'usr'):
                bundle = git_root / directory / 'etc/ssl/certs/ca-bundle.crt'
                if bundle.is_file():
                    os.environ['SSL_CERT_FILE'] = str(bundle)
                    break


def output(command):
    result = subprocess.run(command, capture_output=True, text=True, encoding='utf-8',
                            errors='replace', timeout=20, check=True)
    return result.stdout.strip()


def probe(key):
    """Return (usable, detail); don't trust presence in PATH alone."""
    try:
        if key == 'gcc':
            compilers = [shutil.which(name) for name in ('gcc', 'g++')]
            if not all(compilers):
                return False, 'gcc/g++ nao encontrados'
            if Path(compilers[0]).parent != Path(compilers[1]).parent:
                return False, 'gcc e g++ pertencem a pastas diferentes'
            for compiler in compilers:
                if output([compiler, '-dumpfullversion']) != '15.2.0':
                    return False, 'requer GCC 15.2.0'
                if output([compiler, '-dumpmachine']) != 'x86_64-w64-mingw32':
                    return False, 'requer MinGW-w64 x86_64'
            folder = Path(compilers[1]).parent
            for dll in ('libgcc_s_seh-1.dll', 'libstdc++-6.dll', 'libwinpthread-1.dll'):
                if not (folder / dll).is_file():
                    return False, 'DLL ausente: ' + dll
            return True, str(compilers[1]) + ' (15.2.0)'
        executable = shutil.which(key)
        if not executable:
            return False, key + ' nao encontrado'
        version = output([executable, '--version'])
        if key == 'cmake':
            match = re.search(r'cmake version (\d+)\.(\d+)\.(\d+)', version)
            if not match or tuple(map(int, match.groups())) < (3, 22, 1):
                return False, 'requer CMake >= 3.22.1'
            ctest = Path(executable).parent / 'ctest.exe'
            if not ctest.is_file():
                return False, 'CMake sem ctest.exe'
        return True, str(executable) + ' | ' + version.splitlines()[0]
    except (OSError, subprocess.SubprocessError, IndexError) as exc:
        return False, str(exc)


def download(package, cache, announce):
    cache.mkdir(parents=True, exist_ok=True)
    destination = cache / package['asset']
    if destination.is_file() and fingerprint(destination) == package['sha256']:
        announce('Download ja verificado: ' + destination.name)
        return destination
    if shutil.disk_usage(cache).free < package['size'] + 128 * 1024 * 1024:
        raise RuntimeError('Espaco insuficiente para baixar ' + destination.name)
    url = f"https://github.com/{package['repo']}/releases/download/{package['tag']}/{package['asset']}"
    part = destination.with_suffix(destination.suffix + '.part')
    for attempt in range(1, 4):
        announce(f"Baixando {package['asset']} ({package['size'] / 1048576:.1f} MiB), tentativa {attempt}/3")
        try:
            request = urllib.request.Request(url, headers={'User-Agent': 'Yotei-PC-Lab/0.3.1'})
            with urllib.request.urlopen(request, timeout=30) as response, part.open('wb') as stream:
                if not response.geturl().startswith('https://'):
                    raise RuntimeError('Download redirecionado para protocolo nao seguro.')
                received, last_report = 0, 0.0
                digest = hashlib.sha256()
                while True:
                    data = response.read(1024 * 1024)
                    if not data:
                        break
                    received += len(data)
                    if received > package['size']:
                        raise RuntimeError('Download maior que o asset oficial registrado.')
                    stream.write(data)
                    digest.update(data)
                    if time.monotonic() - last_report >= 3:
                        announce(f"{package['asset']}: {received * 100 // package['size']}%")
                        last_report = time.monotonic()
            if received != package['size'] or digest.hexdigest() != package['sha256']:
                raise RuntimeError('Tamanho/SHA-256 divergente; arquivo nao sera instalado.')
            os.replace(part, destination)
            announce('Download concluido e SHA-256 confirmado.')
            return destination
        except (OSError, urllib.error.URLError) as exc:
            if attempt == 3:
                raise RuntimeError(f'Download falhou: {exc}. Confira internet/proxy e tente novamente.') from exc
        finally:
            part.unlink(missing_ok=True)
    raise RuntimeError('Download nao concluido.')


def unpack(archive, target):
    """Reject traversal, Windows ADS and links before extracting anything."""
    with zipfile.ZipFile(archive) as zipped:
        entries = zipped.infolist()
        if shutil.disk_usage(target).free < sum(item.file_size for item in entries) + 128 * 1024 * 1024:
            raise RuntimeError('Espaco insuficiente para extrair ' + archive.name)
        for info in entries:
            name = info.filename.replace('\\', '/')
            relative = PurePosixPath(name)
            mode = info.external_attr >> 16
            if (relative.is_absolute() or '..' in relative.parts or ':' in name
                    or stat.S_ISLNK(mode) or '\x00' in name):
                raise RuntimeError('Caminho inseguro no ZIP: ' + info.filename)
            if not (target / name).resolve().is_relative_to(target.resolve()):
                raise RuntimeError('Arquivo fora da pasta de instalacao.')
        zipped.extractall(target)


def install_package(key, root, announce):
    package = PACKAGES[key]
    toolchain = root / '.toolchain'
    toolchain.mkdir(parents=True, exist_ok=True)
    archive = download(package, toolchain / 'downloads', announce)
    with tempfile.TemporaryDirectory(prefix='unpack-', dir=toolchain) as directory:
        stage = Path(directory) / 'package'
        stage.mkdir()
        announce('Extraindo ' + key + '...')
        unpack(archive, stage)
        executable = stage / package['bin'] / package['exe']
        if not executable.is_file():
            raise RuntimeError('Layout inesperado do pacote: ' + str(executable))
        # Verify executable before replacing any existing local installation.
        version = output([str(executable), *package['args']])
        if key == 'gcc' and version != '15.2.0':
            raise RuntimeError('Compilador extraido nao corresponde ao GCC 15.2.0.')
        (stage / '.install.json').write_text(json.dumps(package, indent=2), encoding='utf-8')
        destination = toolchain / key
        previous = Path(directory) / 'previous'
        if destination.exists():
            destination.rename(previous)
        try:
            stage.rename(destination)
        except OSError:
            if previous.exists():
                previous.rename(destination)
            raise
    activate(root)
    ok, reason = probe(key)
    if not ok:
        raise RuntimeError('Ferramenta instalada mas verificacao falhou: ' + reason)
    announce(key + ' pronto: ' + reason)


def install_tools(root, announce):
    if not windows_host():
        raise RuntimeError('Instalacao automatica disponivel para Windows x86-64. Em Linux, instale as ferramentas pelo gerenciador do sistema.')
    activate(root)
    # Compiler distribution also provides CMake/Ninja; reuse them when valid.
    for key in ('git', 'gcc', 'cmake', 'ninja'):
        ok, reason = probe(key)
        if ok:
            announce('Ja disponivel: ' + key + ' | ' + reason)
        else:
            announce('Preparando ' + key + ': ' + reason)
            install_package(key, root, announce)
    activate(root)
    results = {key: probe(key) for key in PACKAGES}
    if any(not ok for ok, reason in results.values()):
        raise RuntimeError('Verificacao final falhou: ' + str(results))
    announce('Ferramentas prontas. O PATH do Windows nao foi alterado.')
    return results
