#!/usr/bin/env python3
"""Upload Substack comments to MotherDuck for semantic search.

Default mode is incremental: only inserts new comments not already in MotherDuck.
Embeddings are only generated for rows that don't have them yet.

Usage:
    python motherduck/upload_comments.py                           # incremental from both DBs
    python motherduck/upload_comments.py --database substack       # substack DB only
    python motherduck/upload_comments.py --database podcasts       # podcasts DB only
    python motherduck/upload_comments.py --publication edwardslavsquat  # one publication
    python motherduck/upload_comments.py --full                    # drop and recreate (full reload)
    python motherduck/upload_comments.py --skip-embeddings         # insert only, no embedding()
    python motherduck/upload_comments.py --embed-only              # only run embedding update
"""

import argparse
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

import duckdb
import tiktoken

sys.path.insert(0, str(Path(__file__).parent.parent))
from src.db.connection import DatabaseConnection

# Truncate comments exceeding the embedding model's 8191 token limit
MAX_TOKENS = 8000
tokenizer = tiktoken.get_encoding("cl100k_base")

MOTHERDUCK_DB = "md:my_db"

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS substack_comments (
    comment_id      VARCHAR,
    post_id         VARCHAR,
    post_title      VARCHAR,
    post_slug       VARCHAR,
    author_name     VARCHAR,
    author_handle   VARCHAR,
    comment_date    TIMESTAMP,
    body            VARCHAR,
    reaction_count  INTEGER,
    is_valuable     BOOLEAN,
    depth           INTEGER,
    subdomain       VARCHAR,
    source_database VARCHAR,
    body_tokens     INTEGER,
    body_embedding  FLOAT[512]
);
"""

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


def truncate_to_tokens(text: str, max_tokens: int = MAX_TOKENS) -> str:
    tokens = tokenizer.encode(text)
    if len(tokens) <= max_tokens:
        return text
    return tokenizer.decode(tokens[:max_tokens])


def count_tokens(text: str) -> int:
    return len(tokenizer.encode(text))


# ---------------------------------------------------------------------------
# PostgreSQL reading
# ---------------------------------------------------------------------------

def read_comments(database: str, publication: str | None = None) -> list[dict]:
    filters = []
    params = []

    if publication:
        filters.append("AND pub.subdomain = %s")
        params.append(publication)

    extra_filters = "\n  ".join(filters)
    query = COMMENTS_QUERY.format(extra_filters=extra_filters)

    with DatabaseConnection(database=database) as conn:
        with conn.cursor() as cur:
            cur.execute(query, params or None)
            columns = [desc[0] for desc in cur.description]
            return [dict(zip(columns, r)) for r in cur.fetchall()]


# ---------------------------------------------------------------------------
# MotherDuck operations
# ---------------------------------------------------------------------------

def get_existing_comment_ids(conn: duckdb.DuckDBPyConnection) -> set[str]:
    """Get set of comment_ids already in MotherDuck."""
    try:
        rows = conn.execute(
            "SELECT DISTINCT comment_id FROM substack_comments"
        ).fetchall()
        return {r[0] for r in rows}
    except Exception:
        return set()


def insert_comments(conn: duckdb.DuckDBPyConnection, rows: list[dict],
                    source_database: str, batch_size: int = 500):
    if not rows:
        return

    insert_sql = """
    INSERT INTO substack_comments (
        comment_id, post_id, post_title, post_slug,
        author_name, author_handle, comment_date, body,
        reaction_count, is_valuable, depth,
        subdomain, source_database, body_tokens
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """

    start = time.time()
    truncated = 0
    for i in range(0, len(rows), batch_size):
        batch = rows[i:i + batch_size]
        values = []
        for r in batch:
            body = r["body"]
            trimmed = truncate_to_tokens(body)
            if len(trimmed) < len(body):
                truncated += 1
            tokens = count_tokens(trimmed)
            values.append((
                str(r["comment_id"]), str(r["post_id"]),
                r["post_title"] or "", r["post_slug"] or "",
                r["author_name"] or "", r["author_handle"] or "",
                r["date"], trimmed,
                r["reaction_count"] or 0, bool(r["is_valuable"]),
                r["depth"] or 0,
                r["subdomain"] or "", source_database, tokens,
            ))
        conn.executemany(insert_sql, values)
        inserted = min(i + batch_size, len(rows))
        elapsed = time.time() - start
        rate = inserted / elapsed if elapsed > 0 else 0
        print(f"  Inserted {inserted}/{len(rows)} ({rate:.0f}/sec)")

    if truncated:
        print(f"  {truncated} comments truncated to {MAX_TOKENS} tokens")


def run_embeddings(conn: duckdb.DuckDBPyConnection):
    result = conn.execute(
        "SELECT count(*) FROM substack_comments WHERE body_embedding IS NULL"
    ).fetchone()
    pending = result[0]

    if pending == 0:
        print("All rows already have embeddings.")
        return

    print(f"Generating embeddings for {pending} rows (server-side)...")
    start = time.time()
    conn.execute("""
        UPDATE substack_comments
        SET body_embedding = embedding(body)
        WHERE body_embedding IS NULL
    """)
    elapsed = time.time() - start
    print(f"  Embeddings generated in {elapsed:.1f}s ({pending / elapsed:.0f} rows/sec)")


def create_fts_index(conn: duckdb.DuckDBPyConnection, table: str,
                     id_col: str, text_cols: list[str]):
    """Create a full-text search index using DuckDB's FTS extension."""
    cols_str = ", ".join(f"'{c}'" for c in text_cols)

    try:
        conn.execute(f"PRAGMA drop_fts_index('{table}')")
    except Exception:
        pass

    print(f"Creating FTS index on {table} ({', '.join(text_cols)})...")
    start = time.time()
    conn.execute(f"""
        PRAGMA create_fts_index(
            '{table}', '{id_col}', {cols_str},
            stemmer='english', stopwords='english',
            ignore='(\\.|[^a-z])+', strip_accents=1, lower=1
        )
    """)
    elapsed = time.time() - start
    print(f"  FTS index created in {elapsed:.1f}s")


def verify(conn: duckdb.DuckDBPyConnection):
    print("\n--- Verification ---")
    result = conn.execute("""
        SELECT
            count(*) AS total,
            count(DISTINCT post_id) AS unique_posts,
            count(DISTINCT subdomain) AS unique_subdomains
        FROM substack_comments
    """).fetchone()
    print(f"  Comments: {result[0]}, Posts: {result[1]}, Subdomains: {result[2]}")

    result = conn.execute(
        "SELECT count(*) FROM substack_comments WHERE body_embedding IS NOT NULL"
    ).fetchone()
    print(f"  With embeddings: {result[0]}")

    result = conn.execute("""
        SELECT source_database, count(*) AS comments, count(DISTINCT post_id) AS posts
        FROM substack_comments
        GROUP BY source_database
    """).fetchall()
    for row in result:
        print(f"  {row[0]}: {row[1]} comments, {row[2]} posts")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Upload Substack comments to MotherDuck"
    )
    parser.add_argument("--database",
                        help="PostgreSQL database (substack, podcasts, or both if omitted)")
    parser.add_argument("--publication", metavar="SUBDOMAIN",
                        help="Only upload comments from this publication")
    parser.add_argument("--full", action="store_true",
                        help="Full reload: drop and recreate table")
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

        databases = [args.database] if args.database else ["substack", "podcasts"]

        # Read comments from each database
        all_data = []  # (rows, source_database)
        total_rows = 0
        for db in databases:
            print(f"\nReading comments from PostgreSQL ({db})...")
            rows = read_comments(db, args.publication)
            print(f"  {len(rows)} comments")
            all_data.append((rows, db))
            total_rows += len(rows)

        print(f"\nTotal comments from PG: {total_rows}")
        if total_rows == 0:
            print("No data to upload.")
            return

        if args.full:
            # Full reload: drop and recreate
            print("\nFull reload: dropping and recreating substack_comments...")
            md.execute("DROP TABLE IF EXISTS substack_comments")
            md.execute(CREATE_TABLE_SQL)
            new_data = all_data
        else:
            # Incremental: create table if not exists, filter out existing comments
            md.execute(CREATE_TABLE_SQL)
            existing = get_existing_comment_ids(md)
            new_data = []
            total_new = 0
            for rows, db in all_data:
                new_rows = [r for r in rows if str(r["comment_id"]) not in existing]
                new_data.append((new_rows, db))
                total_new += len(new_rows)
            skipped = total_rows - total_new
            print(f"  Already in MotherDuck: {len(existing)} comments ({skipped} skipped)")
            print(f"  New comments to insert: {total_new}")

            if total_new == 0:
                print("Nothing new to insert.")
                if not args.skip_embeddings:
                    run_embeddings(md)
                verify(md)
                return

        # Insert
        for rows, db in new_data:
            if rows:
                print(f"\nInserting comments from {db}...")
                insert_comments(md, rows, source_database=db)

        # Embeddings
        if not args.skip_embeddings:
            print()
            run_embeddings(md)

        # Recreate FTS index (covers all rows including new ones)
        create_fts_index(md, "substack_comments", "comment_id", ["body"])

        verify(md)

    finally:
        md.close()


if __name__ == "__main__":
    main()
