"""Tool-neutral process-priority / timeout settings (issue #132).

These knobs (nice level, I/O priority, info/verify timeouts) govern every
conversion tool's subprocess, not just chdman. They are exposed under
tool-neutral ``COMPRESSATORIUM_TOOL_*`` names while the chdman-era
``CHD_*``/``CHDMAN_*`` names remain backwards-compatible aliases, mirroring the
``chd_volumes`` -> ``COMPRESSATORIUM_VOLUMES`` pattern. Optional per-tool
overrides (``COMPRESSATORIUM_<TOOL>_*``) fall back to the shared default.
"""
from __future__ import annotations

import pytest

from app.config import Settings


def test_neutral_defaults_match_chdman_era_values():
    s = Settings()
    assert s.tool_nice == 10
    assert s.tool_ioprio_class == 2
    assert s.tool_ioprio_level == 6
    assert s.tool_info_timeout == 60
    # Verify is bounded out of the box (issue #266): a generous baseline plus a
    # per-GiB allowance, capped. It used to default to 0 -- no bound at all --
    # so a verify that never returned froze the whole queue behind it.
    assert s.tool_verify_timeout == 1800
    assert s.tool_verify_timeout_per_gib == 600
    assert s.tool_verify_timeout_cap == 86400
    assert s.tool_verify_progress_timeout == 600
    # Per-tool overrides are unset by default -> fall back to the shared values.
    assert s.chdman_nice is None
    assert s.dolphin_tool_nice is None
    assert s.nsz_nice is None
    assert s.z3ds_nice is None
    assert s.maxcso_nice is None


def test_legacy_chd_env_names_still_populate_shared_settings():
    """The old CHD_*/CHDMAN_* env names keep working unchanged."""
    s = Settings(
        CHD_CHDMAN_NICE=5,
        CHD_CHDMAN_IOPRIO_CLASS=3,
        CHD_CHDMAN_IOPRIO_LEVEL=7,
        CHD_INFO_TIMEOUT=99,
        CHD_VERIFY_TIMEOUT=42,
    )
    assert s.tool_nice == 5
    assert s.tool_ioprio_class == 3
    assert s.tool_ioprio_level == 7
    assert s.tool_info_timeout == 99
    assert s.tool_verify_timeout == 42


def test_neutral_env_names_populate_shared_settings():
    s = Settings(
        COMPRESSATORIUM_TOOL_NICE=4,
        COMPRESSATORIUM_TOOL_IOPRIO_CLASS=1,
        COMPRESSATORIUM_TOOL_IOPRIO_LEVEL=0,
        COMPRESSATORIUM_TOOL_INFO_TIMEOUT=30,
        COMPRESSATORIUM_TOOL_VERIFY_TIMEOUT=15,
    )
    assert s.tool_nice == 4
    assert s.tool_ioprio_class == 1
    assert s.tool_ioprio_level == 0
    assert s.tool_info_timeout == 30
    assert s.tool_verify_timeout == 15


def test_neutral_name_takes_precedence_over_legacy_alias():
    """When both the new and old names are present, the new name wins."""
    s = Settings(COMPRESSATORIUM_TOOL_NICE=1, CHD_CHDMAN_NICE=9)
    assert s.tool_nice == 1

    s = Settings(COMPRESSATORIUM_TOOL_INFO_TIMEOUT=11, CHD_INFO_TIMEOUT=88)
    assert s.tool_info_timeout == 11

    s = Settings(COMPRESSATORIUM_TOOL_VERIFY_TIMEOUT=22, CHD_VERIFY_TIMEOUT=77)
    assert s.tool_verify_timeout == 22


@pytest.mark.parametrize(
    ("env", "field"),
    [
        ("CHD_CHDMAN_NICE", "tool_nice"),
        ("CHD_CHDMAN_IOPRIO_CLASS", "tool_ioprio_class"),
        ("CHD_CHDMAN_IOPRIO_LEVEL", "tool_ioprio_level"),
        ("CHD_INFO_TIMEOUT", "tool_info_timeout"),
        ("CHD_VERIFY_TIMEOUT", "tool_verify_timeout"),
    ],
)
def test_each_legacy_alias_maps_to_its_neutral_field(env, field):
    s = Settings(**{env: 3})
    assert getattr(s, field) == 3


def test_per_tool_override_takes_precedence_when_set():
    s = Settings(
        COMPRESSATORIUM_TOOL_NICE=10,
        COMPRESSATORIUM_DOLPHIN_TOOL_NICE=15,
    )
    assert s.tool_nice == 10
    assert s.dolphin_tool_nice == 15


def test_nsz_z3ds_maxcso_expose_verify_timeout_override_only():
    """nsz/z3ds/maxcso run a verify subprocess but no info subprocess.

    They take a per-tool ``*_verify_timeout`` override but, unlike chdman and
    dolphin, have no ``*_info_timeout`` field (their ``info()`` is a filesystem
    read).
    """
    s = Settings()
    assert s.nsz_verify_timeout is None
    assert s.z3ds_verify_timeout is None
    assert s.maxcso_verify_timeout is None
    assert not hasattr(s, "nsz_info_timeout")
    assert not hasattr(s, "z3ds_info_timeout")
    assert not hasattr(s, "maxcso_info_timeout")

    s = Settings(
        COMPRESSATORIUM_NSZ_VERIFY_TIMEOUT=30,
        COMPRESSATORIUM_Z3DS_VERIFY_TIMEOUT=45,
        COMPRESSATORIUM_MAXCSO_VERIFY_TIMEOUT=50,
    )
    assert s.nsz_verify_timeout == 30
    assert s.z3ds_verify_timeout == 45
    assert s.maxcso_verify_timeout == 50


class _StubSettings:
    """Minimal stand-in for ``config.settings`` used to exercise the resolver."""

    def __init__(self, **values):
        self._values = values

    def __getattr__(self, name):
        try:
            return self._values[name]
        except KeyError as exc:  # pragma: no cover - mirrors getattr default path
            raise AttributeError(name) from exc


def _patch_settings(monkeypatch, **values):
    from app.services import subprocess_runner

    monkeypatch.setattr(subprocess_runner, "settings", _StubSettings(**values))
    return subprocess_runner


def test_resolver_falls_back_to_shared_default(monkeypatch):
    sr = _patch_settings(
        monkeypatch,
        tool_nice=10,
        tool_info_timeout=60,
        tool_verify_timeout=0,
        tool_ioprio_class=2,
        tool_ioprio_level=6,
    )
    # No per-tool override defined -> shared default for every owner.
    assert sr.nice_value("chdman") == 10
    assert sr.nice_value("dolphin_tool") == 10
    assert sr.info_timeout("dolphin_tool") == 60
    assert sr.verify_timeout("chdman") == 0


def test_resolver_prefers_per_tool_override(monkeypatch):
    sr = _patch_settings(
        monkeypatch,
        tool_nice=10,
        dolphin_tool_nice=15,
        tool_info_timeout=60,
        dolphin_tool_info_timeout=120,
    )
    assert sr.nice_value("dolphin_tool") == 15
    # An owner without an override still falls back to the shared default.
    assert sr.nice_value("chdman") == 10
    assert sr.info_timeout("dolphin_tool") == 120
    assert sr.info_timeout("chdman") == 60


def test_resolver_prefers_maxcso_verify_timeout_override(monkeypatch):
    """The runtime path maxcso uses (verify_timeout("maxcso")) honors the
    per-tool COMPRESSATORIUM_MAXCSO_VERIFY_TIMEOUT override, not just Settings
    parsing."""
    sr = _patch_settings(
        monkeypatch,
        tool_verify_timeout=0,
        maxcso_verify_timeout=50,
    )
    assert sr.verify_timeout("maxcso") == 50
    # Owners without an override still fall back to the shared default.
    assert sr.verify_timeout("chdman") == 0


def test_resolver_none_override_falls_back(monkeypatch):
    """An explicitly-None per-tool override defers to the shared default."""
    sr = _patch_settings(monkeypatch, tool_nice=10, chdman_nice=None)
    assert sr.nice_value("chdman") == 10


def test_verify_timeout_resolves_per_tool_for_nsz_and_z3ds(monkeypatch):
    sr = _patch_settings(
        monkeypatch,
        tool_verify_timeout=0,
        nsz_verify_timeout=30,
        z3ds_verify_timeout=45,
    )
    assert sr.verify_timeout("nsz") == 30
    assert sr.verify_timeout("z3ds") == 45
    # A tool without an override still falls back to the shared default.
    assert sr.verify_timeout("chdman") == 0


def test_timeout_helpers_clamp_negative_and_none(monkeypatch):
    sr = _patch_settings(
        monkeypatch,
        tool_info_timeout=-5,
        tool_verify_timeout=None,
    )
    assert sr.info_timeout() == 0
    assert sr.verify_timeout() == 0
