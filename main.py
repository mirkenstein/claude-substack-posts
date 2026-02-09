#!/usr/bin/env python3
"""
Substack Article Fetcher - CLI tool to fetch articles and comments.

Usage:
    python main.py --url <post_url> [--cookies <path>] [--output <path>]
    python main.py --newsletter <url> [--cookies <path>] [--output-dir <dir>] [--limit <n>]
"""

import argparse
import json
import sys
import time
from pathlib import Path

from src.substack_fetcher import SubstackFetcher


def fetch_single_post(
    fetcher: SubstackFetcher,
    url: str,
    output: str | None,
    include_comments: bool,
) -> None:
    """Fetch a single post."""
    print(f"Fetching post: {url}")

    if include_comments:
        data = fetcher.get_post_with_comments(url)
    else:
        data = fetcher.get_post_content(url)

    if output:
        fetcher.save_post(url, output, include_comments=include_comments)
        print(f"Saved to: {output}")
    else:
        print(json.dumps(data, indent=2, ensure_ascii=False))


def fetch_newsletter(
    fetcher: SubstackFetcher,
    url: str,
    output_dir: str | None,
    limit: int | None,
    include_comments: bool,
    delay: float = 2.0,
) -> None:
    """Fetch multiple posts from a newsletter."""
    if output_dir:
        if limit:
            print(f"Fetching up to {limit} posts from: {url}", file=sys.stderr)
        else:
            print(f"Fetching all posts from: {url}", file=sys.stderr)

        saved_files = fetcher.fetch_all_posts(
            url,
            output_dir,
            limit=limit,
            include_comments=include_comments,
            delay=delay,
        )
        print(f"Fetched {len(saved_files)} posts to {output_dir}", file=sys.stderr)
    else:
        # No output dir - fetch posts and print as JSON array to stdout
        posts = fetcher.get_posts(url, limit=limit)
        all_posts_data = []
        for i, post in enumerate(posts):
            slug = post.get("slug")
            if slug:
                post_url = f"{url.rstrip('/')}/p/{slug}"
                if include_comments:
                    post_data = fetcher.get_post_with_comments(post_url)
                else:
                    post_data = fetcher.get_post_content(post_url)
                all_posts_data.append(post_data)

                # Delay between requests (skip after last post)
                if delay > 0 and i < len(posts) - 1:
                    time.sleep(delay)

        print(json.dumps(all_posts_data, indent=2, ensure_ascii=False))


def list_posts(fetcher: SubstackFetcher, url: str, limit: int | None, output_dir: str | None) -> None:
    """List posts from a newsletter as JSON."""
    posts = fetcher.get_posts(url, limit=limit)

    # Build clean list of post metadata
    posts_data = []
    for post in posts:
        posts_data.append({
            "title": post.get("title", "Untitled"),
            "slug": post.get("slug", ""),
            "url": f"{url}/p/{post.get('slug', '')}",
            "date": post.get("post_date", "")[:10] if post.get("post_date") else None,
            "audience": post.get("audience"),
            "subtitle": post.get("subtitle"),
        })

    output = {
        "newsletter": url,
        "count": len(posts_data),
        "posts": posts_data,
    }

    json_str = json.dumps(output, indent=2, ensure_ascii=False)

    if output_dir:
        Path(output_dir).mkdir(parents=True, exist_ok=True)
        # Extract newsletter name from URL
        newsletter_name = url.rstrip("/").split("/")[-1].replace(".substack.com", "")
        output_file = Path(output_dir) / f"{newsletter_name}_posts.json"
        output_file.write_text(json_str, encoding="utf-8")
        print(f"Saved {len(posts_data)} posts to: {output_file}")
    else:
        print(json_str)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Fetch Substack articles and comments",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Fetch a single post with comments
  python main.py --url https://newsletter.substack.com/p/post-slug

  # Fetch with authentication (for paid content)
  python main.py --url https://newsletter.substack.com/p/post-slug --cookies cookies.json

  # Fetch multiple posts from a newsletter
  python main.py --newsletter https://newsletter.substack.com --limit 20 --output-dir ./posts

  # List posts without fetching
  python main.py --newsletter https://newsletter.substack.com --list --limit 50

  # Fetch without comments
  python main.py --url https://newsletter.substack.com/p/post-slug --no-comments
        """,
    )

    # Input options
    parser.add_argument(
        "--url",
        help="URL of a specific Substack post to fetch",
    )
    parser.add_argument(
        "--newsletter",
        help="URL of a Substack newsletter to fetch multiple posts",
    )

    # Authentication
    parser.add_argument(
        "--cookies",
        help="Path to cookies JSON file for authenticated access",
    )

    # Output options
    parser.add_argument(
        "--output", "-o",
        help="Output file path for single post (default: stdout)",
    )
    parser.add_argument(
        "--output-dir",
        help="Output directory for saving JSON files (prints to stdout if not specified)",
    )

    # Fetch options
    parser.add_argument(
        "--limit",
        type=int,
        default=10,
        help="Maximum number of posts to fetch (default: 10)",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Fetch all posts (overrides --limit)",
    )
    parser.add_argument(
        "--no-comments",
        action="store_true",
        help="Don't fetch comments",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List posts without fetching content",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=2.0,
        help="Delay in seconds between requests to avoid rate limiting (default: 2.0)",
    )

    args = parser.parse_args()

    # Validate arguments
    if not args.url and not args.newsletter:
        parser.error("Either --url or --newsletter is required")

    if args.url and args.newsletter:
        parser.error("Cannot use both --url and --newsletter")

    # Initialize fetcher
    fetcher = SubstackFetcher(cookies_path=args.cookies)
    include_comments = not args.no_comments

    # Use None for limit if --all is specified
    limit = None if args.all else args.limit

    try:
        if args.url:
            fetch_single_post(fetcher, args.url, args.output, include_comments)
        elif args.list:
            list_posts(fetcher, args.newsletter, limit, args.output_dir)
        else:
            fetch_newsletter(
                fetcher,
                args.newsletter,
                args.output_dir,
                limit,
                include_comments,
                delay=args.delay,
            )
        return 0

    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
