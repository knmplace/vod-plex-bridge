#!/usr/bin/env python3
"""
Resync stream IDs from stream_mapping.json into the database.
Use this when the cron job regenerates mappings and activated movies get stale IDs.
"""
import json
import sqlite3
import sys

mapping_file = "/data/stream_mapping.json"
db_file = "/data/vod_bridge.db"

# Load current mappings
with open(mapping_file) as f:
    mappings = json.load(f)

# Connect to database
conn = sqlite3.connect(db_file)
c = conn.cursor()

# Get all activated movies
c.execute("SELECT id, name, stream_id FROM movies WHERE activated = 1")
activated = c.fetchall()

updated = 0
not_found = 0

for movie_id, name, old_stream_id in activated:
    movie_id_str = str(movie_id)

    if movie_id_str in mappings:
        new_stream_id = mappings[movie_id_str].get('stream_id')

        if new_stream_id and new_stream_id != old_stream_id:
            print(f"UPDATE {movie_id} ({name}): {old_stream_id} → {new_stream_id}")
            c.execute("UPDATE movies SET stream_id = ? WHERE id = ?", (new_stream_id, movie_id))
            updated += 1
        elif new_stream_id == old_stream_id:
            print(f"OK {movie_id} ({name}): {old_stream_id} (unchanged)")
        else:
            print(f"WARN {movie_id} ({name}): new mapping has no stream_id")
    else:
        print(f"NOT_FOUND {movie_id} ({name}): mapping disappeared")
        not_found += 1

conn.commit()
conn.close()

print(f"\n{updated} updated, {not_found} not found")
