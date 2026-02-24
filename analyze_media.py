#!/usr/bin/env python3
"""Analyze downloaded images with LLM vision (Anthropic Haiku or Ollama).

Two-pass workflow:
  Pass 1 (ollama):  Fast triage on all images — description, category, basic OCR
  Pass 2 (haiku):   Deep analysis on text-heavy/data-rich images only

Usage:
    python analyze_media.py                                    # pass 1: all unanalyzed with gemma3
    python analyze_media.py --model ollama/gemma3:27b          # explicit model
    python analyze_media.py --pass2 --api-key sk-...           # pass 2: Haiku on critical images
    python analyze_media.py --publication kk                   # one publication
    python analyze_media.py --limit 50                         # test run
    python analyze_media.py --reanalyze                        # redo already-analyzed
    python analyze_media.py --type cover_image                 # cover images only
"""

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from src.db.connection import DatabaseConnection
from src.image_analyzer import analyze_image, SUPPORTED_MODELS

# Publication subdomain aliases
PUB_ALIASES = {
    "ae": "anti-empire",
    "esq": "edwardslavsquat",
    "slc": "slavlandchronicles",
    "kk": "kamilkazani",
    "drl": "drlivci",
    "woaw": "woaw",
}

# Categories that warrant pass-2 deep analysis
CRITICAL_CATEGORIES = {"screenshot", "tweet", "document", "chart", "table", "infographic", "map"}


def get_pending_images(conn, args) -> list[dict]:
    """Query post_media for images needing analysis."""
    where = ["pm.downloaded_at IS NOT NULL", "pm.local_path IS NOT NULL"]
    params: list = []

    if args.pass2:
        # Pass 2: only images already analyzed by pass 1 with critical categories
        where.append("pm.analyzed_at IS NOT NULL")
        where.append("pm.image_category IN %s")
        params.append(tuple(CRITICAL_CATEGORIES))
        if not args.reanalyze:
            where.append("pm.analysis_model NOT LIKE 'claude%%'")
    elif not args.reanalyze:
        where.append("pm.analyzed_at IS NULL")

    media_types = ["'image'", "'cover_image'"]
    if args.type:
        media_types = [f"'{args.type}'"]
    where.append(f"pm.media_type IN ({', '.join(media_types)})")

    if args.publication:
        pub = PUB_ALIASES.get(args.publication, args.publication)
        where.append("pub.subdomain = %s")
        params.append(pub)

    query = f"""
        SELECT pm.media_id, pm.post_id, pm.media_type, pm.local_path,
               pm.source_url, pm.alt_text, pm.caption, pm.width, pm.height,
               pm.image_category, pm.image_description,
               p.title AS post_title, pub.subdomain
        FROM substack.post_media pm
        JOIN substack.posts p ON p.id = pm.post_id
        JOIN substack.publications pub ON pub.id = p.publication_id
        WHERE {' AND '.join(where)}
        ORDER BY pm.post_id, pm.position_in_post
    """

    if args.limit:
        query += f" LIMIT {args.limit}"

    with conn.cursor() as cur:
        cur.execute(query, params)
        columns = [desc[0] for desc in cur.description]
        return [dict(zip(columns, r)) for r in cur.fetchall()]


def save_analysis_json(local_path: str, result: dict):
    """Save analysis JSON alongside the image file."""
    img_path = Path(local_path)
    json_path = img_path.with_suffix(img_path.suffix + ".analysis.json")
    json_path.write_text(json.dumps(result, indent=2, ensure_ascii=False))


def update_media_row(conn, media_id: int, result: dict, model_name: str):
    """Update post_media with analysis results."""
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE substack.post_media
            SET image_description = %s,
                image_data = %s,
                image_category = %s,
                analysis_model = %s,
                analyzed_at = NOW(),
                analysis_error = NULL
            WHERE media_id = %s
        """, (
            result.get("description"),
            json.dumps(result.get("data")) if result.get("data") else None,
            result.get("category"),
            model_name,
            media_id,
        ))
    conn.commit()


def update_media_error(conn, media_id: int, error: str, model_name: str):
    """Record analysis error."""
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE substack.post_media
            SET analysis_error = %s, analysis_model = %s, analyzed_at = NOW()
            WHERE media_id = %s
        """, (error[:1000], model_name, media_id))
    conn.commit()


def main():
    parser = argparse.ArgumentParser(description="Analyze images with LLM vision")
    parser.add_argument("--model", default=None,
                        help="Model: 'haiku', 'sonnet', or 'ollama/<model>'")
    parser.add_argument("--pass2", action="store_true",
                        help="Pass 2: re-analyze critical images (screenshot/tweet/chart/...) with Haiku")
    parser.add_argument("--publication", "-p",
                        help="Only analyze this publication (subdomain or alias)")
    parser.add_argument("--type", choices=["image", "cover_image"],
                        help="Only analyze this media type")
    parser.add_argument("--limit", type=int, help="Max images to analyze")
    parser.add_argument("--reanalyze", action="store_true",
                        help="Re-analyze already-analyzed images")
    parser.add_argument("--api-key", help="Anthropic API key (or set ANTHROPIC_API_KEY)")
    args = parser.parse_args()

    if args.api_key:
        import os
        os.environ["ANTHROPIC_API_KEY"] = args.api_key

    # Default model: haiku for pass2, gemma3 for pass1
    if args.model is None:
        model_name = "haiku" if args.pass2 else "ollama/gemma3:27b"
    else:
        model_name = args.model

    if model_name not in SUPPORTED_MODELS and not model_name.startswith("ollama/"):
        print(f"Unknown model: {model_name}")
        print(f"Supported: {', '.join(SUPPORTED_MODELS)}, ollama/<model>")
        sys.exit(1)

    db = DatabaseConnection()
    try:
        conn = db.connect()
        images = get_pending_images(conn, args)

        if not images:
            print("No images to analyze.")
            return

        pass_label = "Pass 2 (deep)" if args.pass2 else "Pass 1 (triage)"
        print(f"{pass_label}: analyzing {len(images)} images with {model_name}...")
        start = time.time()
        analyzed = 0
        errors = 0

        for i, img in enumerate(images):
            local_path = img["local_path"]
            if not Path(local_path).exists():
                print(f"  [{i+1}/{len(images)}] MISSING: {local_path}")
                errors += 1
                continue

            try:
                is_cover = img["media_type"] == "cover_image"
                context = {
                    "post_title": img["post_title"],
                    "alt_text": img["alt_text"],
                    "caption": img["caption"],
                    "subdomain": img["subdomain"],
                }

                # For pass 2, include pass-1 results as extra context
                if args.pass2 and img.get("image_description"):
                    context["prior_description"] = img["image_description"]
                    context["prior_category"] = img.get("image_category")

                result = analyze_image(
                    local_path,
                    model=model_name,
                    is_cover=is_cover,
                    context=context,
                )

                save_analysis_json(local_path, result)
                update_media_row(conn, img["media_id"], result, model_name)
                analyzed += 1

                if (i + 1) % 25 == 0 or i == len(images) - 1:
                    elapsed = time.time() - start
                    rate = analyzed / elapsed if elapsed > 0 else 0
                    print(f"  [{i+1}/{len(images)}] {analyzed} analyzed, "
                          f"{errors} errors ({rate:.1f}/sec)")

            except KeyboardInterrupt:
                print(f"\nInterrupted after {analyzed} images.")
                break
            except Exception as e:
                errors += 1
                update_media_error(conn, img["media_id"], str(e), model_name)
                if (i + 1) % 25 == 0:
                    print(f"  [{i+1}/{len(images)}] ERROR: {e}")

        elapsed = time.time() - start
        print(f"\nDone: {analyzed} analyzed, {errors} errors in {elapsed:.1f}s")

        if not args.pass2:
            # Show pass-1 category breakdown
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT image_category, count(*),
                           count(*) FILTER (WHERE image_category IN %s) AS critical
                    FROM substack.post_media
                    WHERE analyzed_at IS NOT NULL AND media_type IN ('image', 'cover_image')
                    GROUP BY image_category ORDER BY count(*) DESC
                """, (tuple(CRITICAL_CATEGORIES),))
                print("\nCategory breakdown:")
                total_critical = 0
                for cat, cnt, crit in cur.fetchall():
                    marker = " *" if crit > 0 else ""
                    print(f"  {cat or 'null':<20} {cnt:>5}{marker}")
                    total_critical += crit
                print(f"\n  * = critical for pass 2 ({total_critical} images)")

    finally:
        db.close()


if __name__ == "__main__":
    main()
