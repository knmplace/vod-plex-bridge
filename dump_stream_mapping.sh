#!/bin/bash
# Run on .94 host to dump stream mappings from Dispatcharr into the bridge's data directory.
# Usage: bash /opt/vod-plex-bridge/dump_stream_mapping.sh
# Add to cron: 0 */6 * * * /opt/vod-plex-bridge/dump_stream_mapping.sh
#
# Outputs stream_mapping.json to the bridge's bind-mounted data directory.

DISPATCHARR_CONTAINER="${DISPATCHARR_CONTAINER:-dispatcharr-IPTV2-94}"
BRIDGE_DATA_DIR="${BRIDGE_DATA_DIR:-/etc/docker/plexbridge/data}"

mkdir -p "$BRIDGE_DATA_DIR"

docker exec -i "$DISPATCHARR_CONTAINER" python3 - <<'PYTHON' > "$BRIDGE_DATA_DIR/stream_mapping.json"
import os, sys, json, django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'dispatcharr.settings')
sys.path.insert(0, '/app')
django.setup()

from django.apps import apps
from django.db.models import Min

M3UMovieRelation = apps.get_model('vod', 'M3UMovieRelation')

rels = (
    M3UMovieRelation.objects
    .filter(m3u_account__is_active=True)
    .values('movie_id', 'container_extension')
    .annotate(first_stream_id=Min('stream_id'))
)

mapping = {}
for r in rels:
    mapping[r['movie_id']] = {
        'stream_id': r['first_stream_id'],
        'ext': r['container_extension'] or 'mkv',
    }

json.dump(mapping, sys.stdout)
PYTHON

COUNT=$(python3 -c "import json; print(len(json.load(open('$BRIDGE_DATA_DIR/stream_mapping.json'))))" 2>/dev/null || echo "?")
echo "$(date): Dumped $COUNT movie stream mappings to $BRIDGE_DATA_DIR/stream_mapping.json"
