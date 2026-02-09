"""
Substack Fetcher - Fetch articles and comments from Substack newsletters.

Supports authenticated access for paid subscriber content.
"""

import json
import re
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import requests
from substack_api import Newsletter, Post, SubstackAuth


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
        limit: int = 10,
        sorting: str = "new",
    ) -> list[dict]:
        """
        Get posts from a newsletter.

        Args:
            publication_url: The Substack publication URL.
            limit: Maximum number of posts to fetch.
            sorting: Sort order - "new" or "top".

        Returns:
            List of post dictionaries (metadata).
        """
        newsletter = self.get_newsletter(publication_url)
        posts = newsletter.get_posts(limit=limit, sorting=sorting)

        # Convert Post objects to dictionaries using their metadata
        result = []
        for post in posts:
            if hasattr(post, 'get_metadata'):
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
        limit: int = 10,
        include_comments: bool = True,
    ) -> list[str]:
        """
        Fetch multiple posts from a newsletter and save them.

        Args:
            publication_url: The Substack publication URL.
            output_dir: Directory to save posts.
            limit: Maximum number of posts to fetch.
            include_comments: Whether to include comments.

        Returns:
            List of saved file paths.
        """
        posts = self.get_posts(publication_url, limit=limit)
        saved_files = []

        output_path = Path(output_dir)
        output_path.mkdir(parents=True, exist_ok=True)

        for post in posts:
            slug = post.get("slug", post.get("id", "unknown"))
            post_url = f"{publication_url}/p/{slug}"
            file_path = output_path / f"{slug}.json"

            try:
                self.save_post(
                    post_url,
                    str(file_path),
                    include_comments=include_comments,
                )
                saved_files.append(str(file_path))
                print(f"Saved: {file_path}")
            except Exception as e:
                print(f"Error saving {post_url}: {e}")

        return saved_files
