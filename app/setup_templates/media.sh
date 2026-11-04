# ---- Command B: Plex / Jellyfin host (Linux, Proxmox LXC) --------------------------------
# Mounts the bridge's movie and series folders here with rclone, so the media server sees
# them as ordinary folders. A folder that is already mounted and working is left exactly as
# it is. Re-running updates only the mounts this script made; nothing else on the host changes.
#
#   curl -fsSL <this url> | sudo bash                    install, or check what's already there
#   curl -fsSL <this url> | sudo bash -s -- --replace    swap working older mounts for the bridge's own
#   curl -fsSL <this url> | sudo bash -s -- --uninstall
set -euo pipefail

SETUP_URL="$BRIDGE_URL/setup/$SETUP_TOKEN"
UNIT_DIR=/etc/systemd/system
REMOTES=()   # "remote|mount path|bridge folder|unit"
[ "$WANT_MOVIES" = 1 ] && REMOTES+=("vodbridge-movies|$MOVIE_MOUNT|vod|rclone-vod-bridge-movies.service")
[ "$WANT_MOVIES" = 1 ] && REMOTES+=("vodbridge-movies-cat|$MOVIE_CAT_MOUNT|vod-movies|rclone-vod-bridge-movies-cat.service")
[ "$WANT_SERIES" = 1 ] && REMOTES+=("vodbridge-series|$SERIES_MOUNT|vod-series|rclone-vod-bridge-series.service")
ALL_UNITS="rclone-vod-bridge-movies.service rclone-vod-bridge-movies-cat.service rclone-vod-bridge-series.service"
# Our remote names and the bridge folder each one serves. The "-setup" names are spares, used
# when a hand-made unit already uses one of ours.
declare -A FOLDER_OF=([vodbridge-movies]=vod [vodbridge-movies-cat]=vod-movies [vodbridge-series]=vod-series)
OUR_REMOTES="vodbridge-movies vodbridge-movies-cat vodbridge-series vodbridge-movies-setup vodbridge-movies-cat-setup vodbridge-series-setup"

say()  { printf '\033[1m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[33m!!\033[0m  %s\n' "$*" >&2; }
die()  { printf '\033[31mxx\033[0m  %s\n' "$*" >&2; exit 1; }
esc()  { printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'; }

# Questions go to the terminal even when the script itself arrives through a pipe.
ask() {
    local reply
    { exec 3</dev/tty; } 2>/dev/null || return 1
    printf '%s [y/N] ' "$1" >/dev/tty
    read -r reply <&3 || reply=
    exec 3<&-
    case "$reply" in y|Y|yes|YES) return 0 ;; *) return 1 ;; esac
}

REPLACE=0; UNINSTALL=0
for arg in "$@"; do
    case "$arg" in
        --replace|--yes|-y) REPLACE=1 ;;
        --uninstall) UNINSTALL=1 ;;
        *) die "Unknown option: $arg" ;;
    esac
done

[ "$(id -u)" -eq 0 ] || die "Run this as root (pipe it to 'sudo bash')."
if [ "$MEDIA_OS" != linux ]; then
    die "This script is for Linux. The Setup tab shows the steps for $MEDIA_OS instead."
fi
command -v systemctl >/dev/null && [ -d /run/systemd/system ] \
    || die "This host doesn't run systemd. See the manual steps on the Setup tab."

rclone_conf() {
    command -v rclone >/dev/null || { echo /root/.config/rclone/rclone.conf; return; }
    rclone config file 2>/dev/null | tail -n 1
}

# Print rclone.conf ($1) without the named sections; every other remote is left exactly as it is.
strip_remotes() {
    local f=$1; shift
    awk -v drop=" $* " '/^\[/{ n = substr($0, 2, length($0) - 2); skip = index(drop, " " n " ") > 0 } !skip' "$f"
}

# "unit|remote|config file" for every rclone mount unit on this host except ours.
# A unit with no --config uses the default rclone.conf.
foreign_mounts() {
    local f name line remote cfg
    for f in "$UNIT_DIR"/*.service; do
        [ -f "$f" ] || continue
        name=$(basename "$f")
        case " $ALL_UNITS " in *" $name "*) continue ;; esac
        line=$(grep -m1 -E '^ExecStart=.*rclone[^[:space:]]*[[:space:]].*mount[[:space:]]' "$f") || continue
        remote=$(printf '%s' "$line" | sed -nE 's/.*[[:space:]]mount[[:space:]]+([^[:space:]:]+):.*/\1/p')
        [ -n "$remote" ] || continue
        cfg=$(printf '%s' "$line" | sed -nE 's/.*--config[= ]+([^[:space:]]+).*/\1/p')
        echo "$name|$remote|${cfg:-$(rclone_conf)}"
    done
}

# Our remote names that a hand-made unit relies on. These are never removed or rewritten.
foreign_ours() {
    local unit remote cfg
    while IFS='|' read -r unit remote cfg; do
        case " $OUR_REMOTES " in *" $remote "*) echo "$remote" ;; esac
    done < <(foreign_mounts) | sort -u | tr '\n' ' '
}

# Our remote names that are safe to remove: the ones no hand-made unit uses.
droppable() {
    local keep r out=""
    keep=" $(foreign_ours) "
    for r in $OUR_REMOTES; do
        case "$keep" in *" $r "*) ;; *) out+="$r " ;; esac
    done
    echo "$out"
}

has_section() { [ -f "$2" ] && grep -qxF "[$1]" "$2"; }

if [ "$UNINSTALL" = 1 ]; then
    say "Removing the bridge mounts"
    for unit in $ALL_UNITS; do
        systemctl disable --now "$unit" 2>/dev/null || true
        rm -f "$UNIT_DIR/$unit"
    done
    systemctl disable --now vod-bridge-disk-report.timer 2>/dev/null || true
    rm -f "$UNIT_DIR/vod-bridge-disk-report.service" "$UNIT_DIR/vod-bridge-disk-report.timer" \
          /usr/local/bin/vod-bridge-disk-report
    systemctl daemon-reload
    conf=$(rclone_conf)
    if [ -f "$conf" ]; then
        cp -p "$conf" "$conf.bak.$(date +%Y%m%d%H%M%S)"
        kept=$(foreign_ours)
        strip_remotes "$conf" $(droppable) > "$conf.new" && cat "$conf.new" > "$conf" && rm -f "$conf.new"
        [ -z "${kept// }" ] || say "Kept remote(s) ${kept% } in $conf: another rclone unit on this host uses them."
    fi
    say "Done. rclone itself, the empty mount folders and any media-server libraries are left in place."
    exit 0
fi

# ---- Preflight ----
command -v curl >/dev/null || die "curl is missing. Install it and run this again."
curl -fsS --max-time 10 "$BRIDGE_URL/version" >/dev/null \
    || die "Can't reach the bridge at $BRIDGE_URL from this host. Check the address on the Setup tab and any firewall."
if [ ! -e /dev/fuse ]; then
    if [ "$(systemd-detect-virt -c 2>/dev/null || true)" = lxc ]; then
        die "This LXC container has no FUSE device. On the Proxmox host run:
      pct set <container id> --features fuse=1     (keep any features already set, e.g. nesting=1)
    then restart the container and run this again."
    fi
    modprobe fuse 2>/dev/null || true
    [ -e /dev/fuse ] || die "/dev/fuse is missing. Load the fuse kernel module (modprobe fuse) and run this again."
fi

# ---- What's already here ----
# A folder that is mounted and has something in it is kept as is, whoever set it up.
# Earlier guides used hand-made units (rclone-vod.service, rclone-vodbridge*.service).
working() { mountpoint -q "$1" && [ -n "$(ls -A "$1" 2>/dev/null | head -n 1)" ]; }
other_unit() {
    local f name
    for f in "$UNIT_DIR"/*.service; do
        [ -f "$f" ] || continue
        name=$(basename "$f")
        case " $ALL_UNITS " in *" $name "*) continue ;; esac
        grep -q 'rclone mount' "$f" || continue
        grep -qE "[[:space:]]${1//./\\.}/?([[:space:]]|$)" "$f" && { echo "$name"; return; }
    done
}
declare -A UNIT_OF
TODO=()
KEPT_CACHE=()   # remote|cache-dir of hand-made mounts left in place
for entry in "${REMOTES[@]}"; do
    IFS='|' read -r remote path folder unit <<< "$entry"
    UNIT_OF[$path]=$unit
    if [ -f "$UNIT_DIR/$unit" ]; then
        TODO+=("$entry")   # ours: re-running updates it
        continue
    fi
    other=$(other_unit "$path")
    if working "$path" && [ "$REPLACE" = 0 ]; then
        say "$path is already mounted${other:+ by $other} and working. Leaving it as it is."
        if [ -n "$other" ] && grep -q -- '--vfs-cache-mode full' "$UNIT_DIR/$other" \
                && ! grep -q -- '--vfs-cache-max-size' "$UNIT_DIR/$other"; then
            warn "$other has no cache limit, so the Setup tab's limit doesn't apply to it. Re-run with --replace to use the bridge's own mount."
        fi
        if [ -n "$other" ]; then   # its cache still counts on the disk gauge
            line=$(grep -m1 '^ExecStart=' "$UNIT_DIR/$other")
            kept_remote=$(sed -nE 's/.*rclone mount +([^ :]+):.*/\1/p' <<< "$line")
            kept_dir=$(sed -nE 's/.*--cache-dir[ =]+([^ ]+).*/\1/p' <<< "$line")
            [ -n "$kept_remote" ] && KEPT_CACHE+=("$kept_remote|$kept_dir")
        fi
        UNIT_OF[$path]=$other
        continue
    fi
    if [ -n "$other" ]; then
        if working "$path"; then
            warn "Replacing $other, which mounts $path (--replace)."
        else
            warn "$other is meant to mount $path, but that folder isn't working:"
            grep -m1 '^ExecStart=' "$UNIT_DIR/$other" | sed 's/^/      /' >&2
            ask "Replace it with the bridge's own mount?" \
                || die "Left $other as it is. Fix it yourself, or re-run with: | sudo bash -s -- --replace"
        fi
        systemctl disable --now "$other" 2>/dev/null || true
        mv -f "$UNIT_DIR/$other" "$UNIT_DIR/$other.replaced-by-vod-bridge"
        say "Disabled $other (kept as $UNIT_DIR/$other.replaced-by-vod-bridge)"
        systemctl daemon-reload
    fi
    TODO+=("$entry")
done

# ---- Install rclone + FUSE (only when something needs mounting) ----
pkg_install() {
    if command -v apt-get >/dev/null; then
        DEBIAN_FRONTEND=noninteractive apt-get install -y -q "$@" >/dev/null \
            || { apt-get update -q >/dev/null && DEBIAN_FRONTEND=noninteractive apt-get install -y -q "$@" >/dev/null; }
    elif command -v dnf >/dev/null; then dnf install -y -q "$@" >/dev/null
    elif command -v yum >/dev/null; then yum install -y -q "$@" >/dev/null
    elif command -v pacman >/dev/null; then pacman -S --noconfirm --needed "$@" >/dev/null
    elif command -v apk >/dev/null; then apk add -q "$@" >/dev/null
    else return 1
    fi
}
if [ ${#TODO[@]} -gt 0 ]; then
    need=()
    command -v fusermount3 >/dev/null || command -v fusermount >/dev/null || need+=(fuse3)
    if [ ${#need[@]} -gt 0 ]; then
        say "Installing ${need[*]}"
        pkg_install "${need[@]}" || die "Couldn't install ${need[*]}. Install them with your package manager and run this again."
    fi
    if ! command -v rclone >/dev/null; then
        say "Installing rclone"
        curl -fsSL https://rclone.org/install.sh | bash >/dev/null || die "rclone install failed. See https://rclone.org/install/"
    fi

    touch /etc/fuse.conf
    if ! grep -qE '^[[:space:]]*user_allow_other' /etc/fuse.conf; then
        echo user_allow_other >> /etc/fuse.conf
        say "Enabled user_allow_other in /etc/fuse.conf"
    fi
fi
RCLONE=$(command -v rclone || echo rclone)
FUSERMOUNT=$(command -v fusermount3 || command -v fusermount || echo fusermount)
[ ${#TODO[@]} -eq 0 ] || say "Using $($RCLONE version 2>/dev/null | head -n 1)"

# ---- Cache limit ----
# --vfs-cache-mode full keeps one connection per title instead of one per seek, but it stores
# a copy of what is playing on this disk. CACHE_MAX_GB (Settings -> Setup) is shared by the
# mounts; rclone removes the oldest closed copies above it. A title that is still playing is
# never removed, so the cache can go over while it plays. rclone 1.64+ also keeps 5 GB free.
n_mounts=${#REMOTES[@]}; [ "$n_mounts" -gt 0 ] || n_mounts=1
CACHE_PER_MB=$(( (CACHE_MAX_GB * 1024 + n_mounts - 1) / n_mounts ))
CACHE_FLAGS="--vfs-cache-max-size ${CACHE_PER_MB}M"
min_free=false
rv=$($RCLONE version 2>/dev/null | head -n 1 | sed -nE 's/.*v([0-9]+)\.([0-9]+).*/\1 \2/p') || rv=""
if [ -n "$rv" ]; then
    read -r rmaj rmin <<< "$rv"
    if [ "$rmaj" -gt 1 ] || { [ "$rmaj" -eq 1 ] && [ "$rmin" -ge 64 ]; }; then
        CACHE_FLAGS+=" --vfs-cache-min-free-space 5G"
        min_free=true
    fi
fi
if [ ${#TODO[@]} -gt 0 ]; then
    say "rclone cache limit: $CACHE_MAX_GB GB across the bridge mounts (change it in Settings -> Setup, then run this again)"
    $min_free || warn "This rclone is older than 1.64, so it can't keep free disk space in reserve. Update it: curl https://rclone.org/install.sh | sudo bash"
fi

# ---- rclone remotes (only the folders this script mounts) ----
# Our remotes are rewritten. A remote that a hand-made unit uses is never removed: if it has
# one of our names, our own mount takes the spare "-setup" name instead. Before 0.45.1 this
# step deleted such a remote, and that unit then failed from its next start (e.g. a reboot).
conf=$(rclone_conf)
in_use=" $(foreign_ours) "
for i in "${!TODO[@]}"; do
    IFS='|' read -r remote path folder unit <<< "${TODO[$i]}"
    case "$in_use" in
        *" $remote "*)
            say "Another rclone unit uses the remote name $remote; the bridge's mount of $path uses $remote-setup."
            TODO[$i]="$remote-setup|$path|$folder|$unit" ;;
    esac
done

# Repair: a hand-made unit whose bridge remote is missing from rclone.conf (an older version
# of this script removed it). It is the same bridge folder, so the section is put back.
REPAIR=()
while IFS='|' read -r ounit oremote ocfg; do
    [ -n "${FOLDER_OF[$oremote]:-}" ] || continue
    has_section "$oremote" "$ocfg" && continue
    if [ "$ocfg" = "$conf" ]; then
        warn "$ounit uses the remote $oremote, which is missing from $conf. Putting it back."
        REPAIR+=("$oremote|$ounit")
    else
        warn "$ounit uses the remote $oremote, which is missing from $ocfg. Add this to that file:"
        printf '      [%s]\n      type = http\n      url = %s/%s/\n' "$oremote" "$BRIDGE_URL" "${FOLDER_OF[$oremote]}" >&2
    fi
done < <(foreign_mounts)

if [ ${#TODO[@]} -gt 0 ] || [ ${#REPAIR[@]} -gt 0 ]; then
mkdir -p "$(dirname "$conf")"
touch "$conf"
chmod 600 "$conf"
cp -p "$conf" "$conf.bak.$(date +%Y%m%d%H%M%S)"
{
    strip_remotes "$conf" $(droppable)
    for entry in "${TODO[@]}"; do
        IFS='|' read -r remote path folder unit <<< "$entry"
        printf '\n[%s]\ntype = http\nurl = %s/%s/\n' "$remote" "$BRIDGE_URL" "$folder"
    done
    for entry in "${REPAIR[@]}"; do
        IFS='|' read -r remote unit <<< "$entry"
        printf '\n[%s]\ntype = http\nurl = %s/%s/\n' "$remote" "$BRIDGE_URL" "${FOLDER_OF[$remote]}"
    done
} > "$conf.new"
cat "$conf.new" > "$conf"
rm -f "$conf.new"
say "Wrote the bridge remotes to $conf (backup saved next to it)"
fi
# A repaired unit that is failing picks the section up when it starts again.
for entry in "${REPAIR[@]}"; do
    IFS='|' read -r remote unit <<< "$entry"
    if ! systemctl is-active -q "$unit"; then
        systemctl reset-failed "$unit" 2>/dev/null || true
        systemctl restart "$unit" || warn "$unit didn't start: journalctl -u $unit -n 30"
    fi
done

# ---- systemd mounts ----
for entry in "${TODO[@]}"; do
    IFS='|' read -r remote path folder unit <<< "$entry"
    if mountpoint -q "$path" && ! systemctl is-active -q "$unit"; then
        "$FUSERMOUNT" -uz "$path" 2>/dev/null || umount -l "$path" 2>/dev/null || true
    fi
    mkdir -p "$path"
    cat > "$UNIT_DIR/$unit" <<EOF
[Unit]
Description=VOD Plex Bridge $folder mount (rclone)
After=network-online.target
Wants=network-online.target

[Service]
Type=notify
ExecStartPre=-/bin/mkdir -p $path
ExecStart=$RCLONE mount $remote: $path --config $conf --allow-other --read-only --dir-cache-time 1m --vfs-cache-mode full --vfs-cache-max-age 10m $CACHE_FLAGS --timeout 30s
ExecStop=$FUSERMOUNT -uz $path
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload
    systemctl enable -q "$unit"
    systemctl restart "$unit" || warn "$unit didn't start: journalctl -u $unit -n 30"
done
# A folder the wizard no longer wants (e.g. series turned off) shouldn't stay mounted.
for unit in $ALL_UNITS; do
    case " ${TODO[*]} " in *"|$unit"*) continue ;; esac
    if [ -f "$UNIT_DIR/$unit" ]; then
        systemctl disable --now "$unit" 2>/dev/null || true
        rm -f "$UNIT_DIR/$unit"
        systemctl daemon-reload
    fi
done

# ---- Disk report ----
# Every 5 minutes this machine tells the bridge its free disk space and how much the bridge
# mounts' rclone cache uses, for the gauge on the Health and Setup tabs. Nothing else is sent.
CACHE_DIR=$($RCLONE config paths 2>/dev/null | sed -n 's/^Cache dir:[[:space:]]*//p') || CACHE_DIR=""
CACHE_DIR=${CACHE_DIR:-/root/.cache/rclone}
mkdir -p "$CACHE_DIR"
# Counts the bridge's own mounts and any hand-made bridge mount left in place.
cache_dirs=""
for entry in "${TODO[@]}"; do IFS='|' read -r remote _ <<< "$entry"; cache_dirs+="$CACHE_DIR/vfs/$remote "; done
for entry in "${KEPT_CACHE[@]}"; do
    IFS='|' read -r remote dir <<< "$entry"
    cache_dirs+="${dir:-$CACHE_DIR}/vfs/$remote "
done
{
    echo '#!/bin/sh'
    echo '# Written by VOD Plex Bridge Command B: sends disk space and rclone cache use to the bridge.'
    printf 'CACHE_DIR=%q\nREPORT_URL=%q\nMACHINE_ID=%q\nCACHE_DIRS=%q\nCACHE_MAX=%s\n' \
        "$CACHE_DIR" "$SETUP_URL/report" "$MACHINE_ID" "${cache_dirs% }" "$((CACHE_MAX_GB * 1073741824))"
    cat <<'EOF'
set -- $(df -Pk "$CACHE_DIR" | awk 'NR == 2 {print $2, $4}')
used=0
for d in $CACHE_DIRS; do
    [ -d "$d" ] && used=$((used + $(du -sk "$d" | cut -f1)))
done
curl -fsS --max-time 20 -H 'Content-Type: application/json' -X POST "$REPORT_URL" \
    -d "{\"kind\": \"disk\", \"machine\": \"$MACHINE_ID\", \"total\": $(($1 * 1024)), \"free\": $(($2 * 1024)), \"cache\": $((used * 1024)), \"cache_max\": $CACHE_MAX}" >/dev/null
EOF
} > /usr/local/bin/vod-bridge-disk-report
chmod 755 /usr/local/bin/vod-bridge-disk-report
cat > "$UNIT_DIR/vod-bridge-disk-report.service" <<'EOF'
[Unit]
Description=VOD Plex Bridge disk report
After=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/local/bin/vod-bridge-disk-report
EOF
cat > "$UNIT_DIR/vod-bridge-disk-report.timer" <<'EOF'
[Unit]
Description=VOD Plex Bridge disk report every 5 minutes

[Timer]
OnBootSec=2min
OnUnitActiveSec=5min

[Install]
WantedBy=timers.target
EOF
systemctl daemon-reload
systemctl enable -q --now vod-bridge-disk-report.timer || warn "The disk report timer didn't start"
/usr/local/bin/vod-bridge-disk-report || warn "Could not send disk space to the bridge"

# ---- Verify ----
read_as=""
# Each server runs as a user named after it; try the ones on this machine first.
for u in $MEDIA_SERVERS plex jellyfin emby; do id "$u" >/dev/null 2>&1 && { read_as=$u; break; }; done
ok=true
checks=""
for entry in "${REMOTES[@]}"; do
    IFS='|' read -r remote path folder unit <<< "$entry"
    unit=${UNIT_OF[$path]}
    for _ in $(seq 1 20); do mountpoint -q "$path" && break; sleep 1; done
    count=-1; readable=false
    if mountpoint -q "$path"; then
        count=$(ls -1 "$path" 2>/dev/null | wc -l) || count=0
        if [ -n "$read_as" ]; then
            runuser -u "$read_as" -- ls "$path" >/dev/null 2>&1 && readable=true
        else
            readable=true
        fi
    fi
    if [ "$count" -ge 0 ] && [ "$readable" = true ]; then
        say "$path is mounted: $count items${read_as:+, readable as $read_as}"
    else
        ok=false
        warn "$path isn't working.${unit:+ Check: systemctl status $unit}"
    fi
    checks+="{\"path\": \"$(esc "$path")\", \"unit\": \"$unit\", \"items\": $count, \"readable\": $readable},"
done

# A running mount keeps working after its remote is removed from rclone.conf and fails only
# when it next starts (often at the next reboot), so check each unit's config directly too.
while IFS='|' read -r ounit oremote ocfg; do
    systemctl is-enabled -q "$ounit" 2>/dev/null || continue
    if ! has_section "$oremote" "$ocfg"; then
        ok=false
        warn "$ounit mounts the remote $oremote, but $ocfg has no [$oremote] section. It will fail when it next starts."
    fi
done < <(foreign_mounts; for entry in "${TODO[@]}"; do
    IFS='|' read -r remote path folder unit <<< "$entry"; echo "$unit|$remote|$conf"; done)

docker_lines=""
docker_ok=null
if [ "$MEDIA_DOCKER" = 1 ]; then
    for entry in "${REMOTES[@]}"; do
        IFS='|' read -r remote path folder unit <<< "$entry"
        docker_lines+="      - $path:$path:ro,rslave"$'\n'
    done
    say "Your media server runs in Docker. Add these lines under its volumes:, then recreate the container:"
    printf '%s' "$docker_lines"
    say "(Recreate it after the mounts are up; a container started earlier sees empty folders.)"
    if [ -n "$MEDIA_CONTAINER" ] && command -v docker >/dev/null && docker inspect "$MEDIA_CONTAINER" >/dev/null 2>&1; then
        docker_ok=true
        for entry in "${REMOTES[@]}"; do
            IFS='|' read -r remote path folder unit <<< "$entry"
            # The category folder stays empty until the first movie category exists.
            if docker exec "$MEDIA_CONTAINER" ls "$path" >/dev/null 2>&1 && { [ "$folder" = vod-movies ] \
                    || [ "$(docker exec "$MEDIA_CONTAINER" ls -1 "$path" 2>/dev/null | wc -l)" -gt 0 ]; }; then
                say "$MEDIA_CONTAINER can see $path"
            else
                docker_ok=false
                warn "$MEDIA_CONTAINER can't see $path yet. Add the volume line above and recreate the container."
            fi
        done
    fi
fi

# ---- Rclone Manager (optional web page for the mounts) ----
rcm=false
if systemctl is-active -q rclone-mount-manager.service; then
    # Reported even when not asked for, so the Setup tab can link to it.
    say "Rclone Manager is already running"
    rcm=true
elif [ "$RCLONE_MANAGER" = 1 ]; then
    if ! command -v python3 >/dev/null && ! pkg_install python3; then
        warn "Rclone Manager needs python3; skipped."
    elif ! command -v unzip >/dev/null && ! pkg_install unzip; then
        warn "Rclone Manager needs unzip; skipped."
    else
        say "Installing Rclone Manager"
        zip_url=$(curl -fsSL --max-time 20 https://api.github.com/repos/knmplace/rclone-manager/releases/latest \
                  | grep -o '"browser_download_url": *"[^"]*\.zip"' | head -n 1 | sed 's/.*"\(http[^"]*\)"$/\1/') || zip_url=""
        work=$(mktemp -d)
        # The release zip uses Windows-style paths; unzip fixes them but exits 1 with a warning.
        [ -n "$zip_url" ] && curl -fsSL "$zip_url" -o "$work/rcm.zip" \
            && { unzip -qo "$work/rcm.zip" -d "$work/rcm" >/dev/null 2>&1 || true; }
        if [ -f "$work/rcm/install.sh" ]; then
            pass=""
            if { exec 3</dev/tty; } 2>/dev/null; then
                printf 'Choose a password for the Rclone Manager page (user admin, blank = ChangeMeNow!): ' >/dev/tty
                read -rs pass <&3 || pass=""
                exec 3<&-
                echo >/dev/tty
            fi
            if RCLONE_MANAGER_PORT=5573 RCLONE_MANAGER_USER=admin RCLONE_MANAGER_PASS="${pass:-ChangeMeNow!}" \
                    bash -c 'cd "$1" && bash install.sh' _ "$work/rcm" >/dev/null 2>&1; then
                rcm=true
                say "Rclone Manager is at http://$(hostname -I 2>/dev/null | awk '{print $1}'):5573 (user admin)"
                [ -n "$pass" ] || warn "Change its password now: it is still the default, ChangeMeNow!"
            else
                warn "Rclone Manager's installer failed; the mounts are fine without it."
            fi
        else
            warn "Couldn't download Rclone Manager; the mounts are fine without it."
        fi
        rm -rf "$work"
    fi
fi

# ---- Report back to the bridge ----
os_name=$(. /etc/os-release 2>/dev/null && echo "${PRETTY_NAME:-linux}" || echo linux)
curl -fsS --max-time 20 -H 'Content-Type: application/json' -X POST "$SETUP_URL/report" -d "{
  \"kind\": \"media\", \"machine\": \"$MACHINE_ID\", \"ok\": $ok, \"hostname\": \"$(esc "$(hostname)")\", \"os\": \"$(esc "$os_name")\",
  \"virt\": \"$(esc "$(systemd-detect-virt 2>/dev/null || echo none)")\",
  \"rclone\": \"$(esc "$($RCLONE version 2>/dev/null | head -n 1)")\", \"read_as\": \"$read_as\",
  \"mounts\": [${checks%,}], \"docker_ok\": $docker_ok, \"rclone_manager\": $rcm,
  \"cache_gb\": $CACHE_MAX_GB, \"cache_min_free\": $min_free
}" >/dev/null || warn "Could not send the result to the bridge"

$ok || die "Some mounts aren't working; see the messages above."
say "All set. Go back to the Setup tab to create the libraries."
