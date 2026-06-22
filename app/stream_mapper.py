"""
Fetches stream_id + container_extension mappings for VOD movies.

Two modes:
1. Django ORM (runs inside Dispatcharr container) — dumps to JSON file
2. JSON file reader (runs in bridge container) — reads the dump

The dump script is deployed as a cron job or called before each sync.
"""
import json
import logging
import os

logger = logging.getLogger(__name__)

MAPPING_FILE = os.environ.get("STREAM_MAPPING_FILE", "/data/stream_mapping.json")


def load_stream_mapping() -> dict:
    if not os.path.exists(MAPPING_FILE):
        logger.warning(f"Stream mapping file not found: {MAPPING_FILE}")
        return {}

    with open(MAPPING_FILE) as f:
        data = json.load(f)

    return {int(k): v for k, v in data.items()}


async def apply_stream_mapping_to_db():
    from database import get_db

    mapping = load_stream_mapping()
    if not mapping:
        logger.warning("No stream mapping data to apply")
        return 0

    db = await get_db()
    try:
        updated = 0
        for movie_id, info in mapping.items():
            stream_id = info.get("stream_id")
            ext = info.get("ext", "mkv")
            content_type = "video/x-matroska" if ext == "mkv" else "video/mp4"
            account_id = info.get("account_id")
            account_name = info.get("account_name", "")

            result = await db.execute(
                "UPDATE movies SET stream_id = ?, content_type = ?, account_id = ?, account_name = ? "
                "WHERE id = ? AND (stream_id IS NULL OR stream_id != ? OR account_id IS NULL)",
                (stream_id, content_type, account_id, account_name, movie_id, stream_id),
            )
            if result.rowcount > 0:
                updated += 1

        await db.commit()
        logger.info(f"Applied stream mapping: {updated} movies updated out of {len(mapping)} total")
        return updated
    finally:
        await db.close()


# ---- Django dump script (runs inside Dispatcharr container) ----
DUMP_SCRIPT = '''#!/usr/bin/env python3
"""Run inside Dispatcharr container to dump movie->stream_id mapping."""
import os, sys, json, django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'dispatcharr.settings')
sys.path.insert(0, '/app')
django.setup()

from django.apps import apps

M3UMovieRelation = apps.get_model('vod', 'M3UMovieRelation')

M3UAccount = apps.get_model('m3u', 'M3UAccount')
account_names = dict(M3UAccount.objects.values_list('id', 'name'))

PREFERRED_ACCOUNTS = [10, 17, 13, 14]  # Amber Baby accounts first

rels = (
    M3UMovieRelation.objects
    .filter(m3u_account__is_active=True)
    .values('movie_id', 'stream_id', 'container_extension', 'm3u_account_id')
)

mapping = {}
for r in rels:
    mid = r['movie_id']
    acct_id = r['m3u_account_id']
    is_preferred = acct_id in PREFERRED_ACCOUNTS
    if mid not in mapping or (is_preferred and not mapping[mid].get('_preferred')):
        mapping[mid] = {
            'stream_id': r['stream_id'],
            'ext': r['container_extension'] or 'mkv',
            'account_id': acct_id,
            'account_name': account_names.get(acct_id, 'Unknown'),
            '_preferred': is_preferred,
        }

for mid in mapping:
    mapping[mid].pop('_preferred', None)

output_path = sys.argv[1] if len(sys.argv) > 1 else '/tmp/stream_mapping.json'
with open(output_path, 'w') as f:
    json.dump(mapping, f)

print(f"Dumped {len(mapping)} movie stream mappings to {output_path}")
'''
