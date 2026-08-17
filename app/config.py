"""Application configuration settings for the CHD converter."""

import os
from pathlib import Path

from pydantic import AliasChoices, Field, PrivateAttr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables and defaults."""

    model_config = SettingsConfigDict(
        populate_by_name=True,
        extra="ignore",
    )
    _startup_discovered_volumes: list[str] = PrivateAttr(default_factory=list)
    _startup_scan_completed: bool = PrivateAttr(default=False)

    # Volume configuration. Prefer COMPRESSATORIUM_* names; CHD_* remain supported aliases.
    chd_volumes: str = Field(
        default="",
        alias="COMPRESSATORIUM_VOLUMES",
        validation_alias=AliasChoices("COMPRESSATORIUM_VOLUMES", "CHD_VOLUMES"),
    )
    data_mount_root: str = Field(
        default="/data",
        alias="COMPRESSATORIUM_MOUNT_ROOT",
        validation_alias=AliasChoices("COMPRESSATORIUM_MOUNT_ROOT", "CHD_MOUNT_ROOT"),
    )

    # Persistent data directory
    data_dir: str = Field(default="/config", alias="CHD_DATA_DIR")

    # Unified SQLite database path.  Defaults to <data_dir>/compressatorium.db.
    db_path: str | None = Field(
        default=None,
        alias="COMPRESSATORIUM_DB_PATH",
        description="SQLite database file; default: <CHD_DATA_DIR>/compressatorium.db",
    )

    # Web UI authentication. Disabled by default; set COMPRESSATORIUM_ENABLE_AUTH=true
    # to require a token for the Web UI and /api routes. When enabled, set a token
    # explicitly or the app creates a persistent token in CHD_DATA_DIR/auth_token.
    enable_auth: bool = Field(default=False, alias="COMPRESSATORIUM_ENABLE_AUTH")
    auth_token: str | None = Field(
        default=None,
        alias="COMPRESSATORIUM_AUTH_TOKEN",
        validation_alias=AliasChoices("COMPRESSATORIUM_AUTH_TOKEN", "CHD_AUTH_TOKEN"),
    )
    auth_username: str = Field(default="admin", alias="COMPRESSATORIUM_AUTH_USERNAME")

    # Web UI behavior
    search_auto_return_to_file_list: bool = Field(
        default=True,
        alias="COMPRESSATORIUM_SEARCH_AUTO_RETURN_TO_FILE_LIST",
        validation_alias=AliasChoices(
            "COMPRESSATORIUM_SEARCH_AUTO_RETURN_TO_FILE_LIST",
            "CHD_SEARCH_AUTO_RETURN_TO_FILE_LIST",
        ),
    )

    # Job limits
    max_concurrent_jobs: int = Field(default=1, alias="MAX_CONCURRENT_JOBS")
    max_queue_depth: int = Field(
        default=0,
        alias="MAX_QUEUE_DEPTH",
        description="Maximum queued+processing conversion jobs (0 disables backpressure)",
    )
    max_verify_concurrency: int = Field(
        default=1,
        alias="MAX_VERIFY_CONCURRENCY",
        description="Maximum concurrent verify workloads across all endpoints",
    )
    max_metadata_scan_concurrency: int = Field(
        default=1,
        alias="MAX_METADATA_SCAN_CONCURRENCY",
        description="Maximum concurrent metadata scan tasks",
    )
    max_match_concurrency: int = Field(
        default=1,
        alias="MAX_MATCH_CONCURRENCY",
        description=(
            "Maximum concurrent DAT-match hashing operations. Matching a raw "
            "ISO/WBFS requires full-file SHA1; bound this to protect "
            "disk/CPU when many uncached files are browsed at once."
        ),
    )
    match_max_file_size: int = Field(
        default=0,
        alias="MATCH_MAX_FILE_SIZE",
        description=(
            "If non-zero, skip DAT hash-matching for files larger than this "
            "many bytes. 0 disables the cap (match any size)."
        ),
    )
    concurrency_lock_dir: str | None = Field(
        default=None,
        alias="CHD_CONCURRENCY_LOCK_DIR",
        description=(
            "Directory for job lock files "
            "(default: ephemeral /tmp subdirectory, auto-cleaned on restart)"
        ),
    )
    max_job_history: int = Field(default=500, alias="MAX_JOB_HISTORY")

    # Temporary working directory (archive extraction, etc.)
    temp_dir: str | None = Field(default=None, alias="CHD_TEMP_DIR")

    # Archive safety limits (listing + related extraction for cue/gdi)
    archive_max_entries: int = Field(default=5000, alias="CHD_ARCHIVE_MAX_ENTRIES")
    archive_max_member_size: int = Field(
        default=0,
        alias="CHD_ARCHIVE_MAX_MEMBER_SIZE",
    )
    archive_max_total_size: int = Field(
        default=0,
        alias="CHD_ARCHIVE_MAX_TOTAL_SIZE",
    )

    # chdman binary path
    chdman_path: str = Field(default="/usr/bin/chdman", alias="CHDMAN_PATH")

    # dolphin-tool binary path
    dolphin_tool_path: str = Field(
        default="/usr/local/bin/dolphin-tool", alias="DOLPHIN_TOOL_PATH",
    )

    # z3ds_compressor binary path
    z3ds_compressor_path: str = Field(
        default="/usr/local/bin/z3ds_compressor", alias="Z3DS_COMPRESSOR_PATH",
    )

    # maxcso binary path (PSP/PS2 ISO <-> CSO/ZSO). Built from source into the
    # image at /usr/local/bin/maxcso; set MAXCSO_PATH to relocate/override.
    maxcso_path: str = Field(
        default="/usr/local/bin/maxcso", alias="MAXCSO_PATH",
    )

    # makeps3iso binary path (decrypted PS3 disc/JB folder -> .iso). Built from
    # source (bucanero/ps3iso-utils, GPL-3.0) into the image at
    # /usr/local/bin/makeps3iso; set MAKEPS3ISO_PATH to relocate/override.
    makeps3iso_path: str = Field(
        default="/usr/local/bin/makeps3iso", alias="MAKEPS3ISO_PATH",
    )

    # JWUDTool launcher (Wii U .wud <-> .wux). JWUDTool is a Java .jar, so the
    # image ships a tiny `exec java -jar ...` launcher at /usr/local/bin/jwudtool
    # next to the jar; set JWUDTOOL_PATH to point at your own launcher/wrapper
    # (e.g. for a local checkout without the image's JRE).
    jwudtool_path: str = Field(
        default="/usr/local/bin/jwudtool", alias="JWUDTOOL_PATH",
    )
    # nkit2iso binary path (NKit-shrunk GameCube/Wii image -> plain .iso). Built
    # from source (DonMikone/nkit2iso, MIT) into the image at
    # /usr/local/bin/nkit2iso; set NKIT2ISO_PATH to relocate/override.
    nkit2iso_path: str = Field(
        default="/usr/local/bin/nkit2iso", alias="NKIT2ISO_PATH",
    )
    # What nkit2iso does with a Wii image whose update partition was removed at
    # shrink time: that data is not in the file and cannot be regenerated.
    #   "none"     - zero-fill the region (default). Fully offline; the result is
    #                playable but not redump-verifiable, and the CRC32 check is
    #                skipped, so the job reports the caveat in its final message.
    #   "download" - fetch the publicly archived recovery partition and splice it
    #                in for a bit-exact restore. This is the ONLY situation in
    #                which nkit2iso touches the network, so it stays opt-in: a
    #                converter should not reach out to the internet unless the
    #                operator asked it to.
    # The tool's own "ask" mode is deliberately not offered — it prompts on a
    # terminal, and a job worker has no one to answer it.
    nkit2iso_recovery: str = Field(
        default="none", alias="NKIT2ISO_RECOVERY",
        pattern="^(none|download)$",
    )

    # Extra free space (MB) a chained conversion (e.g. cso->iso->chd) keeps
    # beyond its estimated peak before starting. A chain holds source + full
    # intermediate + partial final at once, so it preflights disk headroom on
    # both the work-dir and output-dir volumes; this is the per-volume cushion.
    chain_disk_margin_mb: int = Field(
        default=512, alias="COMPRESSATORIUM_CHAIN_DISK_MARGIN_MB",
    )

    # nsz (Nintendo Switch NSP/XCI <-> NSZ/XCZ). Installed via pip into the
    # venv, so the console script lives on PATH (/opt/venv/bin in Docker, .venv
    # locally). A bare name resolves in both; set NSZ_PATH to pin an absolute
    # path if needed.
    nsz_path: str = Field(default="nsz", alias="NSZ_PATH")
    # Directory holding the operator's own Switch keys (a `prod.keys`, or a
    # `keys.txt`). nsz needs console keys to decrypt the NCA content before
    # compressing (and to re-encrypt on decompress). We ship NO keys: the
    # operator mounts their own and points SWITCH_KEYS at the directory. This is
    # the source of truth when set. When unset, the app best-effort searches the
    # standard locations (~/.switch, ~/.config/nsz) at runtime.
    switch_keys_dir: str | None = Field(default=None, alias="SWITCH_KEYS")
    # zstandard level for nsz compression (1-22, nsz default is 18).
    nsz_compression_level: int = Field(
        default=18, alias="NSZ_COMPRESSION_LEVEL", ge=1, le=22,
    )

    # 7z CLI (handheld ROM .gb/.gbc/.gba/.nds <-> .7z/.zip). Ships in the image
    # via the p7zip-full package; the binary is `7z`. A bare name resolves on
    # PATH; set SEVENZIP_PATH to pin an absolute path (e.g. `7zz` on distros that
    # ship the newer `7zip` package instead).
    sevenzip_path: str = Field(default="7z", alias="SEVENZIP_PATH")

    # MAMERedump DAT sync
    mameredump_repo: str = Field(
        default="MetalSlug/MAMERedump", alias="MAMEREDUMP_REPO",
    )
    mameredump_auto_sync: bool = Field(
        default=False, alias="MAMEREDUMP_AUTO_SYNC",
        description="Auto-sync DATs from MAMERedump on startup if none loaded",
    )

    # RomM catalog overlay. Compressatorium reads RomM's catalog over HTTP but
    # touches the ROM bytes only through the filesystem: RomM's library is
    # mounted as an ordinary Compressatorium volume (locally via the same bind
    # mount, remotely via NFS/SMB/rclone), so no ROM ever crosses the network.
    # Unset romm_url disables the feature and hides the view.
    #
    # The credential is deliberately NOT a Settings field. It is read from the
    # environment inside services.romm, the same choice made for
    # MAMEREDUMP_GITHUB_TOKEN: a secret on the settings singleton ends up in
    # every repr()/log line that dumps config.
    romm_url: str = Field(
        default="", alias="ROMM_URL",
        description="Base URL of the RomM instance, e.g. http://romm:8080",
    )
    romm_library_root: str = Field(
        default="", alias="ROMM_LIBRARY_ROOT",
        description=(
            "Local mount point of RomM's library directory (the path RomM sees "
            "as /romm/library). RomM's fs_path/full_path are relative to it."
        ),
    )

    # Process-priority and timeout policy shared by EVERY conversion tool's
    # subprocess (chdman, Dolphin, 3DS, Switch) and the shared SubprocessRunner.
    # These began life as chdman-only knobs; prefer the tool-neutral
    # COMPRESSATORIUM_* names. The chdman-era CHD_*/CHDMAN_* names remain
    # supported as backwards-compatible validation aliases (the same pattern as
    # chd_volumes -> COMPRESSATORIUM_VOLUMES).
    #
    # Optional per-tool overrides (the *_nice / *_ioprio_* / *_info_timeout /
    # *_verify_timeout fields below) default to None and fall back to these
    # shared defaults; see services.subprocess_runner for the resolution policy.
    tool_nice: int | None = Field(
        default=10,
        alias="COMPRESSATORIUM_TOOL_NICE",
        validation_alias=AliasChoices("COMPRESSATORIUM_TOOL_NICE", "CHD_CHDMAN_NICE"),
    )
    tool_ioprio_class: int | None = Field(
        default=2,
        alias="COMPRESSATORIUM_TOOL_IOPRIO_CLASS",
        validation_alias=AliasChoices(
            "COMPRESSATORIUM_TOOL_IOPRIO_CLASS", "CHD_CHDMAN_IOPRIO_CLASS",
        ),
    )
    tool_ioprio_level: int | None = Field(
        default=6,
        alias="COMPRESSATORIUM_TOOL_IOPRIO_LEVEL",
        validation_alias=AliasChoices(
            "COMPRESSATORIUM_TOOL_IOPRIO_LEVEL", "CHD_CHDMAN_IOPRIO_LEVEL",
        ),
    )
    tool_info_timeout: int = Field(
        default=60,
        alias="COMPRESSATORIUM_TOOL_INFO_TIMEOUT",
        validation_alias=AliasChoices(
            "COMPRESSATORIUM_TOOL_INFO_TIMEOUT", "CHD_INFO_TIMEOUT",
        ),
    )
    # Verify is bounded the same adaptive way a conversion is: a *baseline* plus
    # an allowance per GiB of the file being read, capped. A flat bound cannot
    # serve both a 400 MB CIA and a 90 GB PS3 ISO -- one is generous, the other
    # kills healthy work -- because verify reads the whole file, so its runtime
    # scales with size. Defaults are deliberately loose (30 min + 10 min/GiB,
    # i.e. a ~1.7 MB/s floor, capped at 24h): the point is that a verify wedged
    # on dead storage *ends*, not that a slow one is policed. Set
    # COMPRESSATORIUM_TOOL_VERIFY_TIMEOUT_PER_GIB=0 for a flat bound, or
    # COMPRESSATORIUM_TOOL_VERIFY_TIMEOUT=0 to disable *this* bound (issue #266
    # -- it used to default to 0, so a verify that never returned never ended,
    # freezing the whole queue behind it). The stall bound below is a separate
    # knob and stays on when this is zeroed, deliberately: an operator raising
    # or removing the overall bound for a huge image still wants a verifier that
    # has gone completely silent to be caught. Zero both for no bound at all.
    tool_verify_timeout: int = Field(
        default=1800,
        alias="COMPRESSATORIUM_TOOL_VERIFY_TIMEOUT",
        validation_alias=AliasChoices(
            "COMPRESSATORIUM_TOOL_VERIFY_TIMEOUT", "CHD_VERIFY_TIMEOUT",
        ),
    )
    tool_verify_timeout_per_gib: int = Field(
        default=600,
        alias="COMPRESSATORIUM_TOOL_VERIFY_TIMEOUT_PER_GIB",
    )
    tool_verify_timeout_cap: int = Field(
        default=86400,
        alias="COMPRESSATORIUM_TOOL_VERIFY_TIMEOUT_CAP",
    )
    # Stall bound for verifiers that stream progress (chdman, dolphin-tool):
    # no output at all for this long means wedged, and it catches a hang far
    # sooner than the size-scaled overall bound above. Mirrors the conversion
    # path's CHD_PROGRESS_TIMEOUT default, and is independent of it in the same
    # way -- zeroing the overall verify timeout does not zero this. Named
    # `tool_*` so per-tool overrides resolve through the same policy as every
    # other knob here.
    tool_verify_progress_timeout: int = Field(
        default=600,
        alias="COMPRESSATORIUM_TOOL_VERIFY_PROGRESS_TIMEOUT",
        validation_alias=AliasChoices(
            "COMPRESSATORIUM_TOOL_VERIFY_PROGRESS_TIMEOUT",
            "CHD_VERIFY_PROGRESS_TIMEOUT",
        ),
    )

    # Optional per-tool overrides. Each defaults to None, meaning "use the
    # shared tool_* default above". Tool keys match the SubprocessRunner owner
    # names (chdman, dolphin_tool, nsz, z3ds) so per-tool resolution is a plain
    # getattr lookup. Set e.g. COMPRESSATORIUM_DOLPHIN_TOOL_NICE to give Dolphin
    # a different nice level than chdman.
    chdman_nice: int | None = Field(
        default=None, alias="COMPRESSATORIUM_CHDMAN_NICE",
    )
    chdman_ioprio_class: int | None = Field(
        default=None, alias="COMPRESSATORIUM_CHDMAN_IOPRIO_CLASS",
    )
    chdman_ioprio_level: int | None = Field(
        default=None, alias="COMPRESSATORIUM_CHDMAN_IOPRIO_LEVEL",
    )
    chdman_info_timeout: int | None = Field(
        default=None, alias="COMPRESSATORIUM_CHDMAN_INFO_TIMEOUT",
    )
    chdman_verify_timeout: int | None = Field(
        default=None, alias="COMPRESSATORIUM_CHDMAN_VERIFY_TIMEOUT",
    )
    dolphin_tool_nice: int | None = Field(
        default=None, alias="COMPRESSATORIUM_DOLPHIN_TOOL_NICE",
    )
    dolphin_tool_ioprio_class: int | None = Field(
        default=None, alias="COMPRESSATORIUM_DOLPHIN_TOOL_IOPRIO_CLASS",
    )
    dolphin_tool_ioprio_level: int | None = Field(
        default=None, alias="COMPRESSATORIUM_DOLPHIN_TOOL_IOPRIO_LEVEL",
    )
    dolphin_tool_info_timeout: int | None = Field(
        default=None, alias="COMPRESSATORIUM_DOLPHIN_TOOL_INFO_TIMEOUT",
    )
    dolphin_tool_verify_timeout: int | None = Field(
        default=None, alias="COMPRESSATORIUM_DOLPHIN_TOOL_VERIFY_TIMEOUT",
    )
    nsz_nice: int | None = Field(
        default=None, alias="COMPRESSATORIUM_NSZ_NICE",
    )
    nsz_ioprio_class: int | None = Field(
        default=None, alias="COMPRESSATORIUM_NSZ_IOPRIO_CLASS",
    )
    nsz_ioprio_level: int | None = Field(
        default=None, alias="COMPRESSATORIUM_NSZ_IOPRIO_LEVEL",
    )
    # nsz/z3ds expose only a verify subprocess (their info() is a filesystem
    # read with no child process), so they take a *_verify_timeout override but
    # no *_info_timeout.
    nsz_verify_timeout: int | None = Field(
        default=None, alias="COMPRESSATORIUM_NSZ_VERIFY_TIMEOUT",
    )
    z3ds_nice: int | None = Field(
        default=None, alias="COMPRESSATORIUM_Z3DS_NICE",
    )
    z3ds_ioprio_class: int | None = Field(
        default=None, alias="COMPRESSATORIUM_Z3DS_IOPRIO_CLASS",
    )
    z3ds_ioprio_level: int | None = Field(
        default=None, alias="COMPRESSATORIUM_Z3DS_IOPRIO_LEVEL",
    )
    z3ds_verify_timeout: int | None = Field(
        default=None, alias="COMPRESSATORIUM_Z3DS_VERIFY_TIMEOUT",
    )
    # maxcso (CSO/ZSO): like nsz/z3ds, info() is a filesystem read so only a
    # verify-timeout override is exposed, no info-timeout.
    maxcso_nice: int | None = Field(
        default=None, alias="COMPRESSATORIUM_MAXCSO_NICE",
    )
    maxcso_ioprio_class: int | None = Field(
        default=None, alias="COMPRESSATORIUM_MAXCSO_IOPRIO_CLASS",
    )
    maxcso_ioprio_level: int | None = Field(
        default=None, alias="COMPRESSATORIUM_MAXCSO_IOPRIO_LEVEL",
    )
    maxcso_verify_timeout: int | None = Field(
        default=None, alias="COMPRESSATORIUM_MAXCSO_VERIFY_TIMEOUT",
    )
    # makeps3iso (PS3 folder -> ISO): one packing subprocess, and no *reachable*
    # verify -- the mode declines delete-on-verify, the tool registers no verify
    # extensions or route, and the post-build PARAM.SFO readback is part of
    # convert(), not verify(). So no _VERIFY_TIMEOUT here: a knob no flow reads
    # is worse than no knob. Add one alongside a bounded verify entry point if
    # this tool ever gains one.
    makeps3iso_nice: int | None = Field(
        default=None, alias="COMPRESSATORIUM_MAKEPS3ISO_NICE",
    )
    makeps3iso_ioprio_class: int | None = Field(
        default=None, alias="COMPRESSATORIUM_MAKEPS3ISO_IOPRIO_CLASS",
    )
    makeps3iso_ioprio_level: int | None = Field(
        default=None, alias="COMPRESSATORIUM_MAKEPS3ISO_IOPRIO_LEVEL",
    )
    # romz (7z ROM packer): like nsz/z3ds/maxcso, info() is a filesystem read so
    # only a verify-timeout override is exposed, no info-timeout.
    romz_nice: int | None = Field(
        default=None, alias="COMPRESSATORIUM_ROMZ_NICE",
    )
    romz_ioprio_class: int | None = Field(
        default=None, alias="COMPRESSATORIUM_ROMZ_IOPRIO_CLASS",
    )
    romz_ioprio_level: int | None = Field(
        default=None, alias="COMPRESSATORIUM_ROMZ_IOPRIO_LEVEL",
    )
    romz_verify_timeout: int | None = Field(
        default=None, alias="COMPRESSATORIUM_ROMZ_VERIFY_TIMEOUT",
    )
    # jwud (JWUDTool, Wii U .wud <-> .wux): one conversion subprocess. Like
    # makeps3iso its verify is pure Python (a WUX header/index-table walk, no
    # child process), so its verify timeout likewise bounds the call itself.
    jwud_nice: int | None = Field(
        default=None, alias="COMPRESSATORIUM_JWUD_NICE",
    )
    jwud_ioprio_class: int | None = Field(
        default=None, alias="COMPRESSATORIUM_JWUD_IOPRIO_CLASS",
    )
    jwud_ioprio_level: int | None = Field(
        default=None, alias="COMPRESSATORIUM_JWUD_IOPRIO_LEVEL",
    )
    jwud_verify_timeout: int | None = Field(
        default=None, alias="COMPRESSATORIUM_JWUD_VERIFY_TIMEOUT",
    )
    # nkit2iso (NKit -> ISO): one restore subprocess, and no info/verify child
    # process of its own (integrity is the header CRC32 checked inline during the
    # restore), so only the nice/ioprio overrides are exposed.
    nkit2iso_nice: int | None = Field(
        default=None, alias="COMPRESSATORIUM_NKIT2ISO_NICE",
    )
    nkit2iso_ioprio_class: int | None = Field(
        default=None, alias="COMPRESSATORIUM_NKIT2ISO_IOPRIO_CLASS",
    )
    nkit2iso_ioprio_level: int | None = Field(
        default=None, alias="COMPRESSATORIUM_NKIT2ISO_IOPRIO_LEVEL",
    )

    # Logging
    log_level: str = Field(default="INFO", alias="LOGLEVEL")
    log_path: str | None = Field(
        default=None,
        alias="LOG_PATH",
        validation_alias=AliasChoices("LOG_PATH", "CHD_DEBUG_LOG_PATH"),
    )
    log_color: str = Field(
        default="always",
        alias="LOG_COLOR",
        description=(
            "ANSI-color the stdout log stream: 'always' (default), 'auto' "
            "(TTY + no NO_COLOR env), or 'never'. File logs are never colored. "
            "Default is 'always' so `docker logs` is colored out of the box; "
            "set 'never' to opt out, or 'auto' to follow TTY / NO_COLOR."
        ),
    )
    debug_heartbeat_interval: int = Field(default=30, alias="CHD_DEBUG_HEARTBEAT")
    debug_progress_interval: int = Field(
        default=30,
        alias="CHD_DEBUG_PROGRESS_INTERVAL",
    )
    debug_progress_timeout: int = Field(default=300, alias="CHD_DEBUG_PROGRESS_TIMEOUT")
    progress_timeout: int = Field(default=600, alias="CHD_PROGRESS_TIMEOUT")
    progress_timeout_per_gib: int = Field(
        default=120,
        alias="CHD_PROGRESS_TIMEOUT_PER_GIB",
    )
    progress_timeout_cap: int = Field(
        default=7200,
        alias="CHD_PROGRESS_TIMEOUT_CAP",
    )

    def model_post_init(self, __context: object, /) -> None:  # pylint: disable=arguments-differ
        """Set default paths after model initialization."""
        if self.temp_dir is None:
            self.temp_dir = str(Path(self.data_dir) / "temp")
        if self.concurrency_lock_dir is None:
            # Use a subdirectory under /tmp for ephemeral lock storage
            # This is secure in the container context because:
            # 1. Container filesystem is isolated
            # 2. Non-root user runs the application
            # 3. Directory permissions are set restrictively (0o700) during creation
            #    in concurrency_manager.py and lock_manager.py, mitigating Bandit_B108
            #    concerns about predictable temporary paths
            # 4. Locks don't persist across container restarts
            # The fixed path is intentional to allow multiple processes to share locks
            self.concurrency_lock_dir = os.path.join(os.environ.get('TMPDIR', '/tmp'), 'chd-locks')
        # CHD_DEBUG=true backwards compatibility: map to LOGLEVEL=DEBUG when LOGLEVEL
        # is not explicitly set in the environment and log_level was not explicitly
        # provided to Settings (e.g. via constructor argument in tests).
        # This ensures explicit config always wins over the legacy env-var fallback.
        if (
            os.environ.get("CHD_DEBUG", "").lower() == "true"
            and "LOGLEVEL" not in os.environ
            and "log_level" not in getattr(self, "__pydantic_fields_set__", set())
        ):
            self.log_level = "DEBUG"

    @property
    def volumes(self) -> list[str]:
        """Return explicit volume list when set, otherwise auto-discover /data/*."""
        explicit = str(self.chd_volumes).strip()
        if explicit:
            return [v.strip() for v in explicit.split(",") if v.strip()]
        if self._startup_scan_completed:
            return list(self._startup_discovered_volumes)
        return self.discover_data_volumes()

    def scan_data_mounts_on_startup(self) -> list[str]:
        """Capture discovered volumes once during startup for stable runtime behavior."""
        self._startup_discovered_volumes = self.discover_data_volumes()
        self._startup_scan_completed = True
        return list(self._startup_discovered_volumes)

    def discover_data_volumes(self) -> list[str]:
        """Return configured data volumes discovered from data_mount_root.

        Discovery strategy:
        1. Scan direct non-symlink subdirectories under `data_mount_root`.
        2. If one or more entries are mount points, use only mount points.
        3. Otherwise use all direct subdirectories.
        4. If no subdirectories exist, allow `data_mount_root` itself when present.
        """
        root_path = Path(str(self.data_mount_root)).expanduser()
        try:
            resolved_root = root_path.resolve(strict=True)
        except (OSError, RuntimeError):
            return []

        if not resolved_root.is_dir():
            return []

        children: list[Path] = []
        try:
            children = sorted(
                [p for p in resolved_root.iterdir() if not p.is_symlink() and p.is_dir()],
                key=lambda p: p.name.lower(),
            )
        except OSError:
            return []

        mount_children = [p for p in children if os.path.ismount(str(p))]
        selected = mount_children if mount_children else children

        if not selected:
            return [str(resolved_root)]

        return [str(path) for path in selected]

    def get_volume_name(self, path: str) -> str:
        """Extract a friendly name from a volume path."""
        return Path(path.rstrip("/")).name or path


settings = Settings()
