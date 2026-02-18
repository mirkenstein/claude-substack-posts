"""
Substack Fetcher - Fetch articles and comments from Substack newsletters.

Supports authenticated access for paid subscriber content.
"""

import json
import random
import re
import sys
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import requests
from substack_api import Newsletter, Post, SubstackAuth


def _log(msg: str, verbose: bool = False) -> None:
    """Print debug message if verbose mode is enabled."""
    if verbose:
        print(f"[DEBUG] {msg}", file=sys.stderr)


class SubstackFetcher:
    """Fetches Substack articles and comments with optional authentication."""

    def __init__(self, cookies_path: Optional[str] = None):
        """
        Initialize the fetcher.

        Args:
            cookies_path: Path to JSON file containing Substack session cookies.
                         Required for accessing paywalled content.
        """
        self.auth = None
        self.session = requests.Session()
        self.cookies_path = cookies_path

        if cookies_path:
            self._setup_auth(cookies_path)

    def _setup_auth(self, cookies_path: str) -> None:
        """Set up authentication from cookies file."""
        self.auth = SubstackAuth(cookies_path=cookies_path)

        # Also set up requests session with cookies for direct API calls
        with open(cookies_path) as f:
            cookies = json.load(f)
            for cookie in cookies:
                self.session.cookies.set(
                    cookie["name"],
                    cookie["value"],
                    domain=cookie.get("domain", ".substack.com"),
                    path=cookie.get("path", "/"),
                )

    def _extract_publication_url(self, url: str) -> str:
        """Extract the base publication URL from any Substack URL."""
        parsed = urlparse(url)
        return f"https://{parsed.netloc}"

    def _extract_post_slug(self, url: str) -> str:
        """Extract the post slug from a Substack post URL."""
        # URL format: https://publication.substack.com/p/post-slug
        match = re.search(r"/p/([^/?#]+)", url)
        if match:
            return match.group(1)
        raise ValueError(f"Could not extract post slug from URL: {url}")

    def get_newsletter(self, publication_url: str) -> Newsletter:
        """
        Get a Newsletter object for a publication.

        Args:
            publication_url: The Substack publication URL.

        Returns:
            Newsletter object.
        """
        if self.auth:
            return Newsletter(publication_url, auth=self.auth)
        return Newsletter(publication_url)

    def get_posts(
        self,
        publication_url: str,
        limit: int | None = 10,
        sorting: str = "new",
        verbose: bool = False,
    ) -> list[dict]:
        """
        Get posts from a newsletter.

        Args:
            publication_url: The Substack publication URL.
            limit: Maximum number of posts to fetch. None for all posts.
            sorting: Sort order - "new" or "top".
            verbose: Enable debug logging.

        Returns:
            List of post dictionaries (metadata).
        """
        _log(f"Creating Newsletter object for: {publication_url}", verbose)
        newsletter = self.get_newsletter(publication_url)
        _log(f"Newsletter object created", verbose)

        # Fetch posts - use a large number if limit is None (fetch all)
        fetch_limit = limit if limit is not None else 10000
        _log(f"Fetching posts with limit={fetch_limit}, sorting={sorting}", verbose)
        posts = newsletter.get_posts(limit=fetch_limit, sorting=sorting)
        _log(f"Received {len(posts) if posts else 0} posts from API", verbose)

        # Convert Post objects to dictionaries using their metadata
        result = []
        for i, post in enumerate(posts):
            try:
                if hasattr(post, 'get_metadata'):
                    _log(f"Converting post {i+1}/{len(posts)} to metadata", verbose)
                    result.append(post.get_metadata())
                elif isinstance(post, dict):
                    result.append(post)
                else:
                    # Fallback: try to access common attributes
                    result.append({
                        'slug': getattr(post, 'slug', None),
                        'title': getattr(post, 'title', None),
                        'id': getattr(post, 'id', None),
                    })
            except Exception as e:
                # Skip posts that fail to fetch (e.g., 404 deleted posts)
                _log(f"Skipping post {i+1}/{len(posts)}: {e}", verbose)
                print(f"Warning: Skipping post {i+1}: {e}", file=sys.stderr)
                continue
        _log(f"Converted {len(result)} posts to dictionaries", verbose)
        return result

    def get_post(self, post_url: str) -> Post:
        """
        Get a Post object for a specific post.

        Args:
            post_url: The full URL to the Substack post.

        Returns:
            Post object.
        """
        if self.auth:
            return Post(post_url, auth=self.auth)
        return Post(post_url)

    def get_post_content(self, post_url: str) -> dict:
        """
        Get the full content of a post.

        Args:
            post_url: The full URL to the Substack post.

        Returns:
            Dictionary with post metadata and content.
        """
        post = self.get_post(post_url)
        return {
            "metadata": post.get_metadata(),
            "content": post.get_content(),
            "is_paywalled": post.is_paywalled(),
        }

    def get_comments(
        self,
        post_url: str,
        limit: int = 100,
    ) -> list[dict]:
        """
        Get comments for a post.

        Uses direct API calls since the substack-api library
        may not expose comments directly.

        Args:
            post_url: The full URL to the Substack post.
            limit: Maximum number of comments to fetch.

        Returns:
            List of comment dictionaries.
        """
        publication_url = self._extract_publication_url(post_url)
        post_slug = self._extract_post_slug(post_url)

        # First, get the post ID from the post metadata
        post = self.get_post(post_url)
        metadata = post.get_metadata()
        post_id = metadata.get("id")

        if not post_id:
            raise ValueError(f"Could not get post ID for: {post_url}")

        # Fetch comments using the Substack API
        comments_url = f"{publication_url}/api/v1/post/{post_id}/comments"
        params = {
            "token": "",
            "all_comments": "true",
            "sort": "best_first",
        }

        response = self.session.get(comments_url, params=params)
        response.raise_for_status()

        data = response.json()
        comments = data.get("comments", [])

        return comments[:limit]

    def get_post_with_comments(self, post_url: str) -> dict:
        """
        Get a post with all its comments.

        Args:
            post_url: The full URL to the Substack post.

        Returns:
            Dictionary with post content and comments.
        """
        post_data = self.get_post_content(post_url)
        comments = self.get_comments(post_url)

        return {
            **post_data,
            "comments": comments,
            "comment_count": len(comments),
        }

    def save_post(
        self,
        post_url: str,
        output_path: str,
        include_comments: bool = True,
    ) -> None:
        """
        Save a post (and optionally comments) to a JSON file.

        Args:
            post_url: The full URL to the Substack post.
            output_path: Path to save the JSON file.
            include_comments: Whether to include comments.
        """
        if include_comments:
            data = self.get_post_with_comments(post_url)
        else:
            data = self.get_post_content(post_url)

        output_file = Path(output_path)
        output_file.parent.mkdir(parents=True, exist_ok=True)

        with open(output_file, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

    def fetch_all_posts(
        self,
        publication_url: str,
        output_dir: str,
        limit: int | None = 10,
        include_comments: bool = True,
        delay: float = 2.0,
        jitter: float = 1.0,
        verbose: bool = False,
    ) -> list[str]:
        """
        Fetch multiple posts from a newsletter and save them.

        Args:
            publication_url: The Substack publication URL.
            output_dir: Directory to save posts.
            limit: Maximum number of posts to fetch. None for all posts.
            include_comments: Whether to include comments.
            delay: Delay in seconds between requests to avoid rate limiting.
            jitter: Random jitter (0 to jitter) added to delay.
            verbose: Enable debug logging.

        Returns:
            List of saved file paths.
        """
        _log(f"Fetching post list from: {publication_url}", verbose)
        posts = self.get_posts(publication_url, limit=limit, verbose=verbose)
        _log(f"Found {len(posts)} posts to fetch", verbose)
        saved_files = []

        output_path = Path(output_dir)
        output_path.mkdir(parents=True, exist_ok=True)

        for i, post in enumerate(posts):
            slug = post.get("slug", post.get("id", "unknown"))
            post_url = f"{publication_url}/p/{slug}"
            file_path = output_path / f"{slug}.json"

            try:
                _log(f"[{i+1}/{len(posts)}] Fetching: {slug}", verbose)
                self.save_post(
                    post_url,
                    str(file_path),
                    include_comments=include_comments,
                )
                saved_files.append(str(file_path))
                print(f"Saved: {file_path}", file=sys.stderr)

                # Delay between requests (skip after last post)
                if (delay > 0 or jitter > 0) and i < len(posts) - 1:
                    wait = delay + random.uniform(0, jitter)
                    _log(f"Waiting {wait:.1f}s before next request...", verbose)
                    time.sleep(wait)

            except Exception as e:
                print(f"Error saving {post_url}: {e}", file=sys.stderr)

        return saved_files

    def get_saved_posts(
        self,
        limit: int | None = None,
        verbose: bool = False,
    ) -> list[dict]:
        """
        Get posts from user's saved/bookmarked list.

        Requires authentication via cookies.

        Args:
            limit: Maximum number of posts to fetch. None for all.
            verbose: Enable debug logging.

        Returns:
            List of post dictionaries with metadata.
        """
        if not self.cookies_path:
            raise ValueError("--saved requires authentication. Use --cookies to provide cookies.")

        _log("Fetching saved posts from reader API", verbose)

        all_posts = []
        offset = 0
        batch_size = 50  # Max allowed by API

        while True:
            endpoint = "https://substack.com/api/v1/reader/posts"
            params = {
                "inboxType": "saved",
                "limit": batch_size,
                "offset": offset,
            }

            _log(f"Fetching batch: offset={offset}, limit={batch_size}", verbose)
            response = self.session.get(endpoint, params=params)
            response.raise_for_status()

            data = response.json()
            posts = data.get("posts", [])

            if not posts:
                _log("No more posts returned", verbose)
                break

            # Transform to our standard format
            for post in posts:
                all_posts.append({
                    "id": post.get("id"),
                    "title": post.get("title", "Untitled"),
                    "slug": post.get("slug", ""),
                    "url": post.get("canonical_url", ""),
                    "post_date": post.get("post_date"),
                    "audience": post.get("audience"),
                    "subtitle": post.get("subtitle"),
                    "publication_id": post.get("publication_id"),
                    "publication_name": post.get("publishedBylines", [{}])[0].get("publicationUsers", [{}])[0].get("publication", {}).get("name"),
                })

            _log(f"Fetched {len(posts)} posts, total: {len(all_posts)}", verbose)

            # Check if we've reached the limit
            if limit and len(all_posts) >= limit:
                all_posts = all_posts[:limit]
                break

            # Check if we got fewer than requested (end of list)
            if len(posts) < batch_size:
                break

            offset += batch_size
            time.sleep(1)  # Be polite

        _log(f"Total saved posts fetched: {len(all_posts)}", verbose)
        return all_posts
