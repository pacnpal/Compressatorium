from pathlib import Path

from app.config import Settings


def test_explicit_volumes_skip_scan(tmp_path: Path):
    data_root = tmp_path / "data"
    (data_root / "games").mkdir(parents=True)
    (data_root / "games2").mkdir(parents=True)

    settings = Settings(
        COMPRESSATORIUM_VOLUMES="/data/games,/data/games2",
        COMPRESSATORIUM_MOUNT_ROOT=str(data_root),
    )

    assert settings.volumes == ["/data/games", "/data/games2"]


def test_auto_discovery_uses_data_children_when_env_unset(tmp_path: Path):
    data_root = tmp_path / "data"
    games = data_root / "games"
    games2 = data_root / "games2"
    games.mkdir(parents=True)
    games2.mkdir(parents=True)

    settings = Settings(
        COMPRESSATORIUM_VOLUMES="",
        COMPRESSATORIUM_MOUNT_ROOT=str(data_root),
    )

    assert settings.volumes == [str(games), str(games2)]


def test_auto_discovery_skips_symlink_children(tmp_path: Path):
    data_root = tmp_path / "data"
    games = data_root / "games"
    outside = tmp_path / "outside"
    games.mkdir(parents=True)
    outside.mkdir()
    (data_root / "escape").symlink_to(outside, target_is_directory=True)

    settings = Settings(
        COMPRESSATORIUM_VOLUMES="",
        COMPRESSATORIUM_MOUNT_ROOT=str(data_root),
    )

    assert settings.volumes == [str(games)]


def test_startup_scan_caches_discovered_volumes(tmp_path: Path):
    data_root = tmp_path / "data"
    games = data_root / "games"
    games.mkdir(parents=True)

    settings = Settings(
        COMPRESSATORIUM_VOLUMES="",
        COMPRESSATORIUM_MOUNT_ROOT=str(data_root),
    )

    assert settings.scan_data_mounts_on_startup() == [str(games)]

    # Startup cache should remain stable even if mounts change later.
    games2 = data_root / "games2"
    games2.mkdir()
    assert settings.volumes == [str(games)]


def test_concurrency_defaults_are_serial(monkeypatch):
    monkeypatch.delenv("MAX_CONCURRENT_JOBS", raising=False)
    monkeypatch.delenv("MAX_VERIFY_CONCURRENCY", raising=False)

    settings = Settings()

    assert settings.max_concurrent_jobs == 1
    assert settings.max_verify_concurrency == 1


def test_search_auto_return_default_and_legacy_alias(monkeypatch):
    monkeypatch.delenv("COMPRESSATORIUM_SEARCH_AUTO_RETURN_TO_FILE_LIST", raising=False)
    monkeypatch.delenv("CHD_SEARCH_AUTO_RETURN_TO_FILE_LIST", raising=False)

    assert Settings().search_auto_return_to_file_list is True
    assert (
        Settings(CHD_SEARCH_AUTO_RETURN_TO_FILE_LIST="false").search_auto_return_to_file_list
        is False
    )
