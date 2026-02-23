#!/usr/bin/env python3
"""Read comments from PostgreSQL and upload to ChromaDB with Jina AI embeddings.

Usage:
    python upload_comments.py
"""

import sys
import time
from pathlib import Path

import tiktoken

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from src.db.connection import DatabaseConnection

sys.path.insert(0, str(Path(__file__).parent))
from config import get_client, get_jina_ef, COMMENTS_COLLECTION

MAX_TOKENS = 8000
BATCH_SIZE = 50
tokenizer = tiktoken.get_encoding("cl100k_base")


def truncate_to_tokens(text: str, max_tokens: int = MAX_TOKENS) -> str:
    tokens = tokenizer.encode(text)
    if len(tokens) <= max_tokens:
        return text
    return tokenizer.decode(tokens[:max_tokens])


COMMENTS_QUERY = """
SELECT
    c.id              AS comment_id,
    c.post_id,
    c.author_name,
    c.author_handle,
    c.date,
    c.body,
    c.reaction_count,
    c.is_valuable,
    c.depth,
    p.title           AS post_title,
    p.slug            AS post_slug,
    pub.subdomain
FROM comments c
JOIN posts p             ON p.id = c.post_id
LEFT JOIN publications pub ON pub.id = p.publication_id
WHERE c.deleted = false
  AND c.body IS NOT NULL
  AND c.body != ''
ORDER BY c.date
"""


def main():
    print("Reading comments from PostgreSQL...")
    with DatabaseConnection() as conn:
        with conn.cursor() as cur:
            cur.execute(COMMENTS_QUERY)
            columns = [desc[0] for desc in cur.description]
            rows = [dict(zip(columns, r)) for r in cur.fetchall()]
    print(f"  {len(rows)} comments loaded")

    client = get_client()
    jina_ef = get_jina_ef()
    collection = client.get_collection(COMMENTS_COLLECTION, embedding_function=jina_ef)

    print(f"Uploading to {COMMENTS_COLLECTION}...")
    start = time.time()
    uploaded = 0
    truncated = 0

    for i in range(0, len(rows), BATCH_SIZE):
        batch = rows[i:i + BATCH_SIZE]

        ids = []
        documents = []
        metadatas = []

        for row in batch:
            body = row["body"]
            trimmed = truncate_to_tokens(body)
            if len(trimmed) < len(body):
                truncated += 1

            ids.append(f"comment-{row['comment_id']}")
            documents.append(trimmed)
            metadatas.append({
                "commentId": str(row["comment_id"]),
                "postId": str(row["post_id"]),
                "postTitle": row["post_title"] or "",
                "postSlug": row["post_slug"] or "",
                "authorName": row["author_name"] or "",
                "authorHandle": row["author_handle"] or "",
                "date": row["date"].isoformat() if row["date"] else "",
                "reactionCount": row["reaction_count"] or 0,
                "depth": row["depth"] or 0,
                "isValuable": bool(row["is_valuable"]),
                "subdomain": row["subdomain"] or "",
            })

        try:
            collection.upsert(ids=ids, documents=documents, metadatas=metadatas)
        except RuntimeError as e:
            if "rate limit" in str(e).lower():
                print(f"  Rate limited at {uploaded}/{len(rows)}, waiting 60s...")
                time.sleep(60)
                collection.upsert(ids=ids, documents=documents, metadatas=metadatas)
            else:
                raise

        uploaded += len(batch)
        if uploaded % 2000 < BATCH_SIZE:
            elapsed = time.time() - start
            print(f"  {uploaded}/{len(rows)} ({uploaded/elapsed:.0f}/sec)")

    if truncated:
        print(f"  {truncated} comments truncated to {MAX_TOKENS} tokens")

    elapsed = time.time() - start
    print(f"Done: {uploaded} uploaded in {elapsed:.1f}s ({uploaded/elapsed:.0f}/sec)")
    print(f"Collection count: {collection.count()}")


if __name__ == "__main__":
    main()
