"""LanceDB connection helper and shared constants."""

import os
from pathlib import Path

import lancedb
from lancedb.embeddings import get_registry

# Database path
DB_PATH = os.environ.get(
    "LANCEDB_PATH",
    str(Path.home() / ".local/share/lancedb-substack"),
)

# Collection (table) names
POSTS_TABLE = "substack_posts"
COMMENTS_TABLE = "substack_comments"

# Jina embedding config
JINA_MODEL = "jina-embeddings-v3"
EMBEDDING_DIM = 1024


def get_db() -> lancedb.DBConnection:
    """Open (or create) the LanceDB database."""
    return lancedb.connect(DB_PATH)


def list_tables(db) -> list[str]:
    """Return table names as a plain list."""
    result = db.list_tables()
    return result.tables if hasattr(result, "tables") else list(result)


def get_jina_embeddings():
    """Return a Jina embedding function for LanceDB."""
    return get_registry().get("jina").create(name=JINA_MODEL)
