"""The two tool seams that used to hard-code a single tool's identity.

Both were per-tool branches sitting on a shared path, so a *second* tool of the
same shape would have silently inherited the first tool's behavior:

* ``JobManager._clear_existing_output`` called ``makeps3iso_service.remove_outputs``
  for every ``InputKind.DIRECTORY`` job — a second folder-input tool would have
  had its outputs swept by makeps3iso's split-part logic (silent corruption,
  not an error).
* ``GET /api/tools`` branched on ``tool.id == "nsz"`` to decide availability —
  a second key-gated tool could never be hidden, so the UI would advertise a
  converter that can only fail at job time.

They are now the ``overwrite_targets`` and ``is_ready`` plugin hooks. These
tests pin the generic behavior with a *synthetic* second tool, which is the
only way to catch a regression back to an id/kind branch: asserting on
makeps3iso or nsz alone passes either way.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.models import ConversionJob, ConversionMode, InputKind, JobStatus
from app.services.tools import registry
from app.services.tools.base import BaseTool


# --- overwrite_targets --------------------------------------------------------

def test_base_default_is_primary_plus_companions():
    # The default covers every ordinary mode: extractcd's .cue primary plus its
    # .bin sidecar, primary first.
    assert registry.for_mode("extractcd").overwrite_targets(
        "/data/Game.cue", "extractcd",
    ) == ["/data/Game.cue", "/data/Game.bin"]


def test_base_default_is_just_the_primary_for_a_single_file_mode():
    assert registry.for_mode("createcd").overwrite_targets(
        "/data/Game.chd", "createcd",
    ) == ["/data/Game.chd"]


def test_makeps3iso_sweeps_base_and_parts_together(tmp_path):
    """The state ``companion_outputs`` deliberately won't report.

    A ``-s`` build that failed mid-split leaves the not-yet-renamed base *and*
    numbered parts — a combination a successful build never produces, so
    ``split_parts`` (and thus ``companion_outputs``) stops at the base and
    hides the parts. ``overwrite_targets`` must still enumerate all of it or
    the stale parts survive the overwrite and collide with the new output.
    """
    base = tmp_path / "MyGame.iso"
    base.write_bytes(b"partial")
    (tmp_path / "MyGame.iso.0").write_bytes(b"0")
    (tmp_path / "MyGame.iso.1").write_bytes(b"1")
    tool = registry.get("makeps3iso")

    # companion_outputs reports nothing here (the base exists, so it wins)...
    assert tool.companion_outputs(str(base), "folder_to_iso") == []
    # ...but the overwrite still has to sweep the orphaned parts.
    assert tool.overwrite_targets(str(base), "folder_to_iso") == [
        str(base), f"{base}.0", f"{base}.1",
    ]


@pytest.mark.asyncio
async def test_directory_job_clear_uses_the_hook_not_makeps3iso(tmp_path, monkeypatch):
    """A directory job must sweep via its own plugin, not makeps3iso's.

    The regression this guards: ``_clear_existing_output`` branching on
    ``InputKind.DIRECTORY`` and calling ``makeps3iso_service.remove_outputs``.
    A synthetic directory tool whose overwrite set is deliberately *not*
    makeps3iso-shaped (a ``.sidecar``, no numbered parts) catches that — under
    the old code its sidecar survived and a stray ``.0`` was deleted instead.
    """
    import app.services.job_manager as jm_module
    from app.services.job_manager import job_manager

    out = tmp_path / "Built.iso"
    out.write_bytes(b"iso")
    sidecar = tmp_path / "Built.sidecar"
    sidecar.write_bytes(b"meta")
    # Not in this tool's overwrite set: makeps3iso's logic would remove it.
    stray_part = tmp_path / "Built.iso.0"
    stray_part.write_bytes(b"unrelated")

    class _FakeDirTool:
        id = "fakedir"

        def overwrite_targets(self, output_path, mode):  # noqa: ARG002
            return [output_path, f"{Path(output_path).with_suffix('.sidecar')}"]

    monkeypatch.setattr(
        jm_module.registry, "for_mode", lambda _mode: _FakeDirTool(),
    )

    async def _noop_clear(_path):
        return None

    monkeypatch.setattr(jm_module.verification_store, "clear", _noop_clear)

    job = ConversionJob(
        id="fakedir-clear",
        file_path=str(tmp_path / "SourceFolder"),
        filename="SourceFolder",
        mode=ConversionMode.FOLDER_TO_ISO,
        status=JobStatus.PROCESSING,
        created_at=datetime.now(timezone.utc),
        output_path=str(out),
        input_kind=InputKind.DIRECTORY,
        allow_overwrite=True,
    )

    await job_manager._clear_existing_output(job)

    assert not out.exists()
    assert not sidecar.exists(), "the tool's own companion must be swept"
    assert stray_part.exists(), "makeps3iso's part logic must not be applied"


@pytest.mark.asyncio
async def test_directory_job_still_rejects_a_non_file_primary(tmp_path, monkeypatch):
    """The old branch's one real guard survives the generalization.

    A directory squatting on the output name can't be unlinked, and makeps3iso
    would write *inside* it while the job still reported the bare path as its
    output. The tool-neutral sweep rejects it for the same reason the file path
    always did.

    (A directory on a *part* name needs no guard: ``_numbered_parts`` only
    enumerates files, so it is never a sweep target — and a split build would
    fail outright trying to create that part, rather than corrupting anything.)
    """
    import app.services.job_manager as jm_module
    from app.services.job_manager import job_manager

    out = tmp_path / "MyGame.iso"
    out.mkdir()

    async def _noop_clear(_path):
        return None

    monkeypatch.setattr(jm_module.verification_store, "clear", _noop_clear)

    job = ConversionJob(
        id="ps3-nonfile-primary",
        file_path=str(tmp_path / "MyGame"),
        filename="MyGame",
        mode=ConversionMode.FOLDER_TO_ISO,
        status=JobStatus.PROCESSING,
        created_at=datetime.now(timezone.utc),
        output_path=str(out),
        input_kind=InputKind.DIRECTORY,
        allow_overwrite=True,
    )

    with pytest.raises(RuntimeError, match="not a file"):
        await job_manager._clear_existing_output(job)
    assert out.is_dir(), "left intact, not partially clobbered"


# --- ChainTool: per-spec resolution instead of modes[0] -----------------------

def _second_chain_spec():
    """A synthetic second chain: .foo -> .bar, verified by its own final tool."""
    from app.services.tools.spec import ChainSpec, ChainStep, ModeKind

    return ChainSpec(
        mode="foo_to_bar",
        tool_id="chain",
        kind=ModeKind.CREATE,
        label="FOO to BAR",
        group="chain",
        output_ext=".bar",
        input_extensions=frozenset({".foo"}),
        steps=(
            ChainStep(tool_id="cso", mode="cso_decompress", weight=0.5, output_ratio=2.0),
            ChainStep(tool_id="z3ds", mode="z3ds_compress", weight=0.5, output_ratio=1.0),
        ),
        intermediate_exts=(".iso",),
        verify_step=1,
    )


def test_chain_detect_output_uses_each_specs_own_extension(tmp_path, monkeypatch):
    # Regression: detect_output hard-coded a ".chd" candidate, so a second chain
    # ending in another format probed the *first* chain's product.
    chain = registry.get("chain")
    monkeypatch.setattr(chain, "modes", (*chain.modes, _second_chain_spec()))

    source = tmp_path / "Game.foo"
    source.write_bytes(b"src")
    # Only the .bar product exists; a ".chd" probe would miss it entirely.
    (tmp_path / "Game.bar").write_bytes(b"out")

    status = chain.detect_output(str(source))

    assert status is not None
    assert status.path == str(tmp_path / "Game.bar")


def test_chain_final_tool_resolves_per_output_extension(monkeypatch):
    # Regression: _final_tool() read modes[0], so every chain's verify/info was
    # delegated to the first chain's final tool.
    chain = registry.get("chain")
    monkeypatch.setattr(chain, "modes", (*chain.modes, _second_chain_spec()))

    # The shipped chain still verifies through chdman...
    assert chain._final_tool("/data/Game.chd").id == "chdman"
    # ...while the synthetic one routes to its own verify_step tool.
    assert chain._final_tool("/data/Game.bar").id == "z3ds"


# --- is_ready -----------------------------------------------------------------

@pytest.mark.asyncio
async def test_base_tool_is_ready_by_default():
    class _Plain(BaseTool):
        id = "plain"
        display_name = "Plain"

    assert await _Plain("/bin/true").is_ready() is True


@pytest.mark.asyncio
async def test_every_registered_tool_implements_the_hook():
    # Cheap contract check: the route awaits this on every tool, so a plugin
    # that forgot it (or returned a non-awaitable) would 500 the endpoint.
    for tool in registry.all():
        assert isinstance(await tool.is_ready(), bool)


@pytest.mark.asyncio
async def test_list_tools_hides_any_unready_tool(monkeypatch):
    """Availability is driven by the hook, not by a hard-coded tool id.

    Gating a tool that is *not* nsz is the regression test: under the old
    ``tool.id == "nsz"`` branch this tool stayed available no matter what its
    readiness said.
    """
    from app.routes import info as info_routes

    # Patch through the route's own registry: `services.tools` (what app code
    # imports under PYTHONPATH=app) and `app.services.tools` (what tests import)
    # are distinct module objects with distinct singletons, so patching the
    # latter would not be visible here.
    victim = info_routes.registry.get("cso")

    async def _not_ready():
        return False

    monkeypatch.setattr(victim, "is_ready", _not_ready)

    result = await info_routes.list_tools()

    assert "cso" in result["unavailable"]
    assert "cso" not in result["available"]
    # Everything else is unaffected.
    assert "chdman" in result["available"]
