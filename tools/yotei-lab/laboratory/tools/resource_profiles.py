"""Opt-in cache retention settings. They do not cap total guest RAM or VRAM."""
import os

CACHE_STAGES_MIB = tuple(gib * 1024 for gib in (1, 2, 3, 4))
MAX_CACHE_MIB = CACHE_STAGES_MIB[-1]

PROFILES = {
    "original": {},
    "economico": {
        "APS5_SMALL_POOL_MIB": "16",
        "APS5_HOST_POOL_MIB": "128",
        "APS5_STAGING_POOL_MIB": "128",
        "APS5_LOCAL_TEXTURE_CACHE_MIB": "64",
        "APS5_DEFER_SHADER_PREPARE": "1",
        "APS5_EMULATE_UNNORMALIZED_WRAP": "1",
        "APS5_TEXTURE_CACHE_CAP_MIB": "256",
    },
    # Retain images, use GPU DCC clears and keep Windows resident imports watched.
    # Validated in logs/watch-investigation.json. Ceiling covers both image caches.
    "desempenho": {
        "APS5_SMALL_POOL_MIB": "16",
        "APS5_HOST_POOL_MIB": "128",
        "APS5_STAGING_POOL_MIB": "128",
        "APS5_LOCAL_TEXTURE_CACHE_MIB": "64",
        "APS5_DEFER_SHADER_PREPARE": "1",
        "APS5_EMULATE_UNNORMALIZED_WRAP": "1",
        "APS5_TEXTURE_CACHE_CAP_MIB": "2048",
        "APS5_KEYS_FILL_CLEAR": "1",
        "APS5_WINDOWS_ALIAS_IMPORTS": "1",
    },
}


def environment(profile, diagnostics=False, parent=None):
    result = dict(os.environ if parent is None else parent)
    # Explicit profile overrides inherited settings only for its listed controls.
    result.update(PROFILES[profile])
    if diagnostics:
        result["APS5_RESOURCE_TRACE"] = "1"
        result["APS5_PROFILE_DRAW"] = "1"
    keys = set().union(*(settings.keys() for settings in PROFILES.values()))
    keys.update(("APS5_RESOURCE_TRACE", "APS5_PROFILE_DRAW", "APS5_TEXTURE_CACHE_MIB"))
    return result, {key: result[key] for key in sorted(keys) if key in result}


def explicit_cache_budget(env, settings, mib):
    """Use the runtime's explicit budget without a smaller inherited cap.

    Neither this function nor the runtime preallocates the selected budget.
    """
    if type(mib) is not int or not 1 <= mib <= MAX_CACHE_MIB:
        raise ValueError(f'O cache deve estar entre 1 e {MAX_CACHE_MIB} MiB.')
    env, settings = dict(env), dict(settings)
    for target in (env, settings):
        target.pop('APS5_TEXTURE_CACHE_CAP_MIB', None)
        target['APS5_TEXTURE_CACHE_MIB'] = str(mib)
    return env, settings
