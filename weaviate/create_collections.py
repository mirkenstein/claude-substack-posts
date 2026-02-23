#!/usr/bin/env python3
"""Create SubstackPostEngRu and SubstackCommentEngRu collections in Weaviate."""

from config import get_client, POSTS_COLLECTION, COMMENTS_COLLECTION, USE_EMBEDDED
from weaviate.classes.config import Configure, Property, DataType


def _posts_vector_config():
    """Return vector config based on connection mode."""
    if USE_EMBEDDED:
        return Configure.Vectors.text2vec_jinaai(
            model="jina-embeddings-v3",
            dimensions=1024,
            vectorize_collection_name=False,
            source_properties=["content"],
        )
    return Configure.Vectors.text2vec_openai(
        model="text-embedding-3-small",
        dimensions=1536,
        base_url="https://api.openai.com",
        vectorize_collection_name=False,
        source_properties=["content"],
    )


def _posts_reranker_config():
    """Return reranker config based on connection mode."""
    if USE_EMBEDDED:
        return Configure.Reranker.jinaai(
            model="jina-reranker-v2-base-multilingual",
        )
    return Configure.Reranker.cohere(
        model="rerank-english-v3.0"
    )


def create_posts_collection(client):
    """Create SubstackPostEngRu collection for chunked post content."""
    collection = client.collections.create(
        name=POSTS_COLLECTION,
        description="EngRu Substack posts chunked for semantic search",
        vector_config=_posts_vector_config(),
        reranker_config=_posts_reranker_config(),
        properties=[
            Property(name="content", data_type=DataType.TEXT,
                     description="Post content chunk (vectorized)"),
            Property(name="postId", data_type=DataType.TEXT,
                     index_searchable=True),
            Property(name="title", data_type=DataType.TEXT,
                     index_searchable=True),
            Property(name="subtitle", data_type=DataType.TEXT,
                     index_searchable=True),
            Property(name="slug", data_type=DataType.TEXT,
                     index_searchable=True),
            Property(name="canonicalUrl", data_type=DataType.TEXT,
                     index_filterable=False),
            Property(name="postDate", data_type=DataType.DATE),
            Property(name="audience", data_type=DataType.TEXT,
                     index_filterable=True),
            Property(name="authorName", data_type=DataType.TEXT,
                     index_filterable=True),
            Property(name="publicationName", data_type=DataType.TEXT,
                     index_filterable=True),
            Property(name="subdomain", data_type=DataType.TEXT,
                     index_filterable=True),
            Property(name="wordcount", data_type=DataType.INT),
            Property(name="commentCount", data_type=DataType.INT),
            Property(name="restacks", data_type=DataType.INT),
            Property(name="chunkNumber", data_type=DataType.INT),
            Property(name="totalChunks", data_type=DataType.INT),
            Property(name="chunkTokens", data_type=DataType.INT),
        ],
    )
    return collection


def _comments_vector_config():
    """Return vector config for comments based on connection mode."""
    if USE_EMBEDDED:
        return Configure.Vectors.text2vec_jinaai(
            model="jina-embeddings-v3",
            dimensions=1024,
            vectorize_collection_name=False,
            source_properties=["body"],
        )
    return Configure.Vectors.text2vec_openai(
        model="text-embedding-3-small",
        dimensions=1536,
        base_url="https://api.openai.com",
        vectorize_collection_name=False,
        source_properties=["body"],
    )


def create_comments_collection(client):
    """Create SubstackCommentEngRu collection for comment bodies."""
    collection = client.collections.create(
        name=COMMENTS_COLLECTION,
        description="EngRu Substack comments for semantic search",
        vector_config=_comments_vector_config(),
        reranker_config=_posts_reranker_config(),
        properties=[
            Property(name="body", data_type=DataType.TEXT,
                     description="Comment text (vectorized)"),
            Property(name="commentId", data_type=DataType.TEXT,
                     index_searchable=True),
            Property(name="postId", data_type=DataType.TEXT,
                     index_searchable=True),
            Property(name="postTitle", data_type=DataType.TEXT,
                     index_searchable=True),
            Property(name="postSlug", data_type=DataType.TEXT),
            Property(name="authorName", data_type=DataType.TEXT,
                     index_filterable=True),
            Property(name="authorHandle", data_type=DataType.TEXT,
                     index_filterable=True),
            Property(name="date", data_type=DataType.DATE),
            Property(name="reactionCount", data_type=DataType.INT),
            Property(name="depth", data_type=DataType.INT),
            Property(name="isValuable", data_type=DataType.BOOL,
                     index_filterable=True),
            Property(name="subdomain", data_type=DataType.TEXT,
                     index_filterable=True),
        ],
    )
    return collection


def main():
    client = get_client()
    try:
        print(f"Connected to Weaviate (ready: {client.is_ready()})")
        print()

        existing = [c for c in client.collections.list_all()]

        # Posts collection
        if POSTS_COLLECTION in existing:
            resp = input(f"{POSTS_COLLECTION} exists. Delete and recreate? (yes/no): ")
            if resp.lower() == "yes":
                client.collections.delete(POSTS_COLLECTION)
                print(f"  Deleted {POSTS_COLLECTION}")
            else:
                print(f"  Skipping {POSTS_COLLECTION}")
        if POSTS_COLLECTION not in client.collections.list_all():
            create_posts_collection(client)
            print(f"  Created {POSTS_COLLECTION}")

        # Comments collection
        if COMMENTS_COLLECTION in existing:
            resp = input(f"{COMMENTS_COLLECTION} exists. Delete and recreate? (yes/no): ")
            if resp.lower() == "yes":
                client.collections.delete(COMMENTS_COLLECTION)
                print(f"  Deleted {COMMENTS_COLLECTION}")
            else:
                print(f"  Skipping {COMMENTS_COLLECTION}")
        if COMMENTS_COLLECTION not in client.collections.list_all():
            create_comments_collection(client)
            print(f"  Created {COMMENTS_COLLECTION}")

        # Show summary
        print()
        for name in [POSTS_COLLECTION, COMMENTS_COLLECTION]:
            if name in client.collections.list_all():
                col = client.collections.get(name)
                cfg = col.config.get()
                print(f"{cfg.name}: {len(cfg.properties)} properties")
                for p in cfg.properties:
                    print(f"  {p.name} ({p.data_type})")
                print()
    finally:
        client.close()


if __name__ == "__main__":
    main()
