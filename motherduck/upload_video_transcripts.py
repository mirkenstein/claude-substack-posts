#!/usr/bin/env python3
"""Upload chunked YouTube video transcripts to MotherDuck for semantic search.

Mirrors weaviate/upload_videos.py: reads full transcripts from
youtube.video_transcripts, chunks by token window (1000/250 overlap),
and uploads to a youtube_transcript_chunks table.

Usage:
    python motherduck/upload_video_transcripts.py                         # full reload
    python motherduck/upload_video_transcripts.py --database podcasts     # explicit DB
    python motherduck/upload_video_transcripts.py --skip-embeddings       # insert only
    python motherduck/upload_video_transcripts.py --embed-only            # only run embeddings
"""

import argparse
import sys
import time
from pathlib import Path

import duckdb
import tiktoken

sys.path.insert(0, str(Path(__file__).parent.parent))
from src.db.connection import DatabaseConnection

MOTHERDUCK_DB = "md:my_db"

tokenizer = tiktoken.get_encoding("cl100k_base")
MAX_EMBED_TOKENS = 8000

# Chunking settings — same as weaviate/upload_videos.py
CHUNK_SIZE = 1000
OVERLAP = 250
MIN_CHUNK_SIZE = 500


def count_tokens(text: str) -> int:
    return len(tokenizer.encode(text))


def truncate_to_tokens(text: str, max_tokens: int = MAX_EMBED_TOKENS) -> str:
    tokens = tokenizer.encode(text)
    if len(tokens) <= max_tokens:
        return text
    return tokenizer.decode(tokens[:max_tokens])


# ---------------------------------------------------------------------------
# Chunking (from weaviate/upload_videos.py)
# ---------------------------------------------------------------------------

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
# PostgreSQL query
# ---------------------------------------------------------------------------

VIDEOS_QUERY = """
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
ORDER BY vt.upload_date
"""


# ---------------------------------------------------------------------------
# Build chunks
# ---------------------------------------------------------------------------

def build_chunks(video: dict) -> list[dict]:
    text = video["transcript"]
    if not text:
        return []

    shared = {
        "video_id": video["video_id"],
        "video_title": video["title"] or "",
        "description": video["description"] or "",
        "channel_name": video["channel_name"] or "",
        "video_url": video["url"] or "",
        "upload_date": video["upload_date"],
        "playlist_name": video["playlist_name"] or "",
    }

    text_chunks = chunk_by_tokens(text)
    chunks = []
    for idx, chunk_text in enumerate(text_chunks):
        tok_count = count_tokens(chunk_text)
        if tok_count < MIN_CHUNK_SIZE and idx > 0 and chunks:
            chunks[-1]["transcript"] += "\n\n" + chunk_text
            chunks[-1]["chunk_tokens"] = count_tokens(chunks[-1]["transcript"])
            continue
        chunks.append({
            **shared,
            "transcript": chunk_text,
            "chunk_number": idx,
            "total_chunks": len(text_chunks),
            "chunk_tokens": tok_count,
        })

    total = len(chunks)
    for c in chunks:
        c["total_chunks"] = total
    return chunks


# ---------------------------------------------------------------------------
# MotherDuck table
# ---------------------------------------------------------------------------

CREATE_TABLE_SQL = """
CREATE OR REPLACE TABLE youtube_transcript_chunks (
    row_id              BIGINT,
    video_id            VARCHAR,
    video_title         VARCHAR,
    description         VARCHAR,
    channel_name        VARCHAR,
    video_url           VARCHAR,
    upload_date         DATE,
    playlist_name       VARCHAR,
    transcript          VARCHAR,
    chunk_number        INTEGER,
    total_chunks        INTEGER,
    chunk_tokens        INTEGER,
    transcript_embedding FLOAT[512]
);
"""


# ---------------------------------------------------------------------------
# Insert
# ---------------------------------------------------------------------------

def insert_chunks(conn: duckdb.DuckDBPyConnection, chunks: list[dict],
                  batch_size: int = 200):
    insert_sql = """
    INSERT INTO youtube_transcript_chunks (
        row_id, video_id, video_title, description, channel_name,
        video_url, upload_date, playlist_name,
        transcript, chunk_number, total_chunks, chunk_tokens
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """
    start = time.time()
    for i in range(0, len(chunks), batch_size):
        batch = chunks[i:i + batch_size]
        values = [
            (
                c["row_id"],
                c["video_id"], c["video_title"], c["description"],
                c["channel_name"], c["video_url"], c["upload_date"],
                c["playlist_name"],
                c["transcript"], c["chunk_number"],
                c["total_chunks"], c["chunk_tokens"],
            )
            for c in batch
        ]
        conn.executemany(insert_sql, values)
        inserted = min(i + batch_size, len(chunks))
        elapsed = time.time() - start
        rate = inserted / elapsed if elapsed > 0 else 0
        print(f"  Inserted {inserted}/{len(chunks)} ({rate:.0f}/sec)")


# ---------------------------------------------------------------------------
# Embeddings
# ---------------------------------------------------------------------------

def run_embeddings(conn: duckdb.DuckDBPyConnection):
    result = conn.execute(
        "SELECT count(*) FROM youtube_transcript_chunks WHERE transcript_embedding IS NULL"
    ).fetchone()
    pending = result[0]
    if pending == 0:
        print("All rows already have embeddings.")
        return

    print(f"Generating embeddings for {pending} chunks...")
    start = time.time()
    conn.execute("""
        UPDATE youtube_transcript_chunks
        SET transcript_embedding = embedding(
            LEFT(transcript, 32000)
        )
        WHERE transcript_embedding IS NULL
    """)
    elapsed = time.time() - start
    print(f"  Done in {elapsed:.1f}s ({pending / elapsed:.0f} rows/sec)")


# ---------------------------------------------------------------------------
# FTS
# ---------------------------------------------------------------------------

def create_fts_index(conn: duckdb.DuckDBPyConnection):
    try:
        conn.execute("PRAGMA drop_fts_index('youtube_transcript_chunks')")
    except Exception:
        pass

    print("Creating FTS index on youtube_transcript_chunks (transcript, video_title)...")
    start = time.time()
    conn.execute("""
        PRAGMA create_fts_index(
            'youtube_transcript_chunks', 'row_id',
            'transcript', 'video_title',
            stemmer='english', stopwords='english',
            ignore='(\\.|[^a-z])+', strip_accents=1, lower=1,
            overwrite=1
        )
    """)
    elapsed = time.time() - start
    print(f"  Done in {elapsed:.1f}s")


# ---------------------------------------------------------------------------
# Verify
# ---------------------------------------------------------------------------

def verify(conn: duckdb.DuckDBPyConnection):
    print("\n--- Verification ---")
    result = conn.execute(
        "SELECT count(*), count(DISTINCT video_id) FROM youtube_transcript_chunks"
    ).fetchone()
    print(f"  Chunks: {result[0]} across {result[1]} videos")

    result = conn.execute(
        "SELECT count(*) FROM youtube_transcript_chunks WHERE transcript_embedding IS NOT NULL"
    ).fetchone()
    print(f"  With embeddings: {result[0]}")

    result = conn.execute("""
        SELECT channel_name, count(DISTINCT video_id) AS videos, count(*) AS chunks
        FROM youtube_transcript_chunks
        GROUP BY channel_name
        ORDER BY videos DESC
    """).fetchall()
    print("  By channel:")
    for row in result:
        print(f"    {row[0]}: {row[1]} videos, {row[2]} chunks")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Upload chunked YouTube video transcripts to MotherDuck"
    )
    parser.add_argument("--database", default="podcasts",
                        help="PostgreSQL database (default: podcasts)")
    parser.add_argument("--skip-embeddings", action="store_true",
                        help="Insert data only, skip embedding() call")
    parser.add_argument("--embed-only", action="store_true",
                        help="Only run embedding update, no data insert")
    args = parser.parse_args()

    print("Connecting to MotherDuck...")
    md = duckdb.connect(MOTHERDUCK_DB)

    try:
        if args.embed_only:
            run_embeddings(md)
            verify(md)
            return

        database = args.database

        print(f"\nReading transcripts from PostgreSQL ({database})...")
        with DatabaseConnection(database=database, schema="youtube") as conn:
            with conn.cursor() as cur:
                cur.execute(VIDEOS_QUERY)
                columns = [desc[0] for desc in cur.description]
                rows = [dict(zip(columns, r)) for r in cur.fetchall()]
        print(f"  {len(rows)} videos with transcripts")

        if not rows:
            print("No data to upload.")
            return

        # Chunk all transcripts
        print("\nChunking transcripts...")
        all_chunks = []
        for row in rows:
            chunks = build_chunks(row)
            all_chunks.extend(chunks)
        print(f"  {len(all_chunks)} chunks from {len(rows)} videos")

        # Assign sequential row_id (BIGINT for FTS)
        for i, c in enumerate(all_chunks, start=1):
            c["row_id"] = i

        # Create table and insert
        print("\nCreating MotherDuck table...")
        md.execute(CREATE_TABLE_SQL)

        print("\nInserting chunks...")
        insert_chunks(md, all_chunks)

        # Embeddings
        if not args.skip_embeddings:
            print()
            run_embeddings(md)

        # FTS
        print()
        create_fts_index(md)

        verify(md)

    finally:
        md.close()


if __name__ == "__main__":
    main()
