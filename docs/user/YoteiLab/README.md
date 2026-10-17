# AnyPS5 Yotei laboratory integration

This development branch preserves our tested AnyPS5 v0.1.2 integration on pinned
upstream revision `a48c00660eae7afd204fdb7513a52380dddf5a04`. It includes the existing
local fixes and the two additions below. It is a source snapshot, not a packaged
release, and must not be overlaid on current upstream main.

- [#6670](https://github.com/boykopovar/AnyPS5/pull/6670), MarcZX100,
  `4864a748022df4fb673202a0c1f430dac339ed10`: initialize the LDS lock only from local
  invocation zero. A regression checks the emitted control flow and fails on
  the preceding implementation.
- [#6492](https://github.com/boykopovar/AnyPS5/pull/6492), Pipodi,
  `349420af7fb2ed7c738d0eefb54d0d4744172026`: discard descriptor-set cache entries
  retaining released textures while preserving ownership of in-flight work.
  The regression verifies that the released resources actually expire.

Shader disk-cache identity is changed for this integration. Existing local
corrections are preserved; the branch does not incorporate the other experimental
PRs. Attribution and GPL-2.0-only are retained in LICENCA_E_CREDITOS.md and LICENSE.

Validation: NVIDIA RTX 4090 24 GB, driver 617.14, Windows 11, fixed 2048 MiB texture
cache. An incremental native build, 11 relevant native regressions, 20 synthetic
SPIR-V validations and 50 updater/retention/export tests completed (two Python
tests skipped because this Windows host cannot create symlinks). Isolated real
startup runs were bounded and monitored; menu and gameplay remain unverified.
No gameplay FPS improvement is claimed. TDR, paging and driver settings were not
changed. The updater retains five backups and verifies the new rollback payload.

PR #5353 was separately tested over the same production source in four serial
real-startup runs, with fresh application caches and with caches reused from those
bounded runs. It is excluded: this base showed no consistent real benefit and
substantially longer initial compilation. The global NVIDIA cache was not cleared,
so those observations are not a fully controlled cold-driver benchmark.

The native changes live in their normal repository paths. Initialize the pinned
submodules before building. The libusb script under tools/yotei-lab/dependency-overrides/ must be
reviewed and applied inside that dependency separately; the submodule gitlink is
preserved. Follow the pinned base build instructions. A full clean build on other
platforms and current upstream compatibility have not been established.

tools/yotei-lab/laboratory/ contains source for the controller, interface and its tests. It needs
a prepared base, source manifests and update package; it is not a self-contained
installation. tools/yotei-lab/source-manifest.json identifies the reviewed public files and their
hashes. Games, extracted shaders, runtime binaries, captures, logs, caches,
credentials and local backup state are excluded. Tests use synthetic programs.

AI assistance was used for local adaptations, tests and validation. This integrated
laboratory branch is not an upstream pull request. Contributions to current main
need separate porting, review and validation under its current contribution rules.
