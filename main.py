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
    output_dir: str,
    limit: int | None,
    include_comments: bool,
) -> None:
    """Fetch multiple posts from a newsletter."""
    if limit:
        print(f"Fetching up to {limit} posts from: {url}")
    else:
        print(f"Fetching all posts from: {url}")

    saved_files = fetcher.fetch_all_posts(
        url,
        output_dir,
        limit=limit,
        include_comments=include_comments,
    )

    print(f"\nFetched {len(saved_files)} posts to {output_dir}")


def list_posts(fetcher: SubstackFetcher, url: str, limit: int | None) -> None:
    """List posts from a newsletter without fetching content."""
    print(f"Listing posts from: {url}\n")

    posts = fetcher.get_posts(url, limit=limit)

    for i, post in enumerate(posts, 1):
        title = post.get("title", "Untitled")
        slug = post.get("slug", "")
        date = post.get("post_date", "")[:10] if post.get("post_date") else ""
        paywalled = " [PAID]" if post.get("audience") == "only_paid" else ""

        print(f"{i:3}. {title}{paywalled}")
        print(f"     {url}/p/{slug}")
        if date:
            print(f"     Date: {date}")
        print()


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
        default="./output",
        help="Output directory for multiple posts (default: ./output)",
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
            list_posts(fetcher, args.newsletter, limit)
        else:
            fetch_newsletter(
                fetcher,
                args.newsletter,
                args.output_dir,
                limit,
                include_comments,
            )
        return 0

    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
