"""Delegation-parity tests for the convert/verify dispatch routed through the
tool registry in ``job_manager._process_job`` (design Phase 3).

The real CLIs cannot run in the sandbox, so these assert that the *selection*
is correct: ``registry.for_mode(mode).convert`` / ``.verify`` reach the same
underlying service the legacy dispatch ladders would have chosen.  Each tool
delegates to its service via the wrapper's ``_service`` attribute, so patching
``_service`` there observes exactly what ``_process_job`` would dispatch to.
"""
from __future__ import annotations

import asyncio
import inspect

import pytest

from app.models import ConversionMode
from app.services.tools import registry

EXTERNAL_MODES = {ConversionMode.METADATA_SCAN, ConversionMode.DAT_MATCH}
# Composite/chain modes orchestrate several services and have no single
# ``_service`` to monkeypatch, so the single-tool ladder/parity model below
# doesn't apply (they get dedicated coverage in test_chain_service.py).
CONVERSION_MODES = [
    m.value
    for m in ConversionMode
    if m not in EXTERNAL_MODES and registry.for_mode(m.value).id != "chain"
]

# Tools whose service singleton is monkeypatched below. Kept in one place so
# the two parity tests can't drift apart.
_PATCHED_TOOLS = (
    "chdman", "dolphin", "z3ds", "nsz", "cso", "romz", "makeps3iso", "nkit", "jwud",
)

# Modes whose tool exposes a verify at all. nkit2iso has no verify subcommand —
# integrity is the NKit header CRC32 its restore checks inline — so its plugin
# defines no ``verify`` and there is no dispatch to assert. Derived rather than
# listed so a tool that later grows one is covered automatically.
#
# The predicate is deliberately "has a verify method", NOT "has verify_extensions":
# makeps3iso exposes a real verify (a PARAM.SFO readback) while registering no
# verify ROUTE, so its verify_extensions are empty and the narrower predicate
# would silently drop folder_to_iso from this parity matrix.
# ``BaseTool`` defines no ``verify``, so nothing inherits one by accident —
# ``test_verify_modes_covers_every_verifying_tool`` pins both halves.
VERIFY_MODES = [
    m for m in CONVERSION_MODES if hasattr(registry.for_mode(m), "verify")
]


def _legacy_dispatch_id(mode: str) -> str:
    """Replicates the convert/verify dispatch ladders formerly in
    ``_process_job`` (job_manager.py:1464 / :1576 / :1611).  Both ladders make
    the same tool selection, differing only in the progress message."""
    if mode == "folder_to_iso":
        return "makeps3iso"
    if mode.startswith("dolphin_"):
        return "dolphin"
    if mode.startswith("z3ds_"):
        return "z3ds"
    if mode.startswith("nsz_"):
        return "nsz"
    if mode.startswith(("cso_", "cso2_", "zso_", "dax_")):
        return "cso"
    if mode.startswith("romz_"):
        return "romz"
    if mode.startswith("nkit_"):
        return "nkit"
    if mode.startswith("jwud_"):
        return "jwud"
    return "chdman"


@pytest.mark.parametrize("mode", VERIFY_MODES)
def test_verify_dispatch_matches_legacy_ladder(mode, monkeypatch):
    called: dict[str, str] = {}

    def _record(tool_id):
        async def _verify(path):
            called["id"] = tool_id
            return {"valid": True, "message": "ok"}

        return _verify

    for tool_id in _PATCHED_TOOLS:
        service = registry.get(tool_id)._service
        if hasattr(service, "verify"):
            monkeypatch.setattr(service, "verify", _record(tool_id))

    result = asyncio.run(registry.for_mode(mode).verify("/data/out"))

    assert result == {"valid": True, "message": "ok"}
    assert called["id"] == _legacy_dispatch_id(mode)


@pytest.mark.parametrize("mode", CONVERSION_MODES)
def test_convert_dispatch_matches_legacy_ladder(mode, monkeypatch):
    called: dict[str, str] = {}

    def _record(tool_id):
        def _convert(input_path, output_path, mode_, *, compression=None,
                     split=False, cancel_event=None):
            called["id"] = tool_id

            async def _gen():
                yield {"progress": 100, "message": "done"}

            return _gen()

        return _convert

    for tool_id in _PATCHED_TOOLS:
        monkeypatch.setattr(
            registry.get(tool_id)._service, "convert", _record(tool_id)
        )

    async def _drain():
        return [
            u
            async for u in registry.for_mode(mode).convert(
                "/data/in", "/data/out", mode, compression=None, split=False,
                cancel_event=None,
            )
        ]

    updates = asyncio.run(_drain())

    assert updates == [{"progress": 100, "message": "done"}]
    assert called["id"] == _legacy_dispatch_id(mode)


def test_external_modes_never_resolve():
    for mode in EXTERNAL_MODES:
        with pytest.raises(KeyError):
            registry.for_mode(mode.value)


def test_chain_verify_delegates_to_final_step_tool(monkeypatch):
    """cso_to_chd verifies the final .chd via the verify_step tool (chdman)."""
    called: dict[str, str] = {}

    async def _verify(path):
        called["id"] = "chdman"
        return {"valid": True, "message": "ok"}

    monkeypatch.setattr(registry.get("chdman")._service, "verify", _verify)

    result = asyncio.run(registry.for_mode("cso_to_chd").verify("/data/out.chd"))

    assert result == {"valid": True, "message": "ok"}
    assert called["id"] == "chdman"


def test_verify_modes_covers_every_verifying_tool():
    """The VERIFY_MODES filter must not silently shrink.

    It is derived, so a wrong predicate degrades to "fewer modes asserted"
    rather than a failure. Two things are pinned here: ``BaseTool`` grows no
    ``verify`` (which would sweep non-verifying tools like nkit back in), and
    makeps3iso — whose verify exists but registers no route, so it has empty
    ``verify_extensions`` — stays covered.
    """
    from app.services.tools.base import BaseTool

    assert "verify" not in BaseTool.__dict__, (
        "BaseTool grew a default verify(); the hasattr filter now sweeps in "
        "tools that cannot verify (e.g. nkit). Switch to an explicit capability."
    )
    covered = set(VERIFY_MODES)
    assert "folder_to_iso" in covered, (
        "makeps3iso verifies (PARAM.SFO readback) but registers no verify route, "
        "so a verify_extensions-based filter would drop it from this matrix"
    )
    assert not registry.get("makeps3iso").verify_extensions
    # ...and the tool that genuinely cannot verify stays out.
    assert "nkit_restore" not in covered
    assert not hasattr(registry.get("nkit"), "verify")


def test_every_plugin_accepts_job_managers_convert_kwargs():
    """Every registered plugin must be callable the way the pipeline calls it.

    ``JobManager._process_job`` does::

        registry.for_mode(job.mode.value).convert(
            input_path, output_path, mode,
            compression=..., split=..., cancel_event=...,
        )

    unconditionally, for every mode. A plugin that omits a kwarg it doesn't use
    (``split`` applies only to makeps3iso) raises ``TypeError`` the first time a
    real job runs — and unit tests that drive the underlying *service* directly
    never catch it, because the plugin is the boundary the pipeline calls.
    ``docs/ADDING_PLATFORMS_AND_TOOLS.md`` §5.3 warns about this in prose; this
    enforces it.

    Binding the signature (rather than checking parameter names) proves the call
    would actually succeed, including for a plugin that takes ``**kwargs``.
    """
    for tool in registry.all():
        try:
            inspect.signature(tool.convert).bind(
                "/in", "/out", "any_mode",
                compression=None, split=False, cancel_event=None,
            )
        except TypeError as exc:  # pragma: no cover - the failure is the message
            pytest.fail(
                f"{tool.id}.convert cannot be called the way job_manager calls "
                f"it, so every job for this tool would raise: {exc}"
            )
