#!/usr/bin/env python3
"""Create the substack database and run migrations."""

import sys
from pathlib import Path

import psycopg2
from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))
from db.config import get_admin_config, get_db_config


def create_database():
    admin = get_admin_config()
    conn = psycopg2.connect(**admin)
    conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)

    db_name = get_db_config()['database']

    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (db_name,))
        if cur.fetchone():
            print(f"Database '{db_name}' already exists")
        else:
            cur.execute(f'CREATE DATABASE "{db_name}"')
            print(f"Created database '{db_name}'")

    conn.close()


def run_migrations():
    config = get_db_config()
    conn = psycopg2.connect(**config)

    migrations_dir = Path(__file__).parent / 'migrations'
    migration_files = sorted(migrations_dir.glob('*.sql'))

    print(f"Found {len(migration_files)} migration(s)")

    for mf in migration_files:
        print(f"  Running {mf.name}...", end=' ')
        sql = mf.read_text()
        try:
            with conn.cursor() as cur:
                cur.execute(sql)
            conn.commit()
            print("OK")
        except Exception as e:
            conn.rollback()
            print(f"ERROR: {e}")
            conn.close()
            sys.exit(1)

    conn.close()
    print("All migrations complete")


if __name__ == '__main__':
    create_database()
    run_migrations()
