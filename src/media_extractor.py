"""Extract media URLs from posts and download files."""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from urllib.parse import unquote

import requests
from bs4 import BeautifulSoup


BASE_DIR = Path(__file__).parent.parent / "posts" / "saved"


def extract_images_from_html(post_id: int, content_html: str) -> list[dict]:
    """Parse content_html and return image metadata dicts for post_media insertion.

    Only extracts content images (those with data-attrs), skipping avatars,
    tracking pixels, and embedded-post thumbnails.
    """
    if not content_html:
        return []

    soup = BeautifulSoup(content_html, "html.parser")
    images = []
    position = 0

    for img in soup.find_all("img"):
        data_attrs_str = img.get("data-attrs", "")
        if not data_attrs_str:
            continue

        try:
            attrs = json.loads(data_attrs_str)
        except (json.JSONDecodeError, TypeError):
            continue

        src = attrs.get("src", "")
        if not src:
            continue

        # Get the CDN fetch URL from the img src (for downloading)
        cdn_src = img.get("src", "")

        # Extract figcaption
        caption = None
        fig = img.find_parent("figure")
        if fig:
            cap_tag = fig.find("figcaption")
            if cap_tag:
                caption = cap_tag.get_text(strip=True)

        position += 1
        images.append({
            "post_id": post_id,
            "media_type": "image",
            "source_url": src,  # original S3 URL from data-attrs
            "position_in_post": position,
            "alt_text": attrs.get("alt") or img.get("alt") or None,
            "caption": caption,
            "width": attrs.get("width"),
            "height": attrs.get("height"),
            "file_size_bytes": attrs.get("bytes"),
            "image_format": _format_from_type(attrs.get("type", "")),
        })

    return images


def extract_cover_image(post_id: int, cover_image_url: str) -> dict | None:
    """Return a post_media dict for the cover image, or None."""
    if not cover_image_url:
        return None

    # Cover image URLs are CDN fetch URLs; extract the original S3 URL
    source_url = _extract_s3_url(cover_image_url) or cover_image_url

    return {
        "post_id": post_id,
        "media_type": "cover_image",
        "source_url": source_url,
        "position_in_post": 0,
    }


def extract_podcast_audio(post_id: int, podcast_url: str) -> dict | None:
    """Return a post_media dict for podcast audio, or None."""
    if not podcast_url:
        return None
    return {
        "post_id": post_id,
        "media_type": "audio",
        "source_url": podcast_url,
    }


# ── Insertion ────────────────────────────────────────────────────────────────

def insert_media_rows(conn, media_rows: list[dict]) -> int:
    """Insert media rows into post_media. Returns count of new rows inserted."""
    if not media_rows:
        return 0

    inserted = 0
    with conn.cursor() as cur:
        for row in media_rows:
            cur.execute("""
                INSERT INTO substack.post_media
                    (post_id, media_type, source_url, position_in_post,
                     alt_text, caption, width, height, file_size_bytes,
                     image_format, duration_seconds)
                VALUES
                    (%(post_id)s, %(media_type)s, %(source_url)s, %(position_in_post)s,
                     %(alt_text)s, %(caption)s, %(width)s, %(height)s, %(file_size_bytes)s,
                     %(image_format)s, %(duration_seconds)s)
                ON CONFLICT (post_id, source_url) DO NOTHING
            """, {
                "post_id": row["post_id"],
                "media_type": row["media_type"],
                "source_url": row["source_url"],
                "position_in_post": row.get("position_in_post"),
                "alt_text": row.get("alt_text"),
                "caption": row.get("caption"),
                "width": row.get("width"),
                "height": row.get("height"),
                "file_size_bytes": row.get("file_size_bytes"),
                "image_format": row.get("image_format"),
                "duration_seconds": row.get("duration_seconds"),
            })
            if cur.rowcount > 0:
                inserted += 1
    return inserted


# ── Podcast URL fetching ─────────────────────────────────────────────────────

def fetch_podcast_urls(conn, cookies_path: str, delay: float = 1.5) -> int:
    """Fetch podcast_url from the API for all podcast posts missing it.

    Uses publication-specific API: https://{subdomain}.substack.com/api/v1/posts/{slug}
    Updates substack.posts.podcast_url and returns count updated.
    """
    session = _make_session(cookies_path)

    with conn.cursor() as cur:
        cur.execute("""
            SELECT p.id, p.slug, pub.subdomain
            FROM substack.posts p
            JOIN substack.publications pub ON pub.id = p.publication_id
            WHERE p.type = 'podcast' AND p.podcast_url IS NULL
            ORDER BY p.id
        """)
        rows = cur.fetchall()

    if not rows:
        print("All podcast posts already have podcast_url set.")
        return 0

    print(f"Fetching podcast_url for {len(rows)} posts...")
    updated = 0
    consecutive_errors = 0

    for i, (post_id, slug, subdomain) in enumerate(rows):
        try:
            url = f"https://{subdomain}.substack.com/api/v1/posts/{slug}"
            resp = session.get(url, timeout=15)

            if resp.status_code == 429:
                wait = min(30, 5 * (consecutive_errors + 1))
                print(f"  Rate limited, waiting {wait}s...")
                time.sleep(wait)
                consecutive_errors += 1
                resp = session.get(url, timeout=15)

            resp.raise_for_status()
            consecutive_errors = 0
            data = resp.json()
            podcast_url = data.get("podcast_url") or data.get("podcastUrl")

            if podcast_url:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE substack.posts SET podcast_url = %s WHERE id = %s",
                        (podcast_url, post_id),
                    )
                conn.commit()
                updated += 1
                print(f"  [{i+1}/{len(rows)}] {post_id}: OK")
            else:
                print(f"  [{i+1}/{len(rows)}] {post_id}: no podcast_url in response")

            if delay > 0 and i < len(rows) - 1:
                time.sleep(delay)

        except Exception as e:
            print(f"  [{i+1}/{len(rows)}] {post_id}: ERROR {e}")
            conn.rollback()
            consecutive_errors += 1
            if consecutive_errors >= 5:
                wait = 30
                print(f"  Too many errors, waiting {wait}s...")
                time.sleep(wait)

    return updated


# ── Downloading ──────────────────────────────────────────────────────────────

def download_pending(conn, media_type: str | None = None,
                     publication: str | None = None) -> int:
    """Download all media where downloaded_at IS NULL. Returns count downloaded."""
    where = ["pm.downloaded_at IS NULL"]
    params: list = []

    if media_type:
        where.append("pm.media_type = %s")
        params.append(media_type)

    if publication:
        where.append("pub.subdomain = %s")
        params.append(publication)

    query = f"""
        SELECT pm.media_id, pm.post_id, pm.media_type, pm.source_url, pub.subdomain
        FROM substack.post_media pm
        JOIN substack.posts p ON p.id = pm.post_id
        JOIN substack.publications pub ON pub.id = p.publication_id
        WHERE {' AND '.join(where)}
        ORDER BY pm.post_id, pm.position_in_post
    """

    with conn.cursor() as cur:
        cur.execute(query, params)
        rows = cur.fetchall()

    if not rows:
        print("Nothing to download.")
        return 0

    print(f"Downloading {len(rows)} files...")
    downloaded = 0
    session = requests.Session()

    for i, (media_id, post_id, mtype, source_url, subdomain) in enumerate(rows):
        try:
            if mtype == "audio":
                local_path = _download_audio(session, post_id, source_url, subdomain)
            else:
                local_path = _download_image(session, post_id, source_url, mtype, subdomain)

            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE substack.post_media
                    SET local_path = %s, downloaded_at = NOW()
                    WHERE media_id = %s
                """, (str(local_path), media_id))
            conn.commit()
            downloaded += 1

            if (i + 1) % 50 == 0 or i == len(rows) - 1:
                print(f"  [{i+1}/{len(rows)}] downloaded")

        except Exception as e:
            print(f"  [{i+1}/{len(rows)}] FAILED post={post_id} {source_url[:60]}: {e}")
            conn.rollback()

    return downloaded


def adopt_existing_podcasts(conn) -> int:
    """Mark already-downloaded podcast files as downloaded.

    Finds existing MP3s in any of these locations and moves them to the
    canonical path: audio/<subdomain>/<post_id>/episode.mp3
    - posts/saved/podcasts/<post_id>/
    - posts/saved/audio/<post_id>/  (old flat layout)
    - posts/saved/audio/<subdomain>/<post_id>/  (current layout)
    """
    adopted = 0

    # Build post_id -> subdomain lookup for all audio entries
    with conn.cursor() as cur:
        cur.execute("""
            SELECT pm.post_id, pub.subdomain
            FROM substack.post_media pm
            JOIN substack.posts p ON p.id = pm.post_id
            JOIN substack.publications pub ON pub.id = p.publication_id
            WHERE pm.media_type = 'audio'
        """)
        post_subdomain = {r[0]: r[1] for r in cur.fetchall()}

    # Collect MP3 files from old locations and move to canonical path
    old_dirs = [BASE_DIR / "podcasts", BASE_DIR / "audio"]
    for old_dir in old_dirs:
        if not old_dir.exists():
            continue
        for child in sorted(old_dir.iterdir()):
            if not child.is_dir():
                continue
            # child could be <post_id> or <subdomain>
            try:
                post_id = int(child.name)
            except ValueError:
                # It's a subdomain dir — scan its children
                for post_dir in sorted(child.iterdir()):
                    if not post_dir.is_dir():
                        continue
                    mp3 = post_dir / "episode.mp3"
                    if mp3.exists():
                        try:
                            pid = int(post_dir.name)
                        except ValueError:
                            continue
                        subdomain = post_subdomain.get(pid, child.name)
                        canonical = BASE_DIR / "audio" / subdomain / str(pid) / "episode.mp3"
                        if mp3 != canonical:
                            canonical.parent.mkdir(parents=True, exist_ok=True)
                            if not canonical.exists():
                                mp3.rename(canonical)
                continue

            mp3 = child / "episode.mp3"
            if not mp3.exists():
                continue
            subdomain = post_subdomain.get(post_id)
            if not subdomain:
                continue
            canonical = BASE_DIR / "audio" / subdomain / str(post_id) / "episode.mp3"
            if mp3 != canonical:
                canonical.parent.mkdir(parents=True, exist_ok=True)
                if not canonical.exists():
                    mp3.rename(canonical)

    # Now scan canonical audio/<subdomain>/<post_id>/ and update DB
    audio_dir = BASE_DIR / "audio"
    if not audio_dir.exists():
        return 0

    for sub_dir in sorted(audio_dir.iterdir()):
        if not sub_dir.is_dir():
            continue
        for post_dir in sorted(sub_dir.iterdir()):
            if not post_dir.is_dir():
                continue
            mp3 = post_dir / "episode.mp3"
            if not mp3.exists():
                continue
            try:
                post_id = int(post_dir.name)
            except ValueError:
                continue
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE substack.post_media
                    SET local_path = %s, downloaded_at = NOW()
                    WHERE post_id = %s AND media_type = 'audio'
                      AND downloaded_at IS NULL
                """, (str(mp3), post_id))
                if cur.rowcount > 0:
                    adopted += cur.rowcount
            conn.commit()

    return adopted


# ── Download helpers ─────────────────────────────────────────────────────────

def _download_image(session: requests.Session, post_id: int,
                    source_url: str, media_type: str, subdomain: str) -> Path:
    """Download an image and return local path."""
    subdir = "images" if media_type == "image" else "covers"
    dest_dir = BASE_DIR / subdir / subdomain / str(post_id)
    dest_dir.mkdir(parents=True, exist_ok=True)

    # Derive filename from the UUID in the URL
    filename = _filename_from_url(source_url)
    dest = dest_dir / filename

    if not dest.exists():
        resp = session.get(source_url, timeout=30)
        if resp.status_code == 403:
            # Old bucketeer S3 URLs are dead; try via Substack CDN
            cdn_url = _make_cdn_url(source_url)
            resp = session.get(cdn_url, timeout=30)
        resp.raise_for_status()
        dest.write_bytes(resp.content)

    return dest


def _download_audio(session: requests.Session, post_id: int,
                    source_url: str, subdomain: str) -> Path:
    """Download a podcast episode and return local path."""
    dest_dir = BASE_DIR / "audio" / subdomain / str(post_id)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / "episode.mp3"

    if not dest.exists():
        # Podcast URLs often 307 redirect to CDN; follow redirects
        resp = session.get(source_url, timeout=120, allow_redirects=True,
                           stream=True)
        resp.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                f.write(chunk)

    return dest


# ── URL / format helpers ─────────────────────────────────────────────────────

def _make_cdn_url(s3_url: str) -> str:
    """Wrap an S3 URL in the Substack CDN fetch proxy."""
    from urllib.parse import quote
    encoded = quote(s3_url, safe="")
    return f"https://substackcdn.com/image/fetch/f_auto,q_auto:good,fl_progressive:steep/{encoded}"


def _extract_s3_url(cdn_url: str) -> str | None:
    """Extract the original S3 URL from a substackcdn.com fetch URL.

    e.g. https://substackcdn.com/image/fetch/.../https%3A%2F%2Fsubstack-post-media...
    """
    m = re.search(r"/(https%3A%2F%2F[^?]+)", cdn_url)
    if m:
        return unquote(m.group(1))
    return None


def _format_from_type(mime_type: str | None) -> str | None:
    """Convert image/png -> png, etc."""
    if not mime_type:
        return None
    if "/" in mime_type:
        return mime_type.split("/", 1)[1]
    return mime_type


def _filename_from_url(url: str) -> str:
    """Derive a filename from an S3/CDN image URL.

    Extracts the UUID_WxH.ext pattern or falls back to last path segment.
    """
    # Try UUID pattern: .../images/302ed00e-827a-4360-b808-261fe842a15d_631x706.png
    m = re.search(r"([0-9a-f-]{36}_\d+x\d+\.\w+)", url)
    if m:
        return m.group(1)

    # Fallback: last path segment
    path = url.split("?")[0].rstrip("/")
    return path.rsplit("/", 1)[-1] or "image.jpg"


def _make_session(cookies_path: str) -> requests.Session:
    """Create a requests session with Substack auth cookies."""
    session = requests.Session()
    with open(cookies_path) as f:
        cookies = json.load(f)
    for cookie in cookies:
        session.cookies.set(
            cookie["name"],
            cookie["value"],
            domain=cookie.get("domain", ".substack.com"),
            path=cookie.get("path", "/"),
        )
    return session
