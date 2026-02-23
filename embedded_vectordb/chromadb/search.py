#!/usr/bin/env python3
"""Semantic search on ChromaDB posts using Jina AI embeddings.

Usage:
    python search.py "Wagner mutiny Prigozhin"
    python search.py "vaccine safety" --limit 10
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from config import get_client, get_jina_ef, POSTS_COLLECTION, COMMENTS_COLLECTION


def search_semantic(collection, query: str, limit: int = 5, where: dict = None):
    """Semantic search using Jina embeddings."""
    kwargs = {"query_texts": [query], "n_results": limit}
    if where:
        kwargs["where"] = where
    return collection.query(**kwargs)


def display_posts(results, query: str):
    """Pretty-print post search results."""
    print(f"\n--- Posts: semantic search for '{query}' ---")
    if not results["ids"][0]:
        print("  No results found.")
        return

    for i, doc_id in enumerate(results["ids"][0]):
        meta = results["metadatas"][0][i]
        dist = results["distances"][0][i] if results.get("distances") else 0
        doc = results["documents"][0][i]

        chunk_info = ""
        if meta.get("totalChunks", 1) > 1:
            chunk_info = f" [chunk {meta['chunkNumber']}/{meta['totalChunks']}]"
        print(f"  {dist:.4f}  {meta.get('title', '?')}{chunk_info}")
        print(f"           {meta.get('canonicalUrl', '')}")
        print(f"           {doc[:150]}...")
        print()


def main():
    parser = argparse.ArgumentParser(description="Search ChromaDB collections")
    parser.add_argument("query", nargs="?", default="Wagner mutiny Prigozhin",
                        help="Search query")
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--table", choices=["posts", "comments", "both"], default="posts")
    args = parser.parse_args()

    client = get_client()
    jina_ef = get_jina_ef()
    existing = [c.name for c in client.list_collections()]

    if args.table in ("posts", "both") and POSTS_COLLECTION in existing:
        collection = client.get_collection(POSTS_COLLECTION, embedding_function=jina_ef)
        results = search_semantic(collection, args.query, args.limit)
        display_posts(results, args.query)

    if args.table in ("comments", "both") and COMMENTS_COLLECTION in existing:
        collection = client.get_collection(COMMENTS_COLLECTION, embedding_function=jina_ef)
        results = search_semantic(collection, args.query, args.limit)
        print(f"\n--- Comments: semantic search for '{args.query}' ---")
        if not results["ids"][0]:
            print("  No results found.")
        else:
            for i, doc_id in enumerate(results["ids"][0]):
                meta = results["metadatas"][0][i]
                dist = results["distances"][0][i] if results.get("distances") else 0
                doc = results["documents"][0][i]
                print(f"  {dist:.4f}  [{meta.get('authorName', '?')}] on \"{meta.get('postTitle', '?')}\"")
                print(f"           {doc[:200]}...")
                print()


if __name__ == "__main__":
    main()
