#!/usr/bin/env python3
"""Read posts from PostgreSQL, chunk if needed, and upload to Weaviate."""

import re
import sys
import time
import uuid
from html.parser import HTMLParser
from pathlib import Path

import tiktoken

sys.path.insert(0, str(Path(__file__).parent.parent))
from src.db.connection import DatabaseConnection
from config import (
    get_client, POSTS_COLLECTION,
    CHUNK_SIZE, OVERLAP, MIN_CHUNK_SIZE, CHUNK_WORD_THRESHOLD,
)

# Tiktoken encoder (same tokenizer as text-embedding-3-small)
tokenizer = tiktoken.get_encoding("cl100k_base")


# ---------------------------------------------------------------------------
# HTML stripping
# ---------------------------------------------------------------------------

class _HTMLTextExtractor(HTMLParser):
    """Simple HTML-to-text extractor."""

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
    """Convert HTML to plain text."""
    if not html:
        return ""
    extractor = _HTMLTextExtractor()
    extractor.feed(html)
    text = "".join(extractor._pieces)
    # Collapse whitespace while preserving paragraph breaks
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# ---------------------------------------------------------------------------
# Chunking (mirrors ForestParkPharmacy/SUBSTACK/chunking/post_chunker.py)
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
# PostgreSQL query
# ---------------------------------------------------------------------------

POSTS_QUERY = """
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
ORDER BY p.post_date
"""


def build_chunks(row: dict) -> list[dict]:
    """Convert a post row into one or more Weaviate-ready chunk dicts."""
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
        "postDate": row["post_date"].isoformat() if row["post_date"] else None,
        "audience": row["audience"] or "",
        "authorName": row["author_name"] or "",
        "publicationName": row["publication_name"] or "",
        "subdomain": row["subdomain"] or "",
        "wordcount": row["wordcount"] or word_count,
        "commentCount": row["comment_count"] or 0,
        "restacks": row["restacks"] or 0,
    }

    # Single chunk for short posts
    if word_count <= CHUNK_WORD_THRESHOLD:
        tokens = count_tokens(plain)
        return [{
            **shared,
            "content": plain,
            "chunkNumber": 0,
            "totalChunks": 1,
            "chunkTokens": tokens,
        }]

    # Multiple chunks for long posts
    text_chunks = chunk_by_tokens(plain)
    chunks = []
    for idx, chunk_text in enumerate(text_chunks):
        tok_count = count_tokens(chunk_text)
        # Merge tiny last chunk into previous
        if tok_count < MIN_CHUNK_SIZE and idx > 0 and chunks:
            chunks[-1]["content"] += "\n\n" + chunk_text
            chunks[-1]["chunkTokens"] = count_tokens(chunks[-1]["content"])
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
# Upload
# ---------------------------------------------------------------------------

def main():
    # Read posts from Postgres
    print("Reading posts from PostgreSQL...")
    with DatabaseConnection() as conn:
        with conn.cursor() as cur:
            cur.execute(POSTS_QUERY)
            columns = [desc[0] for desc in cur.description]
            rows = [dict(zip(columns, r)) for r in cur.fetchall()]
    print(f"  {len(rows)} posts loaded")

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

    # Upload to Weaviate
    client = get_client()
    try:
        collection = client.collections.get(POSTS_COLLECTION)

        print(f"Uploading to {POSTS_COLLECTION}...")
        start = time.time()
        uploaded = 0
        failed = 0

        # Fixed batch size of 50 to stay under OpenAI's 300k tokens/request limit.
        # Dynamic batching sends up to 240 objects which overflows when posts are large.
        with collection.batch.fixed_size(batch_size=50) as batch:
            for chunk in all_chunks:
                obj_uuid = uuid.uuid5(
                    uuid.NAMESPACE_DNS,
                    f"engru-post-{chunk['postId']}-{chunk['chunkNumber']}"
                )
                batch.add_object(properties=chunk, uuid=obj_uuid)
                uploaded += 1
                if uploaded % 200 == 0:
                    elapsed = time.time() - start
                    print(f"  {uploaded}/{len(all_chunks)} ({uploaded/elapsed:.0f}/sec)")

        if batch.number_errors > 0:
            print(f"  {batch.number_errors} batch errors")
            failed = batch.number_errors

        elapsed = time.time() - start
        print(f"Done: {uploaded - failed} uploaded, {failed} failed in {elapsed:.1f}s")

        # Verify
        result = collection.aggregate.over_all(total_count=True)
        print(f"Collection count: {result.total_count}")
    finally:
        client.close()


if __name__ == "__main__":
    main()
