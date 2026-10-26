# ---- Command A: Dispatcharr host -------------------------------------------------------
# Installs a small agent that sends Dispatcharr's category/stream lists to the bridge:
# every minute it asks whether the bridge wants a fresh copy (Run Now, Daily Run), and every
# 12 hours it sends one anyway. Nothing on this host talks to the provider.
#
#   curl -fsSL <this url> | sudo bash                 install or update
#   curl -fsSL <this url> | sudo bash -s -- --uninstall
set -euo pipefail

INSTALL_DIR=/opt/vod-plex-bridge
CRON_FILE=/etc/cron.d/vod-plex-bridge
LOG_FILE=/var/log/vod-plex-bridge-agent.log
SETUP_URL="$BRIDGE_URL/setup/$SETUP_TOKEN"

say()  { printf '\033[1m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[33m!!\033[0m  %s\n' "$*" >&2; }
die()  { printf '\033[31mxx\033[0m  %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "Run this as root (pipe it to 'sudo bash')."

if [ "${1:-}" = "--uninstall" ]; then
    say "Removing the VOD Plex Bridge agent"
    rm -f "$CRON_FILE"
    rm -rf "$INSTALL_DIR"
    say "Done. The bridge keeps its last dump; Run Now will warn that the dump is stale."
    exit 0
fi

# ---- Preflight ----
command -v curl >/dev/null || die "curl is missing. Install it and run this again."
command -v docker >/dev/null || die "Docker is missing. Run this on the machine where Dispatcharr's container runs."
docker info >/dev/null 2>&1 || die "Docker is installed but not answering. Is the Docker service running?"
curl -fsS --max-time 10 "$BRIDGE_URL/version" >/dev/null \
    || die "Can't reach the bridge at $BRIDGE_URL. Check the address on the Setup tab and any firewall."

if [ -n "$DISPATCHARR_CONTAINER" ]; then
    docker inspect "$DISPATCHARR_CONTAINER" >/dev/null 2>&1 \
        || die "No container named '$DISPATCHARR_CONTAINER' here. Fix the name on the Setup tab, or leave it blank to detect it."
else
    found=$(docker ps --format '{{.Names}}' | grep -i dispatcharr || true)
    [ -n "$found" ] || die "No running Dispatcharr container found. Enter its name on the Setup tab."
    if [ "$(printf '%s\n' "$found" | wc -l)" -gt 1 ]; then
        die "More than one Dispatcharr container is running ($(echo $found)). Enter the right one on the Setup tab."
    fi
    DISPATCHARR_CONTAINER=$found
fi
say "Dispatcharr container: $DISPATCHARR_CONTAINER"

if [ ! -d /etc/cron.d ] || ! { pgrep -x cron || pgrep -x crond || pgrep -f '/usr/sbin/cron'; } >/dev/null 2>&1; then
    warn "No cron service is running here, so the agent won't run on its own."
    warn "Install and start cron (e.g. apt install cron), then run this again."
fi

# ---- Install ----
say "Installing to $INSTALL_DIR"
mkdir -p "$INSTALL_DIR"
curl -fsS --max-time 30 "$SETUP_URL/dump_mappings.sh" -o "$INSTALL_DIR/dump_mappings.sh.new"
mv -f "$INSTALL_DIR/dump_mappings.sh.new" "$INSTALL_DIR/dump_mappings.sh"
chmod 755 "$INSTALL_DIR/dump_mappings.sh"

umask 077
cat > "$INSTALL_DIR/agent.conf" <<EOF
BRIDGE_URL=$(printf %q "$BRIDGE_URL")
SETUP_TOKEN=$(printf %q "$SETUP_TOKEN")
DISPATCHARR_CONTAINER=$(printf %q "$DISPATCHARR_CONTAINER")
EOF
umask 022

cat > "$INSTALL_DIR/agent.sh" <<'AGENT'
#!/usr/bin/env bash
# VOD Plex Bridge agent. "poll": dump only if the bridge asked for one. "dump": always.
set -uo pipefail
DIR=$(cd "$(dirname "$0")" && pwd)
. "$DIR/agent.conf"
SETUP_URL="$BRIDGE_URL/setup/$SETUP_TOKEN"
LOG=/var/log/vod-plex-bridge-agent.log
[ -f "$LOG" ] && [ "$(stat -c %s "$LOG" 2>/dev/null || echo 0)" -gt 1048576 ] && : > "$LOG"

# Older installs used hand-made cron lines that also run the dump. A line counts if it
# names the dump itself, or runs a wrapper script that does.
old_crons() {
    local re='dump_mappings\.sh|vod-bridge-dump|\.dump-trigger' line word hit words
    while IFS= read -r line; do
        hit=false
        if grep -qE "$re" <<<"$line"; then
            hit=true
        else
            read -ra words <<<"$line"
            for word in "${words[@]}"; do
                if [[ $word == /* && -f $word ]] && grep -qE "$re" "$word" 2>/dev/null; then
                    hit=true; break
                fi
            done
        fi
        $hit && printf '%s\n' "$line"
    done < <({ crontab -l 2>/dev/null; cat /etc/crontab 2>/dev/null; } | grep -v '^#' | grep -v '^[[:space:]]*$')
}
if [ "${1:-}" = "old-crons" ]; then old_crons; exit 0; fi

exec 9>"$DIR/.lock"
flock -n 9 || exit 0   # a dump is already running

if [ "${1:-poll}" = "poll" ]; then
    # Sent each minute so the Setup tab's old-cron note clears once those lines are removed.
    old=0; [ -n "$(old_crons)" ] && old=1
    reply=$(curl -fsS --max-time 20 "$SETUP_URL/dump-trigger?old_cron=$old") || { echo "$(date): bridge not reachable"; exit 1; }
    case "$reply" in *'"pending":true'*) ;; *) exit 0 ;; esac
    echo "$(date): bridge asked for a dump"
fi

# Use the dump script that matches the bridge's current version.
curl -fsS --max-time 30 "$SETUP_URL/dump_mappings.sh" -o "$DIR/dump_mappings.sh.new" \
    && mv -f "$DIR/dump_mappings.sh.new" "$DIR/dump_mappings.sh"
out=$(mktemp -d)
trap 'rm -rf "$out"' EXIT
DISPATCHARR_CONTAINER="$DISPATCHARR_CONTAINER" BRIDGE_DATA_DIR="$out" bash "$DIR/dump_mappings.sh" || {
    echo "$(date): dump failed"; exit 1; }
args=()
for f in stream_mapping.json account_credentials.json series_category_mapping.json movie_metadata.json category_mapping.json; do
    [ -s "$out/$f" ] && args+=(-F "$f=@$out/$f;type=application/json")
done
[ ${#args[@]} -gt 0 ] || { echo "$(date): dump produced no files"; exit 1; }
curl -fsS --max-time 300 "${args[@]}" "$SETUP_URL/dump" && echo && echo "$(date): dump sent"
AGENT
chmod 755 "$INSTALL_DIR/agent.sh"

cat > "$CRON_FILE" <<EOF
# VOD Plex Bridge agent (installed from the bridge's Setup tab). Remove with --uninstall.
SHELL=/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
* * * * * root $INSTALL_DIR/agent.sh poll >>$LOG_FILE 2>&1
0 */12 * * * root $INSTALL_DIR/agent.sh dump >>$LOG_FILE 2>&1
EOF
chmod 644 "$CRON_FILE"
say "Scheduled: check every minute, full dump every 12 hours ($CRON_FILE)"

# Older installs used hand-made cron lines; leave them alone but say so.
old=$("$INSTALL_DIR/agent.sh" old-crons)
if [ -n "$old" ]; then
    warn "These older cron lines also run the dump. They're no longer needed; remove them with 'crontab -e'."
    warn "The note on the Setup tab clears by itself within a minute once they're gone:"
    sed 's/^/      /' <<<"$old" >&2
fi

# ---- First dump + report ----
say "Running the first dump (this can take a minute)"
ok=true
if "$INSTALL_DIR/agent.sh" dump | tee -a "$LOG_FILE"; then
    say "Dump sent to the bridge"
else
    ok=false
    warn "The first dump failed; see the output above and $LOG_FILE"
fi

esc() { printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'; }
curl -fsS --max-time 20 -H 'Content-Type: application/json' -X POST "$SETUP_URL/report" -d "{
  \"kind\": \"agent\", \"ok\": $ok, \"hostname\": \"$(esc "$(hostname)")\",
  \"container\": \"$(esc "$DISPATCHARR_CONTAINER")\", \"old_cron\": $([ -n "$old" ] && echo true || echo false)
}" >/dev/null || warn "Could not send the result to the bridge"

$ok || exit 1
say "All set. Go back to the Setup tab; the Dispatcharr light should be green."
