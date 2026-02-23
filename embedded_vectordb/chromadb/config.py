"""ChromaDB connection helper and shared constants."""

import os
from pathlib import Path

import chromadb
from chromadb.utils.embedding_functions import JinaEmbeddingFunction

# Database path
DB_PATH = os.environ.get(
    "CHROMADB_PATH",
    str(Path.home() / ".local/share/chromadb-substack"),
)

# Collection names
POSTS_COLLECTION = "substack_posts"
COMMENTS_COLLECTION = "substack_comments"

# Jina embedding config
JINA_MODEL = "jina-embeddings-v3"


def get_client() -> chromadb.ClientAPI:
    """Open (or create) a persistent ChromaDB client."""
    return chromadb.PersistentClient(path=DB_PATH)


def get_jina_ef() -> JinaEmbeddingFunction:
    """Return a Jina AI embedding function for ChromaDB."""
    api_key = os.environ.get("JINA_API_KEY")
    if not api_key:
        raise ValueError("JINA_API_KEY environment variable is required")
    return JinaEmbeddingFunction(
        api_key=api_key,
        model_name=JINA_MODEL,
    )
