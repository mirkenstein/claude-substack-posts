#!/usr/bin/env python3
"""Read comments from PostgreSQL and upload to Weaviate.

Usage:
    python weaviate/upload_comments.py                                    # incremental (watermark)
    python weaviate/upload_comments.py --publication martyrmade           # one publication (full)
    python weaviate/upload_comments.py --database podcasts --all          # all from podcasts DB
    python weaviate/upload_comments.py --database podcasts --publication martyrmade
    python weaviate/upload_comments.py --since 2026-02-25                 # since specific date
"""

import argparse
import sys
import time
import uuid
from datetime import datetime, timezone
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
  {extra_filters}
ORDER BY c.date
"""


# ═══════════════════════════════════════════════════════════
# Watermark
# ═══════════════════════════════════════════════════════════

def _watermark_file(database: str) -> Path:
    suffix = f"_{database}" if database != "substack" else ""
    return Path(__file__).parent / f".last_upload_comments{suffix}"


def get_last_upload_time(database: str = "substack") -> str | None:
    wf = _watermark_file(database)
    if wf.exists():
        return wf.read_text().strip()
    return None


def save_upload_time(timestamp: str, database: str = "substack"):
    _watermark_file(database).write_text(timestamp)


def main():
    parser = argparse.ArgumentParser(description="Upload comments to Weaviate")
    parser.add_argument("--publication", help="Only upload comments for this subdomain")
    parser.add_argument("--database", default="substack",
                        help="PostgreSQL database to read from (default: substack)")
    parser.add_argument("--all", action="store_true",
                        help="Upload all comments (ignore watermark)")
    parser.add_argument("--since", help="Upload comments loaded after this timestamp")
    args = parser.parse_args()

    # Build filters
    filters = []
    params = []

    if args.publication:
        filters.append("AND pub.subdomain = %s")
        params.append(args.publication)

    if args.since:
        filters.append("AND c.loaded_at >= %s")
        params.append(args.since)
        print(f"Uploading comments loaded since {args.since}")
    elif not args.all and not args.publication:
        last = get_last_upload_time(args.database)
        if last:
            filters.append("AND c.loaded_at > %s")
            params.append(last)
            print(f"Incremental upload: comments loaded after {last}")
        else:
            print("No watermark found — uploading all comments")

    extra_filters = "\n  ".join(filters)
    query = COMMENTS_QUERY.format(extra_filters=extra_filters)

    print(f"Database: {args.database} → Collection: {COMMENTS_COLLECTION}")
    if args.publication:
        print(f"Publication: {args.publication}")

    upload_time = datetime.now(timezone.utc).isoformat()

    with DatabaseConnection(database=args.database) as conn:
        with conn.cursor() as cur:
            cur.execute(query, params or None)
            columns = [desc[0] for desc in cur.description]
            rows = [dict(zip(columns, r)) for r in cur.fetchall()]
    print(f"  {len(rows)} comments to upload")

    if not rows:
        print("Nothing to upload.")
        return

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
                    f"engru-comment-{args.database}-{row['comment_id']}"
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

        # Save watermark (skip when filtering by --publication to avoid advancing past other pubs)
        if not args.publication:
            save_upload_time(upload_time, args.database)
            print(f"Watermark saved: {upload_time}")

        # Verify
        result = collection.aggregate.over_all(total_count=True)
        print(f"Collection count: {result.total_count}")
    finally:
        client.close()


if __name__ == "__main__":
    main()
