#!/usr/bin/env python3
"""Upload YouTube videos and chapters to MotherDuck for catalog browsing and semantic search.

Two tables:
  - youtube_videos:   one row per video, embed description
  - youtube_chapters: one row per chapter, embed transcript

Videos without chapters get a single youtube_chapters row with full transcript.

Usage:
    python motherduck/upload_video_chapters.py                         # full reload from both DBs
    python motherduck/upload_video_chapters.py --database podcasts     # podcasts DB only
    python motherduck/upload_video_chapters.py --database substack     # substack DB only
    python motherduck/upload_video_chapters.py --skip-embeddings       # insert only
    python motherduck/upload_video_chapters.py --embed-only            # only run embeddings
"""

import argparse
import re
import sys
import time
from pathlib import Path

import duckdb
import tiktoken

sys.path.insert(0, str(Path(__file__).parent.parent))
from src.db.connection import DatabaseConnection

MOTHERDUCK_DB = "md:my_db"

tokenizer = tiktoken.get_encoding("cl100k_base")
# MotherDuck embedding() uses text-embedding-3-small with 8191 token limit
MAX_EMBED_TOKENS = 8000


def count_tokens(text: str) -> int:
    return len(tokenizer.encode(text))


def truncate_to_tokens(text: str, max_tokens: int = MAX_EMBED_TOKENS) -> str:
    tokens = tokenizer.encode(text)
    if len(tokens) <= max_tokens:
        return text
    return tokenizer.decode(tokens[:max_tokens])


# ---------------------------------------------------------------------------
# Chapter parsing (from weaviate/upload_videos_chapters.py)
# ---------------------------------------------------------------------------

def parse_chapters_from_description(description: str) -> list[dict]:
    if not description:
        return []

    chapters = []
    for match in re.finditer(
        r'(?:^|\n)\s*(?:[Cc]hapters?:\s*)?(?:(\d+):)?(\d{1,2}):(\d{2})\s+(.+)',
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

    chapters.sort(key=lambda c: c["start_seconds"])
    for i, ch in enumerate(chapters):
        ch["position"] = i
    return chapters


def populate_video_chapters(conn, video_id: str, chapters: list[dict]):
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
# PostgreSQL queries
# ---------------------------------------------------------------------------

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

VIDEOS_WITHOUT_CHAPTERS_QUERY = """
SELECT ts.video_id
FROM youtube.transcript_segments ts
LEFT JOIN youtube.video_chapters vc ON vc.video_id = ts.video_id
WHERE vc.video_id IS NULL
GROUP BY ts.video_id
"""


# ---------------------------------------------------------------------------
# Build chapter rows by grouping segments into chapters
# ---------------------------------------------------------------------------

def build_chapter_rows(video: dict, segments: list[dict],
                       chapters: list[dict]) -> list[dict]:
    """Group segments by chapter boundaries, return one row per chapter."""
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

    # Assign segments to chapters
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
            "video_id": video["video_id"],
            "chapter_number": entry["position"],
            "chapter_title": entry["title"],
            "start_seconds": entry["start_seconds"],
            "source_database": video.get("source_database", ""),
            "transcript": text,
            "transcript_tokens": count_tokens(text),
        })
    return rows


def build_full_transcript_row(video: dict, segments: list[dict]) -> dict | None:
    """Single row for videos without chapters."""
    text = " ".join(seg["text"] for seg in segments).strip()
    if not text:
        return None
    return {
        "video_id": video["video_id"],
        "chapter_number": 0,
        "chapter_title": "Full Transcript",
        "start_seconds": 0,
        "source_database": video.get("source_database", ""),
        "transcript": text,
        "transcript_tokens": count_tokens(text),
    }


# ---------------------------------------------------------------------------
# MotherDuck table creation
# ---------------------------------------------------------------------------

CREATE_VIDEOS_SQL = """
CREATE OR REPLACE TABLE youtube_videos (
    video_id        VARCHAR,
    title           VARCHAR,
    description     VARCHAR,
    channel_name    VARCHAR,
    url             VARCHAR,
    upload_date     DATE,
    playlist_name   VARCHAR,
    source_database VARCHAR,
    chapter_count   INTEGER,
    has_chapters    BOOLEAN,
    description_embedding FLOAT[512]
);
"""

CREATE_CHAPTERS_SQL = """
CREATE OR REPLACE TABLE youtube_chapters (
    video_id            VARCHAR,
    chapter_number      INTEGER,
    chapter_title       VARCHAR,
    start_seconds       INTEGER,
    source_database     VARCHAR,
    transcript          VARCHAR,
    transcript_tokens   INTEGER,
    transcript_embedding FLOAT[512]
);
"""


# ---------------------------------------------------------------------------
# Insert helpers
# ---------------------------------------------------------------------------

def insert_videos(conn: duckdb.DuckDBPyConnection, videos: list[dict],
                  chapter_counts: dict[str, int]):
    insert_sql = """
    INSERT INTO youtube_videos (
        video_id, title, description, channel_name, url,
        upload_date, playlist_name, source_database, chapter_count, has_chapters
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """
    values = [
        (
            v["video_id"], v["title"] or "", v["description"] or "",
            v["channel_name"] or "", v["url"] or "",
            v["upload_date"], v["playlist_name"] or "",
            v.get("source_database", ""),
            chapter_counts.get(v["video_id"], 0),
            chapter_counts.get(v["video_id"], 0) > 0,
        )
        for v in videos
    ]
    conn.executemany(insert_sql, values)
    print(f"  Inserted {len(values)} videos")


def insert_chapters(conn: duckdb.DuckDBPyConnection, chapters: list[dict],
                    batch_size: int = 200):
    insert_sql = """
    INSERT INTO youtube_chapters (
        video_id, chapter_number, chapter_title, start_seconds,
        source_database, transcript, transcript_tokens
    ) VALUES (?, ?, ?, ?, ?, ?, ?)
    """
    start = time.time()
    for i in range(0, len(chapters), batch_size):
        batch = chapters[i:i + batch_size]
        values = [
            (
                c["video_id"], c["chapter_number"], c["chapter_title"],
                c["start_seconds"], c.get("source_database", ""),
                c["transcript"], c["transcript_tokens"],
            )
            for c in batch
        ]
        conn.executemany(insert_sql, values)
        inserted = min(i + batch_size, len(chapters))
        elapsed = time.time() - start
        rate = inserted / elapsed if elapsed > 0 else 0
        print(f"  Inserted {inserted}/{len(chapters)} chapters ({rate:.0f}/sec)")


# ---------------------------------------------------------------------------
# Embeddings
# ---------------------------------------------------------------------------

def run_embeddings(conn: duckdb.DuckDBPyConnection):
    # Videos — embed description (truncated for long descriptions)
    result = conn.execute(
        "SELECT count(*) FROM youtube_videos WHERE description_embedding IS NULL"
    ).fetchone()
    pending = result[0]
    if pending > 0:
        print(f"Generating embeddings for {pending} videos...")
        start = time.time()
        conn.execute("""
            UPDATE youtube_videos
            SET description_embedding = embedding(
                title || ' ' || LEFT(description, 20000)
            )
            WHERE description_embedding IS NULL
        """)
        elapsed = time.time() - start
        print(f"  Done in {elapsed:.1f}s")

    # Chapters — embed transcript (truncated to MAX_EMBED_TOKENS worth of chars)
    # ~4 chars per token, so 8000 tokens ≈ 32000 chars is a safe truncation
    result = conn.execute(
        "SELECT count(*) FROM youtube_chapters WHERE transcript_embedding IS NULL"
    ).fetchone()
    pending = result[0]
    if pending > 0:
        print(f"Generating embeddings for {pending} chapters...")
        start = time.time()
        conn.execute("""
            UPDATE youtube_chapters
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

def create_fts_indexes(conn: duckdb.DuckDBPyConnection):
    for table, id_col, cols in [
        ("youtube_videos", "video_id", ["title", "description", "channel_name", "playlist_name"]),
        ("youtube_chapters", "video_id", ["chapter_title", "transcript"]),
    ]:
        cols_str = ", ".join(f"'{c}'" for c in cols)
        try:
            conn.execute(f"PRAGMA drop_fts_index('{table}')")
        except Exception:
            pass

        print(f"Creating FTS index on {table} ({', '.join(cols)})...")
        start = time.time()
        # youtube_chapters needs a unique ID for FTS — use rowid workaround
        conn.execute(f"""
            PRAGMA create_fts_index(
                '{table}', '{id_col}', {cols_str},
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
    result = conn.execute("SELECT count(*) FROM youtube_videos").fetchone()
    print(f"  Videos: {result[0]}")

    result = conn.execute(
        "SELECT count(*) FROM youtube_videos WHERE description_embedding IS NOT NULL"
    ).fetchone()
    print(f"  Videos with embeddings: {result[0]}")

    result = conn.execute(
        "SELECT count(*), count(DISTINCT video_id) FROM youtube_chapters"
    ).fetchone()
    print(f"  Chapters: {result[0]} across {result[1]} videos")

    result = conn.execute(
        "SELECT count(*) FROM youtube_chapters WHERE transcript_embedding IS NOT NULL"
    ).fetchone()
    print(f"  Chapters with embeddings: {result[0]}")

    result = conn.execute("""
        SELECT has_chapters, count(*) FROM youtube_videos GROUP BY has_chapters
    """).fetchall()
    for row in result:
        label = "with chapters" if row[0] else "no chapters (full transcript)"
        print(f"  {label}: {row[1]} videos")

    result = conn.execute("""
        SELECT source_database, channel_name, count(*) AS videos,
               (SELECT count(*) FROM youtube_chapters yc
                WHERE yc.video_id IN (
                    SELECT video_id FROM youtube_videos yv2
                    WHERE yv2.channel_name = yv.channel_name
                    AND yv2.source_database = yv.source_database
                )) AS chapters
        FROM youtube_videos yv
        GROUP BY source_database, channel_name
        ORDER BY source_database, videos DESC
    """).fetchall()
    print("  By database/channel:")
    for row in result:
        print(f"    [{row[0]}] {row[1]}: {row[2]} videos, {row[3]} chapters")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Upload YouTube videos and chapters to MotherDuck"
    )
    parser.add_argument("--database",
                        help="PostgreSQL database (podcasts, substack, or both if omitted)")
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

        databases = [args.database] if args.database else ["podcasts", "substack"]

        all_videos = []
        all_chapter_rows = []
        chapter_counts = {}
        total_chapter_videos = 0
        total_fixed_videos = 0

        for database in databases:
            print(f"\nReading videos from PostgreSQL ({database})...")
            with DatabaseConnection(database=database, schema="youtube") as conn:
                with conn.cursor() as cur:
                    cur.execute(ALL_VIDEOS_QUERY)
                    columns = [desc[0] for desc in cur.description]
                    videos = [dict(zip(columns, r)) for r in cur.fetchall()]

                for v in videos:
                    v["source_database"] = database
                print(f"  {len(videos)} videos")

                # Populate chapters for videos that need it
                print(f"  Populating video chapters from descriptions...")
                with conn.cursor() as cur:
                    cur.execute(VIDEOS_WITHOUT_CHAPTERS_QUERY)
                    videos_needing_chapters = {r[0] for r in cur.fetchall()}

                chapters_populated = 0
                for video in videos:
                    vid = video["video_id"]
                    if vid in videos_needing_chapters:
                        chapters = parse_chapters_from_description(
                            video.get("description") or ""
                        )
                        if chapters:
                            populate_video_chapters(conn, vid, chapters)
                            chapters_populated += 1
                print(f"  Populated chapters for {chapters_populated} videos")

                # Build chapter rows for each video
                print(f"  Building chapter rows...")
                chapter_videos = 0
                fixed_videos = 0

                for video in videos:
                    vid = video["video_id"]

                    with conn.cursor() as cur:
                        cur.execute(SEGMENTS_QUERY, (vid,))
                        seg_cols = [desc[0] for desc in cur.description]
                        segments = [dict(zip(seg_cols, r)) for r in cur.fetchall()]

                    if not segments:
                        chapter_counts[vid] = 0
                        continue

                    with conn.cursor() as cur:
                        cur.execute(CHAPTERS_QUERY, (vid,))
                        ch_cols = [desc[0] for desc in cur.description]
                        chapters = [dict(zip(ch_cols, r)) for r in cur.fetchall()]

                    title_preview = (video["title"] or "")[:50]
                    if chapters:
                        rows = build_chapter_rows(video, segments, chapters)
                        chapter_counts[vid] = len(rows)
                        chapter_videos += 1
                        print(f"  {vid} | \"{title_preview}\" | {len(chapters)} chapters → {len(rows)} rows")
                    else:
                        row = build_full_transcript_row(video, segments)
                        rows = [row] if row else []
                        chapter_counts[vid] = 0
                        fixed_videos += 1
                        print(f"  {vid} | \"{title_preview}\" | full transcript → 1 row")

                    all_chapter_rows.extend(rows)

                all_videos.extend(videos)
                total_chapter_videos += chapter_videos
                total_fixed_videos += fixed_videos
                print(f"  [{database}] {len(videos)} videos ({chapter_videos} with chapters, {fixed_videos} full transcript)")

        videos = all_videos
        print(f"\nTotal: {len(all_chapter_rows)} chapter rows from {len(videos)} videos "
              f"({total_chapter_videos} with chapters, {total_fixed_videos} full transcript)")

        # Create tables
        print("\nCreating MotherDuck tables...")
        md.execute(CREATE_VIDEOS_SQL)
        md.execute(CREATE_CHAPTERS_SQL)

        # Insert videos
        print("\nInserting videos...")
        insert_videos(md, videos, chapter_counts)

        # Insert chapters
        print("\nInserting chapters...")
        insert_chapters(md, all_chapter_rows)

        # Embeddings
        if not args.skip_embeddings:
            print()
            run_embeddings(md)

        # FTS
        print()
        create_fts_indexes(md)

        verify(md)

    finally:
        md.close()


if __name__ == "__main__":
    main()
