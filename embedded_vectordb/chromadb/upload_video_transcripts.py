#!/usr/bin/env python3
"""Upload chunked YouTube video transcripts to ChromaDB with Jina AI embeddings.

Mirrors motherduck/upload_video_transcripts.py: reads full transcripts from
youtube.video_transcripts, chunks by token window (1000/250 overlap).

Requires JINA_API_KEY environment variable.

Usage:
    JINA_API_KEY=... python upload_video_transcripts.py                         # incremental from both DBs
    JINA_API_KEY=... python upload_video_transcripts.py --database podcasts     # podcasts DB only
    JINA_API_KEY=... python upload_video_transcripts.py --database substack     # substack DB only
    JINA_API_KEY=... python upload_video_transcripts.py --all                   # re-upload all
"""

import argparse
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import tiktoken

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from src.db.connection import DatabaseConnection

sys.path.insert(0, str(Path(__file__).parent))
from config import get_client, get_jina_ef, VIDEO_TRANSCRIPTS_COLLECTION

# Chunking settings (same as weaviate/upload_videos.py)
CHUNK_SIZE = 1000
OVERLAP = 250
MIN_CHUNK_SIZE = 500

BATCH_SIZE = 50
MAX_TOKENS = 7500

tokenizer = tiktoken.get_encoding("cl100k_base")

WATERMARK_FILE = Path(__file__).parent / ".last_upload_video_transcripts"


def count_tokens(text: str) -> int:
    return len(tokenizer.encode(text))


def truncate_to_tokens(text: str, max_tokens: int = MAX_TOKENS) -> str:
    tokens = tokenizer.encode(text)
    if len(tokens) <= max_tokens:
        return text
    return tokenizer.decode(tokens[:max_tokens])


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


def get_last_upload_time() -> str | None:
    if WATERMARK_FILE.exists():
        return WATERMARK_FILE.read_text().strip()
    return None


def save_upload_time(timestamp: str):
    WATERMARK_FILE.write_text(timestamp)


VIDEOS_QUERY = """
SELECT DISTINCT ON (vt.video_id)
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
ORDER BY vt.video_id, vt.upload_date
"""


def build_chunks(video: dict, source_database: str) -> list[dict]:
    text = video["transcript"]
    if not text:
        return []

    shared = {
        "videoId": video["video_id"],
        "videoTitle": video["title"] or "",
        "channelName": video["channel_name"] or "",
        "videoUrl": video["url"] or "",
        "uploadDate": video["upload_date"].isoformat() if video["upload_date"] else "",
        "playlistName": video["playlist_name"] or "",
        "sourceDatabase": source_database,
    }

    text_chunks = chunk_by_tokens(text)
    chunks = []
    for idx, chunk_text in enumerate(text_chunks):
        tok_count = count_tokens(chunk_text)
        if tok_count < MIN_CHUNK_SIZE and idx > 0 and chunks:
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


def main():
    parser = argparse.ArgumentParser(
        description="Upload chunked YouTube video transcripts to ChromaDB"
    )
    parser.add_argument("--database",
                        help="PostgreSQL database (podcasts, substack, or both if omitted)")
    parser.add_argument("--all", action="store_true",
                        help="Re-upload all (ignore watermark)")
    args = parser.parse_args()

    databases = [args.database] if args.database else ["podcasts", "substack"]
    upload_start = datetime.now(timezone.utc).isoformat()

    all_chunks = []
    for db in databases:
        print(f"Reading transcripts from PostgreSQL ({db})...")
        with DatabaseConnection(database=db, schema="youtube") as conn:
            with conn.cursor() as cur:
                cur.execute(VIDEOS_QUERY)
                columns = [desc[0] for desc in cur.description]
                rows = [dict(zip(columns, r)) for r in cur.fetchall()]
        print(f"  {len(rows)} videos with transcripts")

        for row in rows:
            all_chunks.extend(build_chunks(row, source_database=db))

    print(f"\nTotal chunks: {len(all_chunks)}")
    if not all_chunks:
        print("No data to upload.")
        return

    client = get_client()
    jina_ef = get_jina_ef()

    # Create collection if it doesn't exist
    existing = [c.name for c in client.list_collections()]
    if VIDEO_TRANSCRIPTS_COLLECTION not in existing:
        client.create_collection(
            name=VIDEO_TRANSCRIPTS_COLLECTION,
            embedding_function=jina_ef,
            metadata={"hnsw:space": "cosine"},
        )
        print(f"Created collection: {VIDEO_TRANSCRIPTS_COLLECTION}")

    collection = client.get_collection(VIDEO_TRANSCRIPTS_COLLECTION, embedding_function=jina_ef)

    print(f"Uploading to {VIDEO_TRANSCRIPTS_COLLECTION}...")
    start = time.time()
    uploaded = 0

    for i in range(0, len(all_chunks), BATCH_SIZE):
        batch = all_chunks[i:i + BATCH_SIZE]

        ids = [f"video-{c['sourceDatabase']}-{c['videoId']}-chunk-{c['chunkNumber']}" for c in batch]
        documents = [truncate_to_tokens(c["transcript"]) for c in batch]
        metadatas = [{k: v for k, v in c.items() if k != "transcript"} for c in batch]

        try:
            collection.upsert(ids=ids, documents=documents, metadatas=metadatas)
        except RuntimeError as e:
            if "rate limit" in str(e).lower():
                print(f"  Rate limited at {uploaded}/{len(all_chunks)}, waiting 60s...")
                time.sleep(60)
                collection.upsert(ids=ids, documents=documents, metadatas=metadatas)
            else:
                raise

        uploaded += len(batch)
        if uploaded % 500 < BATCH_SIZE:
            elapsed = time.time() - start
            print(f"  {uploaded}/{len(all_chunks)} ({uploaded/elapsed:.0f}/sec)")

    elapsed = time.time() - start
    print(f"Done: {uploaded} uploaded in {elapsed:.1f}s ({uploaded/elapsed:.0f}/sec)")
    print(f"Collection count: {collection.count()}")

    save_upload_time(upload_start)
    print(f"Watermark saved: {upload_start}")


if __name__ == "__main__":
    main()
