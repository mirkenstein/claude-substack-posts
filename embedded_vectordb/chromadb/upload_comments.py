#!/usr/bin/env python3
"""Read comments from PostgreSQL and upload to ChromaDB with Jina AI embeddings.

Requires JINA_API_KEY environment variable (Jina AI API key for client-side
embedding generation). ChromaDB computes embeddings client-side, unlike Weaviate
which uses a server-side vectorizer.

Usage:
    JINA_API_KEY=... python upload_comments.py                          # incremental from both DBs
    JINA_API_KEY=... python upload_comments.py --database substack      # substack DB only
    JINA_API_KEY=... python upload_comments.py --database podcasts      # podcasts DB only
    JINA_API_KEY=... python upload_comments.py --all                    # re-upload everything
    JINA_API_KEY=... python upload_comments.py --publication drlivci     # one publication
    JINA_API_KEY=... python upload_comments.py --since '2026-02-18'     # comments after a date
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
from config import get_client, get_jina_ef, COMMENTS_COLLECTION

MAX_TOKENS = 8000
BATCH_SIZE = 50
tokenizer = tiktoken.get_encoding("cl100k_base")

def _watermark_file(database: str) -> Path:
    suffix = f"_{database}" if database != "substack" else ""
    return Path(__file__).parent / f".last_upload_comments{suffix}"


def truncate_to_tokens(text: str, max_tokens: int = MAX_TOKENS) -> str:
    tokens = tokenizer.encode(text)
    if len(tokens) <= max_tokens:
        return text
    return tokenizer.decode(tokens[:max_tokens])


def get_last_upload_time(database: str = "substack") -> str | None:
    wf = _watermark_file(database)
    if wf.exists():
        return wf.read_text().strip()
    return None


def save_upload_time(timestamp: str, database: str = "substack"):
    _watermark_file(database).write_text(timestamp)


COMMENTS_QUERY_BASE = """
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
"""


def build_query(args, database: str = "substack") -> tuple[str, list]:
    conditions = []
    params = []

    if args.since:
        conditions.append("c.date >= %s")
        params.append(args.since)
    elif not args.all and not args.publication:
        last = get_last_upload_time(database)
        if last:
            conditions.append("c.date > %s")
            params.append(last)

    if args.publication:
        conditions.append("pub.subdomain = %s")
        params.append(args.publication)

    query = COMMENTS_QUERY_BASE
    if conditions:
        query += "  AND " + "\n  AND ".join(conditions) + "\n"
    query += "ORDER BY c.date"
    return query, params


def main():
    parser = argparse.ArgumentParser(description="Upload comments from PostgreSQL to ChromaDB")
    parser.add_argument("--all", action="store_true", help="Re-upload all comments (ignore watermark)")
    parser.add_argument("--publication", metavar="SUBDOMAIN", help="Only upload comments from this subdomain")
    parser.add_argument("--since", metavar="TIMESTAMP", help="Upload comments after this date")
    parser.add_argument("--database",
                        help="PostgreSQL database (substack, podcasts, or both if omitted)")
    args = parser.parse_args()

    upload_start = datetime.now(timezone.utc).isoformat()

    databases = [args.database] if args.database else ["substack", "podcasts"]

    all_rows = []  # (row, source_database)
    for db in databases:
        query, params = build_query(args, db)
        print(f"Reading comments from PostgreSQL ({db})...")
        with DatabaseConnection(database=db) as conn:
            with conn.cursor() as cur:
                cur.execute(query, params)
                columns = [desc[0] for desc in cur.description]
                rows = [dict(zip(columns, r)) for r in cur.fetchall()]
        print(f"  {len(rows)} comments")
        for row in rows:
            row["source_database"] = db
        all_rows.extend(rows)

    print(f"\nTotal comments: {len(all_rows)}")

    if not all_rows:
        print("Nothing new to upload.")
        return

    rows = all_rows

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

            src_db = row.get("source_database", "substack")
            ids.append(f"comment-{src_db}-{row['comment_id']}")
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
                "sourceDatabase": src_db,
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

    # Save watermark (skip if --publication to avoid advancing past other pubs)
    if not args.publication:
        for db in databases:
            save_upload_time(upload_start, db)
        print(f"Watermark saved: {upload_start}")


if __name__ == "__main__":
    main()
