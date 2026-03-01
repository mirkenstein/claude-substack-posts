#!/usr/bin/env python3
"""Upload YouTube video transcripts to Weaviate using chapter-aware chunking.

Parses chapter markers from video descriptions, populates youtube.video_chapters,
splits transcript segments at exact chapter boundaries, and uploads to a dedicated
Weaviate collection. Videos without chapters fall back to fixed-window chunking.

Usage:
    python upload_videos_chapters.py --database podcasts         # incremental
    python upload_videos_chapters.py --database podcasts --all   # re-upload all
    python upload_videos_chapters.py --database podcasts --upload-only
    python upload_videos_chapters.py --database podcasts --create-only
    python upload_videos_chapters.py --database podcasts --since '2026-02-28'
"""

import argparse
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
from config import get_client, VIDEO_CHAPTER_PODCASTS_COLLECTION

from weaviate.classes.config import Configure, Property, DataType

# Chunking settings (same as upload_videos.py)
CHUNK_SIZE = 1000
OVERLAP = 250
MIN_CHUNK_SIZE = 500

tokenizer = tiktoken.get_encoding("cl100k_base")
UUID_NS = uuid.NAMESPACE_DNS


# ---------------------------------------------------------------------------
# Chapter parsing
# ---------------------------------------------------------------------------

def parse_chapters_from_description(description: str) -> list[dict]:
    """Parse timestamp chapter markers from a video description.

    Matches patterns like:
        0:00 Introduction
        7:28 Topic One
        1:02:17 Deep Dive
    """
    if not description:
        return []

    chapters = []
    for match in re.finditer(
        r'(?:^|\n)\s*(?:(\d+):)?(\d{1,2}):(\d{2})\s+(.+)',
        description
    ):
        hours = int(match.group(1)) if match.group(1) else 0
        minutes = int(match.group(2))
        seconds = int(match.group(3))
        title = match.group(4).strip()
        start_seconds = hours * 3600 + minutes * 60 + seconds
        chapters.append({
            "start_seconds": start_seconds,
            "title": title,
        })

    # Sort by time and assign positions
    chapters.sort(key=lambda c: c["start_seconds"])
    for i, ch in enumerate(chapters):
        ch["position"] = i

    return chapters


def populate_video_chapters(conn, video_id: str, chapters: list[dict]):
    """Insert chapters into youtube.video_chapters with ON CONFLICT DO NOTHING."""
    if not chapters:
        return
    with conn.cursor() as cur:
        for ch in chapters:
            cur.execute(
                """INSERT INTO youtube.video_chapters (video_id, position, start_seconds, title)
                   VALUES (%s, %s, %s, %s)
                   ON CONFLICT DO NOTHING""",
                (video_id, ch["position"], ch["start_seconds"], ch["title"])
            )
    conn.commit()


# ---------------------------------------------------------------------------
# Tokenization / chunking
# ---------------------------------------------------------------------------

def count_tokens(text: str) -> int:
    return len(tokenizer.encode(text))


def chunk_by_tokens(text: str) -> list[str]:
    """Split text into overlapping chunks by token count."""
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

def create_collection(client, collection_name):
    """Create chapter-aware video chunk collection with JinaAI v3 vectorizer."""
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
        description=f"Chapter-aware YouTube transcript chunks ({collection_name}) with JinaAI v3 embeddings",
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
            Property(name="chapterTitle", data_type=DataType.TEXT,
                     index_searchable=True,
                     skip_vectorization=True),
            Property(name="chapterNumber", data_type=DataType.INT),
            Property(name="chapterStartTime", data_type=DataType.NUMBER),
            Property(name="chunkMethod", data_type=DataType.TEXT,
                     index_filterable=True),
            Property(name="chunkNumber", data_type=DataType.INT),
            Property(name="totalChunks", data_type=DataType.INT),
            Property(name="chunkTokens", data_type=DataType.INT),
        ],
    )
    print(f"  Created {collection_name}")


# ---------------------------------------------------------------------------
# Watermark
# ---------------------------------------------------------------------------

def _watermark_file(database: str) -> Path:
    return Path(__file__).parent / f".last_upload_videos_chapters_{database}"


def get_last_upload_time(database: str) -> str | None:
    wf = _watermark_file(database)
    if wf.exists():
        return wf.read_text().strip()
    return None


def save_upload_time(timestamp: str, database: str):
    _watermark_file(database).write_text(timestamp)


# ---------------------------------------------------------------------------
# DB queries
# ---------------------------------------------------------------------------

VIDEOS_WITH_SEGMENTS_QUERY = """
SELECT DISTINCT
    ts.video_id,
    vt.title,
    vt.description,
    vt.channel_name,
    vt.url,
    vt.upload_date,
    p.title AS playlist_name
FROM youtube.transcript_segments ts
JOIN youtube.video_transcripts vt ON vt.video_id = ts.video_id
LEFT JOIN youtube.playlist_videos pv ON pv.video_id = ts.video_id
LEFT JOIN youtube.playlists p ON p.playlist_id = pv.playlist_id
{where}
ORDER BY vt.upload_date
"""

SEGMENTS_QUERY = """
SELECT segment_index, start_seconds, end_seconds, text
FROM youtube.transcript_segments
WHERE video_id = %s
ORDER BY start_seconds
"""

CHAPTERS_QUERY = """
SELECT position, start_seconds, title
FROM youtube.video_chapters
WHERE video_id = %s
ORDER BY position
"""

VIDEOS_WITHOUT_CHAPTERS_QUERY = """
SELECT ts.video_id
FROM youtube.transcript_segments ts
LEFT JOIN youtube.video_chapters vc ON vc.video_id = ts.video_id
WHERE vc.video_id IS NULL
GROUP BY ts.video_id
"""


# ---------------------------------------------------------------------------
# Chapter-aware chunking
# ---------------------------------------------------------------------------

def group_segments_by_chapter(segments: list[dict], chapters: list[dict]) -> list[dict]:
    """Group transcript segments into chapters.

    Each segment is assigned to the chapter whose start_seconds is <= segment.start_seconds
    and before the next chapter's start_seconds.

    Returns list of dicts with chapter metadata and concatenated text.
    """
    if not chapters or not segments:
        return []

    # Build chapter boundaries
    chapter_entries = []
    for i, ch in enumerate(chapters):
        next_start = chapters[i + 1]["start_seconds"] if i + 1 < len(chapters) else float("inf")
        chapter_entries.append({
            "position": ch["position"],
            "title": ch["title"],
            "start_seconds": ch["start_seconds"],
            "next_start": next_start,
            "texts": [],
        })

    # Assign each segment to its chapter
    for seg in segments:
        seg_start = seg["start_seconds"]
        for entry in reversed(chapter_entries):
            if seg_start >= entry["start_seconds"]:
                entry["texts"].append(seg["text"])
                break

    # Build result — merge small chapters with adjacent
    result = []
    for entry in chapter_entries:
        text = " ".join(entry["texts"]).strip()
        if not text:
            continue
        result.append({
            "chapterTitle": entry["title"],
            "chapterNumber": entry["position"],
            "chapterStartTime": entry["start_seconds"],
            "text": text,
        })

    # Merge small chapters with next chapter
    merged = []
    i = 0
    while i < len(result):
        current = result[i]
        tok_count = count_tokens(current["text"])
        # Merge forward if too small and not the last
        if tok_count < MIN_CHUNK_SIZE and i + 1 < len(result):
            next_ch = result[i + 1]
            next_ch["text"] = current["text"] + " " + next_ch["text"]
            next_ch["chapterTitle"] = current["chapterTitle"] + " / " + next_ch["chapterTitle"]
            next_ch["chapterStartTime"] = current["chapterStartTime"]
            next_ch["chapterNumber"] = current["chapterNumber"]
            i += 1
            continue
        merged.append(current)
        i += 1

    return merged


def build_chapter_chunks(video_meta: dict, segments: list[dict],
                         chapters: list[dict]) -> list[dict]:
    """Build chunks for a video using chapter-aware splitting."""
    shared = {
        "videoId": video_meta["video_id"],
        "videoTitle": video_meta["title"] or "",
        "description": video_meta.get("description") or "",
        "channelName": video_meta["channel_name"] or "",
        "videoUrl": video_meta["url"] or "",
        "uploadDate": (video_meta["upload_date"].isoformat() + "T00:00:00Z"
                       if video_meta["upload_date"] else ""),
        "playlistName": video_meta["playlist_name"] or "",
    }

    chapter_groups = group_segments_by_chapter(segments, chapters)
    all_chunks = []
    global_chunk_num = 0

    for group in chapter_groups:
        chapter_meta = {
            "chapterTitle": group["chapterTitle"],
            "chapterNumber": group["chapterNumber"],
            "chapterStartTime": float(group["chapterStartTime"]),
            "chunkMethod": "chapter_exact",
        }

        text = group["text"]
        tok_count = count_tokens(text)

        if tok_count <= CHUNK_SIZE:
            # Single chunk for this chapter
            all_chunks.append({
                **shared,
                **chapter_meta,
                "transcript": text,
                "chunkNumber": global_chunk_num,
                "chunkTokens": tok_count,
                "totalChunks": 0,  # placeholder
            })
            global_chunk_num += 1
        else:
            # Sub-chunk large chapter
            sub_chunks = chunk_by_tokens(text)
            for sub_chunk_text in sub_chunks:
                sub_tok = count_tokens(sub_chunk_text)
                # Merge small trailing sub-chunk
                if sub_tok < MIN_CHUNK_SIZE and all_chunks and \
                        all_chunks[-1]["chapterNumber"] == chapter_meta["chapterNumber"]:
                    all_chunks[-1]["transcript"] += " " + sub_chunk_text
                    all_chunks[-1]["chunkTokens"] = count_tokens(all_chunks[-1]["transcript"])
                    continue
                all_chunks.append({
                    **shared,
                    **chapter_meta,
                    "transcript": sub_chunk_text,
                    "chunkNumber": global_chunk_num,
                    "chunkTokens": sub_tok,
                    "totalChunks": 0,  # placeholder
                })
                global_chunk_num += 1

    # Set totalChunks
    total = len(all_chunks)
    for c in all_chunks:
        c["totalChunks"] = total

    return all_chunks


def build_fixed_chunks(video_meta: dict, segments: list[dict]) -> list[dict]:
    """Fall back to fixed-window chunking for videos without chapters."""
    full_text = " ".join(seg["text"] for seg in segments).strip()
    if not full_text:
        return []

    shared = {
        "videoId": video_meta["video_id"],
        "videoTitle": video_meta["title"] or "",
        "description": video_meta.get("description") or "",
        "channelName": video_meta["channel_name"] or "",
        "videoUrl": video_meta["url"] or "",
        "uploadDate": (video_meta["upload_date"].isoformat() + "T00:00:00Z"
                       if video_meta["upload_date"] else ""),
        "playlistName": video_meta["playlist_name"] or "",
        "chapterTitle": "",
        "chapterNumber": 0,
        "chapterStartTime": 0.0,
        "chunkMethod": "fixed_window",
    }

    text_chunks = chunk_by_tokens(full_text)
    chunks = []
    for idx, chunk_text in enumerate(text_chunks):
        tok_count = count_tokens(chunk_text)
        if tok_count < MIN_CHUNK_SIZE and idx > 0 and chunks:
            chunks[-1]["transcript"] += " " + chunk_text
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

def upload_chunks(client, all_chunks: list[dict], collection_name: str):
    """Upload chunks to Weaviate with deterministic UUIDs."""
    collection = client.collections.get(collection_name)

    print(f"Uploading {len(all_chunks)} chunks to {collection_name}...")
    start = time.time()
    uploaded = 0
    errors = 0

    with collection.batch.dynamic() as batch:
        for i, chunk in enumerate(all_chunks):
            obj_uuid = uuid.uuid5(
                UUID_NS,
                f"chapter-video-{chunk['videoId']}-ch{chunk['chapterNumber']}-{chunk['chunkNumber']}"
            )
            transcript = chunk.pop("transcript")
            try:
                batch.add_object(
                    properties={"transcript": transcript, **chunk},
                    uuid=obj_uuid,
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
    print(f"Done: {uploaded} uploaded, {errors} errors in {elapsed:.1f}s")

    count = collection.aggregate.over_all(total_count=True).total_count
    print(f"Collection count: {count}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Upload chapter-aware video transcript chunks to Weaviate")
    parser.add_argument("--all", action="store_true",
                        help="Re-upload all videos (ignore watermark)")
    parser.add_argument("--upload-only", action="store_true",
                        help="Skip collection creation, only upload")
    parser.add_argument("--create-only", action="store_true",
                        help="Only create collection, don't upload")
    parser.add_argument("--since", metavar="TIMESTAMP",
                        help="Upload videos with segments added after this timestamp")
    parser.add_argument("--database", default="podcasts",
                        help="PostgreSQL database (default: podcasts)")
    args = parser.parse_args()

    database = args.database
    collection_name = VIDEO_CHAPTER_PODCASTS_COLLECTION

    print(f"Database: {database} → Collection: {collection_name}")

    client = get_client()
    try:
        print(f"Connected to Weaviate (ready: {client.is_ready()})")

        if not args.upload_only:
            create_collection(client, collection_name)

        if args.create_only:
            return

        upload_start = datetime.now(timezone.utc).isoformat()

        # Determine watermark cutoff
        since = None
        if args.since:
            since = args.since
        elif not args.all:
            since = get_last_upload_time(database)

        # Build WHERE clause
        where_parts = []
        params = []
        if since:
            where_parts.append("ts.created_at > %s")
            params.append(since)

        where = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""
        query = VIDEOS_WITH_SEGMENTS_QUERY.format(where=where)

        # Fetch video list
        print(f"\nReading videos from PostgreSQL ({database})...")
        if since:
            print(f"  Incremental: segments created after {since}")
        else:
            print(f"  Full upload: all videos with segments")

        with DatabaseConnection(database=database, schema="youtube") as conn:
            with conn.cursor() as cur:
                cur.execute(query, params)
                columns = [desc[0] for desc in cur.description]
                videos = [dict(zip(columns, r)) for r in cur.fetchall()]

            print(f"  {len(videos)} videos to process")

            if not videos:
                print("Nothing new to upload.")
                return

            # Step 1: Populate video_chapters for videos that need it
            print("\nPopulating video chapters from descriptions...")
            with conn.cursor() as cur:
                cur.execute(VIDEOS_WITHOUT_CHAPTERS_QUERY)
                videos_needing_chapters = {r[0] for r in cur.fetchall()}

            chapters_populated = 0
            for video in videos:
                vid = video["video_id"]
                if vid in videos_needing_chapters:
                    chapters = parse_chapters_from_description(video.get("description") or "")
                    if chapters:
                        populate_video_chapters(conn, vid, chapters)
                        chapters_populated += 1

            print(f"  Populated chapters for {chapters_populated} videos")

            # Step 2: Build chunks for each video
            print("\nBuilding chapter-aware chunks...")
            all_chunks = []
            chapter_videos = 0
            fixed_videos = 0

            for video in videos:
                vid = video["video_id"]

                # Fetch segments
                with conn.cursor() as cur:
                    cur.execute(SEGMENTS_QUERY, (vid,))
                    seg_cols = [desc[0] for desc in cur.description]
                    segments = [dict(zip(seg_cols, r)) for r in cur.fetchall()]

                if not segments:
                    continue

                # Fetch chapters
                with conn.cursor() as cur:
                    cur.execute(CHAPTERS_QUERY, (vid,))
                    ch_cols = [desc[0] for desc in cur.description]
                    chapters = [dict(zip(ch_cols, r)) for r in cur.fetchall()]

                title_preview = (video["title"] or "")[:50]
                if chapters:
                    chunks = build_chapter_chunks(video, segments, chapters)
                    chapter_videos += 1
                    method = f"{len(chapters)} chapters"
                else:
                    chunks = build_fixed_chunks(video, segments)
                    fixed_videos += 1
                    method = "fixed_window"

                print(f"  {vid} | \"{title_preview}\" | {method} → {len(chunks)} chunks")
                all_chunks.extend(chunks)

            print(f"\n  Total: {len(all_chunks)} chunks from {len(videos)} videos "
                  f"({chapter_videos} chapter-aware, {fixed_videos} fixed-window)")

        if not all_chunks:
            print("No chunks to upload.")
            return

        # Step 3: Upload
        upload_chunks(client, all_chunks, collection_name)

        # Save watermark
        save_upload_time(upload_start, database)
        print(f"Watermark saved: {upload_start}")

    finally:
        client.close()


if __name__ == "__main__":
    main()
