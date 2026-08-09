"""Orchestration tests for ``ChainTool`` (the cso_to_chd pipeline seam).

The real maxcso/chdman CLIs can't run here, so the component steps are mocked:
``registry.for_mode("cso_decompress")`` and ``registry.for_mode("createdvd")``
resolve to the cso/chdman plugins, whose ``convert`` is monkeypatched. These
tests assert the chain's own behavior — step delegation, weighted progress,
intermediate placement/cleanup, compression routing, cancel propagation, and the
disk-headroom preflight — independent of the underlying tools.
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

import app.services.tools.chain as chain_mod
from app.services.disk import InsufficientDiskSpace
from app.services.tools import registry


class _Cancelled(Exception):
    """Stand-in for a sub-tool's cancellation signal."""


def _drain(agen) -> list[dict]:
    async def _run() -> list[dict]:
        return [u async for u in agen]

    return asyncio.run(_run())


@pytest.fixture
def chain_env(monkeypatch, tmp_path):
    """Patch the chain's temp dir + disc-ID embed, and install fake steps."""
    work = tmp_path / "chainwork"

    def _scratch(prefix=""):
        work.mkdir(parents=True, exist_ok=True)
        return str(work)

    monkeypatch.setattr(chain_mod, "create_scratch_dir", _scratch)

    # The chain tags the final CHD by routing its last step through chdman's
    # post_convert hook. Keep it a no-op here so the orchestration assertions
    # don't depend on real disc parsing; the dedicated test below installs its
    # own spy to assert how the hook is invoked.
    async def _noop_post_convert(input_path, output_path, mode):
        return None

    monkeypatch.setattr(
        registry.get("chdman"), "post_convert", _noop_post_convert,
    )

    calls: list[dict] = []

    def install(cso_progress=(0, 100), chd_progress=(0, 100), cso_cancel=False):
        def make(tool_id, progress, cancel_raises):
            def _convert(input_path, output_path, mode, *,
                         compression=None, cancel_event=None):
                async def _gen():
                    calls.append({
                        "tool": tool_id, "mode": mode, "in": input_path,
                        "out": output_path, "compression": compression,
                    })
                    if cancel_raises and cancel_event is not None \
                            and cancel_event.is_set():
                        raise _Cancelled()
                    for p in progress:
                        yield {"progress": p, "message": f"{mode}:{p}"}
                    Path(output_path).write_bytes(b"0" * 32)

                return _gen()

            return _convert

        monkeypatch.setattr(
            registry.get("cso"), "convert", make("cso", cso_progress, cso_cancel),
        )
        monkeypatch.setattr(
            registry.get("chdman"), "convert", make("chdman", chd_progress, False),
        )

    return calls, work, install


def test_chain_orchestration_progress_and_intermediate(chain_env, tmp_path):
    calls, work, install = chain_env
    install(cso_progress=(0, 50, 100), chd_progress=(0, 50, 100))
    src = tmp_path / "Game.cso"
    src.write_bytes(b"x" * 1000)
    out = tmp_path / "Game.chd"

    updates = _drain(
        registry.for_mode("cso_to_chd").convert(
            str(src), str(out), "cso_to_chd", compression="zstd",
        )
    )

    # Step delegation + ordering.
    assert [c["mode"] for c in calls] == ["cso_decompress", "createdvd"]
    # Step 1 reads the source; step 2 reads the intermediate .iso in the work dir.
    assert calls[0]["in"] == str(src)
    assert calls[0]["out"].endswith(".iso")
    assert os.path.dirname(calls[0]["out"]) == str(work)
    assert calls[1]["in"] == calls[0]["out"]
    assert calls[1]["out"] == str(out)
    # cso_to_chd advertises no compression, so a client-supplied preset is
    # dropped (not smuggled to chdman as a codec): neither step receives it.
    assert calls[0]["compression"] is None
    assert calls[1]["compression"] is None

    # Aggregate progress is monotonic and ends at 100.
    progs = [u["progress"] for u in updates]
    assert progs == sorted(progs)
    assert progs[-1] == 100
    # The 0.20-weighted cso step tops out at ~20% of the bar before chd starts.
    assert max(u["progress"] for u in updates if u["message"].startswith("[1/2]")) == 20
    assert any(u["message"].startswith("[2/2]") for u in updates)

    # Final output produced; intermediate + work dir cleaned up.
    assert out.exists()
    assert not work.exists()


def test_chain_tags_final_chd_via_post_convert(chain_env, tmp_path, monkeypatch):
    """The chain drives the final step's ``post_convert`` with the intermediate
    source and the final CHD — the same disc-ID path a direct ``createdvd`` job
    takes. This is what replaces the old bespoke ``_embed_disc_id`` (#181)."""
    _calls, work, install = chain_env
    install()

    seen: list[tuple[str, str, str]] = []

    async def _spy(input_path, output_path, mode):
        seen.append((input_path, output_path, mode))

    monkeypatch.setattr(registry.get("chdman"), "post_convert", _spy)

    src = tmp_path / "Game.cso"
    src.write_bytes(b"x" * 1000)
    out = tmp_path / "Game.chd"

    _drain(
        registry.for_mode("cso_to_chd").convert(str(src), str(out), "cso_to_chd")
    )

    # Exactly one post_convert call, for the final chdman step.
    assert len(seen) == 1
    in_path, out_path, mode = seen[0]
    assert mode == "createdvd"
    assert out_path == str(out)
    # Tagged from the intermediate .iso in the work dir, not the original .cso.
    assert in_path.endswith(".iso")
    assert os.path.dirname(in_path) == str(work)


def test_chain_propagates_cancel_and_cleans_up(chain_env, tmp_path):
    _calls, work, install = chain_env
    install(cso_cancel=True)
    src = tmp_path / "Game.cso"
    src.write_bytes(b"x" * 1000)
    out = tmp_path / "Game.chd"
    event = asyncio.Event()
    event.set()

    with pytest.raises(_Cancelled):
        _drain(
            registry.for_mode("cso_to_chd").convert(
                str(src), str(out), "cso_to_chd", cancel_event=event,
            )
        )

    # Temp work dir removed even though the job aborted; no partial final.
    assert not work.exists()
    assert not out.exists()


def test_uncompressed_iso_size_reads_container_headers(tmp_path):
    import struct

    from app.services.maxcso import uncompressed_iso_size

    cso = tmp_path / "g.cso"
    cso.write_bytes(b"CISO" + struct.pack("<I", 24) + struct.pack("<Q", 5_000_000)
                    + struct.pack("<I", 2048))
    assert uncompressed_iso_size(str(cso)) == 5_000_000

    zso = tmp_path / "g.zso"
    zso.write_bytes(b"ZISO" + struct.pack("<I", 24) + struct.pack("<Q", 7_000_000)
                    + struct.pack("<I", 2048))
    assert uncompressed_iso_size(str(zso)) == 7_000_000

    dax = tmp_path / "g.dax"
    dax.write_bytes(b"DAX\x00" + struct.pack("<I", 3_000_000) + b"\x00" * 8)
    assert uncompressed_iso_size(str(dax)) == 3_000_000

    # Unknown header -> None (caller falls back to the ratio estimate).
    other = tmp_path / "g.bin"
    other.write_bytes(b"\x00" * 16)
    assert uncompressed_iso_size(str(other)) is None


def test_chain_headroom_blocks_before_any_step(chain_env, tmp_path, monkeypatch):
    calls, work, install = chain_env
    install()

    def _boom(targets, *, margin_bytes=0):
        raise InsufficientDiskSpace("not enough space")

    monkeypatch.setattr(chain_mod, "ensure_headroom", _boom)

    src = tmp_path / "Game.cso"
    src.write_bytes(b"x" * 1000)
    out = tmp_path / "Game.chd"

    with pytest.raises(InsufficientDiskSpace):
        _drain(
            registry.for_mode("cso_to_chd").convert(str(src), str(out), "cso_to_chd")
        )

    # Preflight runs before step 1: no steps executed, work dir cleaned up.
    assert calls == []
    assert not work.exists()


# --------------------------------------------------------------------------- #
# nkit_to_rvz: the second chain, and the compound-extension cases it exercises
# --------------------------------------------------------------------------- #


@pytest.fixture
def nkit_chain_env(monkeypatch, tmp_path):
    """The nkit_to_rvz analogue of ``chain_env``: fake nkit + dolphin steps."""
    work = tmp_path / "chainwork"

    def _scratch(prefix=""):
        work.mkdir(parents=True, exist_ok=True)
        return str(work)

    monkeypatch.setattr(chain_mod, "create_scratch_dir", _scratch)

    async def _noop_post_convert(input_path, output_path, mode):
        return None

    monkeypatch.setattr(registry.get("dolphin"), "post_convert", _noop_post_convert)

    calls: list[dict] = []

    def make(tool_id):
        def _convert(input_path, output_path, mode, *,
                     compression=None, cancel_event=None):
            async def _gen():
                calls.append({
                    "tool": tool_id, "mode": mode, "in": input_path,
                    "out": output_path, "compression": compression,
                })
                yield {"progress": 100, "message": f"{mode}:100"}
                Path(output_path).write_bytes(b"0" * 32)

            return _gen()

        return _convert

    monkeypatch.setattr(registry.get("nkit"), "convert", make("nkit"))
    monkeypatch.setattr(registry.get("dolphin"), "convert", make("dolphin"))
    return calls, work


def test_nkit_chain_names_the_intermediate_off_the_first_step(
    nkit_chain_env, tmp_path,
):
    calls, work = nkit_chain_env
    src = tmp_path / "Melee.nkit.iso"
    src.write_bytes(b"x" * 1000)
    out = tmp_path / "Melee.rvz"

    updates = _drain(
        registry.for_mode("nkit_to_rvz").convert(str(src), str(out), "nkit_to_rvz")
    )

    assert [c["mode"] for c in calls] == ["nkit_restore", "dolphin_rvz"]
    # The whole compound extension is stripped: the scratch ISO must NOT be
    # "Melee.nkit.iso" (what Path(input).stem would have produced).
    assert os.path.basename(calls[0]["out"]) == "Melee.iso"
    assert os.path.dirname(calls[0]["out"]) == str(work)
    assert calls[1]["in"] == calls[0]["out"]
    assert calls[1]["out"] == str(out)
    assert updates[-1]["progress"] == 100
    # The full-size ISO is scratch, never left in the library.
    assert not (tmp_path / "Melee.iso").exists()


def test_nkit_chain_output_path_uses_the_chain_suffix(tmp_path):
    # Delegating to the FINAL step's tool (dolphin) would give "Melee.nkit.rvz".
    assert registry.for_mode("nkit_to_rvz").output_path(
        "nkit_to_rvz", "/data/Melee.nkit.iso",
    ) == "/data/Melee.rvz"
    assert registry.for_mode("nkit_to_rvz").output_path(
        "nkit_to_rvz", "/data/Melee.nkit.gcz", "/out",
    ) == "/out/Melee.rvz"
    # The existing plain-suffix chain is unchanged by that generalization.
    assert registry.for_mode("cso_to_chd").output_path(
        "cso_to_chd", "/data/Game.cso",
    ) == "/data/Game.chd"


def test_nkit_chain_detect_output_badges_the_rvz(tmp_path):
    src = tmp_path / "Melee.nkit.iso"
    src.write_bytes(b"x")
    assert registry.get("chain").detect_output(str(src)) is None
    (tmp_path / "Melee.rvz").write_bytes(b"y")
    status = registry.get("chain").detect_output(str(src))
    assert status is not None
    assert status.path == str(tmp_path / "Melee.rvz")


def test_chain_preflight_asks_the_first_step_for_the_intermediate_size(
    nkit_chain_env, tmp_path, monkeypatch,
):
    """The headroom preflight must size the ISO from the NKit header.

    A scrubbed NKit image can be a fraction of its restored size, so the
    ratio fallback would badly under-count. ChainTool asks the first step's
    plugin (``expected_output_size``) instead of importing one tool's reader.
    """
    src = tmp_path / "Melee.nkit.iso"
    src.write_bytes(b"x" * 1000)
    seen: list[tuple[str, int]] = []

    def _record(targets, *, margin_bytes):
        seen.extend(targets)

    monkeypatch.setattr(chain_mod, "ensure_headroom", _record)
    monkeypatch.setattr(
        registry.get("nkit"), "expected_output_size",
        lambda input_path, mode: 4_700_000_000,
    )

    _drain(
        registry.for_mode("nkit_to_rvz").convert(
            str(src), str(tmp_path / "Melee.rvz"), "nkit_to_rvz",
        )
    )

    # Both targets sized from the header, not from 1000 bytes * output_ratio.
    assert seen and all(size == 4_700_000_000 for _path, size in seen)


def test_chain_preflight_falls_back_to_ratio_without_a_header_size(
    nkit_chain_env, tmp_path, monkeypatch,
):
    src = tmp_path / "Melee.nkit.iso"
    src.write_bytes(b"x" * 1000)
    seen: list[tuple[str, int]] = []

    monkeypatch.setattr(
        chain_mod, "ensure_headroom",
        lambda targets, *, margin_bytes: seen.extend(targets),
    )
    monkeypatch.setattr(
        registry.get("nkit"), "expected_output_size",
        lambda input_path, mode: None,
    )

    _drain(
        registry.for_mode("nkit_to_rvz").convert(
            str(src), str(tmp_path / "Melee.rvz"), "nkit_to_rvz",
        )
    )

    # output_ratio 3.0 for the restore step, 1.0 for the RVZ step.
    assert sorted(size for _p, size in seen) == [1000, 3000]
