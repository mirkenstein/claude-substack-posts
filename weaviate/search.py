#!/usr/bin/env python3
"""Example semantic and hybrid search queries on EngRu collections."""

import argparse

from weaviate.classes.query import MetadataQuery, Rerank

from config import get_client, POSTS_COLLECTION, COMMENTS_COLLECTION


def search_posts_semantic(collection, query: str, limit: int = 5):
    """Near-text semantic search on posts."""
    print(f"\n--- Posts: semantic search for '{query}' ---")
    response = collection.query.near_text(
        query=query,
        limit=limit,
        return_metadata=MetadataQuery(distance=True),
    )
    for obj in response.objects:
        p = obj.properties
        dist = obj.metadata.distance
        chunk_info = f" [chunk {p['chunkNumber']}/{p['totalChunks']}]" if p["totalChunks"] > 1 else ""
        print(f"  {dist:.4f}  {p['title']}{chunk_info}")
        print(f"           {p['canonicalUrl']}")
        print(f"           {p['content'][:150]}...")
        print()


def search_posts_hybrid(collection, query: str, limit: int = 5, alpha: float = 0.5):
    """Hybrid (semantic + keyword) search on posts."""
    print(f"\n--- Posts: hybrid search for '{query}' (alpha={alpha}) ---")
    response = collection.query.hybrid(
        query=query,
        alpha=alpha,
        limit=limit,
        return_metadata=MetadataQuery(score=True),
    )
    for obj in response.objects:
        p = obj.properties
        score = obj.metadata.score
        print(f"  {score:.4f}  {p['title']}")
        print(f"           {p['content'][:150]}...")
        print()


def search_posts_reranked(collection, query: str, rerank_query: str, limit: int = 5):
    """Semantic search with Cohere reranking on posts."""
    print(f"\n--- Posts: reranked search for '{query}' ---")
    response = collection.query.near_text(
        query=query,
        limit=limit,
        rerank=Rerank(prop="content", query=rerank_query),
        return_metadata=MetadataQuery(score=True, distance=True),
    )
    for obj in response.objects:
        p = obj.properties
        print(f"  rerank={obj.metadata.score:.4f}  dist={obj.metadata.distance:.4f}  {p['title']}")
        print(f"           {p['content'][:150]}...")
        print()


def search_comments_semantic(collection, query: str, limit: int = 5):
    """Near-text semantic search on comments."""
    print(f"\n--- Comments: semantic search for '{query}' ---")
    response = collection.query.near_text(
        query=query,
        limit=limit,
        return_metadata=MetadataQuery(distance=True),
    )
    for obj in response.objects:
        p = obj.properties
        dist = obj.metadata.distance
        print(f"  {dist:.4f}  [{p['authorName']}] on \"{p['postTitle']}\"")
        print(f"           {p['body'][:200]}...")
        print()


def search_comments_hybrid(collection, query: str, limit: int = 5, alpha: float = 0.5):
    """Hybrid search on comments."""
    print(f"\n--- Comments: hybrid search for '{query}' (alpha={alpha}) ---")
    response = collection.query.hybrid(
        query=query,
        alpha=alpha,
        limit=limit,
        return_metadata=MetadataQuery(score=True),
    )
    for obj in response.objects:
        p = obj.properties
        score = obj.metadata.score
        print(f"  {score:.4f}  [{p['authorName']}] on \"{p['postTitle']}\"")
        print(f"           {p['body'][:200]}...")
        print()


def main():
    parser = argparse.ArgumentParser(description="Search EngRu Weaviate collections")
    parser.add_argument("query", nargs="?", default="Wagner mutiny Prigozhin",
                        help="Search query")
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--collection", choices=["posts", "comments", "both"], default="both")
    parser.add_argument("--mode", choices=["semantic", "hybrid", "rerank"], default="semantic")
    parser.add_argument("--alpha", type=float, default=0.5,
                        help="Hybrid alpha (0=keyword, 1=semantic)")
    args = parser.parse_args()

    client = get_client()
    try:
        if args.collection in ("posts", "both"):
            posts = client.collections.get(POSTS_COLLECTION)
            if args.mode == "semantic":
                search_posts_semantic(posts, args.query, args.limit)
            elif args.mode == "hybrid":
                search_posts_hybrid(posts, args.query, args.limit, args.alpha)
            elif args.mode == "rerank":
                search_posts_reranked(posts, args.query, args.query, args.limit)

        if args.collection in ("comments", "both"):
            comments = client.collections.get(COMMENTS_COLLECTION)
            if args.mode == "semantic":
                search_comments_semantic(comments, args.query, args.limit)
            elif args.mode == "hybrid":
                search_comments_hybrid(comments, args.query, args.limit, args.alpha)
    finally:
        client.close()


if __name__ == "__main__":
    main()
