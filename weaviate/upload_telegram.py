#!/usr/bin/env python3
"""Upload Telegram channel messages and media transcripts to Weaviate.

Reads from telegram.messages and telegram.media_transcripts, chunks long
messages by tokens, and uploads to Weaviate with JinaAI v3 embeddings.

Supports watermark-based incremental uploads: only messages posted/edited after
the last successful upload are processed. Use --all to force a full re-upload.

Usage:
    python upload_telegram.py                        # incremental (new since last run)
    python upload_telegram.py --all                  # re-upload everything
    python upload_telegram.py --since 2025-01-01     # messages posted after date
    python upload_telegram.py --channel strelkov_i   # filter to one channel
    python upload_telegram.py --upload-only           # skip collection creation
    python upload_telegram.py --create-only           # only create collections
    python upload_telegram.py --messages-only         # only messages
    python upload_telegram.py --transcripts-only      # only transcripts
    python upload_telegram.py --database podcasts     # upload from podcasts DB
    python upload_telegram.py --use-cache             # reuse cached vectors
    python upload_telegram.py --recreate              # delete and recreate collections
"""

import argparse
import html
import re
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import tiktoken

sys.path.insert(0, str(Path(__file__).parent.parent))
from src.db.connection import DatabaseConnection

sys.path.insert(0, str(Path(__file__).parent))
from config import (
    get_client,
    TELEGRAM_MESSAGES_COLLECTION,
    TELEGRAM_TRANSCRIPTS_COLLECTION,
    CHUNK_SIZE, OVERLAP, MIN_CHUNK_SIZE,
)
from vector_cache import content_hash, load_cache
from weaviate.classes.config import Configure, Property, DataType

tokenizer = tiktoken.get_encoding("cl100k_base")

# UUID namespace for deterministic IDs
UUID_NS = uuid.NAMESPACE_DNS

# Transcript chunking (matches video transcript settings)
TRANSCRIPT_CHUNK_SIZE = 1000
TRANSCRIPT_OVERLAP = 250
TRANSCRIPT_MIN_CHUNK_SIZE = 500


# ---------------------------------------------------------------------------
# Text cleaning
# ---------------------------------------------------------------------------

def clean_text(text: str) -> str:
    """Clean text for embedding (mostly Russian content)."""
    if not text:
        return ""
    text = text.replace('\xa0', ' ')
    text = html.unescape(text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    text = '\n'.join(line.strip() for line in text.splitlines())
    return text.strip()


# ---------------------------------------------------------------------------
# Tokenization / chunking
# ---------------------------------------------------------------------------

def count_tokens(text: str) -> int:
    return len(tokenizer.encode(text))


def chunk_by_tokens(text: str, chunk_size: int = CHUNK_SIZE,
                    overlap: int = OVERLAP) -> list[str]:
    """Split text into overlapping chunks by token count."""
    tokens = tokenizer.encode(text)
    chunks = []
    start = 0
    while start < len(tokens):
        end = start + chunk_size
        chunk_text = tokenizer.decode(tokens[start:end])
        chunks.append(chunk_text)
        if end >= len(tokens):
            break
        start = end - overlap
    return chunks


# ---------------------------------------------------------------------------
# Collection creation
# ---------------------------------------------------------------------------

def create_messages_collection(client, name=TELEGRAM_MESSAGES_COLLECTION,
                               recreate=False):
    """Create Telegram messages collection with JinaAI v3 vectorizer."""
    if client.collections.exists(name):
        if recreate:
            client.collections.delete(name)
            print(f"  Deleted {name}")
        else:
            resp = input(f"{name} exists. Delete and recreate? (yes/no): ")
            if resp.lower() == "yes":
                client.collections.delete(name)
                print(f"  Deleted {name}")
            else:
                print(f"  Skipping creation of {name}")
                return

    client.collections.create(
        name=name,
        description="Telegram channel message chunks with JinaAI v3 embeddings",
        vector_config=Configure.Vectors.text2vec_jinaai(
            model="jina-embeddings-v3",
            dimensions=1024,
            vectorize_collection_name=False,
            source_properties=["content"],
        ),
        reranker_config=Configure.Reranker.jinaai(
            model="jina-reranker-v3",
        ),
        properties=[
            Property(name="content", data_type=DataType.TEXT,
                     description="Message text chunk (vectorized)"),
            Property(name="channelId", data_type=DataType.INT),
            Property(name="channelName", data_type=DataType.TEXT,
                     index_filterable=True, index_searchable=True),
            Property(name="channelUsername", data_type=DataType.TEXT,
                     index_filterable=True),
            Property(name="messageId", data_type=DataType.INT),
            Property(name="postedAt", data_type=DataType.DATE),
            Property(name="fromName", data_type=DataType.TEXT,
                     index_filterable=True),
            Property(name="forwardedFrom", data_type=DataType.TEXT,
                     index_filterable=True),
            Property(name="language", data_type=DataType.TEXT,
                     index_filterable=True),
            Property(name="reactionCount", data_type=DataType.INT),
            Property(name="topReactions", data_type=DataType.TEXT,
                     index_filterable=False, index_searchable=False),
            Property(name="hasMedia", data_type=DataType.BOOL,
                     index_filterable=True),
            Property(name="mediaTypes", data_type=DataType.TEXT_ARRAY),
            Property(name="chunkNumber", data_type=DataType.INT),
            Property(name="totalChunks", data_type=DataType.INT),
            Property(name="chunkTokens", data_type=DataType.INT),
        ],
    )
    print(f"  Created {name}")


def create_transcripts_collection(client, name=TELEGRAM_TRANSCRIPTS_COLLECTION,
                                   recreate=False):
    """Create Telegram media transcripts collection with JinaAI v3 vectorizer."""
    if client.collections.exists(name):
        if recreate:
            client.collections.delete(name)
            print(f"  Deleted {name}")
        else:
            resp = input(f"{name} exists. Delete and recreate? (yes/no): ")
            if resp.lower() == "yes":
                client.collections.delete(name)
                print(f"  Deleted {name}")
            else:
                print(f"  Skipping creation of {name}")
                return

    client.collections.create(
        name=name,
        description="Telegram media transcript chunks with JinaAI v3 embeddings",
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
                     description="Transcript text chunk (vectorized)"),
            Property(name="channelId", data_type=DataType.INT),
            Property(name="channelName", data_type=DataType.TEXT,
                     index_filterable=True, index_searchable=True),
            Property(name="messageId", data_type=DataType.INT),
            Property(name="postedAt", data_type=DataType.DATE),
            Property(name="fromName", data_type=DataType.TEXT,
                     index_filterable=True),
            Property(name="mediaType", data_type=DataType.TEXT,
                     index_filterable=True),
            Property(name="transcriptType", data_type=DataType.TEXT,
                     index_filterable=True),
            Property(name="chunkNumber", data_type=DataType.INT),
            Property(name="totalChunks", data_type=DataType.INT),
            Property(name="chunkTokens", data_type=DataType.INT),
        ],
    )
    print(f"  Created {name}")


# ---------------------------------------------------------------------------
# Watermark (incremental upload tracking)
# ---------------------------------------------------------------------------

def _watermark_file(database: str) -> Path:
    suffix = f"_{database}" if database != "substack" else ""
    return Path(__file__).parent / f".last_upload_telegram{suffix}"


def get_last_upload_time(database: str = "substack") -> str | None:
    """Read the watermark timestamp from the last successful upload."""
    wf = _watermark_file(database)
    if wf.exists():
        return wf.read_text().strip()
    return None


def save_upload_time(timestamp: str, database: str = "substack"):
    """Save the current upload timestamp as watermark."""
    _watermark_file(database).write_text(timestamp)


# ---------------------------------------------------------------------------
# DB queries
# ---------------------------------------------------------------------------

MESSAGES_QUERY = """
SELECT
    m.channel_id,
    m.id AS message_id,
    m.posted_at,
    m.from_name,
    m.forwarded_from,
    m.text_plain,
    c.name AS channel_name,
    c.username AS channel_username,
    c.language,
    (SELECT COALESCE(SUM(r.count), 0)
     FROM telegram.message_reactions r
     WHERE r.channel_id = m.channel_id AND r.message_id = m.id) AS reaction_count,
    (SELECT string_agg(r.emoji || ' ' || r.count::text, ', ' ORDER BY r.count DESC)
     FROM telegram.message_reactions r
     WHERE r.channel_id = m.channel_id AND r.message_id = m.id) AS top_reactions,
    (SELECT array_agg(DISTINCT mm.media_type)
     FROM telegram.message_media mm
     WHERE mm.channel_id = m.channel_id AND mm.message_id = m.id) AS media_types
FROM telegram.messages m
JOIN telegram.channels c ON c.id = m.channel_id
WHERE m.type = 'message'
  AND m.text_plain IS NOT NULL
  AND m.text_plain != ''
  {extra_filters}
ORDER BY m.posted_at
"""

TRANSCRIPTS_QUERY = """
SELECT
    mt.id AS transcript_id,
    mt.channel_id,
    mt.transcript_text,
    mt.transcript_type,
    mm.message_id,
    mm.media_type,
    m.posted_at,
    m.from_name,
    c.name AS channel_name
FROM telegram.media_transcripts mt
JOIN telegram.message_media mm ON mm.id = mt.media_id
JOIN telegram.messages m ON m.channel_id = mm.channel_id AND m.id = mm.message_id
JOIN telegram.channels c ON c.id = mt.channel_id
WHERE mt.transcript_text IS NOT NULL
  AND mt.transcript_text != ''
  {extra_filters}
ORDER BY m.posted_at
"""

SEGMENTS_QUERY = """
SELECT segment_index, start_seconds, end_seconds, speaker, text
FROM telegram.media_transcript_segments
WHERE transcript_id = %s
ORDER BY segment_index
"""


def fetch_messages(conn, since: str | None = None,
                   channel: str | None = None) -> list[dict]:
    filters = []
    params = []
    if since:
        filters.append("AND m.loaded_at > %s")
        params.append(since)
    if channel:
        filters.append("AND (c.username = %s OR c.name = %s OR c.id::text = %s)")
        params.extend([channel, channel, channel])

    extra = "\n  ".join(filters)
    query = MESSAGES_QUERY.format(extra_filters=extra)
    with conn.cursor() as cur:
        cur.execute(query, params)
        columns = [desc[0] for desc in cur.description]
        return [dict(zip(columns, r)) for r in cur.fetchall()]


def fetch_transcripts(conn, since: str | None = None,
                      channel: str | None = None) -> list[dict]:
    filters = []
    params = []
    if since:
        filters.append("AND mt.transcribed_at > %s")
        params.append(since)
    if channel:
        filters.append("AND (c.username = %s OR c.name = %s OR c.id::text = %s)")
        params.extend([channel, channel, channel])

    extra = "\n  ".join(filters)
    query = TRANSCRIPTS_QUERY.format(extra_filters=extra)
    with conn.cursor() as cur:
        cur.execute(query, params)
        columns = [desc[0] for desc in cur.description]
        return [dict(zip(columns, r)) for r in cur.fetchall()]


def fetch_segments(conn, transcript_id: int) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(SEGMENTS_QUERY, [transcript_id])
        columns = [desc[0] for desc in cur.description]
        return [dict(zip(columns, r)) for r in cur.fetchall()]


# ---------------------------------------------------------------------------
# Message chunking
# ---------------------------------------------------------------------------

def build_message_chunks(row: dict) -> list[dict]:
    """Convert a message row into one or more chunk dicts."""
    text = clean_text(row["text_plain"])
    if not text:
        return []

    media_types = row["media_types"] or []

    shared = {
        "channelId": row["channel_id"],
        "channelName": row["channel_name"] or "",
        "channelUsername": row["channel_username"] or "",
        "messageId": row["message_id"],
        "postedAt": row["posted_at"].isoformat() if row["posted_at"] else None,
        "fromName": row["from_name"] or "",
        "forwardedFrom": row["forwarded_from"] or "",
        "language": row["language"] or "ru",
        "reactionCount": row["reaction_count"] or 0,
        "topReactions": row["top_reactions"] or "",
        "hasMedia": len(media_types) > 0,
        "mediaTypes": media_types,
    }

    text_chunks = chunk_by_tokens(text)
    chunks = []
    for idx, chunk_text in enumerate(text_chunks):
        tok_count = count_tokens(chunk_text)
        if tok_count < MIN_CHUNK_SIZE and idx > 0 and chunks:
            merged_tokens = count_tokens(chunks[-1]["content"] + "\n\n" + chunk_text)
            if merged_tokens <= 7000:
                chunks[-1]["content"] += "\n\n" + chunk_text
                chunks[-1]["chunkTokens"] = merged_tokens
                continue
        chunks.append({
            **shared,
            "content": chunk_text,
            "chunkNumber": idx,
            "totalChunks": len(text_chunks),
            "chunkTokens": tok_count,
        })

    total = len(chunks)
    for c in chunks:
        c["totalChunks"] = total
    return chunks


# ---------------------------------------------------------------------------
# Transcript chunking
# ---------------------------------------------------------------------------

def build_transcript_chunks(row: dict, segments: list[dict]) -> list[dict]:
    """Convert a transcript row (with optional segments) into chunk dicts."""
    # Build full text from segments if available, otherwise use transcript_text
    if segments:
        lines = []
        for seg in segments:
            speaker = seg.get("speaker") or ""
            text = seg.get("text", "").strip()
            if speaker:
                lines.append(f"[{speaker}] {text}")
            else:
                lines.append(text)
        full_text = "\n".join(lines)
    else:
        full_text = row.get("transcript_text", "")

    full_text = clean_text(full_text)
    if not full_text:
        return []

    shared = {
        "channelId": row["channel_id"],
        "channelName": row["channel_name"] or "",
        "messageId": row["message_id"],
        "postedAt": row["posted_at"].isoformat() if row["posted_at"] else None,
        "fromName": row["from_name"] or "",
        "mediaType": row["media_type"] or "",
        "transcriptType": row["transcript_type"] or "",
    }

    text_chunks = chunk_by_tokens(full_text,
                                  chunk_size=TRANSCRIPT_CHUNK_SIZE,
                                  overlap=TRANSCRIPT_OVERLAP)
    chunks = []
    for idx, chunk_text in enumerate(text_chunks):
        tok_count = count_tokens(chunk_text)
        if tok_count < TRANSCRIPT_MIN_CHUNK_SIZE and idx > 0 and chunks:
            merged_tokens = count_tokens(chunks[-1]["transcript"] + "\n\n" + chunk_text)
            if merged_tokens <= 7000:
                chunks[-1]["transcript"] += "\n\n" + chunk_text
                chunks[-1]["chunkTokens"] = merged_tokens
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

def upload_messages(client, messages: list[dict], collection_name: str,
                    vector_cache: dict | None = None):
    """Chunk and upload messages to Weaviate, with per-channel progress."""
    collection = client.collections.get(collection_name)

    # Group by channel
    by_channel = {}
    for row in messages:
        by_channel.setdefault(row["channel_name"], []).append(row)

    total_chunks = 0
    total_errors = 0
    cache_hits = 0
    start = time.time()

    with collection.batch.dynamic() as batch:
        for channel_name, channel_messages in by_channel.items():
            channel_chunks = 0
            for i, row in enumerate(channel_messages):
                chunks = build_message_chunks(row)
                if not chunks:
                    continue

                for chunk in chunks:
                    obj_uuid = uuid.uuid5(
                        UUID_NS,
                        f"telegram-msg-{chunk['channelId']}-{chunk['messageId']}-{chunk['chunkNumber']}"
                    )
                    content = chunk.pop("content")
                    vector = None
                    if vector_cache:
                        h = content_hash(content)
                        vector = vector_cache.get(h)
                        if vector:
                            cache_hits += 1
                    batch.add_object(
                        properties={"content": content, **chunk},
                        uuid=obj_uuid,
                        vector=vector,
                    )

                channel_chunks += len(chunks)
                total_chunks += len(chunks)

                if (i + 1) % 500 == 0:
                    elapsed = time.time() - start
                    print(f"[Messages] {channel_name} | {i+1}/{len(channel_messages)} "
                          f"| {total_chunks} chunks | {elapsed:.0f}s | "
                          f"errors: {batch.number_errors}")

            print(f"[Messages] {channel_name} done: {channel_chunks} chunks "
                  f"from {len(channel_messages)} messages")

    total_errors = batch.number_errors
    elapsed = time.time() - start
    print(f"\nMessages total: {total_chunks} chunks, {total_errors} errors in {elapsed:.1f}s")
    if vector_cache and total_chunks > 0:
        print(f"  Cache hits: {cache_hits}/{total_chunks} "
              f"({100*cache_hits/total_chunks:.0f}% skipped embedding)")

    count = collection.aggregate.over_all(total_count=True).total_count
    print(f"Collection {collection_name} count: {count}")


def upload_transcripts(client, transcripts: list[dict], conn,
                       collection_name: str,
                       vector_cache: dict | None = None):
    """Chunk and upload transcripts to Weaviate."""
    collection = client.collections.get(collection_name)

    total_chunks = 0
    cache_hits = 0
    start = time.time()

    with collection.batch.dynamic() as batch:
        for i, row in enumerate(transcripts):
            segments = fetch_segments(conn, row["transcript_id"])
            chunks = build_transcript_chunks(row, segments)
            if not chunks:
                continue

            for chunk in chunks:
                obj_uuid = uuid.uuid5(
                    UUID_NS,
                    f"telegram-transcript-{row['transcript_id']}-{chunk['chunkNumber']}"
                )
                transcript_text = chunk.pop("transcript")
                vector = None
                if vector_cache:
                    h = content_hash(transcript_text)
                    vector = vector_cache.get(h)
                    if vector:
                        cache_hits += 1
                batch.add_object(
                    properties={"transcript": transcript_text, **chunk},
                    uuid=obj_uuid,
                    vector=vector,
                )

            total_chunks += len(chunks)

            channel = row["channel_name"] or "?"
            print(f"[Transcripts] {channel} | {i+1}/{len(transcripts)} "
                  f"| msg {row['message_id']} | {row['transcript_type']} "
                  f"| {len(chunks)} chunks")

    total_errors = batch.number_errors
    elapsed = time.time() - start
    print(f"\nTranscripts total: {total_chunks} chunks, {total_errors} errors in {elapsed:.1f}s")
    if vector_cache and total_chunks > 0:
        print(f"  Cache hits: {cache_hits}/{total_chunks} "
              f"({100*cache_hits/total_chunks:.0f}% skipped embedding)")

    count = collection.aggregate.over_all(total_count=True).total_count
    print(f"Collection {collection_name} count: {count}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Upload Telegram messages/transcripts to Weaviate")
    parser.add_argument("--upload-only", action="store_true",
                        help="Skip collection creation, only upload")
    parser.add_argument("--create-only", action="store_true",
                        help="Only create collections, don't upload")
    parser.add_argument("--messages-only", action="store_true",
                        help="Only process messages")
    parser.add_argument("--transcripts-only", action="store_true",
                        help="Only process transcripts")
    parser.add_argument("--all", action="store_true",
                        help="Re-upload all (ignore watermark)")
    parser.add_argument("--since", metavar="TIMESTAMP",
                        help="Upload messages posted after this timestamp (ISO format)")
    parser.add_argument("--database", default="substack",
                        help="PostgreSQL database name (default: substack)")
    parser.add_argument("--channel",
                        help="Filter to channel (username, name, or numeric ID)")
    parser.add_argument("--use-cache", action="store_true",
                        help="Use cached vectors to skip embedding API calls")
    parser.add_argument("--recreate", action="store_true",
                        help="Delete and recreate collections without prompting")
    args = parser.parse_args()

    database = args.database
    do_messages = not args.transcripts_only
    do_transcripts = not args.messages_only

    # Collection names (only EngRu for now; add Podcasts variants when needed)
    messages_collection = TELEGRAM_MESSAGES_COLLECTION
    transcripts_collection = TELEGRAM_TRANSCRIPTS_COLLECTION

    # Determine the cutoff for incremental upload
    since = None
    if args.since:
        since = args.since
    elif not args.all:
        since = get_last_upload_time(database)

    upload_start = datetime.now(timezone.utc).isoformat()

    client = get_client()
    try:
        print(f"Connected to Weaviate (ready: {client.is_ready()})")
        print(f"Database: {database} → Messages: {messages_collection}, "
              f"Transcripts: {transcripts_collection}")

        if since:
            print(f"Incremental upload: records after {since}")
        else:
            print("Full upload: all records")

        if args.channel:
            print(f"Channel filter: {args.channel}")

        # Create collections
        if not args.upload_only:
            if do_messages:
                create_messages_collection(client, messages_collection,
                                           recreate=args.recreate)
            if do_transcripts:
                create_transcripts_collection(client, transcripts_collection,
                                              recreate=args.recreate)

        if args.create_only:
            return

        # Read from PostgreSQL
        print(f"\nReading from PostgreSQL ({database}, telegram schema)...")
        with DatabaseConnection(database=database, schema="telegram") as conn:
            if do_messages:
                messages = fetch_messages(conn, since=since, channel=args.channel)
                print(f"  {len(messages)} messages")

            if do_transcripts:
                transcripts = fetch_transcripts(conn, since=since,
                                                channel=args.channel)
                print(f"  {len(transcripts)} transcripts")

            # Load vector caches if requested
            messages_cache = None
            transcripts_cache = None
            if args.use_cache:
                if do_messages:
                    messages_cache = load_cache(messages_collection)
                if do_transcripts:
                    transcripts_cache = load_cache(transcripts_collection)

            # Upload messages
            if do_messages:
                if messages:
                    print(f"\n{'='*60}")
                    print("UPLOADING MESSAGES")
                    print(f"{'='*60}")
                    upload_messages(client, messages, messages_collection,
                                   vector_cache=messages_cache)
                else:
                    print("\nNo new messages to upload.")

            # Upload transcripts (needs conn for segment fetching)
            if do_transcripts:
                if transcripts:
                    print(f"\n{'='*60}")
                    print("UPLOADING TRANSCRIPTS")
                    print(f"{'='*60}")
                    upload_transcripts(client, transcripts, conn,
                                      transcripts_collection,
                                      vector_cache=transcripts_cache)
                else:
                    print("\nNo new transcripts to upload.")

        # Save watermark on success (skip if channel-filtered to avoid
        # advancing past other channels)
        if not args.channel:
            save_upload_time(upload_start, database)
            print(f"\nWatermark saved: {upload_start}")
        else:
            print(f"\nSkipped watermark save (channel filter active)")

    finally:
        client.close()


if __name__ == "__main__":
    main()
