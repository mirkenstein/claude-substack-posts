"""Weaviate connection helper and shared constants for EngRu collections."""

import weaviate

# Collection names
POSTS_COLLECTION = "SubstackPostEngRu"
COMMENTS_COLLECTION = "SubstackCommentEngRu"

# Chunking settings (same as ForestParkPharmacy reference)
CHUNK_SIZE = 700       # Target chunk size in tokens
OVERLAP = 150          # Token overlap between chunks
MIN_CHUNK_SIZE = 400   # Minimum chunk size (merge small remainders)

# Word count threshold for chunking (posts <= this are single objects)
CHUNK_WORD_THRESHOLD = 5000


def get_client() -> weaviate.WeaviateClient:
    """Connect to local Weaviate instance."""
    client = weaviate.connect_to_local(
        host="127.0.0.1",
        port=8080,
        grpc_port=50051,
    )
    return client
