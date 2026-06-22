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

            result = await db.execute(
                "UPDATE movies SET stream_id = ?, content_type = ? WHERE id = ? AND (stream_id IS NULL OR stream_id != ?)",
                (stream_id, content_type, movie_id, stream_id),
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
from django.db.models import Min

M3UMovieRelation = apps.get_model('vod', 'M3UMovieRelation')

# Get one stream_id per movie (lowest stream_id = most common across providers)
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

output_path = sys.argv[1] if len(sys.argv) > 1 else '/tmp/stream_mapping.json'
with open(output_path, 'w') as f:
    json.dump(mapping, f)

print(f"Dumped {len(mapping)} movie stream mappings to {output_path}")
'''
