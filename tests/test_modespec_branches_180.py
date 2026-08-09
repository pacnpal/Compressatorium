"""Guard for #180: the dispatch/consumer sites must read ModeSpec/registry
fields, not branch on tool/mode identity.

The design doc's §3.1 mapping table says every `mode.startswith(...)` /
`== "<modestring>"` capability check should become a `ModeSpec` field read. This
pins the greppable Acceptance (no such residual checks survive in `convert.py` /
`job_manager.py`) and documents the collapsed extension gate's coverage.
"""

import re
from pathlib import Path

import pytest

from routes.convert import _BAD_EXTENSION_REASON

_APP = Path(__file__).resolve().parent.parent / "app"

# The capability checks §3.1 says must become ModeSpec field reads. Note this is
# deliberately narrow: the kept `mode == "romz_extract"` (romz single-ROM
# validation) and `mode == "extractcd"` (rename helper) are NOT capability
# checks and must stay allowed.
_FORBIDDEN = re.compile(
    r'startswith\("(?:dolphin_|extract|z3ds_)"\)'
    r'|== "(?:z3ds_compress|dolphin_iso|dolphin_gcz)"'
)


@pytest.mark.parametrize("rel", ["routes/convert.py", "services/job_manager.py"])
def test_no_residual_capability_branches(rel):
    src = (_APP / rel).read_text(encoding="utf-8")
    # Strip line comments so prose documenting the old idiom (e.g. "collapsed
    # from the dolphin_iso branch") doesn't trip the guard; only live code counts.
    code = "\n".join(line.split("#", 1)[0] for line in src.splitlines())
    hits = _FORBIDDEN.findall(code)
    assert not hits, f"residual tool/mode-identity capability branch in {rel}: {hits}"


def test_bad_extension_reason_covers_non_chdman_tools():
    # The collapsed extension gate is driven by this map. chdman is intentionally
    # absent (it validates by .chd presence, having dropped .chd from
    # input_extensions); makeps3iso (directory input, no suffix) is too.
    assert set(_BAD_EXTENSION_REASON) == {
        "dolphin", "z3ds", "nsz", "cso", "chain", "romz", "nkit",
    }
    assert "chdman" not in _BAD_EXTENSION_REASON


@pytest.mark.asyncio
async def test_chain_compression_is_not_rejected(tmp_path, monkeypatch):
    """A chain mode (cso_to_chd) that just ignores a stale compression preset
    must still queue, not 400. Regression: the compression gate is scoped to
    dolphin, so a chain with supports_compression=False isn't rejected."""
    from unittest.mock import AsyncMock

    from models import ConversionMode, JobCreateRequest
    from routes import convert as convert_routes

    source = tmp_path / "game.cso"
    source.write_bytes(b"cso")
    monkeypatch.setattr(convert_routes.settings, "chd_volumes", str(tmp_path))
    monkeypatch.setattr(convert_routes.settings, "data_mount_root", str(tmp_path))
    create_job_mock = AsyncMock()
    monkeypatch.setattr(convert_routes.job_manager, "create_job", create_job_mock)

    await convert_routes.create_job(JobCreateRequest(
        file_path=str(source), mode=ConversionMode.CSO_TO_CHD, compression="max",
    ))
    create_job_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_dolphin_iso_compression_keeps_specific_message(tmp_path, monkeypatch):
    """dolphin_iso still rejects compression with its exact advisory message,
    preserved via the data lookup rather than a mode-string branch."""
    from fastapi import HTTPException

    from models import ConversionMode, JobCreateRequest
    from routes import convert as convert_routes

    source = tmp_path / "game.rvz"
    source.write_bytes(b"rvz")
    monkeypatch.setattr(convert_routes.settings, "chd_volumes", str(tmp_path))
    monkeypatch.setattr(convert_routes.settings, "data_mount_root", str(tmp_path))

    with pytest.raises(HTTPException) as exc:
        await convert_routes.create_job(JobCreateRequest(
            file_path=str(source), mode=ConversionMode.DOLPHIN_ISO, compression="zstd",
        ))
    assert exc.value.status_code == 400
    assert exc.value.detail == "Compression not applicable for ISO extraction"
