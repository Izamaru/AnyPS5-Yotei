import argparse
import ctypes as ct
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools import project as p, resource_monitor as m, resource_profiles as profiles


class ResourceTests(unittest.TestCase):
    def test_explicit_cache_reaches_runtime_without_a_smaller_inherited_cap(self):
        env, settings = profiles.environment('economico', parent={'PATH': 'keep', 'APS5_TEXTURE_CACHE_MIB': '512'})
        changed, recorded = profiles.explicit_cache_budget(env, settings, 4096)
        self.assertEqual(changed['APS5_TEXTURE_CACHE_MIB'], '4096')
        self.assertEqual(recorded['APS5_TEXTURE_CACHE_MIB'], '4096')
        self.assertNotIn('APS5_TEXTURE_CACHE_CAP_MIB', changed)
        self.assertNotIn('APS5_TEXTURE_CACHE_CAP_MIB', recorded)
        self.assertEqual(changed['APS5_HOST_POOL_MIB'], '128')
        self.assertEqual(changed['PATH'], 'keep')
        self.assertEqual(env['APS5_TEXTURE_CACHE_CAP_MIB'], '256')
        self.assertEqual(env['APS5_TEXTURE_CACHE_MIB'], '512')
        self.assertEqual(p.cache_budget('4096'), 4096)
        for amount in (0, -1, 4097, 24576, True, '4096'):
            with self.subTest(invalid=amount), self.assertRaises(ValueError):
                profiles.explicit_cache_budget(env, settings, amount)
        with self.assertRaises(argparse.ArgumentTypeError):
            p.cache_budget('4097')

    def test_profiles_preserve_parent_and_override_only_controls(self):
        parent = {"PATH": "keep", "APS5_HOST_POOL_MIB": "900", "CUSTOM": "yes"}
        env, settings = profiles.environment("economico", True, parent)
        self.assertEqual(parent["APS5_HOST_POOL_MIB"], "900")
        self.assertEqual(env["APS5_HOST_POOL_MIB"], "128")
        self.assertEqual(env["PATH"], "keep")
        self.assertNotIn("PATH", settings)
        self.assertEqual(profiles.environment("original", False, parent)[0], parent)

    def test_gpu_unavailable_is_not_reported_as_zero(self):
        rows = m.parse_gpu_csv('GPU-1, RTX 4090, 1200, 24564\nGPU-2, Device, [N/A], [N/A]\n')
        self.assertEqual(rows[0]["device_used_bytes"], 1200 * 1048576)
        self.assertIsNone(rows[1]["device_used_bytes"])
        self.assertEqual(m.parse_gpu_csv("invalid"), [])

    @unittest.skipUnless(ct.sizeof(ct.c_void_p) == 8, "Windows x64 structures")
    def test_windows_abi_sizes_and_offsets(self):
        self.assertEqual(ct.sizeof(m.PerformanceInfo), 104)
        self.assertEqual(m.PerformanceInfo.CommitTotal.offset, 8)
        self.assertEqual(m.PerformanceInfo.PageSize.offset, 80)
        self.assertEqual(ct.sizeof(m.ProcessMemory), 80)
        self.assertEqual(m.ProcessMemory.PrivateUsage.offset, 72)

    def test_windows_sampler_keeps_commit_and_working_set_separate(self):
        class Api:
            def GetPerformanceInfo(self, pointer, _):
                value = ct.cast(pointer, ct.POINTER(m.PerformanceInfo)).contents
                value.PageSize = 4096
                value.CommitTotal, value.CommitLimit = 800, 1000
                value.PhysicalTotal, value.PhysicalAvailable = 500, 200
                return 1
            def GetProcessMemoryInfo(self, handle, pointer, _):
                value = ct.cast(pointer, ct.POINTER(m.ProcessMemory)).contents
                value.WorkingSetSize, value.PrivateUsage = 100, 700
                return 1
        sampler = m.WindowsSampler.__new__(m.WindowsSampler)
        sampler.psapi, sampler.handle = Api(), 1
        values = sampler.sample()
        self.assertEqual(values["system_commit_bytes"], 800 * 4096)
        self.assertEqual(values["system_commit_headroom_bytes"], 200 * 4096)
        self.assertEqual(values["process_working_set_bytes"], 100)
        self.assertEqual(values["process_private_commit_bytes"], 700)

    def test_monitor_streams_peaks_and_handles_sampler_failure(self):
        class Sampler:
            calls = 0
            closed = False
            def sample(self):
                self.calls += 1
                if self.calls == 2:
                    raise OSError("counter failed")
                return {"system_commit_bytes": self.calls * 10,
                        "system_commit_headroom_bytes": 100 - self.calls}
            def close(self):
                self.closed = True
        with tempfile.TemporaryDirectory() as tmp:
            sampler = Sampler()
            path = Path(tmp) / "resources.jsonl"
            monitor = m.ResourceMonitor(1, path, {}, sampler_factory=lambda _: sampler, gpu_path="unused")
            monitor.sample()
            monitor.sample()
            monitor.gpu = {"devices": [{"uuid": "GPU-1", "device_used_bytes": 256}]}
            monitor.close()
            rows = [json.loads(line) for line in path.read_text().splitlines()]
            self.assertIn("sampler_error", rows[2])
            self.assertEqual(rows[-1]["sampled_peaks"]["system_commit_bytes"], 30)
            self.assertEqual(rows[-1]["minimum_sampled_commit_headroom_bytes"], 97)
            self.assertEqual(rows[-1]["device_total_vram_sampled_peaks_bytes"], {"GPU-1": 256})
            self.assertTrue(sampler.closed)

    def test_sync_preserves_game_data_and_rolls_back_locked_dll(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = root / "runtime"
            libs = runtime / "libs"
            libs.mkdir(parents=True)
            sources = root / "built"
            sources.mkdir()
            for name in ("a.prx", "b.prx"):
                (libs / name).write_bytes(b"old")
                (sources / name).write_bytes(b"new")
            (runtime / "app0").mkdir()
            (runtime / "app0/keep.dat").write_bytes(b"game")
            original = {"convert_only": False, "registries": ["keep"]}
            p.write_json(runtime / "port_manifest.json", original)
            replace = p.os.replace
            def locked(source, destination):
                if Path(destination) == libs / "b.prx" and Path(source).parent.name != "backup":
                    raise PermissionError("locked")
                return replace(source, destination)
            with patch.object(p, "RUNTIME", runtime), patch.object(p, "library_inputs", return_value=list(sources.glob("*.prx"))):
                with patch.object(p.os, "replace", side_effect=locked):
                    with self.assertRaisesRegex(RuntimeError, "Sincronizacao"):
                        p.cmd_sync_runtime(argparse.Namespace())
                self.assertEqual((libs / "a.prx").read_bytes(), b"old")
                self.assertEqual((libs / "b.prx").read_bytes(), b"old")
                self.assertEqual(json.loads((runtime / "port_manifest.json").read_text()), original)
                p.cmd_sync_runtime(argparse.Namespace())
                self.assertEqual((libs / "a.prx").read_bytes(), b"new")
                self.assertEqual((runtime / "app0/keep.dat").read_bytes(), b"game")
                result = json.loads((runtime / "port_manifest.json").read_text())
                self.assertEqual(result["registries"], ["keep"])
                self.assertEqual(result["library_sha256"]["a.prx"], p.sha256(libs / "a.prx"))

    def test_changed_patched_source_forces_build_despite_old_timestamp(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source = base / "patch.cpp"
            source.write_text("new code")
            p.os.utime(source, (1, 1))
            build = base / "build"
            build.mkdir()
            p.write_json(build / "pc_lab_source_hashes.json", {"patch.cpp": "old hash"})
            def command(*_, **__):
                self.assertGreater(source.stat().st_mtime, 1)
                return 0
            with patch.object(p, "BASE", base), patch.object(p, "RUNTIME", base / "absent"), \
                 patch.object(p, "source_manifest", return_value={"patches": [{"file": "patch.cpp"}]}), \
                 patch.object(p, "require_base"), patch.object(p, "require_tool"), \
                 patch.object(p, "check_compiler"), patch.object(p, "missing_dependencies", return_value=[]), \
                 patch.object(p.ffmpeg_snapshot, "prepare"), patch.object(p, "log_command", side_effect=command):
                p.cmd_build(argparse.Namespace(relinker_only=False, tests=False, jobs=2))
            self.assertEqual(json.loads((build / "pc_lab_source_hashes.json").read_text())["patch.cpp"], p.sha256(source))


if __name__ == "__main__":
    unittest.main()
