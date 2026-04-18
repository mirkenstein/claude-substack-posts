"""Publication configuration: maps subdomains to databases and ingestion modes.

Config file: substacks.json (project root)

Each publication entry has:
  - database: target PostgreSQL database (e.g. "substack", "podcasts")
  - mode: "full" (entire newsletter) or "individual" (cherry-picked articles only)

Usage:
    from src.publication_config import get_config, get_database, get_mode, list_publications

    # Get database for a publication (raises KeyError if unknown)
    db = get_database("martyrmade")  # "podcasts"

    # Get database with fallback (returns default if unknown)
    db = get_database("newblog", default="substack")  # "substack"

    # Get ingestion mode
    mode = get_mode("pikulexpedition")  # "individual"

    # List all publications for a database
    pubs = list_publications(database="podcasts")  # ["escapekey", "martyrmade", ...]

    # List full-ingest publications
    pubs = list_publications(mode="full")
"""

import json
from pathlib import Path

CONFIG_PATH = Path(__file__).resolve().parent.parent / "substacks.json"

_config = None


def _load():
    global _config
    if _config is None:
        with open(CONFIG_PATH) as f:
            _config = json.load(f)["publications"]
    return _config


def get_config(subdomain: str) -> dict | None:
    """Get full config dict for a publication, or None if unknown."""
    return _load().get(subdomain)


def get_database(subdomain: str, default: str | None = None) -> str:
    """Get target database for a publication.

    Returns the configured database, or default if provided and publication unknown.
    Raises KeyError if publication unknown and no default given.
    """
    cfg = get_config(subdomain)
    if cfg is not None:
        return cfg["database"]
    if default is not None:
        return default
    raise KeyError(f"Unknown publication '{subdomain}'. Add it to substacks.json or pass --database.")


def get_mode(subdomain: str, default: str = "full") -> str:
    """Get ingestion mode for a publication: 'full' or 'individual'."""
    cfg = get_config(subdomain)
    if cfg is not None:
        return cfg.get("mode", default)
    return default


def list_publications(database: str | None = None, mode: str | None = None) -> list[str]:
    """List publication subdomains, optionally filtered by database and/or mode."""
    config = _load()
    results = []
    for subdomain, cfg in sorted(config.items()):
        if database and cfg["database"] != database:
            continue
        if mode and cfg.get("mode") != mode:
            continue
        results.append(subdomain)
    return results
