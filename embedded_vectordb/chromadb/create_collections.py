#!/usr/bin/env python3
"""Create ChromaDB collections for posts and comments with Jina AI embeddings."""

from config import get_client, get_jina_ef, POSTS_COLLECTION, COMMENTS_COLLECTION


def main():
    client = get_client()
    jina_ef = get_jina_ef()

    existing = [c.name for c in client.list_collections()]

    # Posts collection
    if POSTS_COLLECTION in existing:
        resp = input(f"{POSTS_COLLECTION} exists. Delete and recreate? (yes/no): ")
        if resp.lower() == "yes":
            client.delete_collection(POSTS_COLLECTION)
            print(f"  Deleted {POSTS_COLLECTION}")

    existing = [c.name for c in client.list_collections()]
    if POSTS_COLLECTION not in existing:
        client.create_collection(
            name=POSTS_COLLECTION,
            embedding_function=jina_ef,
            metadata={"hnsw:space": "cosine"},
        )
        print(f"  Created {POSTS_COLLECTION}")

    # Comments collection
    if COMMENTS_COLLECTION in existing:
        resp = input(f"{COMMENTS_COLLECTION} exists. Delete and recreate? (yes/no): ")
        if resp.lower() == "yes":
            client.delete_collection(COMMENTS_COLLECTION)
            print(f"  Deleted {COMMENTS_COLLECTION}")

    existing = [c.name for c in client.list_collections()]
    if COMMENTS_COLLECTION not in existing:
        client.create_collection(
            name=COMMENTS_COLLECTION,
            embedding_function=jina_ef,
            metadata={"hnsw:space": "cosine"},
        )
        print(f"  Created {COMMENTS_COLLECTION}")

    # Summary
    print()
    for col in client.list_collections():
        c = client.get_collection(col.name, embedding_function=jina_ef)
        print(f"{col.name}: {c.count()} documents")


if __name__ == "__main__":
    main()
