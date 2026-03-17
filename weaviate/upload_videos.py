#!/usr/bin/env python3
"""Read YouTube video transcripts from PostgreSQL, chunk, and upload to Weaviate.

Creates VideoChunk* collections with JinaAI v3 embeddings (1024 dim).
Reads transcripts from youtube.video_transcripts joined with playlist info.

Usage:
    python upload_videos.py                       # upload from both DBs (incremental)
    python upload_videos.py --database substack   # substack DB only
    python upload_videos.py --database podcasts   # podcasts DB only
    python upload_videos.py --all                 # re-upload everything
    python upload_videos.py --upload-only         # skip collection creation
    python upload_videos.py --create-only         # only create collection
    python upload_videos.py --since '2026-02-18'  # videos added after a date
"""

import argparse
import sys
import time
import uuid
from pathlib import Path

import tiktoken

sys.path.insert(0, str(Path(__file__).parent.parent))
from src.db.connection import DatabaseConnection

sys.path.insert(0, str(Path(__file__).parent))
from config import get_client, VIDEO_COLLECTION, VIDEO_PODCASTS_COLLECTION, VIDEO_SC_COLLECTION, VIDEO_LIBOPP_COLLECTION
from vector_cache import content_hash, load_cache

from weaviate.classes.config import Configure, Property, DataType

# Chunking settings for video transcripts (larger than posts — transcripts are long)
CHUNK_SIZE = 1000
OVERLAP = 250
MIN_CHUNK_SIZE = 500

tokenizer = tiktoken.get_encoding("cl100k_base")


# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------

def count_tokens(text: str) -> int:
    return len(tokenizer.encode(text))


def chunk_by_tokens(text: str) -> list[str]:
    tokens = tokenizer.encode(text)
    chunks = []
    start = 0
    while start < len(tokens):
        end = start + CHUNK_SIZE
        chunk_text = tokenizer.decode(tokens[start:end])
        chunks.append(chunk_text)
        if end >= len(tokens):
            break
        start = end - OVERLAP
    return chunks


# ---------------------------------------------------------------------------
# Collection creation
# ---------------------------------------------------------------------------

def create_video_collection(client, collection_name):
    """Create video chunk collection with JinaAI v3 vectorizer."""
    if client.collections.exists(collection_name):
        resp = input(f"{collection_name} exists. Delete and recreate? (yes/no): ")
        if resp.lower() == "yes":
            client.collections.delete(collection_name)
            print(f"  Deleted {collection_name}")
        else:
            print(f"  Skipping creation")
            return

    client.collections.create(
        name=collection_name,
        description=f"YouTube video transcript chunks ({collection_name}) with JinaAI v3 embeddings",
        vector_config=Configure.Vectors.text2vec_jinaai(
            model="jina-embeddings-v3",
            dimensions=1024,
            vectorize_collection_name=False,
            source_properties=["transcript"],
        ),
        reranker_config=Configure.Reranker.jinaai(
            model="jina-reranker-v3",
        ),
        properties=[
            Property(name="transcript", data_type=DataType.TEXT,
                     description="Chunk content (vectorized)"),
            Property(name="description", data_type=DataType.TEXT,
                     index_searchable=True,
                     skip_vectorization=True),
            Property(name="videoId", data_type=DataType.TEXT,
                     index_searchable=True),
            Property(name="videoTitle", data_type=DataType.TEXT,
                     index_searchable=True),
            Property(name="channelName", data_type=DataType.TEXT,
                     index_filterable=True),
            Property(name="videoUrl", data_type=DataType.TEXT,
                     index_filterable=False),
            Property(name="uploadDate", data_type=DataType.DATE),
            Property(name="playlistName", data_type=DataType.TEXT,
                     index_filterable=True),
            Property(name="category", data_type=DataType.TEXT,
                     index_filterable=True),
            Property(name="chunkNumber", data_type=DataType.INT),
            Property(name="totalChunks", data_type=DataType.INT),
            Property(name="chunkTokens", data_type=DataType.INT),
        ],
    )
    print(f"  Created {collection_name}")


# ---------------------------------------------------------------------------
# PostgreSQL query
# ---------------------------------------------------------------------------

VIDEOS_QUERY_BASE = """
SELECT
    vt.video_id,
    vt.title,
    vt.description,
    vt.channel_name,
    vt.url,
    vt.upload_date,
    vt.transcript,
    p.title AS playlist_name
FROM youtube.video_transcripts vt
LEFT JOIN youtube.playlist_videos pv ON pv.video_id = vt.video_id
LEFT JOIN youtube.playlists p ON p.playlist_id = pv.playlist_id
WHERE vt.transcript IS NOT NULL
  AND vt.transcript != ''
"""


def watermark_file(database: str) -> Path:
    suffix = f"_{database}" if database != "substack" else ""
    return Path(__file__).parent / f".last_upload_videos{suffix}"


def get_last_upload_time(database: str) -> str | None:
    wf = watermark_file(database)
    if wf.exists():
        return wf.read_text().strip()
    return None


def save_upload_time(timestamp: str, database: str):
    watermark_file(database).write_text(timestamp)


def build_query(args, database: str) -> tuple[str, list]:
    """Build SQL query and params based on CLI args."""
    query = VIDEOS_QUERY_BASE
    params = []

    if args.since:
        query += "  AND vt.created_at >= %s\n"
        params.append(args.since)
    elif not args.all:
        last = get_last_upload_time(database)
        if last:
            query += "  AND vt.created_at > %s\n"
            params.append(last)

    query += "ORDER BY vt.upload_date"
    return query, params


def build_chunks(row: dict) -> list[dict]:
    """Build chunk dicts from a video transcript row."""
    text = row["transcript"]
    if not text:
        return []

    shared = {
        "videoId": row["video_id"],
        "videoTitle": row["title"] or "",
        "description": row.get("description") or "",
        "channelName": row["channel_name"] or "",
        "videoUrl": row["url"] or "",
        "uploadDate": row["upload_date"].isoformat() + "T00:00:00Z" if row["upload_date"] else "",
        "playlistName": row["playlist_name"] or "",
        "category": "",
    }

    text_chunks = chunk_by_tokens(text)
    chunks = []
    for idx, chunk_text in enumerate(text_chunks):
        tok_count = count_tokens(chunk_text)
        if tok_count < MIN_CHUNK_SIZE and idx > 0 and chunks:
            # Merge small trailing chunk into previous
            chunks[-1]["transcript"] += "\n\n" + chunk_text
            chunks[-1]["chunkTokens"] = count_tokens(chunks[-1]["transcript"])
            continue
        chunks.append({
            **shared,
            "transcript": chunk_text,
            "chunkNumber": idx,
            "totalChunks": len(text_chunks),
            "chunkTokens": tok_count,
        })

    total = len(chunks)
    for c in chunks:
        c["totalChunks"] = total
    return chunks


# ---------------------------------------------------------------------------
# Upload
# ---------------------------------------------------------------------------

def upload_chunks(client, all_chunks: list[dict], collection_name: str,
                  vector_cache: dict | None = None):
    """Upload chunks to Weaviate using batch import with deterministic UUIDs."""
    collection = client.collections.get(collection_name)

    print(f"Uploading {len(all_chunks)} chunks to {collection_name}...")
    start = time.time()
    uploaded = 0
    errors = 0
    cache_hits = 0

    with collection.batch.dynamic() as batch:
        for i, chunk in enumerate(all_chunks):
            obj_uuid = uuid.uuid5(
                uuid.NAMESPACE_DNS,
                f"engru-video-{chunk['videoId']}-{chunk['chunkNumber']}"
            )
            transcript = chunk.pop("transcript")
            vector = None
            if vector_cache:
                h = content_hash(transcript)
                vector = vector_cache.get(h)
                if vector:
                    cache_hits += 1
            try:
                batch.add_object(
                    properties={"transcript": transcript, **chunk},
                    uuid=obj_uuid,
                    vector=vector,
                )
                uploaded += 1
            except Exception as e:
                errors += 1
                if errors <= 5:
                    print(f"  ERROR at chunk {i}: {e}")

            if (i + 1) % 200 == 0 or i == len(all_chunks) - 1:
                elapsed = time.time() - start
                rate = uploaded / elapsed if elapsed > 0 else 0
                print(f"  [{i+1}/{len(all_chunks)}] {uploaded} uploaded ({rate:.0f}/sec)")

    elapsed = time.time() - start
    if vector_cache:
        print(f"Cache: {cache_hits}/{uploaded} hits ({100*cache_hits/max(uploaded,1):.0f}%)")
    print(f"Done: {uploaded} uploaded, {errors} errors in {elapsed:.1f}s")

    count = collection.aggregate.over_all(total_count=True).total_count
    print(f"Collection count: {count}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

COLLECTION_MAP = {
    "substack": VIDEO_COLLECTION,
    "podcasts": VIDEO_PODCASTS_COLLECTION,
    "surgical_compass": VIDEO_SC_COLLECTION,
    "podcasts_lib": VIDEO_LIBOPP_COLLECTION,
}


def upload_for_database(database: str, args, client):
    """Upload video transcripts from one database to its Weaviate collection."""
    collection_name = COLLECTION_MAP.get(database, f"VideoChunk_{database}")
    print(f"\nDatabase: {database} → Collection: {collection_name}")

    if not args.upload_only:
        create_video_collection(client, collection_name)

    if args.create_only:
        return

    from datetime import datetime, timezone
    upload_start = datetime.now(timezone.utc).isoformat()

    # Read from PostgreSQL
    query, params = build_query(args, database)
    print(f"Reading transcripts from PostgreSQL ({database})...")
    with DatabaseConnection(database=database, schema="youtube") as conn:
        with conn.cursor() as cur:
            cur.execute(query, params)
            columns = [desc[0] for desc in cur.description]
            rows = [dict(zip(columns, r)) for r in cur.fetchall()]
    print(f"  {len(rows)} videos to upload")

    if not rows:
        print("Nothing new to upload.")
        return

    # Chunk
    print("Chunking transcripts...")
    all_chunks = []
    for row in rows:
        chunks = build_chunks(row)
        all_chunks.extend(chunks)
    print(f"  {len(all_chunks)} total chunks from {len(rows)} videos")

    # Load vector cache if requested
    vcache = None
    if getattr(args, 'use_cache', False):
        vcache = load_cache(collection_name)

    # Upload
    upload_chunks(client, all_chunks, collection_name, vector_cache=vcache)

    # Save watermark on success
    save_upload_time(upload_start, database)
    print(f"Watermark saved: {upload_start}")


def main():
    parser = argparse.ArgumentParser(description="Upload video transcripts to Weaviate")
    parser.add_argument("--all", action="store_true",
                        help="Re-upload all videos (ignore watermark)")
    parser.add_argument("--upload-only", action="store_true",
                        help="Skip collection creation, only upload")
    parser.add_argument("--create-only", action="store_true",
                        help="Only create collection, don't upload")
    parser.add_argument("--since", metavar="TIMESTAMP",
                        help="Upload videos added after this timestamp (e.g. '2026-02-18')")
    parser.add_argument("--use-cache", action="store_true",
                        help="Use cached vectors to skip embedding API calls for unchanged content")
    parser.add_argument("--database",
                        help="PostgreSQL database (substack, podcasts, or both if omitted)")
    args = parser.parse_args()

    databases = [args.database] if args.database else ["substack", "podcasts"]

    client = get_client()
    try:
        print(f"Connected to Weaviate (ready: {client.is_ready()})")
        for database in databases:
            upload_for_database(database, args, client)
    finally:
        client.close()


if __name__ == "__main__":
    main()
