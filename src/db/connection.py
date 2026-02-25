"""Database connection context manager."""

import sys
from pathlib import Path

import psycopg2

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from db.config import get_db_config


class DatabaseConnection:
    """Context manager for database connections with substack schema."""

    def __init__(self, database=None, schema=None):
        self.config = get_db_config()
        if database:
            self.config['database'] = database
        self._schema = schema or 'substack'
        self._conn = None

    def connect(self):
        if self._conn is None or self._conn.closed:
            self._conn = psycopg2.connect(**self.config)
            with self._conn.cursor() as cur:
                cur.execute(f"SET search_path TO {self._schema}, public")
            self._conn.commit()
        return self._conn

    def close(self):
        if self._conn and not self._conn.closed:
            self._conn.close()

    def __enter__(self):
        return self.connect()

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type is not None and self._conn and not self._conn.closed:
            self._conn.rollback()
        self.close()
