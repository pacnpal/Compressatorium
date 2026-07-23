#!/bin/bash
set -e

# Remap converter UID/GID to PUID/PGID then drop privileges
if [ "$(/usr/bin/id -u)" = "0" ]; then
    PUID=${PUID:-999}
    PGID=${PGID:-999}
    ownership_changed=0

    if ! [[ "$PUID" =~ ^[0-9]+$ && "$PGID" =~ ^[0-9]+$ ]] || [ "$PUID" -eq 0 ] || [ "$PGID" -eq 0 ]; then
        echo "Invalid PUID/PGID. Both must be numeric and greater than 0." >&2
        exit 1
    fi

    if [ "$(/usr/bin/id -g converter)" != "$PGID" ]; then
        if ! groupmod_error="$(/usr/sbin/groupmod -g "$PGID" converter 2>&1)"; then
            if /usr/bin/getent group "$PGID" >/dev/null; then
                echo "GID $PGID already exists; assigning converter to the existing group."
                /usr/sbin/usermod -g "$PGID" converter
            else
                echo "Failed to remap converter to PGID $PGID: $groupmod_error" >&2
                exit 1
            fi
        fi
        ownership_changed=1
    fi
    if [ "$(/usr/bin/id -u converter)" != "$PUID" ]; then
        if /usr/bin/getent passwd "$PUID" >/dev/null; then
            echo "Cannot remap converter to PUID $PUID: UID already exists." >&2
            exit 1
        fi
        /usr/sbin/usermod -u "$PUID" converter
        ownership_changed=1
    fi

    if [ "$ownership_changed" = "1" ]; then
        paths_to_chown=(/app /static /opt/venv)
        for optional_path in /config /data/games; do
            skip_optional_path=0
            if /usr/bin/mountpoint -q "$optional_path" 2>/dev/null; then
                mount_opts="$(/usr/bin/findmnt -n -o OPTIONS --target "$optional_path" 2>/dev/null || true)"
                if [ -z "$mount_opts" ]; then
                    echo "Warning: unable to determine mount options for $optional_path (findmnt failed); skipping ownership update to be safe." >&2
                    skip_optional_path=1
                elif echo "$mount_opts" | /usr/bin/grep -Eqw 'bind|rbind'; then
                    skip_optional_path=1
                fi
            fi

            if [ -e "$optional_path" ] && [ "$skip_optional_path" -eq 0 ]; then
                paths_to_chown+=("$optional_path")
            fi
        done
        /usr/bin/chown -R converter:"$(/usr/bin/id -g converter)" "${paths_to_chown[@]}"
    fi
    unset PUID PGID
    exec /usr/sbin/gosu converter "$0" "$@"
elif [ -n "${PUID:-}" ] || [ -n "${PGID:-}" ]; then
    echo "Warning: PUID/PGID remap requires container startup as root; current UID is $(/usr/bin/id -u). Ignoring PUID/PGID." >&2
fi

# Check if /config is mounted
if ! mountpoint -q /config 2>/dev/null; then
    echo ""
    echo "┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓"
    echo "┃  🚨 WHOOPS! You forgot to mount /config!                                ┃"
    echo "┃                                                                         ┃"
    echo "┃  Your verification data is going to disappear when this container       ┃"
    echo "┃  restarts. Like tears in rain. Gone. Poof. Vanished.                    ┃"
    echo "┃                                                                         ┃"
    echo "┃  Fix it:  -v /path/to/config:/config                                    ┃"
    echo "┃                                                                         ┃"
    echo "┃  Docs: https://github.com/pacnpal/Compressatorium                      ┃"
    echo "┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛"
    echo ""
fi

# CHD_MODE: "webui" (default) or "cli"
MODE="${CHD_MODE:-webui}"
MOUNT_ROOT="${COMPRESSATORIUM_MOUNT_ROOT:-${CHD_MOUNT_ROOT:-/data}}"
EXPLICIT_VOLUMES="${COMPRESSATORIUM_VOLUMES:-${CHD_VOLUMES:-}}"
VOLUMES_EFFECTIVE=""

discover_volumes() {
    local root="$1"
    local explicit="$2"
    local -a dirs=()
    local -a mount_dirs=()
    local -a selected=()

    if [[ -n "$explicit" ]]; then
        echo "$explicit"
        return
    fi

    if [[ -d "$root" ]]; then
        while IFS= read -r dir; do
            dirs+=("$dir")
            if mountpoint -q "$dir" 2>/dev/null; then
                mount_dirs+=("$dir")
            fi
        done < <(find "$root" -mindepth 1 -maxdepth 1 -type d | sort)

        if [[ ${#mount_dirs[@]} -gt 0 ]]; then
            selected=("${mount_dirs[@]}")
        elif [[ ${#dirs[@]} -gt 0 ]]; then
            selected=("${dirs[@]}")
        else
            selected=("$root")
        fi
    fi

    local out=""
    local v
    for v in "${selected[@]}"; do
        if [[ -z "$v" ]]; then
            continue
        fi
        if [[ -n "$out" ]]; then
            out="$out,$v"
        else
            out="$v"
        fi
    done
    echo "$out"
}

VOLUMES_EFFECTIVE="$(discover_volumes "$MOUNT_ROOT" "$EXPLICIT_VOLUMES")"

if [[ "$MODE" == "cli" ]]; then
    # Original CLI behavior - convert all files in volumes
    CHDMAN_MODE="${CHDMAN_MODE:-createcd}"

    case "$CHDMAN_MODE" in
        cd)  CHDMAN_MODE="createcd"  ;;
        dvd) CHDMAN_MODE="createdvd" ;;
        createcd|createdvd) ;;
        *) echo "Unsupported CHDMAN_MODE: '$CHDMAN_MODE'. Use 'createcd' or 'createdvd'." >&2; exit 1 ;;
    esac

    echo "Running in CLI mode with CHDMAN_MODE=$CHDMAN_MODE"
    echo "Volume root: $MOUNT_ROOT"
    if [[ -n "$EXPLICIT_VOLUMES" ]]; then
        echo "Using explicit COMPRESSATORIUM_VOLUMES: $VOLUMES_EFFECTIVE"
    else
        echo "Discovered volumes: $VOLUMES_EFFECTIVE"
    fi

    IFS=',' read -r -a volume_list <<< "$VOLUMES_EFFECTIVE"
    for volume in "${volume_list[@]}"; do
        # Trim incidental whitespace around comma-separated entries.
        volume="${volume#"${volume%%[![:space:]]*}"}"
        volume="${volume%"${volume##*[![:space:]]}"}"
        [[ -z "$volume" ]] && continue
        if [[ -d "$volume" ]]; then
            echo "Processing volume: $volume"
            cd "$volume"
            shopt -s nullglob
            for i in *.gdi *.iso *.cue; do
                [[ -e "$i" ]] || continue
                [[ -e "${i%.*}.chd" ]] && { echo "Skipping '$i' (CHD exists)."; continue; }
                echo "Converting '$i' using chdman ${CHDMAN_MODE} ..."
                if [[ "$CHDMAN_MODE" == "createdvd" ]]; then
                    chdman createdvd -hs 2048 -f -i "$i" -o "${i%.*}.chd"
                else
                    chdman createcd -f -i "$i" -o "${i%.*}.chd"
                fi
            done
        else
            echo "Warning: Volume '$volume' does not exist, skipping."
        fi
    done
    echo "CLI conversion complete."
else
    # Web UI mode (default)
    echo "Starting CHD Converter Web UI..."
    echo "Volume root: $MOUNT_ROOT"
    if [[ -n "$EXPLICIT_VOLUMES" ]]; then
        echo "Using explicit COMPRESSATORIUM_VOLUMES: $VOLUMES_EFFECTIVE"
    else
        echo "Discovered volumes: $VOLUMES_EFFECTIVE"
    fi
    echo "Access the web interface at http://localhost:8080"
    exec uvicorn main:app --host 0.0.0.0 --port 8080 --workers 1
fi
