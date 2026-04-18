#!/usr/bin/env python3
"""
Ingest a single Substack post: fetch, load into PostgreSQL, download media, upload to Weaviate.

Automates the full pipeline from INGEST_SINGLE_POST.md in one command.

Usage:
    # Basic (database auto-detected from substacks.json, or defaults to 'substack')
    python ingest_single_post.py https://movingtorussia.substack.com/p/lenin-inspired-by-vendee-french-revolution

    # Explicit database
    python ingest_single_post.py https://escapekey.substack.com/p/some-post --database podcasts

    # With cookies for paid content
    python ingest_single_post.py https://blog.substack.com/p/paid-post --cookies cookies.json

    # Skip Weaviate upload
    python ingest_single_post.py https://blog.substack.com/p/post --skip-weaviate

    # Dry run (show what would happen)
    python ingest_single_post.py https://blog.substack.com/p/post --dry-run

    # Multiple posts from the same or different blogs
    python ingest_single_post.py https://blog1.substack.com/p/post1 https://blog2.substack.com/p/post2
"""

import argparse
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
from src.publication_config import get_database


def parse_substack_url(url: str) -> tuple[str, str]:
    """Extract subdomain and slug from a Substack URL.

    Handles:
      https://subdomain.substack.com/p/slug
      https://www.customdomain.com/p/slug  (subdomain inferred from domain)

    Returns (subdomain, slug).
    """
    parsed = urlparse(url)
    host = parsed.hostname or ""
    path = parsed.path.strip("/")

    # Extract slug from /p/slug
    parts = path.split("/")
    slug = None
    for i, part in enumerate(parts):
        if part == "p" and i + 1 < len(parts):
            slug = parts[i + 1]
            break

    if not slug:
        print(f"ERROR: Cannot extract slug from URL: {url}", file=sys.stderr)
        sys.exit(1)

    # Extract subdomain
    if "substack.com" in host:
        subdomain = host.split(".substack.com")[0]
        # Handle www.subdomain.substack.com
        if subdomain.startswith("www."):
            subdomain = subdomain[4:]
    else:
        # Custom domain: use first part of hostname
        subdomain = host.replace("www.", "").split(".")[0]

    return subdomain, slug


def run(cmd: list[str], dry_run: bool = False, label: str = "") -> bool:
    """Run a command, printing it first. Returns True on success."""
    cmd_str = " ".join(cmd)
    if label:
        print(f"\n{'='*60}")
        print(f"  {label}")
        print(f"{'='*60}")
    print(f"$ {cmd_str}")

    if dry_run:
        print("  [DRY RUN] skipped")
        return True

    result = subprocess.run(cmd)
    if result.returncode != 0:
        print(f"  WARNING: command exited with code {result.returncode}", file=sys.stderr)
        return False
    return True


def main():
    parser = argparse.ArgumentParser(
        description="Ingest single Substack post(s): fetch, load, download media, upload to Weaviate")
    parser.add_argument("urls", nargs="+", help="Substack post URL(s)")
    parser.add_argument("--database",
                        help="PostgreSQL database (overrides substacks.json)")
    parser.add_argument("--cookies", help="Path to cookies JSON for paid content")
    parser.add_argument("--skip-weaviate", action="store_true",
                        help="Skip Weaviate upload")
    parser.add_argument("--skip-media", action="store_true",
                        help="Skip media download and analysis")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show commands without executing")
    args = parser.parse_args()

    # Group URLs by subdomain
    by_blog: dict[str, list[tuple[str, str]]] = {}  # subdomain -> [(url, slug), ...]
    for url in args.urls:
        subdomain, slug = parse_substack_url(url)
        by_blog.setdefault(subdomain, []).append((url, slug))

    for subdomain, posts in by_blog.items():
        # Resolve database
        if args.database:
            database = args.database
        else:
            database = get_database(subdomain, default="substack")

        print(f"\n{'#'*60}")
        print(f"  Blog: {subdomain} ({len(posts)} post(s)) → database: {database}")
        print(f"{'#'*60}")

        posts_dir = Path(f"posts/{subdomain}")
        if not args.dry_run:
            posts_dir.mkdir(parents=True, exist_ok=True)

        # Step 1: Fetch each post
        fetched_files = []
        for url, slug in posts:
            output = posts_dir / f"{slug}.json"
            cmd = ["python", "main.py", "--url", url, "--output", str(output), "--verbose"]
            if args.cookies:
                cmd.extend(["--cookies", args.cookies])
            run(cmd, dry_run=args.dry_run, label=f"Step 1: Fetch {slug}")
            fetched_files.append(output)

        # Step 2: Load into PostgreSQL
        if len(fetched_files) == 1:
            cmd = ["python", "load_posts.py", "--file", str(fetched_files[0]),
                   "--database", database, "--verbose"]
        else:
            cmd = ["python", "load_posts.py", "--dir", str(posts_dir),
                   "--resume", "--database", database, "--verbose"]
        run(cmd, dry_run=args.dry_run, label="Step 2: Load into PostgreSQL")

        # Step 3: Download media (podcast_url is saved by loader from JSON)
        if not args.skip_media:
            run(["python", "download_media.py", "--extract", "--download",
                 "--publication", subdomain, "--database", database],
                dry_run=args.dry_run, label="Step 3: Download media")

        # Step 4: Upload to Weaviate
        if not args.skip_weaviate:
            run(["python", "weaviate/upload_posts.py",
                 "--publication", subdomain, "--database", database],
                dry_run=args.dry_run, label="Step 4: Upload to Weaviate")

    print(f"\n{'='*60}")
    print(f"  Done: {sum(len(p) for p in by_blog.values())} post(s) from {len(by_blog)} blog(s)")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
