#!/usr/bin/env python3
"""Ingest podcast transcripts into Postgres and Weaviate.

Reads episode_transcript.json files, stores individual speaker turns in
transcript_lines, updates posts.content_html with the full transcript,
and uploads chunked text to Weaviate for semantic search.

Usage:
    python ingest_transcripts.py posts/saved/audio/anti-empire/83709910
    python ingest_transcripts.py posts/saved/audio/               # all episodes
    python ingest_transcripts.py posts/saved/audio/ --skip-weaviate
    python ingest_transcripts.py posts/saved/audio/ --dry-run
"""

import argparse
import json
import re
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from src.db.connection import DatabaseConnection


# ── Quality detection ────────────────────────────────────────────────────────

# Non-Latin/Cyrillic unicode ranges that indicate ASR hallucination
_GARBLED_RE = re.compile(
    r'[\u3000-\u9fff'   # CJK
    r'\u1100-\u11ff'    # Hangul Jamo
    r'\uac00-\ud7af'    # Hangul Syllables
    r'\u0e00-\u0e7f'    # Thai
    r'\u0600-\u06ff'    # Arabic
    r'\u0900-\u097f'    # Devanagari
    r']'
)


def classify_quality(text: str) -> str:
    """Classify a transcript segment as clean, garbled, or short."""
    if _GARBLED_RE.search(text):
        return "garbled"
    words = text.split()
    if len(words) <= 3:
        return "short"
    return "clean"


# ── Transcript loading ───────────────────────────────────────────────────────

def load_transcript(episode_dir: Path) -> dict | None:
    """Load episode_transcript.json from an episode directory."""
    json_path = episode_dir / "episode_transcript.json"
    if not json_path.exists():
        return None
    with open(json_path) as f:
        return json.load(f)


def resolve_speaker(speaker_id: str, speaker_map: dict) -> str:
    """Map SPEAKER_00 etc. to the handle/name from speaker_map."""
    return speaker_map.get(speaker_id, speaker_id)


def format_timestamp(seconds: float) -> str:
    """Convert float seconds to HH:MM:SS."""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


# ── Postgres: transcript_lines ───────────────────────────────────────────────

def ingest_transcript_lines(conn, post_id: int, transcript: dict) -> int:
    """Insert transcript segments into transcript_lines. Returns count."""
    speaker_map = transcript.get("speaker_map", {})
    segments = transcript.get("segments", [])

    with conn.cursor() as cur:
        # Clear existing for idempotency
        cur.execute(
            "DELETE FROM substack.transcript_lines WHERE episode_post_id = %s",
            (post_id,),
        )

        for i, seg in enumerate(segments):
            speaker = resolve_speaker(seg["speaker"], speaker_map)
            text = seg["text"]
            quality = classify_quality(text)
            word_count = len(text.split())

            cur.execute("""
                INSERT INTO substack.transcript_lines
                    (episode_post_id, turn_index, speaker, start_time, end_time,
                     content, word_count, quality)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """, (
                post_id, i, speaker, seg["start"], seg["end"],
                text, word_count, quality,
            ))

    conn.commit()
    return len(segments)


# ── Postgres: update content_html ────────────────────────────────────────────

def build_transcript_html(transcript: dict) -> str:
    """Build HTML blob from transcript segments."""
    speaker_map = transcript.get("speaker_map", {})
    segments = transcript.get("segments", [])

    parts = ['<div class="transcript">']
    for seg in segments:
        speaker = resolve_speaker(seg["speaker"], speaker_map)
        ts = format_timestamp(seg["start"])
        text = seg["text"]
        quality = classify_quality(text)

        if quality == "garbled":
            text_html = f'<span class="garbled">[inaudible/garbled]</span> {_escape(text)}'
        else:
            text_html = _escape(text)

        parts.append(f'<p><strong>[{_escape(speaker)}] [{ts}]:</strong></p>')
        parts.append(f'<p>{text_html}</p>')

    parts.append('</div>')
    return "\n".join(parts)


def build_transcript_text(transcript: dict) -> str:
    """Build plain text from transcript segments (for Weaviate)."""
    speaker_map = transcript.get("speaker_map", {})
    segments = transcript.get("segments", [])

    lines = []
    for seg in segments:
        speaker = resolve_speaker(seg["speaker"], speaker_map)
        ts = format_timestamp(seg["start"])
        quality = classify_quality(seg["text"])

        if quality == "garbled":
            lines.append(f"[{speaker}] [{ts}]: [inaudible/garbled]")
        else:
            lines.append(f"[{speaker}] [{ts}]: {seg['text']}")
        lines.append("")

    return "\n".join(lines)


def update_post_content(conn, post_id: int, html: str, plain_text: str):
    """Update posts.content_html and content_text with the transcript."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE substack.posts SET content_html = %s, content_text = %s WHERE id = %s",
            (html, plain_text, post_id),
        )
    conn.commit()


# ── Weaviate upload ──────────────────────────────────────────────────────────

def upload_to_weaviate(post_id: int, transcript: dict, conn):
    """Chunk transcript and upload to Weaviate."""
    sys.path.insert(0, str(Path(__file__).parent / "weaviate"))
    from config import get_client, POSTS_COLLECTION, CHUNK_SIZE, OVERLAP
    import tiktoken

    tokenizer = tiktoken.get_encoding("cl100k_base")

    # Get post metadata from DB
    with conn.cursor() as cur:
        cur.execute("""
            SELECT p.title, p.subtitle, p.post_date, p.slug, p.canonical_url,
                   p.wordcount, p.comment_count, p.restacks, p.audience,
                   a.name AS author_name,
                   pub.name AS publication_name, pub.subdomain
            FROM substack.posts p
            LEFT JOIN substack.authors a ON a.id = p.primary_author_id
            LEFT JOIN substack.publications pub ON pub.id = p.publication_id
            WHERE p.id = %s
        """, (post_id,))
        row = cur.fetchone()

    if not row:
        print(f"  WARNING: post {post_id} not found in DB, skipping Weaviate")
        return

    (title, subtitle, post_date, slug, canonical_url, wordcount,
     comment_count, restacks, audience, author_name,
     publication_name, subdomain) = row

    plain_text = build_transcript_text(transcript)
    total_words = len(plain_text.split())

    # Chunk by tokens
    tokens = tokenizer.encode(plain_text)
    chunks = []
    start = 0
    while start < len(tokens):
        end = start + CHUNK_SIZE
        chunk_text = tokenizer.decode(tokens[start:end])
        chunks.append(chunk_text)
        if end >= len(tokens):
            break
        start = end - OVERLAP

    # Upload to Weaviate
    client = get_client()
    try:
        collection = client.collections.get(POSTS_COLLECTION)

        # Delete existing chunks for this post
        collection.data.delete_many(
            where=weaviate_filter_by_post_id(post_id)
        )

        with collection.batch.dynamic() as batch:
            for i, chunk_text in enumerate(chunks):
                chunk_tokens = len(tokenizer.encode(chunk_text))
                obj = {
                    "postId": str(post_id),
                    "title": title or "",
                    "subtitle": subtitle or "",
                    "content": chunk_text,
                    "chunkNumber": i,
                    "totalChunks": len(chunks),
                    "chunkTokens": chunk_tokens,
                    "postDate": post_date.isoformat() if post_date else "",
                    "authorName": author_name or "",
                    "publicationName": publication_name or "",
                    "subdomain": subdomain or "",
                    "audience": audience or "everyone",
                    "canonicalUrl": canonical_url or "",
                    "slug": slug or "",
                    "wordcount": total_words,
                    "commentCount": comment_count or 0,
                    "restacks": restacks or 0,
                }
                batch.add_object(
                    properties=obj,
                    uuid=uuid.uuid5(uuid.NAMESPACE_URL, f"post-{post_id}-chunk-{i}"),
                )

        print(f"  Weaviate: uploaded {len(chunks)} chunks")
    finally:
        client.close()


def weaviate_filter_by_post_id(post_id: int):
    """Build a Weaviate filter for postId."""
    import weaviate.classes.query as wq
    return wq.Filter.by_property("postId").equal(str(post_id))


# ── Escape helper ────────────────────────────────────────────────────────────

def _escape(text: str) -> str:
    """Basic HTML escaping."""
    return (text
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;"))


# ── Discovery ────────────────────────────────────────────────────────────────

def find_episodes(path: Path) -> list[tuple[Path, int]]:
    """Find episode directories with transcripts. Returns (dir, post_id) pairs."""
    episodes = []

    if path.is_file():
        # Passed a JSON file directly
        post_id = int(path.parent.name)
        episodes.append((path.parent, post_id))
    elif (path / "episode_transcript.json").exists():
        # Passed a single episode directory
        post_id = int(path.name)
        episodes.append((path, post_id))
    else:
        # Scan recursively: audio/<subdomain>/<post_id>/
        for json_file in sorted(path.rglob("episode_transcript.json")):
            episode_dir = json_file.parent
            try:
                post_id = int(episode_dir.name)
                episodes.append((episode_dir, post_id))
            except ValueError:
                continue

    return episodes


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Ingest podcast transcripts")
    parser.add_argument("path", type=Path,
                        help="Episode directory or parent audio directory")
    parser.add_argument("--skip-weaviate", action="store_true",
                        help="Skip Weaviate upload (Postgres only)")
    parser.add_argument("--skip-content-html", action="store_true",
                        help="Don't update posts.content_html")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be ingested without writing")
    args = parser.parse_args()

    episodes = find_episodes(args.path)
    if not episodes:
        print("No episodes with transcripts found.")
        sys.exit(1)

    print(f"Found {len(episodes)} episode(s) to ingest.")

    if args.dry_run:
        for episode_dir, post_id in episodes:
            transcript = load_transcript(episode_dir)
            segments = transcript.get("segments", []) if transcript else []
            speakers = set()
            for seg in segments:
                speakers.add(transcript["speaker_map"].get(seg["speaker"], seg["speaker"]))
            print(f"  {post_id}: {len(segments)} segments, speakers: {', '.join(sorted(speakers))}")
        return

    db = DatabaseConnection()
    try:
        conn = db.connect()

        for episode_dir, post_id in episodes:
            print(f"\nIngesting post {post_id} from {episode_dir}")

            transcript = load_transcript(episode_dir)
            if not transcript:
                print(f"  SKIP: no episode_transcript.json")
                continue

            # Verify post exists
            with conn.cursor() as cur:
                cur.execute("SELECT id, title FROM substack.posts WHERE id = %s", (post_id,))
                row = cur.fetchone()
            if not row:
                print(f"  SKIP: post {post_id} not in database")
                continue
            print(f"  Post: {row[1]}")

            # Step 1: transcript_lines
            count = ingest_transcript_lines(conn, post_id, transcript)
            print(f"  transcript_lines: {count} segments inserted")

            # Step 2: update content_html and content_text
            if not args.skip_content_html:
                html = build_transcript_html(transcript)
                plain_text = build_transcript_text(transcript)
                update_post_content(conn, post_id, html, plain_text)
                print(f"  content_html: updated ({len(html)} chars)")
                print(f"  content_text: updated ({len(plain_text)} chars)")

            # Step 3: Weaviate
            if not args.skip_weaviate:
                try:
                    upload_to_weaviate(post_id, transcript, conn)
                except Exception as e:
                    print(f"  Weaviate ERROR: {e}")

        # Summary
        with conn.cursor() as cur:
            cur.execute("""
                SELECT speaker, count(*) AS turns, sum(word_count) AS words
                FROM substack.transcript_lines
                GROUP BY speaker ORDER BY words DESC
            """)
            rows = cur.fetchall()
            if rows:
                print(f"\nTranscript lines summary:")
                print(f"  {'speaker':<20} {'turns':>8} {'words':>10}")
                for speaker, turns, words in rows:
                    print(f"  {speaker:<20} {turns:>8} {words:>10}")

    finally:
        db.close()


if __name__ == "__main__":
    main()
