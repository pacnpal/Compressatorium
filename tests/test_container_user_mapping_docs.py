import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def _read_repo_file(relative_path: str) -> str:
    return (REPO_ROOT / relative_path).read_text(encoding="utf-8")


def test_dockerfile_uses_gosu_and_no_static_user_directive():
    dockerfile = _read_repo_file("Dockerfile")

    assert re.search(
        r'apt-get install -y --no-install-recommends[\s\S]*?\bgosu\b',
        dockerfile,
    )
    assert re.search(r'HEALTHCHECK[\s\S]*CHD_MODE:-webui', dockerfile)
    assert 'if [ "$(/usr/bin/id -u)" = "0" ]; then' in dockerfile
    assert re.search(r'HEALTHCHECK[\s\S]*CMD[\s\S]*/usr/sbin/gosu\s+converter\s+/opt/venv/bin/python3\s+-c', dockerfile)
    assert re.search(r'HEALTHCHECK[\s\S]*else[\s\S]*/opt/venv/bin/python3\s+-c', dockerfile)
    assert "urllib.request.urlopen('http://localhost:8080/health')" in dockerfile
    assert re.search(r'HEALTHCHECK[\s\S]*\|\|\s+exit\s+0', dockerfile) is None
    assert re.search(r'groupadd\s+-r\s+-g\s+999\s+converter', dockerfile)
    assert re.search(r'useradd\s+-r\s+-u\s+999\s+-g\s+converter', dockerfile)
    assert "ENTRYPOINT [\"/entrypoint.sh\"]" in dockerfile
    assert re.search(r"^\s*USER\s+converter\s*$", dockerfile, flags=re.MULTILINE) is None


def test_entrypoint_remaps_uid_gid_before_dropping_privileges():
    entrypoint = _read_repo_file("entrypoint.sh")

    assert re.search(r'if\s+\[\s*"\$\(/usr/bin/id -u\)"\s*=\s*"0"\s*\]\s*;\s*then', entrypoint)
    assert re.search(r'PUID=\$\{PUID:-\d+\}', entrypoint)
    assert re.search(r'PGID=\$\{PGID:-\d+\}', entrypoint)
    assert re.search(r'\[\s*"\$PUID"\s*-eq\s*0\s*\]', entrypoint)
    assert re.search(r'\[\s*"\$PGID"\s*-eq\s*0\s*\]', entrypoint)
    assert re.search(r'Both must be numeric and greater than 0', entrypoint)
    assert re.search(r'/usr/sbin/groupmod\s+-g\s+"\$PGID"\s+converter', entrypoint)
    assert re.search(r'/usr/bin/getent\s+group\s+"\$PGID"\s+>/dev/null', entrypoint)
    assert re.search(r'Failed to remap converter to PGID', entrypoint)
    assert re.search(r'/usr/bin/getent\s+passwd\s+"\$PUID"\s+>/dev/null', entrypoint)
    assert re.search(r'Cannot remap converter to PUID', entrypoint)
    assert re.search(r'/usr/sbin/usermod\s+-g\s+"\$PGID"\s+converter', entrypoint)
    assert re.search(r'/usr/sbin/usermod\s+-u\s+"\$PUID"\s+converter', entrypoint)
    assert re.search(r'for\s+optional_path\s+in\s+/config\s+/data/games;\s+do', entrypoint)
    assert re.search(r'skip_optional_path=0', entrypoint)
    assert re.search(r'/usr/bin/findmnt\s+-n\s+-o\s+OPTIONS\s+--target\s+"\$optional_path"', entrypoint)
    assert re.search(r'Warning: unable to determine mount options for', entrypoint)
    assert re.search(r'echo\s+"\$mount_opts"\s+\|\s+/usr/bin/grep\s+-Eqw\s+\'bind\|rbind\'', entrypoint)
    assert re.search(r'\[\s*"\$skip_optional_path"\s+-eq\s+0\s*\]', entrypoint)
    assert re.search(r'/usr/bin/chown\s+-R\s+converter:"\$\(\s*/usr/bin/id -g converter\s*\)"\s+"\$\{paths_to_chown\[@\]\}"', entrypoint)
    assert re.search(r'unset\s+PUID\s+PGID', entrypoint)
    assert re.search(r'exec\s+/usr/sbin/gosu\s+converter\s+"\$0"\s+"\$@"', entrypoint)
    assert re.search(
        r'elif\s+\[\s+-n\s+"\$\{PUID:-\}"\s+\]\s+\|\|\s+\[\s+-n\s+"\$\{PGID:-\}"\s+\]\s*;\s+then',
        entrypoint,
    )
    assert re.search(r'PUID/PGID remap requires container startup as root', entrypoint)

    assert entrypoint.index("/usr/sbin/groupmod") < entrypoint.index("/usr/sbin/usermod -u")
    assert entrypoint.index("/usr/sbin/usermod -u") < entrypoint.index("/usr/bin/chown -R")
    assert entrypoint.index("/usr/bin/chown -R") < entrypoint.index("exec /usr/sbin/gosu converter")


def test_root_mode_commands_bypass_converter_writable_path():
    dockerfile = _read_repo_file("Dockerfile")
    entrypoint = _read_repo_file("entrypoint.sh")

    assert 'ENV PATH="/opt/venv/bin:$PATH"' in dockerfile
    assert re.search(
        r'HEALTHCHECK[\s\S]*if \[ "\$\(/usr/bin/id -u\)" = "0" \]; then[\s\S]*?/usr/sbin/gosu\s+converter\s+/opt/venv/bin/python3',
        dockerfile,
    )
    assert re.search(r'HEALTHCHECK[\s\S]*\$\(id -u\)', dockerfile) is None
    assert re.search(r'HEALTHCHECK[\s\S]*\bgosu\s+converter\s+python3\b', dockerfile) is None

    root_block = entrypoint.split('elif [ -n "${PUID:-}"', maxsplit=1)[0]
    for command in (
        '/usr/bin/id',
        '/usr/sbin/groupmod',
        '/usr/bin/getent',
        '/usr/sbin/usermod',
        '/usr/bin/mountpoint',
        '/usr/bin/findmnt',
        '/usr/bin/grep',
        '/usr/bin/chown',
        '/usr/sbin/gosu',
    ):
        assert command in root_block

    assert re.search(r'(?<!/)(\$\(|!\s+|exec\s+|\|\s+|^\s*)(id|groupmod|getent|usermod|mountpoint|findmnt|grep|chown|gosu)\b', root_block, flags=re.MULTILINE) is None
