#!/usr/bin/env python3
"""Semantic search on LanceDB posts using Jina AI embeddings.

Usage:
    python search.py "Wagner mutiny Prigozhin"
    python search.py "vaccine safety" --limit 10
    python search.py "Chubais privatization" --mode fts
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from config import get_db, get_jina_embeddings, list_tables, POSTS_TABLE, COMMENTS_TABLE


def search_vector(table, query_text: str, limit: int = 5):
    """Vector similarity search using Jina embeddings."""
    jina = get_jina_embeddings()
    query_vec = jina.compute_query_embeddings(query_text)[0]

    results = (
        table.search(query_vec)
        .limit(limit)
        .to_pandas()
    )
    return results


def search_fts(table, query_text: str, limit: int = 5):
    """Full-text search on the content/body column."""
    # Determine text column
    col = "content" if "content" in table.schema.names else "body"
    results = (
        table.search(query_text, query_type="fts")
        .limit(limit)
        .to_pandas()
    )
    return results


def search_hybrid(table, query_text: str, limit: int = 5):
    """Hybrid search (vector + FTS) with reranking."""
    jina = get_jina_embeddings()
    query_vec = jina.compute_query_embeddings(query_text)[0]

    results = (
        table.search(query_vec, query_type="hybrid")
        .limit(limit)
        .to_pandas()
    )
    return results


def display_posts(results, mode: str, query: str):
    """Pretty-print post search results."""
    print(f"\n--- Posts: {mode} search for '{query}' ---")
    if results.empty:
        print("  No results found.")
        return

    score_col = next((c for c in ("_distance", "_relevance_score", "_score") if c in results.columns), None)
    for _, row in results.iterrows():
        score = row.get(score_col, 0) if score_col else 0
        chunk_info = ""
        if row.get("totalChunks", 1) > 1:
            chunk_info = f" [chunk {row['chunkNumber']}/{row['totalChunks']}]"
        print(f"  {score:.4f}  {row['title']}{chunk_info}")
        print(f"           {row.get('canonicalUrl', '')}")
        print(f"           {str(row['content'])[:150]}...")
        print()


def main():
    parser = argparse.ArgumentParser(description="Search LanceDB collections")
    parser.add_argument("query", nargs="?", default="Wagner mutiny Prigozhin",
                        help="Search query")
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--table", choices=["posts", "comments", "both"], default="posts")
    parser.add_argument("--mode", choices=["vector", "fts", "hybrid"], default="vector")
    args = parser.parse_args()

    db = get_db()

    tables = list_tables(db)
    if args.table in ("posts", "both") and POSTS_TABLE in tables:
        table = db.open_table(POSTS_TABLE)

        if args.mode == "vector":
            results = search_vector(table, args.query, args.limit)
        elif args.mode == "fts":
            results = search_fts(table, args.query, args.limit)
        elif args.mode == "hybrid":
            results = search_hybrid(table, args.query, args.limit)

        display_posts(results, args.mode, args.query)

    if args.table in ("comments", "both") and COMMENTS_TABLE in tables:
        table = db.open_table(COMMENTS_TABLE)
        print(f"\n--- Comments: {args.mode} search for '{args.query}' ---")
        if args.mode == "vector":
            results = search_vector(table, args.query, args.limit)
        elif args.mode == "fts":
            results = search_fts(table, args.query, args.limit)
        elif args.mode == "hybrid":
            results = search_hybrid(table, args.query, args.limit)

        if results.empty:
            print("  No results found.")
        else:
            score_col = next((c for c in ("_distance", "_relevance_score", "_score") if c in results.columns), None)
            for _, row in results.iterrows():
                score = row.get(score_col, 0) if score_col else 0
                print(f"  {score:.4f}  [{row.get('authorName', '?')}] on \"{row.get('postTitle', '?')}\"")
                print(f"           {str(row.get('body', ''))[:200]}...")
                print()


if __name__ == "__main__":
    main()
