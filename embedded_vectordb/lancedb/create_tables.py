#!/usr/bin/env python3
"""Create LanceDB tables for posts and comments with Jina AI embeddings."""

import pyarrow as pa

from config import get_db, get_jina_embeddings, list_tables, POSTS_TABLE, COMMENTS_TABLE, EMBEDDING_DIM


def create_posts_table(db):
    """Create posts table with Jina AI vectorization on 'content' field."""
    schema = pa.schema([
        pa.field("content", pa.utf8()),
        pa.field("vector", pa.list_(pa.float32(), EMBEDDING_DIM)),
        pa.field("postId", pa.utf8()),
        pa.field("title", pa.utf8()),
        pa.field("subtitle", pa.utf8()),
        pa.field("slug", pa.utf8()),
        pa.field("canonicalUrl", pa.utf8()),
        pa.field("postDate", pa.utf8()),
        pa.field("audience", pa.utf8()),
        pa.field("authorName", pa.utf8()),
        pa.field("publicationName", pa.utf8()),
        pa.field("subdomain", pa.utf8()),
        pa.field("wordcount", pa.int32()),
        pa.field("commentCount", pa.int32()),
        pa.field("restacks", pa.int32()),
        pa.field("chunkNumber", pa.int32()),
        pa.field("totalChunks", pa.int32()),
        pa.field("chunkTokens", pa.int32()),
    ])
    table = db.create_table(POSTS_TABLE, schema=schema)
    print(f"Created {POSTS_TABLE} ({len(schema)} fields, vector dim={EMBEDDING_DIM})")
    return table


def create_comments_table(db):
    """Create comments table with Jina AI vectorization on 'body' field."""
    schema = pa.schema([
        pa.field("body", pa.utf8()),
        pa.field("vector", pa.list_(pa.float32(), EMBEDDING_DIM)),
        pa.field("commentId", pa.utf8()),
        pa.field("postId", pa.utf8()),
        pa.field("postTitle", pa.utf8()),
        pa.field("postSlug", pa.utf8()),
        pa.field("authorName", pa.utf8()),
        pa.field("authorHandle", pa.utf8()),
        pa.field("date", pa.utf8()),
        pa.field("reactionCount", pa.int32()),
        pa.field("depth", pa.int32()),
        pa.field("isValuable", pa.bool_()),
        pa.field("subdomain", pa.utf8()),
    ])
    table = db.create_table(COMMENTS_TABLE, schema=schema)
    print(f"Created {COMMENTS_TABLE} ({len(schema)} fields, vector dim={EMBEDDING_DIM})")
    return table


def main():
    db = get_db()
    existing = list_tables(db)

    # Posts table
    if POSTS_TABLE in existing:
        resp = input(f"{POSTS_TABLE} exists. Delete and recreate? (yes/no): ")
        if resp.lower() == "yes":
            db.drop_table(POSTS_TABLE)
            print(f"  Deleted {POSTS_TABLE}")
        else:
            print(f"  Skipping {POSTS_TABLE}")
    if POSTS_TABLE not in list_tables(db):
        create_posts_table(db)

    # Comments table
    if COMMENTS_TABLE in existing:
        resp = input(f"{COMMENTS_TABLE} exists. Delete and recreate? (yes/no): ")
        if resp.lower() == "yes":
            db.drop_table(COMMENTS_TABLE)
            print(f"  Deleted {COMMENTS_TABLE}")
        else:
            print(f"  Skipping {COMMENTS_TABLE}")
    if COMMENTS_TABLE not in list_tables(db):
        create_comments_table(db)

    # Summary
    print()
    for name in list_tables(db):
        tbl = db.open_table(name)
        print(f"{name}: {tbl.count_rows()} rows, schema: {tbl.schema.names}")


if __name__ == "__main__":
    main()
