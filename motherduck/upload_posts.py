#!/usr/bin/env python3
"""Upload chunked Substack posts to MotherDuck for semantic search.

Uses MotherDuck's built-in embedding() function (OpenAI text-embedding-3-small, 512 dim)
and array_cosine_similarity() for vector search.

Usage:
    python motherduck/upload_posts.py                           # load from both databases
    python motherduck/upload_posts.py --database substack       # substack DB only
    python motherduck/upload_posts.py --database podcasts       # podcasts DB only
    python motherduck/upload_posts.py --publication edwardslavsquat  # one publication
    python motherduck/upload_posts.py --skip-embeddings         # insert only, no embedding()
    python motherduck/upload_posts.py --embed-only              # only run embedding update
"""

import argparse
import re
import sys
import time
from html.parser import HTMLParser
from pathlib import Path

import duckdb
import tiktoken

sys.path.insert(0, str(Path(__file__).parent.parent))
from src.db.connection import DatabaseConnection

# ---------------------------------------------------------------------------
# Chunking settings (same as weaviate/config.py)
# ---------------------------------------------------------------------------
CHUNK_SIZE = 700
OVERLAP = 150
MIN_CHUNK_SIZE = 400
CHUNK_WORD_THRESHOLD = 5000

# Tiktoken encoder (same tokenizer as text-embedding-3-small)
tokenizer = tiktoken.get_encoding("cl100k_base")

MOTHERDUCK_DB = "md:my_db"

CREATE_TABLE_SQL = """
CREATE OR REPLACE TABLE substack_posts (
    chunk_id        VARCHAR,
    post_id         VARCHAR,
    title           VARCHAR,
    subtitle        VARCHAR,
    slug            VARCHAR,
    canonical_url   VARCHAR,
    post_date       TIMESTAMP,
    audience        VARCHAR,
    word_count      INTEGER,
    comment_count   INTEGER,
    restacks        INTEGER,
    author_name     VARCHAR,
    publication     VARCHAR,
    subdomain       VARCHAR,
    source_database VARCHAR,
    content         VARCHAR,
    chunk_number    INTEGER,
    total_chunks    INTEGER,
    chunk_tokens    INTEGER,
    content_embedding FLOAT[512]
);
"""

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


# ---------------------------------------------------------------------------
# HTML stripping (from weaviate/upload_posts.py)
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
# Chunking (from weaviate/upload_posts.py)
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


def build_chunks(row: dict, source_database: str) -> list[dict]:
    """Convert a post row into one or more chunk dicts for MotherDuck."""
    plain = strip_html(row["content_html"])
    if not plain:
        return []

    word_count = len(plain.split())
    shared = {
        "post_id": str(row["post_id"]),
        "title": row["title"] or "",
        "subtitle": row["subtitle"] or "",
        "slug": row["slug"] or "",
        "canonical_url": row["canonical_url"] or "",
        "post_date": row["post_date"],
        "audience": row["audience"] or "",
        "word_count": row["wordcount"] or word_count,
        "comment_count": row["comment_count"] or 0,
        "restacks": row["restacks"] or 0,
        "author_name": row["author_name"] or "",
        "publication": row["publication_name"] or "",
        "subdomain": row["subdomain"] or "",
        "source_database": source_database,
    }

    if word_count <= CHUNK_WORD_THRESHOLD:
        tokens = count_tokens(plain)
        return [{
            **shared,
            "content": plain,
            "chunk_number": 0,
            "total_chunks": 1,
            "chunk_tokens": tokens,
        }]

    text_chunks = chunk_by_tokens(plain)
    chunks = []
    for idx, chunk_text in enumerate(text_chunks):
        tok_count = count_tokens(chunk_text)
        if tok_count < MIN_CHUNK_SIZE and idx > 0 and chunks:
            chunks[-1]["content"] += "\n\n" + chunk_text
            chunks[-1]["chunk_tokens"] = count_tokens(chunks[-1]["content"])
            continue
        chunks.append({
            **shared,
            "content": chunk_text,
            "chunk_number": idx,
            "total_chunks": len(text_chunks),
            "chunk_tokens": tok_count,
        })

    total = len(chunks)
    for c in chunks:
        c["total_chunks"] = total
    return chunks


# ---------------------------------------------------------------------------
# PostgreSQL reading
# ---------------------------------------------------------------------------

def read_posts(database: str, publication: str | None = None) -> list[dict]:
    """Read posts from PostgreSQL."""
    query = POSTS_QUERY_BASE
    params = []

    if publication:
        query += "WHERE pub.subdomain = %s\n"
        params.append(publication)

    query += "ORDER BY p.post_date"

    with DatabaseConnection(database=database) as conn:
        with conn.cursor() as cur:
            cur.execute(query, params)
            columns = [desc[0] for desc in cur.description]
            return [dict(zip(columns, r)) for r in cur.fetchall()]


# ---------------------------------------------------------------------------
# MotherDuck operations
# ---------------------------------------------------------------------------

def insert_chunks(conn: duckdb.DuckDBPyConnection, chunks: list[dict], batch_size: int = 500):
    """Batch insert chunks into substack_posts (without embeddings)."""
    if not chunks:
        return

    insert_sql = """
    INSERT INTO substack_posts (
        chunk_id, post_id, title, subtitle, slug, canonical_url, post_date,
        audience, word_count, comment_count, restacks,
        author_name, publication, subdomain, source_database,
        content, chunk_number, total_chunks, chunk_tokens
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """

    start = time.time()
    for i in range(0, len(chunks), batch_size):
        batch = chunks[i:i + batch_size]
        values = [
            (
                f"{c['post_id']}-{c['chunk_number']}",
                c["post_id"], c["title"], c["subtitle"], c["slug"],
                c["canonical_url"], c["post_date"],
                c["audience"], c["word_count"], c["comment_count"], c["restacks"],
                c["author_name"], c["publication"], c["subdomain"], c["source_database"],
                c["content"], c["chunk_number"], c["total_chunks"], c["chunk_tokens"],
            )
            for c in batch
        ]
        conn.executemany(insert_sql, values)
        inserted = min(i + batch_size, len(chunks))
        elapsed = time.time() - start
        rate = inserted / elapsed if elapsed > 0 else 0
        print(f"  Inserted {inserted}/{len(chunks)} ({rate:.0f}/sec)")


def run_embeddings(conn: duckdb.DuckDBPyConnection):
    """Generate embeddings server-side using MotherDuck's embedding() function."""
    # Count rows needing embeddings
    result = conn.execute(
        "SELECT count(*) FROM substack_posts WHERE content_embedding IS NULL"
    ).fetchone()
    pending = result[0]

    if pending == 0:
        print("All rows already have embeddings.")
        return

    print(f"Generating embeddings for {pending} rows (server-side)...")
    start = time.time()
    conn.execute("""
        UPDATE substack_posts
        SET content_embedding = embedding(content)
        WHERE content_embedding IS NULL
    """)
    elapsed = time.time() - start
    print(f"  Embeddings generated in {elapsed:.1f}s ({pending / elapsed:.0f} rows/sec)")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Upload chunked Substack posts to MotherDuck"
    )
    parser.add_argument("--database",
                        help="PostgreSQL database (substack, podcasts, or both if omitted)")
    parser.add_argument("--publication", metavar="SUBDOMAIN",
                        help="Only upload posts from this publication")
    parser.add_argument("--skip-embeddings", action="store_true",
                        help="Insert data only, skip embedding() call")
    parser.add_argument("--embed-only", action="store_true",
                        help="Only run embedding update, no data insert")
    args = parser.parse_args()

    # Connect to MotherDuck
    print(f"Connecting to MotherDuck...")
    md = duckdb.connect(MOTHERDUCK_DB)

    try:
        if args.embed_only:
            run_embeddings(md)
            verify(md)
            return

        # Determine which PG databases to read from
        databases = [args.database] if args.database else ["substack", "podcasts"]

        # Read and chunk posts from each database
        all_chunks = []
        for db in databases:
            print(f"\nReading posts from PostgreSQL ({db})...")
            rows = read_posts(db, args.publication)
            print(f"  {len(rows)} posts")

            for row in rows:
                all_chunks.extend(build_chunks(row, source_database=db))

        print(f"\nTotal chunks: {len(all_chunks)}")
        if not all_chunks:
            print("No data to upload.")
            return

        # Drop old table and create new one
        print("\nDropping hua_bin_articles (if exists)...")
        md.execute("DROP TABLE IF EXISTS hua_bin_articles")

        print("Creating substack_posts table...")
        md.execute(CREATE_TABLE_SQL)

        # Insert chunks
        print("Inserting chunks...")
        insert_chunks(md, all_chunks)

        # Generate embeddings
        if not args.skip_embeddings:
            print()
            run_embeddings(md)

        # Create FTS index
        create_fts_index(md, "substack_posts", "chunk_id", ["content", "title", "subtitle"])

        verify(md)

    finally:
        md.close()


def create_fts_index(conn: duckdb.DuckDBPyConnection, table: str,
                     id_col: str, text_cols: list[str]):
    """Create a full-text search index using DuckDB's FTS extension."""
    cols_str = ", ".join(f"'{c}'" for c in text_cols)

    # Drop existing FTS index if any (recreate on full reload)
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
    """Print verification stats."""
    print("\n--- Verification ---")
    result = conn.execute("""
        SELECT
            count(*) AS total_chunks,
            count(DISTINCT post_id) AS unique_posts,
            count(DISTINCT subdomain) AS unique_subdomains
        FROM substack_posts
    """).fetchone()
    print(f"  Chunks: {result[0]}, Posts: {result[1]}, Subdomains: {result[2]}")

    result = conn.execute("""
        SELECT count(*) FROM substack_posts WHERE content_embedding IS NOT NULL
    """).fetchone()
    print(f"  With embeddings: {result[0]}")

    result = conn.execute("""
        SELECT source_database, count(*) AS chunks, count(DISTINCT post_id) AS posts
        FROM substack_posts
        GROUP BY source_database
    """).fetchall()
    for row in result:
        print(f"  {row[0]}: {row[1]} chunks, {row[2]} posts")


if __name__ == "__main__":
    main()
