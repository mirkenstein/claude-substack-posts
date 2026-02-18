#!/usr/bin/env python3
"""Read comments from PostgreSQL and upload to Weaviate."""

import sys
import time
import uuid
from pathlib import Path

import tiktoken

sys.path.insert(0, str(Path(__file__).parent.parent))
from src.db.connection import DatabaseConnection
from config import get_client, COMMENTS_COLLECTION

# Truncate comments exceeding the embedding model's 8191 token limit
MAX_TOKENS = 8000
tokenizer = tiktoken.get_encoding("cl100k_base")


def truncate_to_tokens(text: str, max_tokens: int = MAX_TOKENS) -> str:
    """Truncate text to fit within token limit."""
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
    # Read comments from Postgres
    print("Reading comments from PostgreSQL...")
    with DatabaseConnection() as conn:
        with conn.cursor() as cur:
            cur.execute(COMMENTS_QUERY)
            columns = [desc[0] for desc in cur.description]
            rows = [dict(zip(columns, r)) for r in cur.fetchall()]
    print(f"  {len(rows)} comments loaded")

    # Upload to Weaviate
    client = get_client()
    try:
        collection = client.collections.get(COMMENTS_COLLECTION)

        print(f"Uploading to {COMMENTS_COLLECTION}...")
        start = time.time()
        uploaded = 0

        truncated = 0
        with collection.batch.fixed_size(batch_size=100) as batch:
            for row in rows:
                body = row["body"]
                trimmed = truncate_to_tokens(body)
                if len(trimmed) < len(body):
                    truncated += 1
                props = {
                    "body": trimmed,
                    "commentId": str(row["comment_id"]),
                    "postId": str(row["post_id"]),
                    "postTitle": row["post_title"] or "",
                    "postSlug": row["post_slug"] or "",
                    "authorName": row["author_name"] or "",
                    "authorHandle": row["author_handle"] or "",
                    "date": (
                        row["date"].isoformat()
                        if row["date"] else None
                    ),
                    "reactionCount": row["reaction_count"] or 0,
                    "depth": row["depth"] or 0,
                    "isValuable": bool(row["is_valuable"]),
                    "subdomain": row["subdomain"] or "",
                }
                obj_uuid = uuid.uuid5(
                    uuid.NAMESPACE_DNS,
                    f"engru-comment-{row['comment_id']}"
                )
                batch.add_object(properties=props, uuid=obj_uuid)
                uploaded += 1
                if uploaded % 2000 == 0:
                    elapsed = time.time() - start
                    print(f"  {uploaded}/{len(rows)} ({uploaded/elapsed:.0f}/sec)")

        if truncated:
            print(f"  {truncated} comments truncated to {MAX_TOKENS} tokens")
        if batch.number_errors > 0:
            print(f"  {batch.number_errors} batch errors")

        elapsed = time.time() - start
        print(f"Done: {uploaded} uploaded in {elapsed:.1f}s")

        # Verify
        result = collection.aggregate.over_all(total_count=True)
        print(f"Collection count: {result.total_count}")
    finally:
        client.close()


if __name__ == "__main__":
    main()
