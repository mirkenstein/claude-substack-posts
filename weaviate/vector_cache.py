#!/usr/bin/env python3
"""Export and load cached embedding vectors from Weaviate collections.

Vectors are keyed by MD5 hash of the vectorized content (the text field that
gets embedded), so they survive DB ID changes. When re-uploading from scratch,
the upload script can look up cached vectors by content hash and skip the
embedding API call.

Cache files are stored as JSONL in weaviate/vector_cache/{collection_name}.jsonl

Usage:
    # Export vectors from existing collections
    python vector_cache.py --collection ExternalArticleEngRu
    python vector_cache.py --collection ExternalCommentEngRu
    python vector_cache.py --collection ExternalArticleEngRu ExternalCommentEngRu

    # List cached collections
    python vector_cache.py --list
"""

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from config import get_client

CACHE_DIR = Path(__file__).parent / "vector_cache"

# Map collection name → property that gets vectorized
VECTORIZED_PROPERTY = {
    "ExternalArticleEngRu": "content",
    "ExternalCommentEngRu": "commentBundle",
    "ExternalArticlePodcasts": "content",
    "ExternalCommentPodcasts": "commentBundle",
    "SubstackPostEngRu": "content",
    "SubstackPostPodcasts": "content",
    "SubstackCommentEngRu": "body",
    "SubstackCommentPodcasts": "body",
    "VideoChunkEngRu": "transcript",
    "VideoChunkPodcasts": "transcript",
    "VideoChapterChunkPodcasts": "transcript",
    "VideoChapterChunkEngRu": "transcript",
    "VideoChunkLibOpp": "transcript",
    "VideoChapterChunkLibOpp": "transcript",
    "TelegramMessageEngRu": "content",
    "TelegramTranscriptEngRu": "transcript",
}


def content_hash(text: str) -> str:
    """MD5 hex digest of text content, used as cache key."""
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def export_collection(client, collection_name: str) -> int:
    """Export all vectors from a collection to a JSONL cache file.

    Each line: {"h": "<md5>", "v": [float, ...]}
    Returns number of objects exported.
    """
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_path = CACHE_DIR / f"{collection_name}.jsonl"

    text_prop = VECTORIZED_PROPERTY.get(collection_name)
    if not text_prop:
        # Try to auto-detect from collection config
        col = client.collections.get(collection_name)
        cfg = col.config.get()
        for name, vc in cfg.vector_config.items():
            if hasattr(vc.vectorizer, 'source_properties'):
                text_prop = vc.vectorizer.source_properties[0]
                break
        if not text_prop:
            print(f"ERROR: Cannot determine vectorized property for {collection_name}.")
            print(f"Add it to VECTORIZED_PROPERTY in vector_cache.py")
            return 0

    collection = client.collections.get(collection_name)
    count = collection.aggregate.over_all(total_count=True).total_count
    print(f"Exporting {count} objects from {collection_name} (text prop: {text_prop})...")

    exported = 0
    start = time.time()

    with open(cache_path, "w") as f:
        for obj in collection.iterator(include_vector=True):
            text = obj.properties.get(text_prop, "")
            if not text:
                continue
            vec = obj.vector.get("default", [])
            if not vec:
                continue

            h = content_hash(text)
            line = json.dumps({"h": h, "v": vec}, separators=(",", ":"))
            f.write(line + "\n")
            exported += 1

            if exported % 1000 == 0:
                elapsed = time.time() - start
                print(f"  {exported}/{count} ({exported/elapsed:.0f}/sec)")

    elapsed = time.time() - start
    size_mb = cache_path.stat().st_size / (1024 * 1024)
    print(f"Exported {exported} vectors to {cache_path} ({size_mb:.1f} MB) in {elapsed:.1f}s")
    return exported


def load_cache(collection_name: str) -> dict[str, list[float]]:
    """Load cached vectors from JSONL file.

    Returns dict mapping content MD5 hash → vector (list of floats).
    """
    cache_path = CACHE_DIR / f"{collection_name}.jsonl"
    if not cache_path.exists():
        return {}

    cache = {}
    start = time.time()
    with open(cache_path) as f:
        for line in f:
            row = json.loads(line)
            cache[row["h"]] = row["v"]

    elapsed = time.time() - start
    print(f"Loaded {len(cache)} cached vectors from {cache_path.name} in {elapsed:.1f}s")
    return cache


def list_caches():
    """List available cache files."""
    if not CACHE_DIR.exists():
        print("No cache directory yet.")
        return

    files = sorted(CACHE_DIR.glob("*.jsonl"))
    if not files:
        print("No cache files found.")
        return

    print(f"Cache directory: {CACHE_DIR}")
    for f in files:
        size_mb = f.stat().st_size / (1024 * 1024)
        with open(f) as fh:
            lines = sum(1 for _ in fh)
        name = f.stem
        print(f"  {name}: {lines:,} vectors ({size_mb:.1f} MB)")


def main():
    parser = argparse.ArgumentParser(description="Export/manage Weaviate vector caches")
    parser.add_argument("--collection", "-c", nargs="+",
                        help="Collection name(s) to export")
    parser.add_argument("--list", "-l", action="store_true",
                        help="List available cache files")
    args = parser.parse_args()

    if args.list:
        list_caches()
        return

    if not args.collection:
        parser.print_help()
        return

    client = get_client()
    try:
        for name in args.collection:
            export_collection(client, name)
    finally:
        client.close()


if __name__ == "__main__":
    main()