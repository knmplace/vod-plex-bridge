#!/bin/bash
# Dumps stream mappings from Dispatcharr into the bridge's data directory.
# This maps each VOD movie ID to its stream_id and container extension,
# enabling the bridge to route playback through the correct provider account.
#
# Usage:
#   bash dump_stream_mapping.sh
#
# Schedule via cron (every 6 hours):
#   0 */6 * * * /path/to/dump_stream_mapping.sh
#
# Environment variables:
#   DISPATCHARR_CONTAINER  Name of the Dispatcharr Docker container (default: dispatcharr)
#   BRIDGE_DATA_DIR        Path to the bridge's data volume on the host (default: ./data)

DISPATCHARR_CONTAINER="${DISPATCHARR_CONTAINER:-dispatcharr}"
BRIDGE_DATA_DIR="${BRIDGE_DATA_DIR:-./data}"

mkdir -p "$BRIDGE_DATA_DIR"

docker exec -i "$DISPATCHARR_CONTAINER" python3 - <<'PYTHON' > "$BRIDGE_DATA_DIR/stream_mapping.json"
import os, sys, json, django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'dispatcharr.settings')
sys.path.insert(0, '/app')
django.setup()

from django.apps import apps

M3UMovieRelation = apps.get_model('vod', 'M3UMovieRelation')
M3UAccount = apps.get_model('m3u', 'M3UAccount')
account_names = dict(M3UAccount.objects.values_list('id', 'name'))

rels = (
    M3UMovieRelation.objects
    .filter(m3u_account__is_active=True)
    .values('movie_id', 'stream_id', 'container_extension', 'm3u_account_id')
)

mapping = {}
for r in rels:
    mid = r['movie_id']
    if mid not in mapping:
        mapping[mid] = {}
    sid = r['stream_id']
    if sid not in mapping[mid]:
        acct_id = r['m3u_account_id']
        mapping[mid][sid] = {
            'stream_id': sid,
            'ext': r['container_extension'] or 'mkv',
            'account_id': acct_id,
            'account_name': account_names.get(acct_id, 'Unknown'),
        }

output = {}
for mid, streams in mapping.items():
    output[mid] = list(streams.values())

json.dump(output, sys.stdout)
PYTHON

COUNT=$(python3 -c "import json; print(len(json.load(open('$BRIDGE_DATA_DIR/stream_mapping.json'))))" 2>/dev/null || echo "?")
echo "$(date): Dumped $COUNT movie stream mappings to $BRIDGE_DATA_DIR/stream_mapping.json"
