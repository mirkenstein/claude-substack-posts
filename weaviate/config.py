"""Weaviate connection helper and shared constants for EngRu collections."""

import os
from pathlib import Path

import weaviate

# Load API keys from docker/.env if not already in environment
_ENV_FILE = Path(__file__).parent / "docker" / ".env"
if _ENV_FILE.exists():
    for line in _ENV_FILE.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())

# Collection names
POSTS_COLLECTION = "SubstackPostEngRu"
COMMENTS_COLLECTION = "SubstackCommentEngRu"
VIDEO_COLLECTION = "VideoChunkEngRu"
EXTERNAL_ARTICLES_COLLECTION = "ExternalArticleEngRu"
EXTERNAL_COMMENTS_COLLECTION = "ExternalCommentEngRu"

# Chunking settings (same as ForestParkPharmacy reference)
CHUNK_SIZE = 700       # Target chunk size in tokens
OVERLAP = 150          # Token overlap between chunks
MIN_CHUNK_SIZE = 400   # Minimum chunk size (merge small remainders)

# Word count threshold for chunking (posts <= this are single objects)
CHUNK_WORD_THRESHOLD = 5000

# Connection mode: set WEAVIATE_EMBEDDED=1 to use embedded instance
USE_EMBEDDED = os.environ.get("WEAVIATE_EMBEDDED", "").strip() in ("1", "true", "yes")


def get_client() -> weaviate.WeaviateClient:
    """Connect to Weaviate. Uses embedded if WEAVIATE_EMBEDDED=1, otherwise standalone."""
    api_key = os.environ.get("JINAAI_API_KEY") or os.environ.get("JINAAI_APIKEY")
    headers = {"X-Jinaai-Api-Key": api_key} if api_key else {}

    if USE_EMBEDDED:
        return weaviate.connect_to_local(
            host="127.0.0.1",
            port=8079,
            grpc_port=50060,
            headers=headers,
        )

    return weaviate.connect_to_local(
        host="127.0.0.1",
        port=8080,
        grpc_port=50051,
        headers=headers,
    )
