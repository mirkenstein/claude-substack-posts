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
    if not migration_files:
        print("No migrations found")
        conn.close()
        return

    latest = migration_files[-1]

    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('substack.posts') IS NOT NULL")
        if cur.fetchone()[0]:
            cur.execute("SELECT count(*) FROM substack.posts")
            n = cur.fetchone()[0]
            if n > 0:
                print(f"Refusing to re-init: substack.posts has {n} rows in '{config['database']}'")
                conn.close()
                sys.exit(1)
        cur.execute("DROP SCHEMA IF EXISTS substack CASCADE")
        cur.execute("DROP SCHEMA IF EXISTS external CASCADE")
    conn.commit()

    print(f"Applying {latest.name}...", end=' ')
    try:
        with conn.cursor() as cur:
            cur.execute(latest.read_text())
        conn.commit()
        print("OK")
    except Exception as e:
        conn.rollback()
        conn.close()
        print(f"ERROR: {e}")
        sys.exit(1)

    conn.close()
    print("Schema applied")


if __name__ == '__main__':
    create_database()
    run_migrations()
