#!/bin/bash
# Run on .94 host to dump stream mappings + category mappings from Dispatcharr.
# Usage: bash /etc/docker/plexbridge/repo/dump_vod_data.sh
# Add to cron: 0 */6 * * * /etc/docker/plexbridge/repo/dump_vod_data.sh

DISPATCHARR_CONTAINER="${DISPATCHARR_CONTAINER:-dispatcharr-IPTV2-94}"
BRIDGE_DATA_DIR="${BRIDGE_DATA_DIR:-/etc/docker/plexbridge/data}"

mkdir -p "$BRIDGE_DATA_DIR"

# 1. Dump stream mappings — ALL providers per movie (not just one winner)
docker exec "$DISPATCHARR_CONTAINER" python3 -c "
import os, sys, json, logging
logging.disable(logging.CRITICAL)
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'dispatcharr.settings')
sys.path.insert(0, '/app')
sys.stdout = open(os.devnull, 'w')
sys.stderr = open(os.devnull, 'w')
import django; django.setup()
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

with open('/tmp/stream_mapping.json', 'w') as f:
    json.dump(output, f)
" 2>/dev/null
docker cp "$DISPATCHARR_CONTAINER":/tmp/stream_mapping.json "$BRIDGE_DATA_DIR/stream_mapping.json" 2>/dev/null

# 2. Dump M3U account info (names only — no credentials)
docker exec "$DISPATCHARR_CONTAINER" python3 -c "
import os, sys, json, logging
logging.disable(logging.CRITICAL)
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'dispatcharr.settings')
sys.path.insert(0, '/app')
sys.stdout = open(os.devnull, 'w')
sys.stderr = open(os.devnull, 'w')
import django; django.setup()
from django.apps import apps

M3UAccount = apps.get_model('m3u', 'M3UAccount')
accounts = {}
for a in M3UAccount.objects.filter(is_active=True):
    accounts[str(a.id)] = {
        'name': a.name,
    }

with open('/tmp/account_credentials.json', 'w') as f:
    json.dump(accounts, f)
" 2>/dev/null
docker cp "$DISPATCHARR_CONTAINER":/tmp/account_credentials.json "$BRIDGE_DATA_DIR/account_credentials.json" 2>/dev/null

# 3. Dump category mappings (categories + which movies belong to each)
docker exec "$DISPATCHARR_CONTAINER" python3 -c "
import os, sys, json, logging
logging.disable(logging.CRITICAL)
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'dispatcharr.settings')
sys.path.insert(0, '/app')
sys.stdout = open(os.devnull, 'w')
sys.stderr = open(os.devnull, 'w')
import django; django.setup()
from django.apps import apps
VODCategory = apps.get_model('vod', 'VODCategory')
Movie = apps.get_model('vod', 'Movie')
M3UMovieRelation = apps.get_model('vod', 'M3UMovieRelation')
exclude_ids = set(Movie.objects.filter(name='').values_list('id', flat=True))
categories = []
for cat in VODCategory.objects.filter(category_type='movie').order_by('name'):
    movie_ids = [mid for mid in M3UMovieRelation.objects.filter(category=cat, m3u_account__is_active=True).values_list('movie_id', flat=True).distinct() if mid not in exclude_ids]
    categories.append({'id': cat.id, 'name': cat.name, 'movie_ids': movie_ids})
with open('/tmp/category_mapping.json', 'w') as f:
    json.dump(categories, f)
" 2>/dev/null
docker cp "$DISPATCHARR_CONTAINER":/tmp/category_mapping.json "$BRIDGE_DATA_DIR/category_mapping.json" 2>/dev/null

echo "$(date): Dump complete"
