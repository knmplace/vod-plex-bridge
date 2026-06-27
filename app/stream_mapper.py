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
# Provider groups: accounts that share the same stream_ids.
# First account in each group is used as the canonical "provider name".
PROVIDER_GROUPS = {
    "amber": [10, 13, 14],
    "warptv": [2, 11, 12],
}


def _account_to_provider(account_id: int) -> str:
    for group_name, ids in PROVIDER_GROUPS.items():
        if account_id in ids:
            return group_name
    return f"account_{account_id}"


def load_stream_mapping() -> dict:
    if not os.path.exists(MAPPING_FILE):
        logger.warning(f"Stream mapping file not found: {MAPPING_FILE}")
        return {}

    with open(MAPPING_FILE) as f:
        data = json.load(f)

    result = {}
    for k, v in data.items():
        mid = int(k)
        if isinstance(v, list):
            result[mid] = v
        else:
            result[mid] = [v]
    return result


def pick_stream_for_account(entries: list[dict], account_id: int | None) -> dict | None:
    if not entries:
        return None

    if account_id is not None:
        target_provider = _account_to_provider(account_id)
        for entry in entries:
            if _account_to_provider(entry.get("account_id", 0)) == target_provider:
                return entry

    return entries[0]


async def apply_stream_mapping_to_db():
    from database import get_db

    mapping = load_stream_mapping()
    if not mapping:
        logger.warning("No stream mapping data to apply")
        return 0

    db = await get_db()
    try:
        updated = 0
        for movie_id, entries in mapping.items():
            if not entries:
                continue

            row = await db.execute("SELECT account_id FROM movies WHERE id = ?", (movie_id,))
            movie = await row.fetchone()
            current_account_id = movie["account_id"] if movie else None

            info = pick_stream_for_account(entries, current_account_id)
            if not info:
                continue

            stream_id = info.get("stream_id")
            ext = info.get("ext", "mkv")
            content_type = "video/x-matroska" if ext == "mkv" else "video/mp4"
            acct_id = info.get("account_id")
            account_name = info.get("account_name", "")

            result = await db.execute(
                "UPDATE movies SET stream_id = ?, content_type = ?, account_id = ?, account_name = ? "
                "WHERE id = ? AND (stream_id IS NULL OR stream_id != ? OR account_id IS NULL)",
                (stream_id, content_type, acct_id, account_name, movie_id, stream_id),
            )
            if result.rowcount > 0:
                updated += 1

        await db.commit()
        logger.info(f"Applied stream mapping: {updated} movies updated out of {len(mapping)} total")
        return updated
    finally:
        pass


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

rels = (
    M3UMovieRelation.objects
    .filter(m3u_account__is_active=True)
    .values('movie_id', 'stream_id', 'container_extension', 'm3u_account_id')
)

# Collect ALL providers per movie, deduplicated by stream_id
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

# Convert to list format: {movie_id: [{stream_id, ext, account_id, account_name}, ...]}
output = {}
for mid, streams in mapping.items():
    output[mid] = list(streams.values())

output_path = sys.argv[1] if len(sys.argv) > 1 else '/tmp/stream_mapping.json'
with open(output_path, 'w') as f:
    json.dump(output, f)

print(f"Dumped {len(output)} movie stream mappings to {output_path}")
'''
