#!/usr/bin/env python3
"""Local AnyPS5 integration. GPL-2.0-only. No game content is included."""
from __future__ import annotations

import argparse
import configparser
import contextlib
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import struct
import subprocess
import sys
import tempfile

try:
    from . import setup_windows, ffmpeg_snapshot, resource_monitor, resource_profiles, updater, log_retention
except ImportError:
    import setup_windows, ffmpeg_snapshot, resource_monitor, resource_profiles, updater, log_retention

VERSION = "0.4.0"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
BASE = PROJECT_ROOT / "base" / "AnyPS5"
GAME = PROJECT_ROOT / "games" / "Ghost-of-Yotei"
SOURCE_EBOOT = GAME / "source" / "app0" / "eboot.bin"
RUNTIME = GAME / "runtime"
UPSTREAM = "https://github.com/boykopovar/AnyPS5.git"
EXPECTED_GCC_WIN = "15.2.0"
MODULE_DIRS = ("sce_module", "sce_modules", "prx")


def announce(message):
    print(f"[YOTEI] {message}", flush=True)


def error(message):
    print(f"[ERRO] {message}", file=sys.stderr, flush=True)
    return 1


def stamp():
    return datetime.now().astimezone().strftime("%Y%m%d_%H%M%S_%f")


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def tool_path(name):
    return shutil.which(name)


def require_tool(name):
    if not tool_path(name):
        raise RuntimeError(f"'{name}' nao encontrado no PATH. Instale antes de continuar.")


def source_manifest():
    path = BASE.parent / "source_manifest.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}


def git_commit():
    if (BASE / ".git").exists():
        return subprocess.check_output(["git", "-C", str(BASE), "rev-parse", "HEAD"], text=True).strip()
    return source_manifest().get("revision", "snapshot-sem-revisao")


def require_base():
    if not (BASE / "CMakeLists.txt").is_file() or not (BASE / "core/relinker/main.cpp").is_file():
        raise RuntimeError("Codigo AnyPS5 ausente/incompleto em base/AnyPS5.")


def submodule_paths():
    config = configparser.ConfigParser(interpolation=None)
    config.read(BASE / ".gitmodules", encoding="utf-8")
    paths = []
    for section in config.sections():
        value = config[section]["path"]
        path = Path(value)
        if path.is_absolute() or ".." in path.parts or not value.startswith("3rdparty/"):
            raise RuntimeError(f"Caminho de dependencia invalido: {value}")
        paths.append(path)
    return paths


def missing_dependencies():
    return [str(p) for p in submodule_paths()
            if not (BASE / p).is_dir() or not any((BASE / p).iterdir())]


def elf_is_64bit(path):
    try:
        with path.open("rb") as stream:
            header = stream.read(64)
    except OSError:
        return False
    return (len(header) == 64 and header[:7] == b"\x7fELF\x02\x01\x01"
            and struct.unpack_from("<H", header, 18)[0] == 62
            and struct.unpack_from("<I", header, 20)[0] == 1
            and struct.unpack_from("<H", header, 52)[0] == 64)


def validate_modules(source):
    parent = source.parent
    if (parent / "sce_module").is_dir() and (parent / "sce_modules").is_dir():
        raise RuntimeError("sce_module e sce_modules coexistem; mantenha apenas uma das duas.")
    directories = [parent / name for name in MODULE_DIRS if (parent / name).is_dir()]
    if not directories:
        raise RuntimeError("Falta sce_module/, sce_modules/ ou prx/ ao lado do ELF, conforme AnyPS5.")
    for directory in directories:
        for module in sorted(directory.iterdir()):
            if module.is_file() and module.suffix.lower() == ".prx" and not elf_is_64bit(module):
                raise RuntimeError(f"Modulo nao e ELF64 little-endian x86-64: {module}")
    return directories


def log_command(command, cwd=None, label="command", check=True, env=None, monitor_settings=None, capture_shaders=False):
    logs = PROJECT_ROOT / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    path = logs / f"{label}_{stamp()}.log"
    if capture_shaders:
        capture_dir = path.with_suffix(".shaders").resolve()
        capture_dir.mkdir()
        env = dict(os.environ if env is None else env)
        env.update(APS5_DUMP_SHADERS="1", APS5_SHADER_DUMP_DIR=str(capture_dir))
        if monitor_settings is not None:
            monitor_settings = {**monitor_settings, "shader_capture_directory": str(capture_dir)}
        announce(f"Capturas de shaders: {capture_dir}")
    announce("Executando: " + subprocess.list2cmdline([str(x) for x in command]))
    announce(f"Log: {path}")
    with path.open("w", encoding="utf-8") as log:
        log.write(subprocess.list2cmdline([str(x) for x in command]) + "\n")
        log.flush()
        with subprocess.Popen(command, cwd=cwd, env=env, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace") as process:
            monitor = None
            try:
                if monitor_settings is not None:
                    resource_path = path.with_suffix(".resources.jsonl")
                    try:
                        monitor = resource_monitor.ResourceMonitor(process.pid, resource_path, monitor_settings)
                        monitor.start()
                        announce(f"Recursos: {resource_path}")
                    except Exception as exc:
                        announce(f"Monitor indisponivel: {exc}; teste continua.")
                for line in process.stdout:
                    log.write(line)
                    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
                    print(line.encode(encoding, "backslashreplace").decode(encoding), end="", flush=True)
                code = process.wait()
            except KeyboardInterrupt:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                raise
            finally:
                if monitor is not None:
                    try:
                        monitor.close()
                    except Exception as exc:
                        announce(f"Falha ao concluir relatorio de recursos: {exc}")
        log.write(f"\nExit code: {code}\n")
    if check and code:
        raise RuntimeError(f"Comando falhou ({code}). Log: {path}")
    return code


def runtime_libraries():
    return RUNTIME / "libs"


def runtime_path(name):
    relative = Path(name)
    path = RUNTIME / relative
    if relative.is_absolute() or not path.resolve().is_relative_to(RUNTIME.resolve()) or path.resolve() == RUNTIME.resolve():
        raise RuntimeError(f"Caminho fora do runtime: {name}")
    return path


def build_dir(only=False):
    return BASE / ("build-relinker" if only else "build")


def locate_relinker():
    name = "relinker.exe" if os.name == "nt" else "relinker"
    candidates = [build_dir(only) / "core/relinker" / name for only in (False, True)]
    return next((path for path in candidates if path.is_file()), candidates[0])


def cmd_status(args):
    require_base()
    report = {"lab_version": VERSION, "platform": platform.platform(), "python": sys.version,
              "source": source_manifest(), "revision": git_commit(),
              "tools": {name: tool_path(name) for name in ("git", "cmake", "ninja", "gcc", "g++", "ctest")},
              "missing_dependencies": missing_dependencies(),
              "elf_header_x86_64": elf_is_64bit(SOURCE_EBOOT),
              "relinker": str(locate_relinker()), "relinker_exists": locate_relinker().is_file(),
              "built_prx_count": len(list((build_dir() / "core/libs/libs").glob("*.prx"))),
              "game_tested": False}
    print(json.dumps(report, indent=2, ensure_ascii=False))
    if args.json:
        write_json(Path(args.json).resolve(), report)
    announce("Diagnostico nao comprova compatibilidade do jogo; dependencias faltantes bloqueiam o build completo.")
    if os.name == "nt":
        for name in setup_windows.PACKAGES:
            ok, detail = setup_windows.probe(name)
            announce(f"{'OK' if ok else 'FALTA/AJUSTAR'} {name}: {detail}")
        announce("Use a opcao 2 do menu (setup) para instalar ferramentas e baixar dependencias.")
    return 0


class Tee:
    def __init__(self, terminal, log):
        self.terminal, self.log = terminal, log

    def write(self, text):
        self.terminal.write(text)
        self.log.write(text)
        self.log.flush()
        return len(text)

    def flush(self):
        self.terminal.flush()
        self.log.flush()


def cmd_setup(args):
    require_base()
    logs = PROJECT_ROOT / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    path = logs / f"setup_{stamp()}.log"
    announce(f"Preparacao automatica; log: {path}")
    with path.open("w", encoding="utf-8") as log:
        with contextlib.redirect_stdout(Tee(sys.stdout, log)):
            try:
                setup_windows.install_tools(PROJECT_ROOT, announce)
                if not args.tools_only:
                    cmd_deps(args)
                announce("Ferramentas prontas." if args.tools_only else "Preparacao concluida. Use a opcao 3 ou 4 para compilar.")
            except Exception as exc:
                announce(f"Preparacao interrompida: {exc}. Execute a opcao 2 novamente apos corrigir a causa.")
                raise
    return 0


def validate_gitmodules(checkout):
    # Git for Windows may check out text with CRLF, while the GitHub ZIP uses LF.
    # Ignore only that representation difference; keep every other byte checked.
    received = (checkout / ".gitmodules").read_bytes().replace(b"\r\n", b"\n")
    bundled = (BASE / ".gitmodules").read_bytes().replace(b"\r\n", b"\n")
    if received != bundled:
        raise RuntimeError(".gitmodules difere do commit fixado mesmo apos normalizar LF/CRLF; "
                           "dependencias nao copiadas. Confira se a base corresponde ao ZIP desta versao.")


def cmd_deps(_):
    """Fetch gitlinks pinned by the ZIP revision; preserve bundled source."""
    require_base()
    require_tool("git")
    missing = missing_dependencies()
    if not missing:
        announce("Todas as pastas de dependencias estao preenchidas; nenhuma foi alterada.")
        return 0
    revision = git_commit()
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise RuntimeError("Sem commit completo da origem: nao e seguro escolher versoes de dependencias.")
    with tempfile.TemporaryDirectory(prefix="anyps5-deps-", dir=BASE.parent) as temporary:
        checkout = Path(temporary)
        log_command(["git", "init", str(checkout)], label="deps")
        log_command(["git", "config", "--local", "core.autocrlf", "false"], cwd=checkout, label="deps")
        log_command(["git", "remote", "add", "origin", UPSTREAM], cwd=checkout, label="deps")
        log_command(["git", "fetch", "--depth", "1", "origin", revision], cwd=checkout, label="deps")
        log_command(["git", "checkout", "--detach", "FETCH_HEAD"], cwd=checkout, label="deps")
        actual = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=checkout, text=True).strip()
        if actual != revision:
            raise RuntimeError("Commit baixado difere do snapshot.")
        validate_gitmodules(checkout)
        log_command(["git", "submodule", "update", "--init", "--recursive"], cwd=checkout, label="deps")
        status = subprocess.check_output(["git", "submodule", "status", "--recursive"], cwd=checkout, text=True)
        if any(line and line[0] != " " for line in status.splitlines()):
            raise RuntimeError("Submodulo nao corresponde ao commit fixado.")
        for relative in missing:
            source, destination = checkout / relative, BASE / relative
            # Do not copy .git files pointing to the temporary checkout.
            staged = checkout / ("copy-" + destination.name)
            shutil.copytree(source, staged, ignore=shutil.ignore_patterns(".git"))
            if destination.exists():
                destination.rmdir()  # Refuse if another process populated it meanwhile.
            shutil.move(str(staged), str(destination))
        write_json(BASE.parent / "dependency_manifest.json", {"revision": revision, "submodules": status.splitlines()})
    announce("Dependencias da revisao fixada preparadas. Codigo local preservado.")
    return 0


def cmd_update(args):
    return updater.update(sys.modules[__name__], getattr(args, "jobs", 4))


def cmd_apply_lab_update(args):
    return updater.apply_lab_update(sys.modules[__name__], args.jobs)


def cmd_check_updates(_):
    updater.check(sys.modules[__name__])
    return 0


def cmd_rollback(_):
    return updater.rollback(sys.modules[__name__])


def cmd_backups(_):
    return updater.backups(sys.modules[__name__])


def cmd_backup(_):
    updater.require_game_closed()
    updater.make_backup(sys.modules[__name__], 'manual-interface')
    return 0


def check_compiler():
    require_tool("g++")
    require_tool("gcc")
    if os.name == "nt":
        version = subprocess.check_output(["g++", "-dumpfullversion"], text=True).strip()
        target = subprocess.check_output(["g++", "-dumpmachine"], text=True).strip()
        if version != EXPECTED_GCC_WIN or "x86_64" not in target or "mingw" not in target:
            raise RuntimeError(f"Use WinLibs MinGW-w64 x86_64 GCC {EXPECTED_GCC_WIN}; encontrado {version} / {target}.")


def cmd_build(args):
    require_base()
    for tool in ("cmake", "ninja"):
        require_tool(tool)
    check_compiler()
    if not args.relinker_only and missing_dependencies():
        raise RuntimeError("Dependencias ausentes: " + ", ".join(missing_dependencies()) + ". Execute deps ou build --relinker-only.")
    if not args.relinker_only:
        ffmpeg_snapshot.prepare(BASE, announce)
    build = build_dir(args.relinker_only)
    source_hashes = {}
    if not args.relinker_only:
        # ZIP timestamps can precede the object files. Touch changed patched inputs
        # before Ninja, recording hashes only after a successful build.
        record = build / "pc_lab_source_hashes.json"
        previous = json.loads(record.read_text(encoding="utf-8")) if record.is_file() else {}
        for entry in source_manifest().get("patches", []):
            relative = entry["file"]
            path = (BASE / relative).resolve()
            if not path.is_relative_to(BASE.resolve()):
                raise RuntimeError("Manifesto de fontes contem caminho fora da base.")
            if path.suffix not in (".cpp", ".hpp", ".h"):
                continue
            source_hashes[relative] = sha256(path)
            if previous.get(relative) != source_hashes[relative]:
                os.utime(path, None)
    command = ["cmake", "-S", str(BASE), "-B", str(build), "-G", "Ninja", "-DCMAKE_BUILD_TYPE=Release",
               "-DCMAKE_CXX_COMPILER=g++", f"-DANYPS5_RELINKER_ONLY={'ON' if args.relinker_only else 'OFF'}",
               f"-DBUILD_TESTING={'ON' if args.tests else 'OFF'}", f"-DPython3_EXECUTABLE={sys.executable}"]
    if not args.relinker_only:
        command.append("-DCMAKE_C_COMPILER=gcc")
    if os.name == "nt":
        # windres otherwise constructs an unquoted absolute GCC command when
        # the bundled toolchain lives in a project path containing spaces.
        command.append("-DCMAKE_RC_FLAGS=--preprocessor=gcc --preprocessor-arg=-E --preprocessor-arg=-xc --preprocessor-arg=-DRC_INVOKED")
    log_command(command, label="configure")
    log_command(["cmake", "--build", str(build), "--parallel", str(args.jobs)], label="build")
    if not args.relinker_only:
        log_command(["cmake", "--build", str(build), "--target", "libs", "--parallel", str(args.jobs)], label="libs")
        # Compilation is complete even if a later CTest fails. Retrying tests
        # must not touch every patched header and rebuild all dependants again.
        write_json(build / "pc_lab_source_hashes.json", source_hashes)
    if args.tests:
        require_tool("ctest")
        log_command(["ctest", "--test-dir", str(build), "--output-on-failure", "--parallel", str(args.jobs)], label="tests")
    if not args.relinker_only:
        if os.name == "nt" and (RUNTIME / "port_manifest.json").is_file():
            manifest = json.loads((RUNTIME / "port_manifest.json").read_text(encoding="utf-8"))
            if not manifest.get("convert_only"):
                cmd_sync_runtime(argparse.Namespace())
    announce("Build concluido. Relinker isolado nao inclui as bibliotecas necessarias para executar jogos." if args.relinker_only else "Build completo concluido.")
    return 0


def library_inputs():
    if os.name != "nt":
        raise RuntimeError("Runtime Windows requer bibliotecas compiladas no Windows; use --convert-only para analisar em Linux.")
    source = build_dir() / "core/libs/libs"
    files = sorted(source.glob("*.prx"))
    if not files:
        raise RuntimeError("Nenhuma PRX encontrada; execute build completo ou use --convert-only.")
    for path in files:
        with path.open("rb") as stream:
            if stream.read(2) != b"MZ":
                raise RuntimeError(f"Biblioteca nao e PE/Windows: {path}")
    compiler = tool_path("g++")
    if not compiler:
        raise RuntimeError("MinGW g++ ausente; necessario para localizar as DLLs da toolchain.")
    for name in ("libgcc_s_seh-1.dll", "libstdc++-6.dll", "libwinpthread-1.dll"):
        path = Path(compiler).parent / name
        if not path.is_file():
            raise RuntimeError(f"DLL ausente na toolchain: {path}")
        files.append(path)
    return files


def cmd_sync_runtime(_):
    """Stage, replace and roll back host libraries; never touch app0 or the EXE."""
    manifest_path = RUNTIME / "port_manifest.json"
    if not manifest_path.is_file():
        raise RuntimeError("Runtime ainda nao convertido; execute a conversao primeiro.")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("convert_only"):
        raise RuntimeError("Runtime de analise; converta com bibliotecas antes de sincronizar.")
    files = library_inputs()
    destination = runtime_libraries()
    destination.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="sync-libs-", dir=RUNTIME) as directory:
        stage = Path(directory)
        backups = stage / "backup"
        backups.mkdir()
        changed = []
        hashes = {}
        for source in files:
            hashes[source.name] = sha256(source)
            current = destination / source.name
            if current.is_file() and sha256(current) == hashes[source.name]:
                continue
            shutil.copy2(source, stage / source.name)
            if sha256(stage / source.name) != hashes[source.name]:
                raise RuntimeError(f"Copia de biblioteca divergente: {source.name}")
            changed.append(source.name)
        manifest["library_files"] = sorted(set(manifest.get("library_files", [])) | {"libs/" + name for name in hashes})
        manifest["library_sha256"] = hashes
        manifest["library_lab_version"] = VERSION
        write_json(stage / "port_manifest.json", manifest)
        promoted = []
        saved = []
        try:
            for name in changed:
                current = destination / name
                if current.exists():
                    os.replace(current, backups / name)
                    saved.append(name)
                os.replace(stage / name, current)
                promoted.append(name)
            os.replace(stage / "port_manifest.json", manifest_path)
        except OSError as exc:
            # Existing binaries are restored even when a later DLL is locked.
            try:
                for name in reversed(promoted):
                    (destination / name).unlink()
                for name in reversed(saved):
                    os.replace(backups / name, destination / name)
            except OSError as restore_error:
                recovery = RUNTIME / f"libs-recovery-{stamp()}"
                shutil.move(str(backups), recovery)
                raise RuntimeError(f"Restauracao incompleta; nao execute o jogo. Bibliotecas anteriores preservadas em {recovery}") from restore_error
            raise RuntimeError("Sincronizacao falhou; feche o jogo antes de compilar/sincronizar.") from exc
    announce(f"Runtime sincronizado: {len(changed)} bibliotecas atualizadas e hashes conferidos.")
    return 0


def cmd_relink(args):
    require_base()
    source = Path(args.input).expanduser().resolve() if args.input else SOURCE_EBOOT.resolve()
    if not elf_is_64bit(source):
        raise RuntimeError(f"Entrada ausente ou sem cabecalho ELF64 little-endian x86-64 valido: {source}. SELF nao e aceito.")
    if source.is_relative_to(RUNTIME.resolve()):
        raise RuntimeError("O original deve ficar fora de runtime/.")
    validate_modules(source)
    relinker = locate_relinker()
    if not relinker.is_file():
        raise RuntimeError("Relinker ausente; execute build ou build --relinker-only.")
    libraries = [] if args.convert_only else library_inputs()
    RUNTIME.mkdir(parents=True, exist_ok=True)
    # Conversion errors do not replace the last successful runtime.
    with tempfile.TemporaryDirectory(prefix="yotei-convert-", dir=GAME) as directory:
        stage = Path(directory)
        output = stage / "GhostOfYotei.exe"
        command = [str(relinker), "--windows", "--windows-diagnostics", "--registry"]
        if args.cpu == "intel":
            command.append("--to-intel")
        command.extend([str(source), str(output)])
        log_command(command, cwd=stage, label="relink")
        if not output.is_file():
            raise RuntimeError("Relinker encerrou sem produzir o executavel.")
        generated = [path for path in stage.rglob("*") if path.is_file()]
        manifest = {"lab_version": VERSION, "generated_at": datetime.now().astimezone().isoformat(),
                    "revision": git_commit(), "source_sha256": sha256(source), "source_file": str(source),
                    "cpu": args.cpu, "convert_only": args.convert_only, "game_tested": False,
                    "registries": sorted(str(p.relative_to(stage)) for p in generated if p.name.endswith(".registry.json")),
                    "generated_files": sorted(str(p.relative_to(stage)) for p in generated),
                    "library_files": ["libs/" + path.name for path in libraries]}
        previous_path = RUNTIME / "port_manifest.json"
        previous = json.loads(previous_path.read_text(encoding="utf-8")) if previous_path.is_file() else {}
        new_files = set(manifest["generated_files"] + manifest["library_files"])
        old_files = set(previous.get("generated_files", []) + previous.get("library_files", []))
        # Validate all destinations before promoting any file or deleting stale outputs.
        for name in new_files | old_files:
            runtime_path(name)
        for path in generated:
            destination = runtime_path(str(path.relative_to(stage)))
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(path, destination)
        if libraries:
            runtime_libraries().mkdir(parents=True, exist_ok=True)
            for path in libraries:
                shutil.copy2(path, runtime_libraries() / path.name)
        for name in old_files - new_files:
            path = runtime_path(name)
            if path.is_file():
                path.unlink()
        write_json(RUNTIME / "port_manifest.json", manifest)
    announce("Conversao concluida; compatibilidade do jogo ainda nao foi comprovada. Execute audit antes de run.")
    return 0


def cmd_audit(_):
    require_base()
    path = RUNTIME / "port_manifest.json"
    if not path.is_file():
        raise RuntimeError("Manifesto ausente; execute relink primeiro.")
    manifest = json.loads(path.read_text(encoding="utf-8"))
    registries = [runtime_path(name) for name in manifest["registries"]]
    if not registries or any(not p.is_file() for p in registries):
        raise RuntimeError("Registros de conversao incompletos. Execute relink novamente.")
    command = [sys.executable, str(BASE / "tools/import_audit.py"), *map(str, registries),
               "--libs", str(runtime_libraries()), "--source", str(BASE / "core/libs/prx"),
               "--json", str(PROJECT_ROOT / "logs" / f"audit_{stamp()}.json")]
    for name in MODULE_DIRS:
        path = RUNTIME / "app0" / name
        if path.is_dir():
            command.extend(["--modules", str(path)])
    code = log_command(command, label="audit", check=False)
    announce("Audit 0 significa ausencia de bloqueios de carregamento detectados; stubs e falhas durante o jogo ainda podem existir.")
    return code


def cmd_run(args):
    if os.name != "nt":
        raise RuntimeError("Execute o runtime em Windows x86-64.")
    manifest_path = RUNTIME / "port_manifest.json"
    if not manifest_path.is_file():
        raise RuntimeError("Conversao ausente; execute relink.")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("convert_only"):
        raise RuntimeError("Conversao de analise sem runtime. Execute build completo e relink sem --convert-only.")
    if not (RUNTIME / "GhostOfYotei.exe").is_file():
        raise RuntimeError("Executavel ausente; execute relink.")
    audit_code = cmd_audit(argparse.Namespace())
    if audit_code:
        raise RuntimeError("Auditoria bloqueou a execucao; consulte logs/audit.")
    profile = getattr(args, "profile", "original")
    monitor = not getattr(args, "no_monitor", False)
    env, settings = resource_profiles.environment(profile, diagnostics=monitor)
    cache_mib = getattr(args, 'cache_mib', None)
    if cache_mib is not None:
        env, settings = resource_profiles.explicit_cache_budget(env, settings, cache_mib)
    announce(f"Perfil de recursos: {profile}; limites sao de caches, nao da memoria total do jogo.")
    return log_command([str(RUNTIME / "GhostOfYotei.exe")], cwd=RUNTIME, label="game", check=False,
                       env=env, monitor_settings={"profile": profile, "environment": settings} if monitor else None,
                       capture_shaders=getattr(args, "dump_shaders", False))


def positive(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("O valor deve ser maior que zero.")
    return number


def cache_budget(value):
    number = positive(value)
    if number > resource_profiles.MAX_CACHE_MIB:
        raise argparse.ArgumentTypeError(f'O cache deve estar entre 1 e {resource_profiles.MAX_CACHE_MIB} MiB.')
    return number


def make_parser():
    parser = argparse.ArgumentParser(description=f"Ghost of Yotei PC Lab {VERSION}; base AnyPS5 local.")
    commands = parser.add_subparsers(dest="command", required=True)
    status = commands.add_parser("status", help="Diagnostico local")
    status.add_argument("--json", help="Salvar relatorio em JSON")
    status.set_defaults(func=cmd_status)
    commands.add_parser("deps", aliases=["bootstrap"], help="Baixar dependencias fixadas; requer internet").set_defaults(func=cmd_deps)
    setup = commands.add_parser("setup", help="Windows: instalar ferramentas locais e baixar dependencias")
    setup.add_argument("--tools-only", action="store_true", help="Preparar somente Git, GCC, CMake e Ninja")
    setup.set_defaults(func=cmd_setup)
    commands.add_parser("check-updates", help="Consultar nova revisao oficial sem alterar a base").set_defaults(func=cmd_check_updates)
    update = commands.add_parser("update", help="Backup, integrar e testar nova revisao antes de ativar")
    update.add_argument("--jobs", type=positive, default=min(os.cpu_count() or 2, 4))
    update.set_defaults(func=cmd_update)
    local_update = commands.add_parser("apply-lab-update", help="Aplicar a correcao incluida neste pacote com backup e retorno")
    local_update.add_argument("--jobs", type=positive, default=min(os.cpu_count() or 2, 4))
    local_update.set_defaults(func=cmd_apply_lab_update)
    commands.add_parser("rollback", help="Restaurar a versao anterior pelo backup verificado").set_defaults(func=cmd_rollback)
    commands.add_parser("backups", help="Listar backups do atualizador").set_defaults(func=cmd_backups)
    commands.add_parser('backup', help='Criar backup completo; manter as cinco copias mais recentes').set_defaults(func=cmd_backup)
    build = commands.add_parser("build", help="Compilar codigo local")
    build.add_argument("--relinker-only", action="store_true")
    build.add_argument("--tests", action="store_true")
    build.add_argument("--jobs", type=positive, default=min(os.cpu_count() or 2, 4))
    build.set_defaults(func=cmd_build)
    relink = commands.add_parser("relink", help="Converter ELF autorizado para Windows")
    relink.add_argument("--input")
    relink.add_argument("--cpu", choices=("amd", "intel"), required=True)
    relink.add_argument("--convert-only", action="store_true", help="Somente analise; nao preparar bibliotecas")
    relink.set_defaults(func=cmd_relink)
    commands.add_parser("audit", help="Auditar imports e bibliotecas reais").set_defaults(func=cmd_audit)
    run = commands.add_parser("run", help="Auditar e testar com log e monitor de recursos")
    run.add_argument("--profile", choices=tuple(resource_profiles.PROFILES), default="original")
    run.add_argument('--cache-mib', type=cache_budget, help='Orcamento explicito de cache de imagens, ate 4096 MiB (4 GiB)')
    run.add_argument("--no-monitor", action="store_true", help="Desativar telemetria e profiling automaticos")
    run.add_argument("--dump-shaders", action="store_true", help="Capturar shaders e memoria para reproduzir falhas")
    run.set_defaults(func=cmd_run)
    commands.add_parser("sync-runtime", help="Sincronizar bibliotecas compiladas sem reconverter o jogo").set_defaults(func=cmd_sync_runtime)
    return parser


def main():
    setup_windows.activate(PROJECT_ROOT)
    args = make_parser().parse_args()
    try:
        global BASE
        with updater.operation_lock(PROJECT_ROOT):
            recovered = updater.recover(PROJECT_ROOT, announce)
            BASE = updater.active_base(PROJECT_ROOT)
            try:
                if recovered and args.command == "rollback":
                    return 0
                return args.func(args)
            finally:
                try:
                    updater.prune_backups(sys.modules[__name__])
                except (OSError, ValueError, RuntimeError) as exc:
                    announce(f'Limpeza automatica de backups adiada: {exc}')
                try:
                    log_retention.prune_logs(PROJECT_ROOT, announce=announce)
                except (OSError, ValueError, RuntimeError) as exc:
                    announce(f'Limpeza automatica de logs adiada: {exc}')
    except KeyboardInterrupt:
        return error("Operacao interrompida.")
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError) as exc:
        return error(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
