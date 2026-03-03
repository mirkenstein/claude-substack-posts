#!/usr/bin/env python3
"""Upload YouTube video chapters to ChromaDB with Jina AI embeddings.

Mirrors motherduck/upload_video_chapters.py: reads from youtube.video_chapters
and youtube.transcript_segments, groups segments by chapter boundaries.
Videos without chapters get a single "Full Transcript" row.

Requires JINA_API_KEY environment variable.

Usage:
    JINA_API_KEY=... python upload_video_chapters.py                         # all from both DBs
    JINA_API_KEY=... python upload_video_chapters.py --database podcasts     # podcasts DB only
    JINA_API_KEY=... python upload_video_chapters.py --database substack     # substack DB only
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
from config import get_client, get_jina_ef, VIDEO_CHAPTERS_COLLECTION

MAX_TOKENS = 7500
BATCH_SIZE = 50

tokenizer = tiktoken.get_encoding("cl100k_base")


def count_tokens(text: str) -> int:
    return len(tokenizer.encode(text))


def truncate_to_tokens(text: str, max_tokens: int = MAX_TOKENS) -> str:
    tokens = tokenizer.encode(text)
    if len(tokens) <= max_tokens:
        return text
    return tokenizer.decode(tokens[:max_tokens])


ALL_VIDEOS_QUERY = """
SELECT DISTINCT ON (vt.video_id)
    vt.video_id,
    vt.title,
    vt.description,
    vt.channel_name,
    vt.url,
    vt.upload_date,
    p.title AS playlist_name
FROM youtube.video_transcripts vt
JOIN youtube.transcript_segments ts ON ts.video_id = vt.video_id
LEFT JOIN youtube.playlist_videos pv ON pv.video_id = vt.video_id
LEFT JOIN youtube.playlists p ON p.playlist_id = pv.playlist_id
ORDER BY vt.video_id, p.title
"""

CHAPTERS_QUERY = """
SELECT position, start_seconds, title
FROM youtube.video_chapters
WHERE video_id = %s
ORDER BY position
"""

SEGMENTS_QUERY = """
SELECT start_seconds, end_seconds, text
FROM youtube.transcript_segments
WHERE video_id = %s
ORDER BY start_seconds
"""


def build_chapter_rows(video: dict, segments: list[dict],
                       chapters: list[dict], source_database: str) -> list[dict]:
    if not chapters or not segments:
        return []

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

    for seg in segments:
        seg_start = seg["start_seconds"]
        for entry in reversed(chapter_entries):
            if seg_start >= entry["start_seconds"]:
                entry["texts"].append(seg["text"])
                break

    rows = []
    for entry in chapter_entries:
        text = " ".join(entry["texts"]).strip()
        if not text:
            continue
        rows.append({
            "videoId": video["video_id"],
            "chapterNumber": entry["position"],
            "chapterTitle": entry["title"],
            "startSeconds": int(entry["start_seconds"]),
            "videoTitle": video["title"] or "",
            "channelName": video["channel_name"] or "",
            "videoUrl": video["url"] or "",
            "uploadDate": video["upload_date"].isoformat() if video["upload_date"] else "",
            "playlistName": video["playlist_name"] or "",
            "sourceDatabase": source_database,
            "hasChapters": True,
            "transcript": text,
            "transcriptTokens": count_tokens(text),
        })
    return rows


def build_full_transcript_row(video: dict, segments: list[dict],
                              source_database: str) -> dict | None:
    text = " ".join(seg["text"] for seg in segments).strip()
    if not text:
        return None
    return {
        "videoId": video["video_id"],
        "chapterNumber": 0,
        "chapterTitle": "Full Transcript",
        "startSeconds": 0,
        "videoTitle": video["title"] or "",
        "channelName": video["channel_name"] or "",
        "videoUrl": video["url"] or "",
        "uploadDate": video["upload_date"].isoformat() if video["upload_date"] else "",
        "playlistName": video["playlist_name"] or "",
        "sourceDatabase": source_database,
        "hasChapters": False,
        "transcript": text,
        "transcriptTokens": count_tokens(text),
    }


def main():
    parser = argparse.ArgumentParser(
        description="Upload YouTube video chapters to ChromaDB"
    )
    parser.add_argument("--database",
                        help="PostgreSQL database (podcasts, substack, or both if omitted)")
    args = parser.parse_args()

    databases = [args.database] if args.database else ["podcasts", "substack"]

    all_rows = []
    for database in databases:
        print(f"Reading videos from PostgreSQL ({database})...")
        with DatabaseConnection(database=database, schema="youtube") as conn:
            with conn.cursor() as cur:
                cur.execute(ALL_VIDEOS_QUERY)
                columns = [desc[0] for desc in cur.description]
                videos = [dict(zip(columns, r)) for r in cur.fetchall()]
            print(f"  {len(videos)} videos with segments")

            chapter_videos = 0
            fixed_videos = 0
            for video in videos:
                vid = video["video_id"]

                with conn.cursor() as cur:
                    cur.execute(SEGMENTS_QUERY, (vid,))
                    seg_cols = [desc[0] for desc in cur.description]
                    segments = [dict(zip(seg_cols, r)) for r in cur.fetchall()]

                if not segments:
                    continue

                with conn.cursor() as cur:
                    cur.execute(CHAPTERS_QUERY, (vid,))
                    ch_cols = [desc[0] for desc in cur.description]
                    chapters = [dict(zip(ch_cols, r)) for r in cur.fetchall()]

                if chapters:
                    rows = build_chapter_rows(video, segments, chapters, database)
                    chapter_videos += 1
                else:
                    row = build_full_transcript_row(video, segments, database)
                    rows = [row] if row else []
                    fixed_videos += 1

                all_rows.extend(rows)

            print(f"  [{database}] {chapter_videos} with chapters, {fixed_videos} full transcript")

    print(f"\nTotal chapter rows: {len(all_rows)}")
    if not all_rows:
        print("No data to upload.")
        return

    client = get_client()
    jina_ef = get_jina_ef()

    # Create collection if it doesn't exist
    existing = [c.name for c in client.list_collections()]
    if VIDEO_CHAPTERS_COLLECTION not in existing:
        client.create_collection(
            name=VIDEO_CHAPTERS_COLLECTION,
            embedding_function=jina_ef,
            metadata={"hnsw:space": "cosine"},
        )
        print(f"Created collection: {VIDEO_CHAPTERS_COLLECTION}")

    collection = client.get_collection(VIDEO_CHAPTERS_COLLECTION, embedding_function=jina_ef)

    print(f"Uploading to {VIDEO_CHAPTERS_COLLECTION}...")
    start = time.time()
    uploaded = 0

    for i in range(0, len(all_rows), BATCH_SIZE):
        batch = all_rows[i:i + BATCH_SIZE]

        ids = [f"chapter-{r['sourceDatabase']}-{r['videoId']}-ch{r['chapterNumber']}" for r in batch]
        documents = [truncate_to_tokens(r["transcript"]) for r in batch]
        metadatas = [{k: v for k, v in r.items() if k != "transcript"} for r in batch]

        try:
            collection.upsert(ids=ids, documents=documents, metadatas=metadatas)
        except RuntimeError as e:
            if "rate limit" in str(e).lower():
                print(f"  Rate limited at {uploaded}/{len(all_rows)}, waiting 60s...")
                time.sleep(60)
                collection.upsert(ids=ids, documents=documents, metadatas=metadatas)
            else:
                raise

        uploaded += len(batch)
        if uploaded % 500 < BATCH_SIZE:
            elapsed = time.time() - start
            print(f"  {uploaded}/{len(all_rows)} ({uploaded/elapsed:.0f}/sec)")

    elapsed = time.time() - start
    print(f"Done: {uploaded} uploaded in {elapsed:.1f}s ({uploaded/elapsed:.0f}/sec)")
    print(f"Collection count: {collection.count()}")


if __name__ == "__main__":
    main()
