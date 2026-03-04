#!/usr/bin/env python3
"""Read posts from PostgreSQL, chunk, and upload to ChromaDB with Jina AI embeddings.

Requires JINA_API_KEY environment variable (Jina AI API key for client-side
embedding generation). ChromaDB computes embeddings client-side, unlike Weaviate
which uses a server-side vectorizer.

Usage:
    JINA_API_KEY=... python upload_posts.py                          # incremental from both DBs
    JINA_API_KEY=... python upload_posts.py --database substack      # substack DB only
    JINA_API_KEY=... python upload_posts.py --database podcasts      # podcasts DB only
    JINA_API_KEY=... python upload_posts.py --all                    # re-upload everything
    JINA_API_KEY=... python upload_posts.py --publication drlivci     # one publication
    JINA_API_KEY=... python upload_posts.py --since '2026-02-18'     # posts loaded after a date
"""

import argparse
import json
import re
import sys
import time
from html.parser import HTMLParser
from pathlib import Path

import tiktoken

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from src.db.connection import DatabaseConnection

sys.path.insert(0, str(Path(__file__).parent))
from config import get_client, get_jina_ef, POSTS_COLLECTION

# Chunking settings (same as Weaviate pipeline)
CHUNK_SIZE = 700
OVERLAP = 150
MIN_CHUNK_SIZE = 400
CHUNK_WORD_THRESHOLD = 5000

# ChromaDB batch limit (keep small to avoid Jina token rate limits)
BATCH_SIZE = 50

tokenizer = tiktoken.get_encoding("cl100k_base")

def _watermark_file(database: str) -> Path:
    suffix = f"_{database}" if database != "substack" else ""
    return Path(__file__).parent / f".last_upload{suffix}"


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


def get_last_upload_time(database: str = "substack") -> str | None:
    wf = _watermark_file(database)
    if wf.exists():
        return wf.read_text().strip()
    return None


def save_upload_time(timestamp: str, database: str = "substack"):
    _watermark_file(database).write_text(timestamp)


def build_query(args, database: str = "substack") -> tuple[str, list]:
    conditions = []
    params = []

    if args.since:
        conditions.append("p.loaded_at >= %s")
        params.append(args.since)
    elif not args.all and not args.publication:
        last = get_last_upload_time(database)
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


def build_chunks(row: dict, source_database: str = "substack") -> list[dict]:
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
        "sourceDatabase": source_database,
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
    parser.add_argument("--database",
                        help="PostgreSQL database (substack, podcasts, or both if omitted)")
    args = parser.parse_args()

    from datetime import datetime, timezone
    upload_start = datetime.now(timezone.utc).isoformat()

    databases = [args.database] if args.database else ["substack", "podcasts"]

    all_chunks = []
    chunked_posts = 0
    for db in databases:
        query, params = build_query(args, db)
        print(f"Reading posts from PostgreSQL ({db})...")
        with DatabaseConnection(database=db) as conn:
            with conn.cursor() as cur:
                cur.execute(query, params)
                columns = [desc[0] for desc in cur.description]
                rows = [dict(zip(columns, r)) for r in cur.fetchall()]
        print(f"  {len(rows)} posts")

        for row in rows:
            chunks = build_chunks(row, source_database=db)
            if len(chunks) > 1:
                chunked_posts += 1
            all_chunks.extend(chunks)

    print(f"\nTotal chunks: {len(all_chunks)} ({chunked_posts} posts were split)")

    if not all_chunks:
        print("Nothing new to upload.")
        return

    # Upload to ChromaDB
    print(f"Uploading to ChromaDB collection '{POSTS_COLLECTION}'...")
    client = get_client()
    jina_ef = get_jina_ef()
    collection = client.get_collection(POSTS_COLLECTION, embedding_function=jina_ef)

    start = time.time()
    uploaded = 0

    for i in range(0, len(all_chunks), BATCH_SIZE):
        batch = all_chunks[i:i + BATCH_SIZE]

        ids = [f"post-{c['sourceDatabase']}-{c['postId']}-chunk-{c['chunkNumber']}" for c in batch]
        documents = [c["content"] for c in batch]
        metadatas = [{k: v for k, v in c.items() if k != "content"} for c in batch]

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

        if uploaded % 200 < BATCH_SIZE:
            elapsed = time.time() - start
            print(f"  {uploaded}/{len(all_chunks)} ({uploaded/elapsed:.0f}/sec)")

    elapsed = time.time() - start
    print(f"Done: {uploaded} uploaded in {elapsed:.1f}s ({uploaded/elapsed:.0f}/sec)")
    print(f"Collection count: {collection.count()}")

    # Save watermark for each database
    for db in databases:
        save_upload_time(upload_start, db)
    print(f"Watermark saved: {upload_start}")


if __name__ == "__main__":
    main()
