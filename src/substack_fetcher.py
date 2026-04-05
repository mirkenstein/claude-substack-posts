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

    def _extract_post_id_from_inbox_url(self, url: str) -> int | None:
        """Extract post ID from inbox URL format."""
        # URL format: https://substack.com/inbox/post/78555703
        match = re.search(r"/inbox/post/(\d+)", url)
        if match:
            return int(match.group(1))
        return None

    def get_post_by_id(self, post_id: int, verbose: bool = False) -> dict:
        """
        Get post data by numeric ID.

        Args:
            post_id: The numeric post ID.
            verbose: Enable debug logging.

        Returns:
            Dictionary with post metadata.
        """
        _log(f"Fetching post by ID: {post_id}", verbose)

        # Try the reader API first (works with authentication)
        endpoint = f"https://substack.com/api/v1/posts/{post_id}"

        response = self.session.get(endpoint)
        response.raise_for_status()

        return response.json()

    def get_post_content_by_id(self, post_id: int, verbose: bool = False) -> dict:
        """
        Get full post content by numeric ID.

        Args:
            post_id: The numeric post ID.
            verbose: Enable debug logging.

        Returns:
            Dictionary with post metadata and content.
        """
        post_data = self.get_post_by_id(post_id, verbose=verbose)

        # The API returns full post data including body_html
        return {
            "metadata": {
                "id": post_data.get("id"),
                "title": post_data.get("title"),
                "subtitle": post_data.get("subtitle"),
                "slug": post_data.get("slug"),
                "post_date": post_data.get("post_date"),
                "audience": post_data.get("audience"),
                "canonical_url": post_data.get("canonical_url"),
                "publication_id": post_data.get("publication_id"),
            },
            "content": post_data.get("body_html", ""),
            "is_paywalled": post_data.get("audience") == "only_paid",
        }

    def get_comments_by_post_id(
        self,
        post_id: int,
        publication_url: str | None = None,
        limit: int = 500,
        verbose: bool = False,
    ) -> list[dict]:
        """
        Get comments for a post by numeric ID.

        Args:
            post_id: The numeric post ID.
            publication_url: The publication base URL (optional, will be fetched if not provided).
            limit: Maximum number of top-level comments to fetch.
            verbose: Enable debug logging.

        Returns:
            List of comment dictionaries with all nested children.
        """
        # If no publication URL, get it from the post data
        if not publication_url:
            post_data = self.get_post_by_id(post_id, verbose=verbose)
            canonical_url = post_data.get("canonical_url", "")
            if canonical_url:
                publication_url = self._extract_publication_url(canonical_url)
            else:
                raise ValueError(f"Could not determine publication URL for post {post_id}")

        return self._fetch_all_comments(publication_url, post_id, limit=limit, verbose=verbose)

    def get_post_with_comments_by_id(self, post_id: int, verbose: bool = False) -> dict:
        """
        Get a post with all its comments by numeric ID.

        Args:
            post_id: The numeric post ID.
            verbose: Enable debug logging.

        Returns:
            Dictionary with post content and comments.
        """
        post_data = self.get_post_content_by_id(post_id, verbose=verbose)
        canonical_url = post_data["metadata"].get("canonical_url", "")
        publication_url = self._extract_publication_url(canonical_url) if canonical_url else None

        comments = self.get_comments_by_post_id(post_id, publication_url=publication_url, verbose=verbose)
        total = self._count_all_comments(comments)

        return {
            **post_data,
            "comments": comments,
            "comment_count": total,
        }

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

        Calls the archive API directly to avoid the substack_api library's
        round-trip through Post objects, which fails for newsletters using
        the newer canonical_url format (https://substack.com/home/post/p-{id}).

        Args:
            publication_url: The Substack publication URL.
            limit: Maximum number of posts to fetch. None for all posts.
            sorting: Sort order - "new" or "top".
            verbose: Enable debug logging.

        Returns:
            List of post dictionaries (metadata).
        """
        _log(f"Fetching posts from archive API: {publication_url}", verbose)

        result = []
        offset = 0
        batch_size = 15

        while True:
            endpoint = f"{publication_url.rstrip('/')}/api/v1/archive?sort={sorting}&offset={offset}&limit={batch_size}"
            _log(f"Fetching archive batch: offset={offset}", verbose)

            response = self.session.get(
                endpoint,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
                timeout=30,
            )
            response.raise_for_status()

            items = response.json()
            if not items:
                break

            result.extend(items)

            if limit and len(result) >= limit:
                result = result[:limit]
                break

            if len(items) < batch_size:
                break

            offset += batch_size
            time.sleep(2)

        _log(f"Fetched {len(result)} posts from archive API", verbose)
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
        verbose: bool = False,
    ) -> list[dict]:
        """
        Get comments for a post.

        Uses direct API calls since the substack-api library
        may not expose comments directly.

        Args:
            post_url: The full URL to the Substack post.
            limit: Maximum number of top-level comments to fetch.
            verbose: Enable debug logging.

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

        return self._fetch_all_comments(publication_url, post_id, limit=limit, verbose=verbose)

    def _fetch_all_comments(
        self,
        publication_url: str,
        post_id: int,
        limit: int = 500,
        verbose: bool = False,
    ) -> list[dict]:
        """
        Fetch all comments for a post with pagination and nested replies.

        Args:
            publication_url: The publication base URL.
            post_id: The numeric post ID.
            limit: Maximum number of top-level comments to fetch.
            verbose: Enable debug logging.

        Returns:
            List of comment dictionaries with all nested children.
        """
        comments_url = f"{publication_url}/api/v1/post/{post_id}/comments"
        all_comments = []
        token = ""

        while True:
            params = {
                "token": token,
                "all_comments": "true",
                "sort": "best_first",
            }

            _log(f"Fetching comments batch (token={token[:20] + '...' if token else 'initial'})", verbose)
            response = self.session.get(comments_url, params=params)
            response.raise_for_status()

            data = response.json()
            comments = data.get("comments", [])

            if not comments:
                break

            # For each comment, recursively fetch all children
            for comment in comments:
                self._fetch_nested_children(publication_url, post_id, comment, verbose=verbose)
                all_comments.append(comment)

            _log(f"Fetched {len(comments)} top-level comments, total: {len(all_comments)}", verbose)

            # Check pagination token
            next_token = data.get("token")
            if not next_token or next_token == token:
                break

            token = next_token

            if limit and len(all_comments) >= limit:
                all_comments = all_comments[:limit]
                break

            time.sleep(0.5)  # Be polite between pages

        _log(f"Total comments fetched: {len(all_comments)} (including nested: {self._count_all_comments(all_comments)})", verbose)
        return all_comments

    def _fetch_nested_children(
        self,
        publication_url: str,
        post_id: int,
        comment: dict,
        verbose: bool = False,
    ) -> None:
        """
        Recursively fetch all nested child comments.

        If a comment has more children than what was returned,
        fetch the remaining children via the API.

        Args:
            publication_url: The publication base URL.
            post_id: The numeric post ID.
            comment: The parent comment dict (modified in place).
            verbose: Enable debug logging.
        """
        children = comment.get("children", [])
        child_count = comment.get("childCount", len(children))

        # If there are more children than returned, fetch them
        if child_count > len(children) and comment.get("id"):
            _log(f"Comment {comment['id']} has {child_count} children but only {len(children)} loaded, fetching rest...", verbose)

            comment_id = comment["id"]
            child_url = f"{publication_url}/api/v1/post/{post_id}/comment/{comment_id}/comments"
            token = ""

            fetched_children = []
            while True:
                params = {
                    "token": token,
                    "all_comments": "true",
                    "sort": "best_first",
                }

                response = self.session.get(child_url, params=params)
                if not response.ok:
                    _log(f"Failed to fetch children for comment {comment_id}: {response.status_code}", verbose)
                    break

                data = response.json()
                batch = data.get("comments", [])

                if not batch:
                    break

                fetched_children.extend(batch)

                next_token = data.get("token")
                if not next_token or next_token == token:
                    break

                token = next_token
                time.sleep(0.3)

            if fetched_children:
                comment["children"] = fetched_children
                _log(f"Loaded {len(fetched_children)} children for comment {comment_id}", verbose)

        # Recursively fetch children of children
        for child in comment.get("children", []):
            self._fetch_nested_children(publication_url, post_id, child, verbose=verbose)

    @staticmethod
    def _count_all_comments(comments: list[dict]) -> int:
        """Count total comments including all nested children."""
        count = 0
        for comment in comments:
            count += 1
            count += SubstackFetcher._count_all_comments(comment.get("children", []))
        return count

    def get_post_with_comments(self, post_url: str, verbose: bool = False) -> dict:
        """
        Get a post with all its comments.

        Args:
            post_url: The full URL to the Substack post.
            verbose: Enable debug logging.

        Returns:
            Dictionary with post content and comments.
        """
        post_data = self.get_post_content(post_url)
        comments = self.get_comments(post_url, verbose=verbose)
        total = self._count_all_comments(comments)

        return {
            **post_data,
            "comments": comments,
            "comment_count": total,
        }

    def save_post(
        self,
        post_url: str,
        output_path: str,
        include_comments: bool = True,
        verbose: bool = False,
    ) -> None:
        """
        Save a post (and optionally comments) to a JSON file.

        Args:
            post_url: The full URL to the Substack post.
            output_path: Path to save the JSON file.
            include_comments: Whether to include comments.
            verbose: Enable debug logging.
        """
        if include_comments:
            data = self.get_post_with_comments(post_url, verbose=verbose)
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
                    verbose=verbose,
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
        batch_size = 20  # API max limit is 20

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
