#!/usr/bin/env python3
"""
Start an embedded Weaviate instance with Jina AI embeddings.

Usage:
    python weaviate/embedded.py              # start and show status
    python weaviate/embedded.py --persist    # use custom persistence path

The embedded instance:
- Runs Weaviate in-process (no Docker needed)
- Enables text2vec-jinaai module for embeddings
- Reads JINAAI_API_KEY from environment
- Persists data to ~/.local/share/weaviate-embedded/ by default

To use from other scripts, set WEAVIATE_EMBEDDED=1 and JINAAI_API_KEY.
"""

import argparse
import os
import sys
from pathlib import Path

import weaviate


DEFAULT_PERSIST_PATH = str(Path.home() / ".local/share/weaviate-embedded")


def get_embedded_client(
    persist_path: str = DEFAULT_PERSIST_PATH,
    port: int = 8079,
    grpc_port: int = 50060,
) -> weaviate.WeaviateClient:
    """Connect to an embedded Weaviate instance with Jina AI vectorization.

    Reads the Jina AI key from JINAAI_API_KEY env var.
    """
    api_key = os.environ.get("JINAAI_API_KEY")
    if not api_key:
        print("ERROR: JINAAI_API_KEY environment variable is required", file=sys.stderr)
        sys.exit(1)

    client = weaviate.connect_to_embedded(
        port=port,
        grpc_port=grpc_port,
        persistence_data_path=persist_path,
        headers={"X-Jinaai-Api-Key": api_key},
        environment_variables={
            "JINAAI_APIKEY": api_key,
            "ENABLE_MODULES": "text2vec-jinaai,reranker-jinaai",
            "DEFAULT_VECTORIZER_MODULE": "text2vec-jinaai",
        },
    )
    return client


def main():
    parser = argparse.ArgumentParser(description="Start embedded Weaviate with Jina AI")
    parser.add_argument("--persist", default=DEFAULT_PERSIST_PATH,
                        help=f"Persistence path (default: {DEFAULT_PERSIST_PATH})")
    parser.add_argument("--port", type=int, default=8079)
    parser.add_argument("--grpc-port", type=int, default=50060)
    args = parser.parse_args()

    print(f"Starting embedded Weaviate...")
    print(f"  Persistence: {args.persist}")
    print(f"  Port: {args.port}, gRPC: {args.grpc_port}")

    client = get_embedded_client(
        persist_path=args.persist,
        port=args.port,
        grpc_port=args.grpc_port,
    )

    try:
        meta = client.get_meta()
        print(f"\nWeaviate {meta.get('version', '?')} running")
        print(f"  Modules: {', '.join(meta.get('modules', {}).keys())}")

        collections = client.collections.list_all()
        if collections:
            print(f"\n  Collections:")
            for name in collections:
                col = client.collections.get(name)
                count = col.aggregate.over_all(total_count=True).total_count
                print(f"    {name}: {count} objects")
        else:
            print("\n  No collections yet. Run create_collections.py to create them.")

        print("\nEmbedded Weaviate is ready. Press Ctrl+C to stop.")
        import time
        while True:
            time.sleep(60)

    except KeyboardInterrupt:
        print("\nShutting down...")
    finally:
        client.close()


if __name__ == "__main__":
    main()
