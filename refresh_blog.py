#!/usr/bin/env python3
"""
Incremental refresh for an already-ingested Substack blog.

Fetches the latest posts, diffs against the existing list, and only
fetches/loads/uploads the new ones.

Usage:
    python refresh_blog.py slavlandchronicles
    python refresh_blog.py slavlandchronicles --check 20
    python refresh_blog.py slavlandchronicles --delay 30 --jitter 4
    python refresh_blog.py slavlandchronicles --skip-weaviate
    python refresh_blog.py slavlandchronicles --dry-run
"""

import argparse
import json
import random
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

from src.substack_fetcher import SubstackFetcher
from src.db.connection import DatabaseConnection
from src.db.loader import PostLoader


def load_existing_list(list_path: Path) -> dict:
    """Load the existing posts list JSON."""
    if not list_path.exists():
        return {"newsletter": "", "count": 0, "posts": []}
    return json.loads(list_path.read_text(encoding="utf-8"))


def archive_list(list_path: Path) -> Path | None:
    """Archive the existing list with a timestamp suffix."""
    if not list_path.exists():
        return None
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    archive_path = list_path.with_suffix(f".{ts}.json")
    shutil.copy2(list_path, archive_path)
    return archive_path


def fetch_latest(subdomain: str, check: int, cookies: str | None, verbose: bool) -> list[dict]:
    """Fetch the latest N posts from the Substack API."""
    fetcher = SubstackFetcher(cookies_path=cookies)
    url = f"https://{subdomain}.substack.com"
    posts = fetcher.get_posts(url, limit=check, verbose=verbose)

    result = []
    for post in posts:
        result.append({
            "title": post.get("title", "Untitled"),
            "slug": post.get("slug", ""),
            "url": f"{url}/p/{post.get('slug', '')}",
            "date": post.get("post_date", "")[:10] if post.get("post_date") else None,
            "audience": post.get("audience"),
            "subtitle": post.get("subtitle"),
        })
    return result


def diff_posts(latest: list[dict], existing: list[dict]) -> list[dict]:
    """Find posts in latest that aren't in the existing list."""
    existing_slugs = {p["slug"] for p in existing}
    return [p for p in latest if p["slug"] not in existing_slugs]


def fetch_new_posts(
    fetcher: SubstackFetcher,
    new_posts: list[dict],
    output_dir: Path,
    delay: float,
    jitter: float,
    verbose: bool,
) -> list[Path]:
    """Fetch full content + comments for new posts."""
    saved = []
    for i, post in enumerate(new_posts):
        slug = post["slug"]
        file_path = output_dir / f"{slug}.json"

        if file_path.exists():
            print(f"  [{i+1}/{len(new_posts)}] Already on disk: {slug}")
            saved.append(file_path)
            continue

        try:
            post_url = post["url"]
            print(f"  [{i+1}/{len(new_posts)}] Fetching: {slug}")
            fetcher.save_post(post_url, str(file_path), include_comments=True)
            saved.append(file_path)

            if i < len(new_posts) - 1:
                wait = delay + random.uniform(0, jitter)
                if verbose:
                    print(f"    Waiting {wait:.1f}s...")
                time.sleep(wait)
        except Exception as e:
            print(f"  [{i+1}/{len(new_posts)}] ERROR: {slug}: {e}", file=sys.stderr)

    return saved


def load_into_postgres(files: list[Path], verbose: bool) -> int:
    """Load new post JSON files into PostgreSQL."""
    loaded = 0
    with DatabaseConnection() as conn:
        loader = PostLoader(conn)
        for fp in files:
            try:
                data = json.loads(fp.read_text(encoding="utf-8"))
                metadata = data.get("metadata", {})
                content_html = data.get("content", "")
                is_paywalled = data.get("is_paywalled", False)
                comments = data.get("comments", [])

                pub_id = loader.upsert_publication(metadata)
                author_id = loader.upsert_author(metadata)
                post_id = loader.upsert_post(metadata, content_html, is_paywalled, pub_id, author_id)
                loader.insert_tags(post_id, metadata)
                loader.insert_post_links(post_id, content_html)
                comment_count = loader.insert_comments(post_id, comments)
                loader.record_status(str(fp), post_id, "success")
                conn.commit()
                loaded += 1
                if verbose:
                    print(f"    Loaded: {fp.name} (post_id={post_id}, comments={comment_count})")
            except Exception as e:
                conn.rollback()
                print(f"    ERROR loading {fp.name}: {e}", file=sys.stderr)

    return loaded


def upload_to_weaviate(subdomain: str) -> None:
    """Upload new posts to Weaviate."""
    sys.path.insert(0, str(Path(__file__).parent / "weaviate"))
    from upload_posts import main as weaviate_main

    sys.argv = ["upload_posts.py", "--publication", subdomain]
    weaviate_main()


def main():
    parser = argparse.ArgumentParser(description="Incremental refresh for a Substack blog")
    parser.add_argument("subdomain", help="Substack subdomain (e.g., slavlandchronicles)")
    parser.add_argument("--check", type=int, default=10,
                        help="Number of latest posts to check (default: 10)")
    parser.add_argument("--delay", type=float, default=30.0,
                        help="Delay between fetches in seconds (default: 30)")
    parser.add_argument("--jitter", type=float, default=4.0,
                        help="Random jitter added to delay (default: 4)")
    parser.add_argument("--cookies", help="Path to cookies JSON for authenticated access")
    parser.add_argument("--skip-weaviate", action="store_true", help="Skip Weaviate upload")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be done")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    subdomain = args.subdomain
    posts_dir = Path(f"posts/{subdomain}")
    list_path = posts_dir / f"{subdomain}_posts.json"

    posts_dir.mkdir(parents=True, exist_ok=True)

    # Step 1: Load existing list
    existing = load_existing_list(list_path)
    existing_posts = existing.get("posts", [])
    print(f"Existing list: {len(existing_posts)} posts")

    # Step 2: Fetch latest N from API
    print(f"Checking latest {args.check} posts from {subdomain}.substack.com...")
    latest = fetch_latest(subdomain, args.check, args.cookies, args.verbose)
    print(f"API returned: {len(latest)} posts")

    # Step 3: Diff
    new_posts = diff_posts(latest, existing_posts)

    if not new_posts:
        print("No new posts found. Everything is up to date.")
        return

    print(f"\nNew posts found: {len(new_posts)}")
    for p in new_posts:
        print(f"  {p['date']}  {p['title']}")

    if args.dry_run:
        print("\n[DRY RUN] No changes made.")
        return

    # Step 4: Archive old list
    archive = archive_list(list_path)
    if archive:
        print(f"\nArchived old list: {archive.name}")

    # Step 5: Merge new posts into list and save
    merged = new_posts + existing_posts
    new_list = {
        "newsletter": f"https://{subdomain}.substack.com",
        "count": len(merged),
        "posts": merged,
    }
    list_path.write_text(json.dumps(new_list, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Updated list: {len(merged)} posts")

    # Step 6: Fetch full content + comments
    print(f"\nFetching {len(new_posts)} new post(s)...")
    fetcher = SubstackFetcher(cookies_path=args.cookies)
    saved = fetch_new_posts(fetcher, new_posts, posts_dir, args.delay, args.jitter, args.verbose)
    print(f"Fetched: {len(saved)} files")

    # Step 7: Load into PostgreSQL
    print(f"\nLoading into PostgreSQL...")
    loaded = load_into_postgres(saved, args.verbose)
    print(f"Loaded: {loaded} posts")

    # Step 8: Weaviate
    if not args.skip_weaviate:
        print(f"\nUploading to Weaviate...")
        try:
            upload_to_weaviate(subdomain)
        except Exception as e:
            print(f"Weaviate error: {e}", file=sys.stderr)

    print(f"\nDone. {len(new_posts)} new post(s) ingested.")


if __name__ == "__main__":
    main()
