#!/usr/bin/env python3
"""Read posts from PostgreSQL, chunk, and upload to ChromaDB with Jina AI embeddings.

Usage:
    python upload_posts.py                          # upload only new posts (since last run)
    python upload_posts.py --all                    # re-upload everything
    python upload_posts.py --publication drlivci     # upload one publication
    python upload_posts.py --since '2026-02-18'     # posts loaded after a date
"""

import argparse
import json
import re
import sys
import time
from html.parser import HTMLParser
from pathlib import Path

import tiktoken

sys.path.insert(0, str(Path(__file__).parent.parent))
from src.db.connection import DatabaseConnection

sys.path.insert(0, str(Path(__file__).parent))
from config import get_client, get_jina_ef, POSTS_COLLECTION

# Chunking settings (same as Weaviate pipeline)
CHUNK_SIZE = 700
OVERLAP = 150
MIN_CHUNK_SIZE = 400
CHUNK_WORD_THRESHOLD = 5000

# ChromaDB batch limit
BATCH_SIZE = 100

tokenizer = tiktoken.get_encoding("cl100k_base")

WATERMARK_FILE = Path(__file__).parent / ".last_upload"


# ---------------------------------------------------------------------------
# HTML stripping
# ---------------------------------------------------------------------------

class _HTMLTextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self._pieces = []
        self._skip = False

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip = True
        elif tag in ("p", "br", "div", "h1", "h2", "h3", "h4", "li", "blockquote"):
            self._pieces.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self._skip = False

    def handle_data(self, data):
        if not self._skip:
            self._pieces.append(data)


def strip_html(html: str) -> str:
    if not html:
        return ""
    extractor = _HTMLTextExtractor()
    extractor.feed(html)
    text = "".join(extractor._pieces)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


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
# PostgreSQL query
# ---------------------------------------------------------------------------

POSTS_QUERY_BASE = """
SELECT
    p.id            AS post_id,
    p.title,
    p.subtitle,
    p.slug,
    p.canonical_url,
    p.post_date,
    p.audience,
    p.wordcount,
    p.comment_count,
    p.restacks,
    p.content_html,
    a.name          AS author_name,
    pub.name        AS publication_name,
    pub.subdomain
FROM posts p
LEFT JOIN authors a   ON a.id = p.primary_author_id
LEFT JOIN publications pub ON pub.id = p.publication_id
"""


def get_last_upload_time() -> str | None:
    if WATERMARK_FILE.exists():
        return WATERMARK_FILE.read_text().strip()
    return None


def save_upload_time(timestamp: str):
    WATERMARK_FILE.write_text(timestamp)


def build_query(args) -> tuple[str, list]:
    conditions = []
    params = []

    if args.since:
        conditions.append("p.loaded_at >= %s")
        params.append(args.since)
    elif not args.all and not args.publication:
        last = get_last_upload_time()
        if last:
            conditions.append("p.loaded_at > %s")
            params.append(last)

    if args.publication:
        conditions.append("pub.subdomain = %s")
        params.append(args.publication)

    query = POSTS_QUERY_BASE
    if conditions:
        query += "WHERE " + " AND ".join(conditions) + "\n"
    query += "ORDER BY p.post_date"
    return query, params


def build_chunks(row: dict) -> list[dict]:
    plain = strip_html(row["content_html"])
    if not plain:
        return []

    word_count = len(plain.split())
    shared = {
        "postId": str(row["post_id"]),
        "title": row["title"] or "",
        "subtitle": row["subtitle"] or "",
        "slug": row["slug"] or "",
        "canonicalUrl": row["canonical_url"] or "",
        "postDate": row["post_date"].isoformat() if row["post_date"] else "",
        "audience": row["audience"] or "",
        "authorName": row["author_name"] or "",
        "publicationName": row["publication_name"] or "",
        "subdomain": row["subdomain"] or "",
        "wordcount": row["wordcount"] or word_count,
        "commentCount": row["comment_count"] or 0,
        "restacks": row["restacks"] or 0,
    }

    if word_count <= CHUNK_WORD_THRESHOLD:
        tokens = count_tokens(plain)
        return [{**shared, "content": plain, "chunkNumber": 0, "totalChunks": 1, "chunkTokens": tokens}]

    text_chunks = chunk_by_tokens(plain)
    chunks = []
    for idx, chunk_text in enumerate(text_chunks):
        tok_count = count_tokens(chunk_text)
        if tok_count < MIN_CHUNK_SIZE and idx > 0 and chunks:
            chunks[-1]["content"] += "\n\n" + chunk_text
            chunks[-1]["chunkTokens"] = count_tokens(chunks[-1]["content"])
            continue
        chunks.append({**shared, "content": chunk_text, "chunkNumber": idx, "totalChunks": len(text_chunks), "chunkTokens": tok_count})

    total = len(chunks)
    for c in chunks:
        c["totalChunks"] = total
    return chunks


# ---------------------------------------------------------------------------
# Upload
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Upload posts from PostgreSQL to ChromaDB")
    parser.add_argument("--all", action="store_true", help="Re-upload all posts (ignore watermark)")
    parser.add_argument("--publication", metavar="SUBDOMAIN", help="Only upload posts from this subdomain")
    parser.add_argument("--since", metavar="TIMESTAMP", help="Upload posts loaded after this timestamp")
    args = parser.parse_args()

    query, params = build_query(args)

    from datetime import datetime, timezone
    upload_start = datetime.now(timezone.utc).isoformat()

    print("Reading posts from PostgreSQL...")
    with DatabaseConnection() as conn:
        with conn.cursor() as cur:
            cur.execute(query, params)
            columns = [desc[0] for desc in cur.description]
            rows = [dict(zip(columns, r)) for r in cur.fetchall()]
    print(f"  {len(rows)} posts to upload")

    if not rows:
        print("Nothing new to upload.")
        return

    # Build chunks
    print("Chunking posts...")
    all_chunks = []
    chunked_posts = 0
    for row in rows:
        chunks = build_chunks(row)
        if len(chunks) > 1:
            chunked_posts += 1
        all_chunks.extend(chunks)
    print(f"  {len(all_chunks)} total chunks ({chunked_posts} posts were split)")

    # Upload to ChromaDB
    print(f"Uploading to ChromaDB collection '{POSTS_COLLECTION}'...")
    client = get_client()
    jina_ef = get_jina_ef()
    collection = client.get_collection(POSTS_COLLECTION, embedding_function=jina_ef)

    start = time.time()
    uploaded = 0

    for i in range(0, len(all_chunks), BATCH_SIZE):
        batch = all_chunks[i:i + BATCH_SIZE]

        ids = [f"post-{c['postId']}-chunk-{c['chunkNumber']}" for c in batch]
        documents = [c["content"] for c in batch]
        metadatas = [{k: v for k, v in c.items() if k != "content"} for c in batch]

        collection.upsert(ids=ids, documents=documents, metadatas=metadatas)
        uploaded += len(batch)

        if uploaded % 200 < BATCH_SIZE:
            elapsed = time.time() - start
            print(f"  {uploaded}/{len(all_chunks)} ({uploaded/elapsed:.0f}/sec)")

    elapsed = time.time() - start
    print(f"Done: {uploaded} uploaded in {elapsed:.1f}s ({uploaded/elapsed:.0f}/sec)")
    print(f"Collection count: {collection.count()}")

    # Save watermark
    save_upload_time(upload_start)
    print(f"Watermark saved: {upload_start}")


if __name__ == "__main__":
    main()
