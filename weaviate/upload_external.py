#!/usr/bin/env python3
"""Upload external articles and comments from PostgreSQL to Weaviate.

Reads from external.articles and external.comments, chunks articles by tokens,
bundles comments by article, and uploads to Weaviate with JinaAI v3 embeddings.

Supports watermark-based incremental uploads: only records with updated_at after
the last successful upload are processed. Use --all to force a full re-upload.

Usage:
    python upload_external.py                    # incremental (new/updated since last run)
    python upload_external.py --all              # re-upload everything
    python upload_external.py --since 2025-01-01 # upload records updated after date
    python upload_external.py --upload-only      # skip collection creation
    python upload_external.py --create-only      # only create collections
    python upload_external.py --articles-only    # only articles
    python upload_external.py --comments-only    # only comments
    python upload_external.py --database podcasts  # upload from podcasts DB → ExternalArticlePodcasts
"""

import argparse
import html
import re
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import tiktoken

sys.path.insert(0, str(Path(__file__).parent.parent))
from src.db.connection import DatabaseConnection

sys.path.insert(0, str(Path(__file__).parent))
from config import (
    get_client,
    EXTERNAL_ARTICLES_COLLECTION,
    EXTERNAL_COMMENTS_COLLECTION,
    EXTERNAL_ARTICLES_PODCASTS_COLLECTION,
    EXTERNAL_COMMENTS_PODCASTS_COLLECTION,
    CHUNK_SIZE, OVERLAP, MIN_CHUNK_SIZE,
)

from vector_cache import content_hash, load_cache
from weaviate.classes.config import Configure, Property, DataType

tokenizer = tiktoken.get_encoding("cl100k_base")

# UUID namespace for deterministic IDs
UUID_NS = uuid.NAMESPACE_DNS


# ---------------------------------------------------------------------------
# Text cleaning
# ---------------------------------------------------------------------------

def clean_text(text: str) -> str:
    """Clean text for embedding (mostly Russian content)."""
    if not text:
        return ""
    text = text.replace('\xa0', ' ')
    text = html.unescape(text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    text = '\n'.join(line.strip() for line in text.splitlines())
    return text.strip()


# ---------------------------------------------------------------------------
# Tokenization / chunking
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
# Collection creation
# ---------------------------------------------------------------------------

def create_articles_collection(client, name=EXTERNAL_ARTICLES_COLLECTION):
    """Create external articles collection with JinaAI v3 vectorizer."""
    if client.collections.exists(name):
        resp = input(f"{name} exists. Delete and recreate? (yes/no): ")
        if resp.lower() == "yes":
            client.collections.delete(name)
            print(f"  Deleted {name}")
        else:
            print(f"  Skipping creation of {name}")
            return

    client.collections.create(
        name=name,
        description="External article chunks (TopWar, Katyusha, LJ, etc.) with JinaAI v3 embeddings",
        vector_config=Configure.Vectors.text2vec_jinaai(
            model="jina-embeddings-v3",
            dimensions=1024,
            vectorize_collection_name=False,
            source_properties=["content"],
        ),
        reranker_config=Configure.Reranker.jinaai(
            model="jina-reranker-v3",
        ),
        properties=[
            Property(name="content", data_type=DataType.TEXT,
                     description="Chunk text (vectorized)"),
            Property(name="articleId", data_type=DataType.INT),
            Property(name="title", data_type=DataType.TEXT,
                     index_searchable=True),
            Property(name="author", data_type=DataType.TEXT,
                     index_filterable=True),
            Property(name="sourceDomain", data_type=DataType.TEXT,
                     index_filterable=True),
            Property(name="sourceName", data_type=DataType.TEXT,
                     index_filterable=True),
            Property(name="url", data_type=DataType.TEXT,
                     index_filterable=False, index_searchable=False),
            Property(name="publishDate", data_type=DataType.DATE),
            Property(name="language", data_type=DataType.TEXT,
                     index_filterable=True),
            Property(name="commentCount", data_type=DataType.INT),
            Property(name="views", data_type=DataType.INT),
            Property(name="chunkNumber", data_type=DataType.INT),
            Property(name="totalChunks", data_type=DataType.INT),
            Property(name="chunkTokens", data_type=DataType.INT),
        ],
    )
    print(f"  Created {name}")


def create_comments_collection(client, name=EXTERNAL_COMMENTS_COLLECTION):
    """Create external comments collection with JinaAI v3 vectorizer."""
    if client.collections.exists(name):
        resp = input(f"{name} exists. Delete and recreate? (yes/no): ")
        if resp.lower() == "yes":
            client.collections.delete(name)
            print(f"  Deleted {name}")
        else:
            print(f"  Skipping creation of {name}")
            return

    client.collections.create(
        name=name,
        description="External article comment bundles with JinaAI v3 embeddings",
        vector_config=Configure.Vectors.text2vec_jinaai(
            model="jina-embeddings-v3",
            dimensions=1024,
            vectorize_collection_name=False,
            source_properties=["commentBundle"],
        ),
        reranker_config=Configure.Reranker.jinaai(
            model="jina-reranker-v3",
        ),
        properties=[
            Property(name="commentBundle", data_type=DataType.TEXT,
                     description="Bundled comment text (vectorized)"),
            Property(name="articleId", data_type=DataType.INT),
            Property(name="articleTitle", data_type=DataType.TEXT,
                     index_searchable=True),
            Property(name="sourceDomain", data_type=DataType.TEXT,
                     index_filterable=True),
            Property(name="sourceName", data_type=DataType.TEXT,
                     index_filterable=True),
            Property(name="commentIds", data_type=DataType.INT_ARRAY),
            Property(name="usernames", data_type=DataType.TEXT_ARRAY),
            Property(name="bundleNumber", data_type=DataType.INT),
            Property(name="totalBundles", data_type=DataType.INT),
            Property(name="bundleTokens", data_type=DataType.INT),
        ],
    )
    print(f"  Created {name}")


# ---------------------------------------------------------------------------
# Watermark (incremental upload tracking)
# ---------------------------------------------------------------------------

def _watermark_file(database: str) -> Path:
    suffix = f"_{database}" if database != "substack" else ""
    return Path(__file__).parent / f".last_upload_external{suffix}"


def get_last_upload_time(database: str = "substack") -> str | None:
    """Read the watermark timestamp from the last successful upload."""
    wf = _watermark_file(database)
    if wf.exists():
        return wf.read_text().strip()
    return None


def save_upload_time(timestamp: str, database: str = "substack"):
    """Save the current upload timestamp as watermark."""
    _watermark_file(database).write_text(timestamp)


# ---------------------------------------------------------------------------
# DB queries
# ---------------------------------------------------------------------------

ARTICLES_QUERY = """
SELECT
    a.id,
    a.title,
    a.author,
    a.url,
    a.publish_date,
    a.text,
    a.comment_count,
    a.views,
    s.domain,
    s.name AS source_name,
    s.language
FROM external.articles a
JOIN external.sources s ON s.id = a.source_id
{where}
ORDER BY s.domain, a.id
"""

COMMENTS_QUERY = """
SELECT
    c.id AS comment_id,
    c.article_id,
    c.username,
    c.body,
    c.rating,
    a.title AS article_title,
    s.domain,
    s.name AS source_name
FROM external.comments c
JOIN external.articles a ON a.id = c.article_id
JOIN external.sources s ON s.id = a.source_id
{where}
ORDER BY s.domain, c.article_id, c.id
"""


def fetch_articles(conn, since: str | None = None) -> list[dict]:
    where = ""
    params = []
    if since:
        where = "WHERE a.updated_at > %s"
        params = [since]
    query = ARTICLES_QUERY.format(where=where)
    with conn.cursor() as cur:
        cur.execute("SET search_path TO external, public")
        cur.execute(query, params)
        columns = [desc[0] for desc in cur.description]
        return [dict(zip(columns, r)) for r in cur.fetchall()]


def fetch_comments(conn, since: str | None = None) -> list[dict]:
    where = ""
    params = []
    if since:
        where = "WHERE c.updated_at > %s"
        params = [since]
    query = COMMENTS_QUERY.format(where=where)
    with conn.cursor() as cur:
        cur.execute("SET search_path TO external, public")
        cur.execute(query, params)
        columns = [desc[0] for desc in cur.description]
        return [dict(zip(columns, r)) for r in cur.fetchall()]


# ---------------------------------------------------------------------------
# Article chunking
# ---------------------------------------------------------------------------

def build_article_chunks(row: dict) -> list[dict]:
    """Convert an article row into one or more chunk dicts."""
    text = clean_text(row["text"])
    if not text:
        return []

    shared = {
        "articleId": row["id"],
        "title": row["title"] or "",
        "author": row["author"] or "",
        "sourceDomain": row["domain"],
        "sourceName": row["source_name"] or "",
        "url": row["url"] or "",
        "publishDate": row["publish_date"].isoformat() if row["publish_date"] else None,
        "language": row["language"] or "ru",
        "commentCount": row["comment_count"] or 0,
        "views": row["views"] or 0,
    }

    text_chunks = chunk_by_tokens(text)
    chunks = []
    for idx, chunk_text in enumerate(text_chunks):
        tok_count = count_tokens(chunk_text)
        if tok_count < MIN_CHUNK_SIZE and idx > 0 and chunks:
            merged_tokens = count_tokens(chunks[-1]["content"] + "\n\n" + chunk_text)
            if merged_tokens <= 7000:
                chunks[-1]["content"] += "\n\n" + chunk_text
                chunks[-1]["chunkTokens"] = merged_tokens
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
# Comment bundling
# ---------------------------------------------------------------------------

def format_comment(comment: dict) -> str:
    """Format a single comment for bundling."""
    username = comment["username"] or "anonymous"
    rating = comment["rating"] or ""
    body = clean_text(comment["body"])
    if rating:
        return f"[{username}] ({rating}): {body}\n---\n"
    return f"[{username}]: {body}\n---\n"


def bundle_comments(comments: list[dict], article_meta: dict) -> list[dict]:
    """Bundle comments for a single article into token-limited bundles."""
    if not comments:
        return []

    bundles = []
    current_texts = []
    current_ids = []
    current_usernames = []
    current_tokens = 0

    for comment in comments:
        formatted = format_comment(comment)
        tok_count = count_tokens(formatted)

        # Long comment gets its own bundle
        if tok_count > CHUNK_SIZE:
            # Flush current bundle first
            if current_texts:
                bundles.append({
                    "texts": current_texts,
                    "ids": current_ids,
                    "usernames": current_usernames,
                    "tokens": current_tokens,
                })
                current_texts, current_ids, current_usernames, current_tokens = [], [], [], 0
            bundles.append({
                "texts": [formatted],
                "ids": [comment["comment_id"]],
                "usernames": [comment["username"] or "anonymous"],
                "tokens": tok_count,
            })
            continue

        # Would exceed limit — flush current bundle
        if current_tokens + tok_count > CHUNK_SIZE and current_texts:
            bundles.append({
                "texts": current_texts,
                "ids": current_ids,
                "usernames": current_usernames,
                "tokens": current_tokens,
            })
            current_texts, current_ids, current_usernames, current_tokens = [], [], [], 0

        current_texts.append(formatted)
        current_ids.append(comment["comment_id"])
        current_usernames.append(comment["username"] or "anonymous")
        current_tokens += tok_count

    # Flush remainder
    if current_texts:
        bundles.append({
            "texts": current_texts,
            "ids": current_ids,
            "usernames": current_usernames,
            "tokens": current_tokens,
        })

    # Convert to Weaviate-ready dicts
    total_bundles = len(bundles)
    result = []
    for idx, b in enumerate(bundles):
        result.append({
            "commentBundle": "".join(b["texts"]),
            "articleId": article_meta["article_id"],
            "articleTitle": article_meta["article_title"] or "",
            "sourceDomain": article_meta["domain"],
            "sourceName": article_meta["source_name"] or "",
            "commentIds": b["ids"],
            "usernames": list(dict.fromkeys(b["usernames"])),  # dedupe, preserve order
            "bundleNumber": idx,
            "totalBundles": total_bundles,
            "bundleTokens": b["tokens"],
        })
    return result


# ---------------------------------------------------------------------------
# Upload
# ---------------------------------------------------------------------------

def upload_article_chunks(client, articles: list[dict], collection_name: str = EXTERNAL_ARTICLES_COLLECTION,
                          vector_cache: dict | None = None):
    """Chunk and upload articles to Weaviate, with per-source progress."""
    collection = client.collections.get(collection_name)

    # Group by source domain
    by_source = {}
    for row in articles:
        by_source.setdefault(row["domain"], []).append(row)

    total_chunks = 0
    total_errors = 0
    cache_hits = 0
    start = time.time()

    for domain, source_articles in by_source.items():
        source_chunks = 0
        for i, row in enumerate(source_articles):
            chunks = build_article_chunks(row)
            if not chunks:
                continue

            title_preview = (row["title"] or "")[:40]
            print(f"[Articles] {domain} | article {i+1}/{len(source_articles)} "
                  f"| \"{title_preview}\" | {len(chunks)} chunks | ", end="", flush=True)

            with collection.batch.dynamic() as batch:
                for chunk in chunks:
                    obj_uuid = uuid.uuid5(
                        UUID_NS,
                        f"engru-ext-article-{chunk['articleId']}-{chunk['chunkNumber']}"
                    )
                    content = chunk.pop("content")
                    # Look up cached vector by content hash
                    vector = None
                    if vector_cache:
                        h = content_hash(content)
                        vector = vector_cache.get(h)
                        if vector:
                            cache_hits += 1
                    batch.add_object(
                        properties={"content": content, **chunk},
                        uuid=obj_uuid,
                        vector=vector,
                    )

            if batch.number_errors > 0:
                print(f"errors: {batch.number_errors}")
                total_errors += batch.number_errors
            else:
                print("loaded")

            source_chunks += len(chunks)
            total_chunks += len(chunks)

        print(f"[Articles] {domain} done: {source_chunks} chunks from {len(source_articles)} articles")

    elapsed = time.time() - start
    print(f"\nArticles total: {total_chunks} chunks, {total_errors} errors in {elapsed:.1f}s")
    if vector_cache:
        print(f"  Cache hits: {cache_hits}/{total_chunks} ({100*cache_hits/total_chunks:.0f}% skipped embedding)")

    count = collection.aggregate.over_all(total_count=True).total_count
    print(f"Collection {collection_name} count: {count}")


def upload_comment_bundles(client, comments: list[dict], collection_name: str = EXTERNAL_COMMENTS_COLLECTION,
                           vector_cache: dict | None = None):
    """Bundle and upload comments to Weaviate, grouped by article and source."""
    collection = client.collections.get(collection_name)

    # Group comments by article_id
    by_article = {}
    for c in comments:
        by_article.setdefault(c["article_id"], []).append(c)

    # Group articles by domain for progress reporting
    article_domains = {}
    for c in comments:
        if c["article_id"] not in article_domains:
            article_domains[c["article_id"]] = {
                "domain": c["domain"],
                "source_name": c["source_name"],
                "article_title": c.get("article_title", ""),
            }

    # Order by domain
    domain_articles = {}
    for article_id, meta in article_domains.items():
        domain_articles.setdefault(meta["domain"], []).append(article_id)

    total_bundles = 0
    total_errors = 0
    cache_hits = 0
    start = time.time()

    for domain, article_ids in domain_articles.items():
        source_bundles = 0
        for i, article_id in enumerate(article_ids):
            article_comments = by_article[article_id]
            meta = {
                "article_id": article_id,
                "article_title": article_domains[article_id]["article_title"],
                "domain": domain,
                "source_name": article_domains[article_id]["source_name"],
            }
            bundles = bundle_comments(article_comments, meta)
            if not bundles:
                continue

            print(f"[Comments] {domain} | article {i+1}/{len(article_ids)} "
                  f"| {len(article_comments)} comments → {len(bundles)} bundles | ",
                  end="", flush=True)

            with collection.batch.dynamic() as batch:
                for b in bundles:
                    obj_uuid = uuid.uuid5(
                        UUID_NS,
                        f"engru-ext-comment-{b['articleId']}-{b['bundleNumber']}"
                    )
                    bundle_text = b.pop("commentBundle")
                    # Look up cached vector by content hash
                    vector = None
                    if vector_cache:
                        h = content_hash(bundle_text)
                        vector = vector_cache.get(h)
                        if vector:
                            cache_hits += 1
                    batch.add_object(
                        properties={"commentBundle": bundle_text, **b},
                        uuid=obj_uuid,
                        vector=vector,
                    )

            if batch.number_errors > 0:
                print(f"errors: {batch.number_errors}")
                total_errors += batch.number_errors
            else:
                print("loaded")

            source_bundles += len(bundles)
            total_bundles += len(bundles)

        print(f"[Comments] {domain} done: {source_bundles} bundles from {len(article_ids)} articles")

    elapsed = time.time() - start
    print(f"\nComments total: {total_bundles} bundles, {total_errors} errors in {elapsed:.1f}s")
    if vector_cache:
        print(f"  Cache hits: {cache_hits}/{total_bundles} ({100*cache_hits/total_bundles:.0f}% skipped embedding)")

    count = collection.aggregate.over_all(total_count=True).total_count
    print(f"Collection {collection_name} count: {count}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Upload external articles/comments to Weaviate")
    parser.add_argument("--upload-only", action="store_true",
                        help="Skip collection creation, only upload")
    parser.add_argument("--create-only", action="store_true",
                        help="Only create collections, don't upload")
    parser.add_argument("--articles-only", action="store_true",
                        help="Only process articles")
    parser.add_argument("--comments-only", action="store_true",
                        help="Only process comments")
    parser.add_argument("--all", action="store_true",
                        help="Re-upload all (ignore watermark)")
    parser.add_argument("--since", metavar="TIMESTAMP",
                        help="Upload records updated after this timestamp (ISO format)")
    parser.add_argument("--database", default="substack",
                        help="PostgreSQL database name (default: substack)")
    parser.add_argument("--use-cache", action="store_true",
                        help="Use cached vectors to skip embedding API calls for unchanged content")
    args = parser.parse_args()

    database = args.database
    do_articles = not args.comments_only
    do_comments = not args.articles_only

    # Select collections based on database
    if database == "podcasts":
        articles_collection = EXTERNAL_ARTICLES_PODCASTS_COLLECTION
        comments_collection = EXTERNAL_COMMENTS_PODCASTS_COLLECTION
    else:
        articles_collection = EXTERNAL_ARTICLES_COLLECTION
        comments_collection = EXTERNAL_COMMENTS_COLLECTION

    # Determine the cutoff for incremental upload
    since = None
    if args.since:
        since = args.since
    elif not args.all:
        since = get_last_upload_time(database)

    upload_start = datetime.now(timezone.utc).isoformat()

    client = get_client()
    try:
        print(f"Connected to Weaviate (ready: {client.is_ready()})")
        print(f"Database: {database} → Articles: {articles_collection}, Comments: {comments_collection}")

        if since:
            print(f"Incremental upload: records updated after {since}")
        else:
            print("Full upload: all records")

        # Create collections
        if not args.upload_only:
            if do_articles:
                create_articles_collection(client, articles_collection)
            if do_comments:
                create_comments_collection(client, comments_collection)

        if args.create_only:
            return

        # Read from PostgreSQL
        print(f"\nReading from PostgreSQL ({database}, external schema)...")
        with DatabaseConnection(database=database) as conn:
            with conn.cursor() as cur:
                cur.execute("SET search_path TO external, public")

            if do_articles:
                articles = fetch_articles(conn, since=since)
                print(f"  {len(articles)} articles")
            if do_comments:
                comments = fetch_comments(conn, since=since)
                print(f"  {len(comments)} comments")

        # Load vector caches if requested
        articles_cache = None
        comments_cache = None
        if args.use_cache:
            if do_articles:
                articles_cache = load_cache(articles_collection)
            if do_comments:
                comments_cache = load_cache(comments_collection)

        # Upload
        if do_articles:
            if articles:
                print(f"\n{'='*60}")
                print("UPLOADING ARTICLES")
                print(f"{'='*60}")
                upload_article_chunks(client, articles, articles_collection,
                                      vector_cache=articles_cache)
            else:
                print("\nNo new articles to upload.")

        if do_comments:
            if comments:
                print(f"\n{'='*60}")
                print("UPLOADING COMMENTS")
                print(f"{'='*60}")
                upload_comment_bundles(client, comments, comments_collection,
                                       vector_cache=comments_cache)
            else:
                print("\nNo new comments to upload.")

        # Save watermark on success
        save_upload_time(upload_start, database)
        print(f"\nWatermark saved: {upload_start}")

    finally:
        client.close()


if __name__ == "__main__":
    main()
